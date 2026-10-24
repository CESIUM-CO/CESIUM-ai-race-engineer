"""
spotter.py - "car on your left / car on your right / clear", like the spotter in Le Mans Ultimate.

How it works (no guessing about the game's coordinate axes):
  * The game gives the world position of every car and of each of YOUR four wheels.
  * Your forward direction = front wheels minus rear wheels. Your LEFT direction = front-left wheel minus front-right wheel.
    So "left" and "right" are right even if the game's axes are mirrored.
  * A car counts as alongside when its centre is within a few metres ahead/behind you and a lane's width to one side.
  * A side needs to be occupied for a moment before it is called, and free for half a second before it is cleared,
    so a car flicking past doesn't make him stutter.
  * Where the other cars' positions come from:
      1. the game's shared memory, if this game version provides them (Assetto Corsa Competizione does), or
      2. the "Pitwall Spotter" in-game app. Vanilla Assetto Corsa does NOT put the other cars into shared memory, so a tiny
         app inside the game (installed by install_spotter.py) writes every car's position to a small file, and this reads it.
  * It checks its own data and tells you in plain words what is missing instead of failing silently.
"""
import math
import os

import config

# (left_before, right_before, left_after, right_after) -> (priority, words)
CALLS = {
    (0, 0, 1, 0): (0, "Car on your left."),
    (0, 0, 0, 1): (0, "Car on your right."),
    (1, 0, 1, 1): (0, "And a car on your right. Three wide!"),
    (0, 1, 1, 1): (0, "And a car on your left. Three wide!"),
    (0, 0, 1, 1): (0, "Cars on both sides! Three wide!"),
    (1, 1, 1, 0): (1, "Right clear. Car still on your left."),
    (1, 1, 0, 1): (1, "Left clear. Car still on your right."),
    (1, 1, 0, 0): (1, "Clear both sides."),
    (1, 0, 0, 0): (1, "Left clear."),
    (0, 1, 0, 0): (1, "Right clear."),
    (1, 0, 0, 1): (0, "Left clear. Car on your right."),
    (0, 1, 1, 0): (0, "Right clear. Car on your left."),
}


def all_phrases():
    return [text for _, text in CALLS.values()]


def _wall():
    import time
    return time.time()


def _finite(*vals):
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals)


NEEDS_APP = ("Spotter has no car data. Enable the Pitwall Spotter app in Assetto Corsa.")


class Spotter:
    def __init__(self):
        self.on = {"L": False, "R": False}
        self.since = {"L": None, "R": None}       # when the raw state last differed from the confirmed one
        self.bad_frames = 0
        self.disabled = not config.SPOTTER
        self.checked_ok = False
        self.source = None                        # "game" or "app" once data has been seen
        self.explained = False
        self._file_cache = (0.0, None)            # (when read, cars)
        self._last_file_check = 0.0

    # ------------------------------------------------------------------ where the cars come from
    def _file_cars(self, now):
        """Positions written by the in-game app, or None if there is no fresh file."""
        read_at, cached = self._file_cache
        if now - read_at < 0.04:                  # re-read at most 25 times a second
            return cached
        cars = None
        try:
            path = config.SPOTTER_FILE
            if abs(os.path.getmtime(path) - _wall()) <= config.SPOTTER_FILE_MAX_AGE_S:
                with open(path, "r") as f:
                    lines = f.read().splitlines()
                cars = []
                for ln in lines[1:]:
                    parts = ln.split(",")
                    if len(parts) >= 4:
                        try:
                            cars.append((float(parts[1]), float(parts[2]), float(parts[3])))
                        except ValueError:
                            pass
                if not cars:
                    cars = None
        except OSError:
            cars = None
        self._file_cache = (now, cars)
        return cars

    def _cars(self, graphics, now):
        game = list(getattr(graphics, "carCoordinates", []) or [])
        if game:
            return game, "game"
        app = self._file_cars(now)
        return (app, "app") if app else (None, None)

    # ------------------------------------------------------------------ geometry
    def _axes(self, physics):
        """(my_x, my_z, forward(x,z), left(x,z)) from the four wheels, or None if the data is unusable."""
        p = list(physics.tyreContactPoint)
        if len(p) < 12 or not _finite(*p):
            return None
        fl, fr, rl, rr = (p[0], p[2]), (p[3], p[5]), (p[6], p[8]), (p[9], p[11])   # (x, z) of each wheel
        fwd = ((fl[0] + fr[0]) / 2 - (rl[0] + rr[0]) / 2, (fl[1] + fr[1]) / 2 - (rl[1] + rr[1]) / 2)
        left = (fl[0] - fr[0], fl[1] - fr[1])
        fn, ln = math.hypot(*fwd), math.hypot(*left)
        if fn < 0.5 or ln < 0.5:                  # wheels all at the same point = no real data
            return None
        mx = (fl[0] + fr[0] + rl[0] + rr[0]) / 4
        mz = (fl[1] + fr[1] + rl[1] + rr[1]) / 4
        return mx, mz, (fwd[0] / fn, fwd[1] / fn), (left[0] / ln, left[1] / ln)

    def raw_sides(self, physics, cars):
        """Which sides have a car alongside right now: (left, right), or None if the data is unusable."""
        if not cars:
            return None
        axes = self._axes(physics)
        if axes is None:
            return None
        mx, mz, fwd, left = axes
        # which entry is me? the one standing on my wheels
        dists = [math.hypot(c[0] - mx, c[2] - mz) if _finite(*c) else 1e9 for c in cars]
        me = min(range(len(cars)), key=lambda i: dists[i])
        if dists[me] > config.SPOTTER_SELF_MATCH_M:
            return None
        my_y = cars[me][1]
        L = R = False
        for i, c in enumerate(cars):
            if i == me or not _finite(*c) or abs(c[1] - my_y) > 4.0:
                continue
            dx, dz = c[0] - mx, c[2] - mz
            long_ = dx * fwd[0] + dz * fwd[1]
            lat = dx * left[0] + dz * left[1]
            if abs(long_) <= config.SPOTTER_LONG_M and 0.5 <= abs(lat) <= config.SPOTTER_LAT_MAX_M:
                if lat > 0:
                    L = True
                else:
                    R = True
        return L, R

    # ------------------------------------------------------------------ the calls
    def update(self, physics, graphics, speed_kmh, in_pit_lane, now):
        """Returns a list of (priority, text)."""
        if self.disabled or not config.SPOTTER:      # config.SPOTTER can be switched live from the Settings tab
            return []
        if speed_kmh < config.SPOTTER_MIN_KMH or in_pit_lane:
            self.on = {"L": False, "R": False}          # grid, pits, crawling: stay silent and start fresh
            self.since = {"L": None, "R": None}
            return []
        cars, source = self._cars(graphics, now)
        raw = self.raw_sides(physics, cars)
        if raw is None:
            self.bad_frames += 1
            if self.bad_frames >= 90 and not self.checked_ok and not self.explained:    # ~3 s of racing with no usable data
                self.explained = True
                print("[SPOTTER] No car positions from the game. Vanilla Assetto Corsa needs the little Pitwall Spotter app:\n"
                      "          1) run   python install_spotter.py     2) in Assetto Corsa open the app bar on the right\n"
                      "          and switch on  PitwallSpotter.   Until then everything else works, the spotter just stays quiet.")
                return [(2, NEEDS_APP)]
            return []
        self.bad_frames = 0
        if not self.checked_ok or self.source != source:
            self.checked_ok, self.source = True, source
            print(f"[SPOTTER] Car positions look good (from {'the game' if source == 'game' else 'the Pitwall Spotter app'}). "
                  "Spotter is on.")
        before = (int(self.on["L"]), int(self.on["R"]))
        for side, now_raw in (("L", raw[0]), ("R", raw[1])):
            if now_raw == self.on[side]:
                self.since[side] = None
                continue
            if self.since[side] is None:
                self.since[side] = now
            hold = config.SPOTTER_ENTER_S if now_raw else config.SPOTTER_EXIT_S
            if now - self.since[side] >= hold:
                self.on[side], self.since[side] = now_raw, None
        after = (int(self.on["L"]), int(self.on["R"]))
        call = CALLS.get(before + after)
        return [call] if call else []
