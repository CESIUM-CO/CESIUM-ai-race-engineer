"""
guard.py - plain-Python watchdog (no AI, zero latency).

ConnectionWatch says out loud when the game stops sending data, and again when it is back.
Paused games, replays and menus are NOT treated as a failure.

(The tyre consumption tracking I first put here was a duplicate of what strategy.py already does,
 so it was removed. Puncture and blow-out calls live in alerts.py.)
"""
import time


class ConnectionWatch:
    LIVE = 2

    def __init__(self, say_urgent, stale_seconds=3.0):
        self.say = say_urgent
        self.stale = stale_seconds
        self.last_packet = None
        self.last_change = time.time()
        self.was_live = False
        self.lost = False

    def tick(self, connected, packet=None, status=None, now=None):
        """Call every loop. packet = anything that changes whenever the game updates (e.g. a tuple of packet ids)."""
        now = time.time() if now is None else now
        if not connected:
            self._lost()
            return
        if packet != self.last_packet:
            self.last_packet = packet
            self.last_change = now
            if status == self.LIVE:
                self.was_live = True
                if self.lost:
                    self.lost = False
                    self.say("Telemetry restored.")
            return
        if status != self.LIVE:
            self.last_change = now          # paused / replay / menu: not a failure
        elif self.was_live and now - self.last_change > self.stale:
            self._lost()

    def _lost(self):
        if self.was_live and not self.lost:
            self.lost = True
            self.say("Telemetry lost. The game has disconnected.")
