import queue
import re
import threading
import time

import config

# Tone cues (frequency Hz, duration ms)
TONE_OPEN = (1000, 50)          # mic is REALLY open and listening
TONE_CLOSE = (600, 50)          # a valid recording session ended
TONE_REJECT = [(330, 90), (220, 140)]       # radio is OFF: press refused. Only played if config.PTT_REJECT_BEEP = True
TONE_MIC_ERROR = [(250, 120), (250, 120), (250, 120)]   # radio is on but the microphone could not be opened


class _Session:
    """One press-and-hold. Everything that belongs to a single transmission lives here, so a fast second press can
    never mix up with the previous one."""
    __slots__ = ("stop", "opened", "aborted")

    def __init__(self):
        self.stop = threading.Event()      # set when the button is released (or the session is cancelled)
        self.opened = threading.Event()    # set only once the microphone stream is really running
        self.aborted = False               # radio was switched off while the button was held


class PushToTalk:
    """Hold the push-to-talk button (any keyboard key, mouse button, wheel or gamepad button), talk, release.
    Local Whisper turns it into text. Pressing the button also silences the engineer.

    STATE MACHINE (fixed):
        press  -> radio OFF?  -> SILENT by default (optional reject cue if config.PTT_REJECT_BEEP = True);
                                 no mic, no transmit beep, no Whisper
               -> radio ON    -> start a session; the mic is opened on its own thread
        mic open OK           -> ONLY NOW the transmit beep plays (and what the beep itself picked up is discarded)
        mic open FAILED       -> error cue, session ends, no transmit beep
        release               -> stop mic, close cue (only if the session really opened), transcribe
        radio switched OFF while holding -> the session is aborted and the audio is thrown away

    is_active: callable returning True while the radio is ON (main_pitwall passes core.active.is_set).
    The button itself is handled by ptt_manager.PTTManager (chosen with "Change..." in the control window and saved to
    disk). If that module cannot start for any reason, this falls back to the single keyboard key config.PTT_KEY."""

    def __init__(self, on_text, on_press=None, on_quit=None, is_active=None):
        self.on_text, self.on_press, self.on_quit = on_text, on_press, on_quit
        self.is_active = is_active or (lambda: True)
        self.ready, self.failed = False, None
        self.jobs = queue.Queue()
        self.enabled = False
        self.ptt = None             # the PTTManager, once it has started (gui.py / main_pitwall.Engineer read it)
        self._session = None
        self._beep_lock = threading.Lock()      # tones never overlap each other
        self._last_reject = 0.0

    # ------------------------------------------------------------------ state helpers
    @property
    def recording(self):
        """True while a valid session is being held down."""
        s = self._session
        return s is not None and not s.stop.is_set()

    def _radio_on(self):
        try:
            return bool(self.is_active())
        except Exception:
            return True             # a broken gate must never make push-to-talk dead

    def start(self):
        """Returns at once. The heavy libraries (keyboard, numpy, sounddevice) load in the background."""
        threading.Thread(target=self._setup, daemon=True).start()

    def stop(self):
        """Release the keyboard / mouse / controller hooks (called when the program quits)."""
        s = self._session
        if s is not None:
            s.stop.set()
        ptt, self.ptt = self.ptt, None
        if ptt is not None:
            try:
                ptt.stop()
            except Exception:
                pass

    def ptt_text(self):
        """Human text for the current push-to-talk button, e.g. 'Keyboard: F' or 'Fanatec ClubSport: Button 6'."""
        ptt = self.ptt
        if ptt is not None:
            return str(ptt.binding)
        return "Keyboard: " + str(config.PTT_KEY).upper()

    def _setup(self):
        try:
            import keyboard
            import numpy
            import sounddevice
        except Exception as e:
            print(f"[VOICE INPUT OFF] {e}")
            return
        self.kb, self.np, self.sd = keyboard, numpy, sounddevice
        self.enabled = True
        threading.Thread(target=self._load_model, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        self._start_ptt()
        if self.on_quit and config.QUIT_KEY:
            try:
                self.kb.add_hotkey(config.QUIT_KEY, self.on_quit)
            except Exception as e:
                print(f"[KEYS] Could not set the quit key '{config.QUIT_KEY}': {e}. Use Ctrl+C in this window to quit.")

    def _start_ptt(self):
        try:
            from ptt_manager import PTTManager
            self.ptt = PTTManager(on_press=lambda: self._down(None),
                                  on_release=lambda: self._up(None), log=print).start()
        except Exception as e:
            self.ptt = None
            print(f"[PTT] The universal push-to-talk could not start ({type(e).__name__}: {e}). "
                  f"Using the keyboard key '{config.PTT_KEY}' only.")
            self.kb.on_press_key(config.PTT_KEY, self._down)
            self.kb.on_release_key(config.PTT_KEY, self._up)

    def _load_model(self):
        try:
            from faster_whisper import WhisperModel
            print("[VOICE INPUT] Loading speech model (first run downloads it, be patient)...")
            self.model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
            self.ready = True
            print(f"[VOICE INPUT] Ready. Hold your push-to-talk button to talk ({self.ptt_text()}).")
        except Exception as e:
            self.failed = str(e)
            print(f"[VOICE INPUT OFF] Speech model failed to load: {e}\n"
                  "                 Run:  python setup_check.py  to see the exact problem.")

    # ------------------------------------------------------------------ tones
    def _play_tones(self, tones):
        """Plays tones one after another. Never raises. Serialised, so cues can't overlap."""
        try:
            import winsound
        except Exception:
            return
        with self._beep_lock:
            for freq, ms in tones:
                try:
                    winsound.Beep(int(freq), int(ms))
                except Exception:
                    return

    def _tone_async(self, tones, setting="PTT_BEEP", default=True):
        """Plays `tones` on a thread if config.<setting> is truthy. `default` is used when config.py lacks the setting."""
        if getattr(config, setting, default):
            threading.Thread(target=self._play_tones, args=(tones,), daemon=True).start()

    # ------------------------------------------------------------------ button events
    def _down(self, _event):
        s = self._session
        if s is not None and not s.stop.is_set():
            return                  # key auto-repeat / duplicate press while a session is held
        if not self._radio_on():
            self._reject()
            return                  # no session, no mic, no transmit beep, no Whisper
        if self.on_press:
            self.on_press()         # silences the engineer (Core._pressed)
        session = _Session()
        self._session = session
        threading.Thread(target=self._record, args=(session,), daemon=True).start()

    def _reject(self):
        """Radio is off: refuse the press. Silent unless PTT_REJECT_BEEP is True (rate-limited so it can't machine-gun)."""
        now = time.monotonic()
        if now - self._last_reject < 0.6:
            return
        self._last_reject = now
        print("[VOICE INPUT] Push-to-talk ignored: the radio is switched off.")   # log only, never audio by default
        # SILENT BY DEFAULT: the cue plays only if config.py explicitly sets PTT_REJECT_BEEP = True
        self._tone_async(TONE_REJECT, "PTT_REJECT_BEEP", default=False)

    def _up(self, _event):
        s = self._session
        if s is not None:
            s.stop.set()            # a rejected press has no session, so nothing happens here

    # ------------------------------------------------------------------ recording
    def _open_mic(self, frames):
        """Opens (and starts) the microphone chosen in Settings. If Windows refuses it, the default microphone is used
        and the reason is printed once, instead of the push-to-talk silently doing nothing."""
        wanted = config.MIC_DEVICE
        for device in ([wanted, None] if wanted is not None else [None]):
            try:
                stream = self.sd.InputStream(samplerate=16000, channels=1, dtype="float32", device=device,
                                             callback=lambda data, *_: frames.append(data.copy()))
                stream.start()
                return stream
            except Exception as e:
                if device is None:
                    raise
                if not getattr(self, "_warned_mic", False):
                    self._warned_mic = True
                    print(f"[VOICE INPUT] The chosen microphone (device {wanted}) could not be opened "
                          f"({type(e).__name__}: {e}). Using the Windows default microphone. Pick another one in Settings.")

    def _record(self, session):
        frames = []
        stream = None
        try:
            stream = self._open_mic(frames)
        except Exception as e:
            print(f"[VOICE INPUT ERROR] microphone: {e}")
            self._tone_async(TONE_MIC_ERROR)          # an honest failure cue, NOT the transmit beep
            session.stop.set()
            return

        try:
            if session.stop.is_set():
                return                                 # released before the mic came up: silent, nothing to say
            # The mic is running NOW. This is the only place the transmit beep may fire.
            if getattr(config, "PTT_BEEP", True):
                self._play_tones([TONE_OPEN])          # synchronous: it is part of opening the session
                frames.clear()                         # don't feed our own beep to Whisper
            session.opened.set()
            while not session.stop.is_set():
                if not self._radio_on():               # radio switched off while the button was held
                    session.aborted = True
                    break
                time.sleep(0.01)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            session.stop.set()

        if not session.opened.is_set():
            return
        if session.aborted:
            print("[VOICE INPUT] The radio was switched off while talking. Recording discarded.")
            return
        self._tone_async([TONE_CLOSE])                 # close cue only for a session that really opened

        if not frames:
            return
        audio = self.np.concatenate(frames).flatten()
        if len(audio) < 16000 * 0.3:
            return
        if not self._radio_on():                       # don't spend CPU on Whisper for a radio that is off
            return
        if not self.ready:
            if self.failed:
                print(f"[VOICE INPUT] Speech model never loaded ({self.failed}). Run: python setup_check.py")
            else:
                print("[VOICE INPUT] Speech model still loading (first run downloads ~460MB). Watch for 'Ready'.")
            return
        try:
            segments, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                                condition_on_previous_text=False,
                                                initial_prompt=config.WHISPER_HINT)
            text = " ".join(s.text for s in segments).lower()
            words = re.findall(r"[a-z0-9']+", text)
            text = " ".join(config.WORD_FIXES.get(w, w) for w in words)
            if text:
                self.jobs.put(text)
        except Exception as e:
            print(f"[VOICE INPUT ERROR] {e}")

    def _worker(self):
        while True:
            text = self.jobs.get()
            try:
                self.on_text(text)
            except Exception:
                import traceback
                traceback.print_exc()
