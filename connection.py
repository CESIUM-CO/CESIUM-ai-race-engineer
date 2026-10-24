"""
connection.py - keeps the link to Assetto Corsa alive, on its own background thread.

STATE MACHINE (two states only):

    DISCONNECTED --(status 1 or 2 AND the game's counters are moving)--> CONNECTED
    CONNECTED    --(status 0 / memory handle fails / game frozen while LIVE)--> DISCONNECTED

  * The "disconnected" voice message plays ONCE per CONNECTED -> DISCONNECTED transition
    (and never more often than DISCONNECT_VOICE_COOLDOWN seconds, so a flapping game can't spam the radio).
  * While DISCONNECTED the thread silently re-opens the game's shared memory every REATTACH_SECONDS.
    A new session = new memory, and re-opening is how we find it. Nothing is spoken while retrying.
  * Every DISCONNECTED -> CONNECTED transition bumps `epoch`. The main loop sees the new epoch and resets
    everything that belongs to a session (lap counters, tyre history, warning flags) before it carries on.
  * status 3 (PAUSE) is not a disconnect: state stays as it is, nothing is spoken, nothing is reset.
  * Nothing in here can raise into the main program: every error is caught, printed once, and retried.
"""
import threading
import time
from collections import namedtuple

import config

DISCONNECTED, CONNECTED = "DISCONNECTED", "CONNECTED"
STATUS_OFF, STATUS_REPLAY, STATUS_LIVE, STATUS_PAUSE = 0, 1, 2, 3

# One consistent snapshot of the game, handed to the main loop.
Frame = namedtuple("Frame", "seq epoch physics graphics static")


class TelemetryLink:
    def __init__(self, reader, on_disconnect=None, on_connect=None, poll=0.02,
                 retry=None, stale=None, voice_cooldown=None):
        self.reader = reader
        self.on_disconnect, self.on_connect = on_disconnect, on_connect
        self.poll = poll
        self.retry = config.REATTACH_SECONDS if retry is None else retry
        self.stale = config.RECONNECT_STALE_SECONDS if stale is None else stale
        self.voice_cooldown = (config.DISCONNECT_VOICE_COOLDOWN
                               if voice_cooldown is None else voice_cooldown)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._state = DISCONNECTED
        self._epoch = 0
        self._seq = 0
        self._frame = None

        self._last_attach = -1e9        # when we last opened the game's memory
        self._last_packet = None        # last (physics, graphics) packet ids seen
        self._last_move = time.monotonic()   # when those ids last changed
        self._moved = False             # have the counters moved since we attached?
        self._last_voice = -1e9
        self._announced_waiting = False
        self._last_error = (None, 0.0)

    # ------------------------------------------------------------------ public
    @property
    def state(self):
        return self._state

    @property
    def epoch(self):
        return self._epoch

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="TelemetryLink", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(1.0)
        try:
            self.reader.close()
        except Exception:
            pass

    def latest(self):
        """The newest Frame, or None while DISCONNECTED."""
        with self._lock:
            return self._frame if self._state == CONNECTED else None

    # ------------------------------------------------------------------ thread
    def _run(self):
        while not self._stop.is_set():
            try:
                delay = self._step()
            except Exception as e:                       # never let the thread die
                self._report_error(e)
                self._go_disconnected("unexpected error")
                self._close_quietly()
                delay = self.retry
            self._stop.wait(delay)

    def _step(self):
        now = time.monotonic()

        # ---- 1. make sure we hold the game's memory ----
        if not self.reader.is_connected:
            if now - self._last_attach < self.retry:
                return 0.05
            self._last_attach = now
            if not self.reader.connect():
                self._go_disconnected("Assetto Corsa memory not found")
                return 0.05
            self._reset_tracking(now)

        # ---- 2. read it ----
        physics, graphics, static = self.reader.get_data()
        if physics is None:                              # the handle failed
            self._go_disconnected("lost the game's memory")
            return 0.05

        packet = (physics.packetId, graphics.packetId)
        if packet != self._last_packet:
            if self._last_packet is not None:
                self._moved = True
            self._last_packet, self._last_move = packet, now
        frozen = now - self._last_move
        status = graphics.status

        # ---- 3. the state machine ----
        if status == STATUS_OFF:                         # menu / session ended
            self._go_disconnected("session ended or menu")
            self._reattach_if_due(now)                   # a new session means new memory: go look for it
            return 0.05

        if status == STATUS_LIVE and frozen > self.stale and self._state == CONNECTED:
            self._go_disconnected("the game stopped responding")
            self._close_quietly()                        # force a clean re-attach on the next step
            return 0.05

        if status in (STATUS_REPLAY, STATUS_LIVE) and self._moved:
            self._go_connected()
        elif frozen > self.stale:
            self._reattach_if_due(now)                   # nothing moving (paused / dead mapping): re-check quietly

        if self._state == CONNECTED:
            self._publish(physics, graphics, static)
        return self.poll

    # ------------------------------------------------------------------ helpers
    def _reset_tracking(self, now):
        self._last_packet, self._last_move, self._moved = None, now, False

    def _reattach_if_due(self, now):
        if now - self._last_attach >= self.retry:
            self._last_attach = now
            self._close_quietly()
            self.reader.connect()                        # failure is handled on the next step
            self._reset_tracking(now)

    def _close_quietly(self):
        try:
            self.reader.close()
        except Exception:
            pass

    def _publish(self, physics, graphics, static):
        with self._lock:
            self._seq += 1
            self._frame = Frame(self._seq, self._epoch, physics, graphics, static)

    def _go_connected(self):
        if self._state == CONNECTED:
            return
        with self._lock:
            self._state = CONNECTED
            self._epoch += 1
            self._announced_waiting = False
        print(f"\n[STATUS] Telemetry active (session #{self._epoch}). Tracking reset.")
        self._call(self.on_connect)

    def _go_disconnected(self, reason):
        with self._lock:
            was_connected = self._state == CONNECTED
            self._state = DISCONNECTED
            self._frame = None
        if was_connected:
            print(f"\n[STATUS] Disconnected ({reason}). Waiting for reconnection, retrying quietly...")
            self._announced_waiting = True
            now = time.monotonic()
            if now - self._last_voice >= self.voice_cooldown:
                self._last_voice = now
                self._call(self.on_disconnect)
        elif not self._announced_waiting:
            print("\r[STATUS] Searching for Assetto Corsa...   ", end="", flush=True)
            self._announced_waiting = True

    def _call(self, fn):
        if fn:
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()

    def _report_error(self, e):
        key, when = self._last_error
        text = f"{type(e).__name__}: {e}"
        if text != key or time.monotonic() - when > 30:   # same error: print at most every 30 s
            self._last_error = (text, time.monotonic())
            print(f"\n[LINK ERROR] {text}")
            import traceback
            traceback.print_exc()
