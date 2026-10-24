import math
import time

import config

TYRE_NAMES = ["front left", "front right", "rear left", "rear right"]

# ---------- fuel warning light (safety net / fallback when there is no usable fuel-per-lap number) ----------
FUEL_COLOURS = ("green", "yellow", "orange", "red")
# Critical fuel always carries the safety instruction: pit if you can, otherwise get OFF the racing line.
FUEL_CRITICAL_CALL = ("Fuel critical. Box, box, box. If you can't make the pit entry, "
                      "get off the racing line and stop safely.")
FUEL_EMPTY_CALL = "Out of fuel! Get off the racing line now. Stop safely, away from the traffic."
FALLBACK_PHRASES = [                       # fixed texts, so the voice can pre-record them
    "Fuel light is yellow. Start thinking about boxing.",
    "Fuel light is orange. Box this lap.",
    FUEL_CRITICAL_CALL,
]

# Critical-warning lock stages (the lock only ever moves UP until the tank is refilled)
CRIT_NONE, CRIT_CRITICAL, CRIT_EMPTY = 0, 1, 2


def all_phrases():
    """Calls worth pre-recording (main_pitwall adds these to the voice cache)."""
    return list(FALLBACK_PHRASES) + [FUEL_EMPTY_CALL]


def _cfg(name, default):
    """A config.py value, or the default if an older config.py doesn't have it yet."""
    return getattr(config, name, default)


def _median(values):
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


class Strategy:
    """Fuel + tyre brain. Uses YOUR measured consumption. Each tyre is tracked separately.

    FUEL = HYBRID SYSTEM
      1. PRIMARY   laps of fuel left, from your measured fuel use per lap (once it can be trusted).
      2. SAFETY NET the tank-level warning light (yellow / orange / red) and the raw litres threshold. It is the only
                    system while the fuel use is unknown / untrusted, or while the prediction raises an error. It ALSO
                    keeps watching while the primary system is active (orange and red), so a sudden drop the prediction
                    misses can never run you dry without a call.
      3. CRITICAL LOCK  "Fuel critical" and "Out of fuel" are each said STRICTLY ONCE, then locked. The lock re-arms by
                    itself when the tank is clearly refilled (checked every second and when you leave the pits)."""

    def __init__(self, car, track, compound, memory, max_fuel=None):
        self.car, self.track, self.memory = car, track, memory
        # fuel warning light: the game does not publish the icon colour, so it is rebuilt from the tank level
        self.max_fuel = max_fuel if (isinstance(max_fuel, (int, float)) and math.isfinite(max_fuel) and max_fuel > 0) else None
        self._peak_fuel = 0.0              # highest fuel seen: stands in for the tank size if the game didn't give one
        self.fuel_warn_said = 0            # worst light level already announced (0 green .. 3 red)
        self._crit_said = CRIT_NONE        # critical-warning lock: CRIT_NONE / CRIT_CRITICAL / CRIT_EMPTY already announced
        self._fuel_mode = None             # "burn" (laps of fuel left) or "light" (warning light fallback)
        self._level_logged = 0
        self.laps = []
        self.stint_start = 0
        self.said = {}
        self.fuel_called = False
        self.set_compound(compound)

    def set_compound(self, compound):
        self.compound = compound
        p = self.memory.get_profile(self.car, self.track, compound)
        self.saved_burn = p["avg_fuel"] if p else None
        self.saved_wear = p["avg_wear"] if p else None
        self.saved_wear_each = self.memory.get_tyre_wear(self.car, self.track, compound)
        # learning ON: the margin learned from your pit stops wins. Learning OFF: the Settings slider always wins.
        learned = p["fuel_margin"] if p and p["fuel_margin"] else None
        self.fuel_margin = learned if (learned and config.FUEL_LEARNING) else config.FUEL_CALL_LAPS
        override = config.TYRE_CLIFF_BY_TRACK.get(self.track)
        self.cliff_locked = override is not None
        self.cliff = override if override is not None else (
            p["tyre_cliff"] if p and p["tyre_cliff"] else config.TYRE_CLIFF_DEFAULT)

    def _save(self):
        self.memory.save_profile(self.car, self.track, self.compound,
                                 self.burn(), self.wear(), self.fuel_margin, self.cliff)

    def set_cliff(self, fraction):
        self.cliff = clamp(fraction, 0.02, 0.8)
        self._save()

    def set_margin(self, laps):
        self.fuel_margin = clamp(laps, 0.3, 6.0)
        self._save()

    # ---------- consumption ----------
    def burn(self):
        vals = [l["fuel"] for l in self.laps[-config.ROLLING_LAPS:]]
        return (sum(vals) / len(vals)) if vals else self.saved_burn

    def wear_each(self):
        recent = self.laps[-config.ROLLING_LAPS:]
        if recent:
            return [sum(l["wear_each"][i] for l in recent) / len(recent) for i in range(4)]
        if self.saved_wear_each:
            return self.saved_wear_each
        return [self.saved_wear] * 4 if self.saved_wear else None

    def wear(self):
        w = self.wear_each()
        return sum(w) / 4 if w else None

    def fuel_laps_left(self, fuel):
        b = self.burn()
        return fuel / b if b else None

    def tyre_laps(self, healths):
        """Returns (laps until AVERAGE hits cliff, laps until WORST tyre hits its own cliff, worst index)."""
        worst = min(range(4), key=lambda i: healths[i])
        rates = self.wear_each()
        if not rates:
            return None, None, worst
        avg_rate = sum(rates) / 4
        group = (sum(healths) / 4 - self.cliff) / avg_rate if avg_rate > 0 else None
        single = (healths[worst] - config.TYRE_SINGLE_CLIFF) / rates[worst] if rates[worst] > 0 else None
        return group, single, worst

    def add_lap(self, lap_no, lap_time, fuel_used, wear_each, valid, health_each):
        self.laps.append(dict(lap=lap_no, time=lap_time, fuel=fuel_used, wear_each=list(wear_each),
                              wear=sum(wear_each) / 4, valid=valid, health=sum(health_each) / 4))
        self.memory.add_lap(self.car, self.track, self.compound, lap_no, lap_time,
                            fuel_used, sum(wear_each) / 4, valid)
        self.memory.save_tyre_wear(self.car, self.track, self.compound, self.wear_each())
        self._save()

    def pace_dropping(self):
        stint = [l for l in self.laps[self.stint_start:] if l["valid"]]
        if len(stint) < 5:
            return False
        limit = _median(l["time"] for l in stint[:3]) * (1 + config.CLIFF_SLOWDOWN)
        return stint[-1]["time"] > limit and stint[-2]["time"] > limit

    # ---------- fuel: is the fuel-per-lap number trustworthy? ----------
    def fuel_unreliable_reason(self):
        """None when the measured / saved fuel use can be trusted, otherwise a short reason (used for the log)."""
        b = self.burn()
        if b is None:
            return "no fuel history for this car and track yet"
        if not math.isfinite(b) or b < 0.05:
            return "the measured fuel use is not believable"
        if self.saved_burn is None and len(self.laps) < _cfg("FUEL_MIN_LAPS", 2):
            return "only %d clean lap measured so far" % len(self.laps)
        recent = [l["fuel"] for l in self.laps[-config.ROLLING_LAPS:]]
        if len(recent) >= 2:
            spread = (max(recent) - min(recent)) / (sum(recent) / len(recent))
            if spread > _cfg("FUEL_BURN_MAX_SPREAD", 0.5):
                return "the last laps used very different amounts of fuel"
        return None

    def fuel_reliable(self):
        try:
            return self.fuel_unreliable_reason() is None
        except Exception:                                   # a broken number must mean "not reliable", never a crash
            return False

    # ---------- fuel: the warning light (yellow -> orange -> red), rebuilt from the tank level ----------
    def _warn_thresholds(self):
        """(yellow, orange, red) as fractions of a full tank, highest first. config.FUEL_WARN_BY_CAR can override per car."""
        default = (_cfg("FUEL_WARN_YELLOW", 0.15), _cfg("FUEL_WARN_ORANGE", 0.08), _cfg("FUEL_WARN_RED", 0.04))
        mine = _cfg("FUEL_WARN_BY_CAR", {}).get(self.car)
        try:
            y, o, r = sorted((float(x) for x in (mine or default)), reverse=True)
        except (TypeError, ValueError):
            y, o, r = sorted(default, reverse=True)
        return y, o, r

    def fuel_warning_state(self, fuel):
        """(level, fraction of tank). level: 0 green, 1 yellow, 2 orange, 3 red. (0, None) when the tank size is unknown."""
        if not math.isfinite(fuel):
            return 0, None
        self._peak_fuel = max(self._peak_fuel, fuel)
        cap = self.max_fuel or self._peak_fuel
        if not cap or cap <= 0:
            return 0, None
        frac = max(0.0, fuel / cap)
        y, o, r = self._warn_thresholds()
        return (3 if frac <= r else 2 if frac <= o else 1 if frac <= y else 0), frac

    def _note_fuel_mode(self, mode, why=""):
        if mode == self._fuel_mode:
            return
        if mode == "light":
            print("[FUEL] Fuel prediction not available (%s). Using the tank-level warning light instead." % why)
        elif self._fuel_mode == "light":
            print("[FUEL] Fuel use is known now. Back to laps of fuel left (the warning light stays on guard).")
        self._fuel_mode = mode

    # ---------- fuel: the critical-warning lock ----------
    @property
    def fuel_critical_locked(self):
        """True after a critical / empty call has been said and until the tank is refilled."""
        return self._crit_said != CRIT_NONE

    def _critical_once(self, stage, text):
        """Announce a critical stage STRICTLY ONCE. A stage that is already locked (or a lesser one after a worse one)
        says nothing. Returns a list of (priority, text)."""
        if stage <= self._crit_said:
            return []
        self._crit_said = stage
        self.fuel_called = True
        print("[FUEL] %s warning announced. Locked: it will not repeat until the tank is refilled."
              % ("OUT OF FUEL" if stage == CRIT_EMPTY else "Critical fuel"))
        return [(0, text)]

    def _fuel_is_empty(self, fuel):
        """The tank is (nearly) dry: the car is about to stop. Never true if the tank was never clearly fuller than the
        limit (garage / empty-tank test sessions). config.FUEL_EMPTY_LITRES overrides the default."""
        if not math.isfinite(fuel):
            return False
        limit = _cfg("FUEL_EMPTY_LITRES", 1.0)
        self._peak_fuel = max(self._peak_fuel, fuel)
        return fuel <= limit and self._peak_fuel >= limit * 3

    def _in_critical_zone(self, fuel):
        """True while ANY system still sees a critical tank. Each limit carries a re-arm margin (hysteresis), so a tiny
        top-up does not unlock the warning: the tank must be clearly refilled."""
        if not math.isfinite(fuel):
            return False
        if fuel <= _cfg("FUEL_EMPTY_LITRES", 1.0) * 2.0:                       # raw litres
            return True
        cap = self.max_fuel or self._peak_fuel
        if cap and cap > 0 and fuel / cap <= self._warn_thresholds()[2] + _cfg("FUEL_WARN_REARM", 0.02):   # red light
            return True
        if self.fuel_reliable():                                                # laps of fuel left
            fl = self.fuel_laps_left(fuel)
            if fl is not None and fl < 0.6 + 0.5:
                return True
        return False

    def _maybe_unlock_critical(self, fuel):
        """Re-arm the critical warning once the tank is clearly out of the critical zone. Returns True if it unlocked."""
        if self._crit_said != CRIT_NONE and not self._in_critical_zone(fuel):
            self._crit_said = CRIT_NONE
            self.said.pop("fuel_light2", None)
            level, _frac = self.fuel_warning_state(fuel)       # the light re-arms too, even if no check ran at the full tank
            self.fuel_warn_said = min(self.fuel_warn_said, level)
            print("[FUEL] Tank refilled (%.1f L). The critical fuel warning is armed again." % fuel)
            return True
        return False

    def on_pit_exit(self, fuel):
        """Call when the car leaves the pits back onto the track. If it was refuelled, the critical lock resets, so the
        warning can fire again later in this session or on the next stint."""
        self._peak_fuel = max(self._peak_fuel, fuel) if math.isfinite(fuel) else self._peak_fuel
        self._maybe_unlock_critical(fuel)

    # ---------- fuel: the hybrid calls ----------
    def _fuel_calls(self, fuel):
        """Primary: laps of fuel left. Safety net: the tank-level light. See the class docstring."""
        out, why = [], None
        try:
            why = self.fuel_unreliable_reason()
            if why is None:
                self._note_fuel_mode("burn")
                out = self._fuel_burn_calls(fuel)
        except Exception as e:                              # the prediction broke: fall back, never go silent
            why, out = "prediction error (%s: %s)" % (type(e).__name__, e), []
        if why is not None:                                 # not enough laps yet, untrusted numbers, or an error
            if not _cfg("FUEL_WARN_FALLBACK", True):
                return []
            self._note_fuel_mode("light", why)
            return self._fuel_light_calls(fuel)
        # prediction is active: the light still guards orange / red. It stays quiet if the prediction already spoke.
        return out + self._fuel_light_calls(fuel, min_level=2, quiet=bool(out))

    def _fuel_burn_calls(self, fuel):
        """The primary system: calls from the measured laps of fuel left."""
        out = []
        fl = self.fuel_laps_left(fuel)
        if fl is not None:
            if fl < 0.6:
                out += self._critical_once(CRIT_CRITICAL, FUEL_CRITICAL_CALL)          # once, then locked
            elif fl < self.fuel_margin and self._ok("fuel", 90):
                self.fuel_called = True
                out.append((1, ["Fuel for %.1f laps only. Box this lap." % fl,
                                "You won't make another %.1f laps on fuel. Box this lap." % self.fuel_margin]))
        return out

    def _fuel_light_calls(self, fuel, min_level=1, quiet=False):
        """The safety net. Yellow = heads-up (once). Orange = box this lap (repeats). Red = critical (ONCE, locked).
        A level is only announced when it gets WORSE, and it re-arms once the tank is clearly fuller again (a pit stop).
        min_level: lowest level allowed to speak (2 while the prediction is the primary system).
        quiet: True when the prediction just spoke, so yellow / orange stay silent (red / critical never does)."""
        level, frac = self.fuel_warning_state(fuel)
        if frac is None:
            return []
        if _cfg("FUEL_WARN_DEBUG", True) and level != self._level_logged:
            self._level_logged = level
            cap = fuel / frac if frac > 0 else 0
            print("[FUEL] light level -> %s: %.1f L left (%.0f%% of about %.0f L). Compare this with the icon in the game."
                  % (FUEL_COLOURS[level], fuel, frac * 100, cap))
        said = self.fuel_warn_said
        if level < said and frac >= self._warn_thresholds()[said - 1] + _cfg("FUEL_WARN_REARM", 0.02):
            self.fuel_warn_said = said = level            # refuelled: warn again next time
        out = []
        if level > said:
            self.fuel_warn_said = level
            if level == 3:
                out += self._critical_once(CRIT_CRITICAL, FUEL_CRITICAL_CALL)
            elif level >= min_level and not quiet:
                if level == 1:
                    if _cfg("FUEL_WARN_YELLOW_CALL", True):
                        out.append((2, FALLBACK_PHRASES[0]))
                else:
                    self._ok("fuel_light2", 0)            # start the orange repeat timer now
                    self.fuel_called = True
                    out.append((1, FALLBACK_PHRASES[1]))
            elif level == 2:
                self._ok("fuel_light2", 0)                # silent, but the repeat timer still starts
        elif level == said == 2 and level >= min_level and not quiet:
            if self._ok("fuel_light2", _cfg("FUEL_WARN_ORANGE_COOLDOWN", 90)):
                out.append((1, FALLBACK_PHRASES[1]))
        return out

    # ---------- advice ----------
    def _ok(self, key, secs):
        now = time.time()
        if now - self.said.get(key, 0) < secs:
            return False
        self.said[key] = now
        return True

    def check(self, fuel, healths):
        self._maybe_unlock_critical(fuel)                 # refilled? the lock re-arms by itself
        if self._fuel_is_empty(fuel):                     # dry tank: this call replaces the other fuel calls (once)
            self.fuel_called = True
            out = self._critical_once(CRIT_EMPTY, FUEL_EMPTY_CALL)
        else:
            out = self._fuel_calls(fuel)

        group, single, worst = self.tyre_laps(healths)
        name = TYRE_NAMES[worst]
        call = config.TYRE_CALL_LAPS
        avg = sum(healths) / 4
        # no wear rate yet? still call on the absolute levels (like a fuel gauge)
        if group is None:
            group = 0.0 if avg <= self.cliff else 99.0
        if single is None:
            single = 0.0 if healths[worst] <= config.TYRE_SINGLE_CLIFF else 99.0

        if group is not None and group < call:            # the whole set is fading
            key, prio, text = (("cliff_past", 0, "Tyres are past the cliff. Box now.") if group <= 0 else
                               ("cliff", 1, "Tyres hit the cliff in %.1f laps. Box this lap." % group))
            if self._ok(key, 60 if prio == 0 else 90):
                out.append((prio, text))
        elif single is not None and single < call:        # ONE tyre is finished, the rest are fine
            key, text = (("single_gone", f"The {name} is gone. Box this lap.") if single <= 0 else
                         ("single", f"The {name} is nearly done. Box this lap."))
            if self._ok(key, 75):
                out.append((1, text))
        elif group is not None and group < call * 2 and self._ok("cliff_soon", 240):
            out.append((2, "Heads up. About %.0f laps to the tyre cliff." % group))

        if avg - min(healths) > config.TYRE_IMBALANCE and self._ok("imbalance", 300):
            out.append((2, f"The {name} is wearing much faster than the others. Ease off it if you can."))
        if self.pace_dropping() and group is not None and group < call * 3 and self._ok("pace", 180):
            out.append((2, "Your pace is dropping and the tyres are fading. Think about boxing."))
        return out

    # ---------- learning when you enter the pits ----------
    def on_pit_entry(self, fuel):
        notes = []
        b = self.burn()
        if config.FUEL_LEARNING and b and self.fuel_reliable():
            spare, old = fuel / b, self.fuel_margin
            if spare < 0.6:
                self.fuel_margin = clamp(old + 0.25, config.FUEL_MARGIN_MIN, config.FUEL_MARGIN_MAX)
                notes.append("That was tight. I'll call the fuel earlier next time.")
            elif self.fuel_called and spare < old:
                self.fuel_margin = clamp(0.8 * old + 0.2 * spare, config.FUEL_MARGIN_MIN, config.FUEL_MARGIN_MAX)
                notes.append("You had a bit extra. I'll call the fuel a little later next time.")
        self.fuel_called = False
        if config.TYRE_LEARN_CLIFF and not self.cliff_locked and self._learn_cliff():
            notes.append("Updated my tyre cliff to %.0f percent." % (self.cliff * 100))
        self.stint_start = len(self.laps)
        self._save()
        return notes

    def _learn_cliff(self):
        stint = [l for l in self.laps[self.stint_start:] if l["valid"]]
        if len(stint) < 5:
            return False
        limit = _median(l["time"] for l in stint[:3]) * (1 + config.CLIFF_SLOWDOWN)
        for a, b, c in zip(stint, stint[1:], stint[2:]):
            if b["time"] > limit and c["time"] > limit:
                self.cliff = clamp(0.7 * self.cliff + 0.3 * a["health"], 0.05, 0.6)
                return True
        return False
