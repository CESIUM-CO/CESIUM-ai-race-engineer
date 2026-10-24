import sys
import boot_guard   # noqa: F401  (must stay first: logs every later import error)

# ---------------------------------------------------------------------------------------------------------------------
# HELPER MODES. These must stay at the very top: they run before any heavy import, before the single-instance lock and
# before the updater, because they are separate processes started from this same program / .exe.
# ---------------------------------------------------------------------------------------------------------------------
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--llm-worker":
    # A packaged .exe starts itself again in this mode to host the AI model in a separate process.
    from llm_worker import main as _llm_worker_main
    _llm_worker_main(sys.argv[2:])
    sys.exit(0)

if __name__ == "__main__" and ("--installer" in sys.argv[1:] or "--auto" in sys.argv[1:]):
    # The setup wizard for the in-game PitwallSpotter app. "--installer" is what the control window's button passes;
    # "--auto" is what the wizard's own administrator (UAC) relaunch passes, so it lands here as well.
    from installer_gui import main as _installer_main
    sys.exit(_installer_main())

import threading
import time
import llm_engineer

import config
from ac_telemetry import ACTelemetryReader
from alerts import AlertMonitor, all_phrases as alert_phrases
from brain import Brain, spoken_time
from cloud_coach import CloudCoach
from connection import STATUS_LIVE, TelemetryLink
from gameinfo import car_name, track_name
from learner import BackgroundLearner
from listener import PushToTalk
from memory import Memory
from spotter import Spotter, all_phrases as spotter_phrases
from strategy import Strategy, all_phrases as fuel_phrases
from voice_radio import PitwallVoiceEngine
import speech_repair
import user_settings


def tyre_healths(physics):
    """Four tyre healths, each 0.0-1.0."""
    wear = list(physics.tyreWear)
    scale = 100.0 if max(wear) > 1.5 else 1.0
    return [min(1.0, max(0.0, w / scale)) for w in wear]


def ms_to_sec(ms):
    return ms / 1000.0 if 0 < ms < 3_600_000 else None


def rnd(x, digits=0):
    """round() that survives a missing / broken number from the game."""
    try:
        return round(float(x), digits) if digits else round(float(x))
    except (TypeError, ValueError, OverflowError):
        return None


def clean(text):
    return "".join(c for c in text if c.isascii() and c.isprintable()).strip()


def stint_summary(session, car_id, label):
    """Small, anonymous summary of the stint just finished (what the cloud coach sees)."""
    stint = session.laps[session.stint_start:]
    wear = session.wear_each() or [0, 0, 0, 0]
    return {
        "car": car_id, "track": label, "compound": session.compound, "laps_in_stint": len(stint),
        "laps": [{"lap": l["lap"], "time_s": round(l["time"], 2), "fuel_l": round(l["fuel"], 2),
                  "tyre_loss_pct": round(l["wear"] * 100, 2), "valid": l["valid"]} for l in stint[-12:]],
        "avg_fuel_l_per_lap": None if session.burn() is None else round(session.burn(), 2),
        "tyre_loss_pct_per_lap_FL_FR_RL_RR": [round(w * 100, 2) for w in wear],
        "current_settings": {"tyre_cliff": round(session.cliff, 3), "fuel_margin_laps": round(session.fuel_margin, 2),
                             "tyre_call_laps": config.TYRE_CALL_LAPS},
    }


# =====================================================================================================================
#  Core: everything that lives for the WHOLE program run. It is built once.
#  Switching the radio off and on again only restarts the telemetry loop (run_pitwall below), so the AI model, the
#  natural voice and the push-to-talk hooks are never loaded twice.
# =====================================================================================================================
class Core:
    def __init__(self):
        self.memory = Memory()
        self.radio = PitwallVoiceEngine()
        self.brain = Brain(self.radio, self.memory)

        # --- LLM Chat & Memory Integration ---
        self.chat = llm_engineer.LLMEngineer.from_brain(self.brain)
        self.chat.attach(self.brain)
        # -------------------------------------

        self.quit_event = threading.Event()  # set by the quit hotkey
        self.active = threading.Event()  # set while the radio is ON; the driver's voice is ignored when it is OFF
        self.ptt = PushToTalk(self._heard, on_press=self._pressed, on_quit=self.quit_event.set,
                              is_active=self.active.is_set)   # radio OFF -> PTT is refused before any beep / recording
        self.ptt.start()
        BackgroundLearner(self.memory, self.brain).start()
        self.radio.prerender(self.phrases())

    def phrases(self):
        """Every call worth pre-recording. The tyre-pressure calls depend on the current PSI limits."""
        return (alert_phrases() + spotter_phrases() + fuel_phrases() +
                [config.DISCONNECT_MESSAGE, "Engineer online. Waiting for track session."])

    def _heard(self, text):
        if not self.active.is_set():
            return
        st = self.brain.state  # the current car / track names help fix mishearings
        fixed, notes = speech_repair.repair_verbose(text, extra_terms=[st.get("car_name"), st.get("track_name")],
                                                    name=config.ENGINEER_NAME)
        if notes:
            print(f"[STT] heard {text!r} -> {fixed!r}   ({'; '.join(notes)})")

        if fixed:  # "" = Whisper noise ("thanks for watching"): ignore it
            # 1. Run LLM casual/memory repair on top of STT repair
            chat_fixed = self.chat.repair(fixed)

            # 2. Check if it's casual chat or fact learning (like Khaled & 150 podiums)
            if not self.chat.handle_if_casual(chat_fixed):
                # 3. Otherwise, pass it to the normal telemetry brain as before
                self.brain.handle(chat_fixed)

    def _pressed(self):
        if self.active.is_set():
            self.radio.shush()

    def ptt_text(self):
        return self.ptt.ptt_text()


# =====================================================================================================================
#  Engineer: the object gui.py talks to (start / stop / status ...). It runs run_pitwall on its own thread.
# =====================================================================================================================
class Engineer:
    STOP_TIMEOUT_S = 12

    def __init__(self, core):
        self.core = core
        self._stop = None
        self._thread = None
        self._lock = threading.Lock()

    # ---------------- on / off
    def is_running(self):
        t = self._thread
        return t is not None and t.is_alive()

    def start(self):
        with self._lock:
            if self.is_running():
                return
            self._stop = threading.Event()
            self.core.active.set()
            self._thread = threading.Thread(target=run_pitwall, args=(self._stop, self.core),
                                            name="PitwallLoop", daemon=True)
            self._thread.start()

    def stop(self):
        """Blocks until the loop has said goodbye and let go of the game (up to STOP_TIMEOUT_S). gui.py calls it from a worker thread."""
        with self._lock:
            t, ev = self._thread, self._stop
            if t is None:
                return
            self.core.active.clear()
            ev.set()
        t.join(self.STOP_TIMEOUT_S)
        if t.is_alive():
            print("[ENGINEER] The radio loop did not stop in time.")
        else:
            self._thread = None

    # ---------------- what the window shows
    def status(self):
        return "live" if self.core.brain.state.get("live") else "waiting"

    def details(self):
        return {"ai": self.core.brain.ai_state(), "voice": self.core.radio.kokoro_state}

    def quit_requested(self):
        return self.core.quit_event.is_set()

    def set_language(self, code):
        config.LANGUAGE = code               # hook for translated phrases later; only the window is translated today

    # ---------------- push-to-talk binding
    def ptt_label(self):
        return self.core.ptt_text()

    def rebind_ptt(self, on_done):
        """Listen for the next key / button on ANY device. on_done(label) or on_done(None) when cancelled / timed out.
        Returns the listening session (it has .cancel(), .last_seen, .deadline), or None if push-to-talk is not running."""
        mgr = self.core.ptt.ptt
        if mgr is None:
            on_done(None)
            return None
        return mgr.rebind(timeout=20, on_done=lambda s: on_done(str(s.binding) if s.binding else None))

    def ptt_sources(self):
        """Which input sources really run (for the popup): {"status": {...}, "devices": [controller names]}."""
        mgr = self.core.ptt.ptt
        if mgr is None:
            return None
        return {"status": mgr.status(), "devices": [d["name"] for d in mgr.devices()]}

    # ---------------- Settings tab
    def apply_settings(self):
        """The Settings tab calls this after Save: re-read the file and apply it right now (no restart)."""
        state = self.core.brain.state
        key = state.get("session_key")                       # (car_id, track) once a session has started
        data = user_settings.load()
        user_settings.apply_hardware(config, data)
        self.core.radio.set_output_device(config.SPEAKER_DEVICE, data.get("speaker"))   # natural AND Windows voice
        psi_before = (config.TYRE_PSI_WARN, config.TYRE_PSI_BOX)
        klass = user_settings.apply_car_class(config, data, key[0] if key else None)
        if (config.TYRE_PSI_WARN, config.TYRE_PSI_BOX) != psi_before:
            self.core.radio.prerender(self.core.phrases())      # record the new tyre-pressure calls in the background
        self.core.radio.set_voice(config.KOKORO_VOICE)
        strategy = state.get("strategy")
        if strategy is not None and not config.FUEL_LEARNING:   # learning off: the slider is the truth, right now
            strategy.set_margin(config.FUEL_CALL_LAPS)
        print(f"[SETTINGS] applied (car class: {klass}, voice: {config.KOKORO_VOICE}, mic: {data.get('mic') or 'default'}, "
              f"speaker: {data.get('speaker') or 'default'})")

    def set_audio_device(self, kind, name):
        """Settings tab: a microphone (kind="mic") or speaker (kind="speaker") was picked. It is checked, applied to the
        running program and saved to disk RIGHT NOW. Returns "ok" or "failed" (the window shows the result)."""
        io = "input" if kind == "mic" else "output"
        index = user_settings.resolve_device(name, io)
        ok, why = user_settings.probe_device(index, io, 16000 if kind == "mic" else 24000)
        rejected = bool(name) and (index is None or not ok)
        if rejected:
            print(f"[AUDIO] {kind} '{name}' cannot be used ({why or 'not found'}). Staying on the Windows default.")
            name, index = None, None
        if kind == "mic":
            config.MIC_DEVICE = index                       # listener.py opens the microphone from this on every push-to-talk
        else:
            self.core.radio.set_output_device(index, name)  # voice_radio.py: the very next sentence comes out of it
        user_settings.save_audio(**{kind: name})
        print(f"[AUDIO] {kind} is now: {name or 'Windows default'}")
        if kind == "speaker":
            self.core.radio.speak("Speaker check. Can you hear me?", 1, kind="answer")
        return "failed" if rejected else "ok"

    def preview_voice(self, voice_id):
        """Say a test line in this voice. It does NOT change the engineer's real voice: Save does that (apply_settings)."""
        self.core.radio.preview(voice_id, "Radio check. Box, box, box. Tyres are up to temperature.")

    # ---------------- program exit
    def shutdown(self):
        try:
            self.core.brain.llm.shutdown()
        except Exception:
            pass
        try:
            self.core.brain.ollama.unload()                     # give the model's memory back to the game
        except Exception:
            pass
        try:
            self.core.ptt.stop()
        except Exception:
            pass


# =====================================================================================================================
#  The telemetry / radio loop. Runs on its own thread until stop_event is set; call it again to switch back on.
# =====================================================================================================================
def run_pitwall(stop_event, core):
    memory, radio, brain = core.memory, core.radio, core.brain
    reader = ACTelemetryReader()
    alerts = AlertMonitor()
    spotter = Spotter()
    state = brain.state

    def on_disconnect():
        """Called ONCE per CONNECTED -> DISCONNECTED transition (never while it is quietly retrying)."""
        state["live"] = False
        radio.speak(config.DISCONNECT_MESSAGE, 0)

    link = TelemetryLink(reader, on_disconnect=on_disconnect)

    pending = {}                                     # cloud suggestions, applied when you leave the pits

    def on_cloud(result):
        pending.update(result["suggestions"])
        if state.get("session_key"):
            state["lessons"] = CloudCoach.lessons_for(*state["session_key"])
        print(f"[CLOUD] lessons: {result['lessons']}  suggestions (applied at pit exit): {result['suggestions']}")

    coach = CloudCoach(on_result=on_cloud) if config.USE_CLOUD else None
    if coach and not coach.available:
        print("[CLOUD] No ANTHROPIC_API_KEY set, cloud coach is off (everything else works).")

    print("=" * 56)
    print("      PITWALL RACE ENGINEER ONLINE (v3.3)")
    print(f"      Push-to-talk: {core.ptt_text()}")
    print(f"      Quit hotkey:  {str(config.QUIT_KEY).upper()}")
    print("=" * 56)
    radio.speak("Engineer online. Waiting for track session.")

    session, setup_key = None, None
    last_laps, fuel0, health0 = 0, 0.0, [1.0] * 4
    lap_dirty, was_in_pit = True, False
    last_check = 0.0
    seen_epoch, seen_seq, last_err_print = 0, -1, 0.0
    link.start()        # background thread: attaches to the game, watches it, reconnects silently

    try:
        while True:
            try:
                if stop_event.is_set() or core.quit_event.is_set():     # radio switched off, or the quit key was pressed
                    raise KeyboardInterrupt
                frame = link.latest()
                if frame is None:                       # DISCONNECTED: the link thread keeps retrying by itself
                    state["live"] = False
                    time.sleep(0.1)
                    continue
                if frame.seq == seen_seq:               # no new data yet
                    time.sleep(0.005)
                    continue
                seen_seq = frame.seq

                # ---------- telemetry (re)started: reset everything that belongs to a session ----------
                if frame.epoch != seen_epoch:
                    seen_epoch = frame.epoch
                    session, setup_key = None, None     # fuel/tyre history rebuilt below (learned profile reloads from memory)
                    alerts = AlertMonitor()             # warning flags, timers, damage baseline
                    spotter = Spotter()                 # who is alongside
                    last_laps, fuel0, health0 = 0, 0.0, [1.0] * 4
                    lap_dirty, was_in_pit, last_check = True, False, 0.0
                    pending.clear()
                    state["live"] = False

                physics, graphics, static = frame.physics, frame.graphics, frame.static
                if graphics.status != STATUS_LIVE:      # replay: stay connected but don't coach
                    state["live"] = False
                    time.sleep(0.05)
                    continue

                fuel_now, health_now = float(physics.fuel), tyre_healths(physics)
                in_pit = bool(graphics.isInPit or graphics.isInPitLane)

                # ---------- new session ----------
                car_id, layout = clean(static.carModel), clean(static.trackConfiguration)
                track_id = clean(static.track)
                label = track_id + (f"-{layout}" if layout else "")
                if session is None or (car_id, label) != setup_key:
                    setup_key = (car_id, label)
                    psi_before = (config.TYRE_PSI_WARN, config.TYRE_PSI_BOX)
                    klass = user_settings.apply_for_session(config, car_id)    # GT3 / road / drift thresholds (BEFORE Strategy)
                    if klass:
                        print(f"[SETTINGS] car class for {car_id}: {klass}")
                        if (config.TYRE_PSI_WARN, config.TYRE_PSI_BOX) != psi_before:
                            radio.prerender(core.phrases())      # record the new tyre-pressure calls in the background
                    compound = clean(graphics.tyreCompound)
                    session = Strategy(car_id, label, compound, memory, max_fuel=float(static.maxFuel))
                    alerts = AlertMonitor()
                    last_laps, fuel0, health0 = graphics.completedLaps, fuel_now, health_now
                    lap_dirty, was_in_pit = True, in_pit
                    state.update(car_name=car_name(car_id), track_name=track_name(track_id, layout),
                                 session_key=(car_id, label), lessons=CloudCoach.lessons_for(car_id, label))
                    print(f"[STATUS] Session: {state['car_name']} @ {state['track_name']} ({compound})")
                    intro = f"Alright, {state['car_name']} at {state['track_name']}. "
                    if session.saved_burn:
                        radio.speak(intro + f"Welcome back. Last time here you used {session.saved_burn:.1f} litres a lap.")
                    else:
                        radio.speak(intro + "Give me a couple of laps to learn your numbers.")

                # ---------- instant warnings ----------
                for prio, text in alerts.update(physics, graphics, static):
                    radio.speak(text, prio)
                for prio, text in spotter.update(physics, graphics, float(physics.speedKmh),
                                                 bool(graphics.isInPitLane), time.time()):
                    radio.speak(text, prio, max_age=config.SPOTTER_MAX_AGE_S)

                # ---------- pit in / out ----------
                if in_pit and not was_in_pit:
                    lap_dirty = True
                    if coach and coach.available and len(session.laps) - session.stint_start >= 3:
                        coach.submit(stint_summary(session, car_id, label),
                                     {"tyre_cliff": session.cliff, "fuel_margin_laps": session.fuel_margin,
                                      "tyre_call_laps": config.TYRE_CALL_LAPS})
                    for note in session.on_pit_entry(fuel_now):
                        radio.speak(note, 2)
                elif was_in_pit and not in_pit:
                    lap_dirty = True
                    fuel0, health0 = fuel_now, health_now
                    session.set_compound(clean(graphics.tyreCompound))
                    if pending:                                  # cloud advice, already clamped to safe limits
                        got = dict(pending)
                        pending.clear()
                        if "tyre_cliff" in got and not session.cliff_locked:
                            session.set_cliff(got["tyre_cliff"])
                        if "fuel_margin_laps" in got:
                            session.set_margin(got["fuel_margin_laps"])
                        if "tyre_call_laps" in got:
                            config.TYRE_CALL_LAPS = got["tyre_call_laps"]
                        print(f"[CLOUD] applied for this stint: {got}")
                        radio.speak("I've tuned my calls for this stint from the last one.", 2)
                was_in_pit = in_pit

                # ---------- lap completed ----------
                laps = graphics.completedLaps
                if laps < last_laps:
                    last_laps, fuel0, health0, lap_dirty = laps, fuel_now, health_now, True
                    alerts.new_lap()
                elif laps > last_laps:
                    lap_time = graphics.iLastTime / 1000.0
                    if lap_dirty:
                        lap_dirty = False
                    else:
                        fuel_used = fuel0 - fuel_now
                        wear_each = [max(0.0001, a - b) for a, b in zip(health0, health_now)]
                        valid = not alerts.lap_invalid
                        if fuel_used > 0:
                            session.add_lap(laps, lap_time, fuel_used, wear_each, valid, health_now)
                            if config.TYRE_DEBUG:
                                g, sg, _w = session.tyre_laps(health_now)
                                print(f"[TYRES] health % FL/FR/RL/RR = {[round(h * 100, 1) for h in health_now]} | "
                                      f"wear per lap % = {[round(x * 100, 2) for x in session.wear_each()]} | "
                                      f"laps to cliff: all={None if g is None else round(g, 1)} "
                                      f"worst={None if sg is None else round(sg, 1)}")
                            is_pb = valid and 0 < graphics.iLastTime <= graphics.iBestTime
                            if config.LAP_TIME_CALLS == "all":
                                radio.speak(f"Lap {laps}. {spoken_time(lap_time)}.", 2)
                            elif config.LAP_TIME_CALLS == "pb" and is_pb:
                                radio.speak(["Personal best. " + spoken_time(lap_time) + ".",
                                             "That's a PB. " + spoken_time(lap_time) + "."], 2)
                    alerts.new_lap()
                    fuel0, health0, last_laps = fuel_now, health_now, laps

                # ---------- once per second: silent monitoring ----------
                if time.time() - last_check > 1.0:
                    last_check = time.time()
                    group, single, _ = session.tyre_laps(health_now)
                    fuel_level, fuel_frac = session.fuel_warning_state(fuel_now)   # 0 green .. 3 red
                    # a blown tyre is no longer a good tyre: take TYRE_BLOWN_PENALTY off what he REPORTS for it
                    blown_idx = [i for i in range(4) if alerts.sev[i] >= 4]
                    shown_health = [max(0.0, h - config.TYRE_BLOWN_PENALTY) if i in blown_idx else h
                                    for i, h in enumerate(health_now)]
                    state.update(live=True, strategy=session, laps=laps, position=graphics.position,
                                 fuel_laps=session.fuel_laps_left(fuel_now) if session.fuel_reliable() else None,
                                 fuel_warning=fuel_level, fuel_pct=fuel_frac, tyre_each=shown_health, tyre_blown=blown_idx,
                                 tyre_laps=group, tyre_worst_laps=single,
                                 last_lap=ms_to_sec(graphics.iLastTime), best_lap=ms_to_sec(graphics.iBestTime),
                                 air_temp=float(physics.airTemp), road_temp=float(physics.roadTemp),
                                 wind=float(graphics.windSpeed),
                                 grip=float(graphics.surfaceGrip) if graphics.surfaceGrip > 0 else None,
                                 damage=list(alerts.zones), engine_load=alerts.engine_load,
                                 engine_dead=alerts.engine_dead, rpm=int(physics.rpms), max_rpm=int(static.maxRpm),
                                 tyre_psi=[rnd(p, 1) for p in physics.wheelsPressure],
                                 tyre_temp=[rnd(c) for c in physics.tyreCoreTemperature])
                    if not in_pit:
                        for prio, text in session.check(fuel_now, health_now):
                            radio.speak(text, prio)

                time.sleep(0.033)

            except KeyboardInterrupt:
                raise
            except Exception:
                if time.time() - last_err_print > 5:    # one bad frame must not wipe the session or flood the screen
                    last_err_print = time.time()
                    import traceback
                    print("\n[ERROR] Something glitched in the main loop (the game link is unaffected):")
                    traceback.print_exc()
                time.sleep(0.2)

    except KeyboardInterrupt:
        print("\nRadio stopping...")
        radio.speak("Session finished. Everything is saved. Engineer signing off.")
        radio.wait_until_done()
    finally:
        link.stop()
        state["live"] = False
        print("Engineer stopped.")


# =====================================================================================================================
#  Program entry
# =====================================================================================================================
def main():
    import gui
    gui.setup_runtime()                                  # log file, print() that can't crash, native-crash trace
    headless = "--headless" in sys.argv                  # no window: run the radio straight away, Ctrl+C to quit

    lock = gui.acquire_single_instance()                 # keep this variable alive until the program ends
    if lock is None:
        gui.tell_user(gui.tr(gui.system_language(), "already_running"))
        return 1

    try:                                                 # undo a half-finished update, or offer a new release
        from updater import check_and_update
        check_and_update()
    except Exception as e:                               # an update problem must never stop the engineer
        print(f"[UPDATE] skipped ({type(e).__name__}: {e})")

    core = Core()
    engineer = Engineer(core)
    if headless:
        engineer.start()
        try:
            while engineer.is_running() and not core.quit_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        engineer.stop()
        engineer.shutdown()
        print("Goodbye.")
        return 0
    return gui.run_gui(engineer)


if __name__ == "__main__":
    sys.exit(main())
