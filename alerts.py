import math
import time
from collections import deque

import config
from gameinfo import ZONE_NAMES
from strategy import TYRE_NAMES

FLAGS = {
    1: (1, "Blue flag. Let the faster car through."),
    2: (1, "Yellow flag. Caution."),
    3: (0, "Black flag. Come into the pits."),
    4: (2, "White flag. Slow car ahead."),
    5: (2, "Chequered flag."),
    6: (0, "Penalty flag. Check your track limits."),
}
FLAG_LABELS = {0: "none", 1: "blue", 2: "yellow", 3: "black", 4: "white", 5: "chequered", 6: "penalty"}
QUALI_LIKE = (1, 3, 4)

# Per-wheel tyre-pressure severity. A wheel's state only ever moves UP until its pressure recovers,
# so a lesser warning can never be spoken after a worse one (e.g. "about to blow" after it already blew).
SEV_NONE, SEV_WARN, SEV_CRIT, SEV_LEAK, SEV_BLOWN = 0, 1, 2, 3, 4


def _nm(i):
    return TYRE_NAMES[i].replace(" ", "-").capitalize()


# Every pressure call is built here, so the voice can pre-record them and they play with no delay.
def msg_warn(i, psi):
    return f"{_nm(i)} pressure is {psi} PSI. You must slow down!"


def msg_crit(i, psi=None):
    return f"{_nm(i)} pressure is critical! Box, box, box now, it's about to blow up!"      # no number: always pre-recorded


def msg_blown(i):
    return f"{_nm(i)} tyre has blown up! Slow down and come to the box!"


def msg_blown_again(i):
    return f"{_nm(i)} tyre is still blown. Slow down and get to the box."


def msg_heat(words, single, stage):
    """words like 'the rear right tyre' / 'the rear tyres' / 'all four tyres'. No numbers, so every one is pre-recorded."""
    verb = "is" if single else "are"
    if stage == 2:
        text = f"{words} {verb} way too hot! You must slow down!"
    else:
        text = f"{words} {verb} overheating."
    return text[0].upper() + text[1:]


def heat_phrases():
    from itertools import combinations
    out = []
    for n in range(1, 5):
        for idx in combinations(range(4), n):
            words, single = AlertMonitor._group(idx)
            out += [msg_heat(words, single, 1), msg_heat(words, single, 2)]
    return out


def msg_leak(i):
    return f"{_nm(i)} pressure is dropping fast! Possible puncture. Box this lap!"


def msg_leak_again(i):
    return f"{_nm(i)} is still losing pressure. Box this lap."


def all_phrases():
    """Calls worth pre-recording so they play instantly (the numbers outside these ranges are recorded on first use)."""
    out = [text for _, text in FLAGS.values()]
    out += ["Track limits. This lap won't count.", "Track limits. Watch for a penalty.", "Pit limiter!",
            "Engine failure! We've lost the engine. Coast to the pits if you can."]   # fuel calls: strategy.all_phrases()
    out += [f"Contact, {z}. Light damage." for z in ZONE_NAMES]
    out += [f"Heavy damage, {z}. Check the car and think about boxing." for z in ZONE_NAMES]
    for i in range(4):
        out += [msg_blown(i), msg_blown_again(i), msg_leak(i), msg_leak_again(i), msg_crit(i)]
        out += [msg_warn(i, p) for p in range(config.TYRE_PSI_WARN, config.TYRE_PSI_BOX + 1)]
    return out + heat_phrases()


class AlertMonitor:
    """Instant warnings: flags, penalties, track limits, damage, engine, punctures, blow-outs, grip."""

    def __init__(self):
        self.last_flag, self.last_penalty = 0, 0.0
        self.lap_invalid = False
        self.zones, self.damage_time = [0.0] * 5, 0.0
        self.dead_since, self.engine_dead = None, False
        self.engine_load = 0.0
        self.last_t = time.time()
        self.psi_hist = [deque() for _ in range(4)]
        self.sev = [SEV_NONE] * 4              # worst stage already announced for each wheel
        self.said_at = [-1e9] * 4              # when each wheel was last announced
        self.blown_since = [None] * 4          # when pressure first fell to the blown level
        self.ever_ok = [False] * 4             # has this tyre had a healthy pressure? (stops a false 'blown' in the garage)
        self.leak_top = [None] * 4             # pressure the wheel had before a rapid loss
        self.said = {}
        self.grip_ref = None
        self.heat_lvl = [0] * 4                # 0 none, 1 "getting hot" said, 2 "overheating" said
        self.heat_at = [-1e9] * 4
        self.cold_told = self.window_told = False
        self.fast_since = None
        self.in_pit_before = False
        self.surf = [{"mid": 0.0, "cam": 0.0} for _ in range(4)]   # slow averages of the tread temperature spreads

    def new_lap(self):
        self.lap_invalid = False

    def _ok(self, key, secs):
        now = time.time()
        if now - self.said.get(key, 0) < secs:
            return False
        self.said[key] = now
        return True

    def _tyre_pressure(self, i, p, speed, in_pit_lane, warn_psi, box_psi, now, data_gone=False):
        """One wheel, one frame. Returns a list of (priority, text).
        Stage 1  warn_psi..box_psi : you must slow down         (priority 1, per-wheel cooldown)
        Stage 2  box_psi and above : critical, box now          (priority 0, repeats)
        Stage 3  blown_psi or less : the tyre HAS blown up      (priority 0, repeats until the pit lane)"""
        out = []
        hist = self.psi_hist[i]
        hist.append((now, p))
        while hist and now - hist[0][0] > config.PRESSURE_DROP_SECONDS:
            hist.popleft()
        top = max((v for _, v in hist if math.isfinite(v)), default=0.0)

        # ---- Stage 3: already blown. "not (p > x)" also catches a broken number (NaN) from a burst tyre ----
        # (data_gone: the game stopped giving this tyre a temperature at all, which is what the in-game Tyres app
        #  shows as a blank tyre. That means the tyre is destroyed, whatever the pressure number says.)
        # At ANY speed: a tyre can blow in a burnout or while crawling. The "has been healthy" check is what keeps the
        # garage (where pressures read 0 before the car is on track) from raising a false alarm.
        if math.isfinite(p) and p >= 10:
            self.ever_ok[i] = True
        dead = self.ever_ok[i] and (not (p > config.TYRE_BLOWN_PSI) or data_gone)
        self.blown_since[i] = (self.blown_since[i] or now) if dead else None
        if self.blown_since[i] is not None and now - self.blown_since[i] >= config.TYRE_BLOWN_CONFIRM_S:
            self.leak_top[i] = None
            if self.sev[i] < SEV_BLOWN:
                self.sev[i], self.said_at[i] = SEV_BLOWN, now
                out.append((0, msg_blown(i)))
            elif (config.TYRE_BLOWN_REPEAT and not in_pit_lane
                  and now - self.said_at[i] >= config.TYRE_BLOWN_REPEAT):
                self.said_at[i] = now
                out.append((0, msg_blown_again(i)))
            return out
        if self.blown_since[i] is not None:
            return out                      # pressure just hit zero: wait to confirm, never say anything "lesser"
        if self.sev[i] == SEV_BLOWN:
            if math.isfinite(p) and p > 15:  # a fresh set of tyres
                self.sev[i] = SEV_NONE
            else:
                return out                  # still flat but stopped / in the garage: stay quiet, never downgrade

        # ---- extra: pressure collapsing fast but not at zero yet (puncture) ----
        frac = config.PRESSURE_DROP_FRACTION
        if frac and self.leak_top[i] is None and top >= 15 and p < top * (1 - frac):
            self.leak_top[i] = top
        if self.leak_top[i] is not None:
            if in_pit_lane or p >= 0.9 * self.leak_top[i]:           # boxed, or the pressure came back
                self.leak_top[i] = None
                if self.sev[i] == SEV_LEAK:
                    self.sev[i] = SEV_NONE
            else:
                if self.sev[i] < SEV_LEAK:
                    self.sev[i], self.said_at[i] = SEV_LEAK, now
                    out.append((0, msg_leak(i)))
                elif (config.TYRE_BLOWN_REPEAT and now - self.said_at[i] >= config.TYRE_BLOWN_REPEAT):
                    self.said_at[i] = now
                    out.append((0, msg_leak_again(i)))
                return out

        # ---- Stages 1 and 2: pressure too high (only while actually driving, not in the pit lane) ----
        if p < warn_psi - config.TYRE_PSI_HYSTERESIS and self.sev[i] in (SEV_WARN, SEV_CRIT):
            self.sev[i] = SEV_NONE          # cooled down: re-arm
        stage = SEV_NONE
        if not in_pit_lane and math.isfinite(p):
            stage = SEV_CRIT if p >= box_psi else SEV_WARN if p >= warn_psi else SEV_NONE
        if stage:
            repeat = config.TYRE_PSI_BOX_COOLDOWN if stage == SEV_CRIT else config.TYRE_PSI_WARN_COOLDOWN
            if stage > self.sev[i] or (stage == self.sev[i] and now - self.said_at[i] >= repeat):
                self.sev[i], self.said_at[i] = stage, now
                psi = round(p)
                if stage == SEV_CRIT:
                    out.append((0, msg_crit(i)))
                else:
                    out.append((1, msg_warn(i, psi)))
        return out

    # ---------------- tyre temperature: heads-ups only, never a safety call ----------------
    @staticmethod
    def _group(idx):
        """(words, is_single) for a set of tyres: 'front tyres', 'all four tyres', 'rear left tyre'..."""
        s = sorted(idx)
        if len(s) == 1:
            return "the " + TYRE_NAMES[s[0]] + " tyre", True
        named = {(0, 1, 2, 3): "all four tyres", (0, 1): "the front tyres", (2, 3): "the rear tyres",
                 (0, 2): "the left tyres", (1, 3): "the right tyres"}
        if tuple(s) in named:
            return named[tuple(s)], False
        names = [TYRE_NAMES[k] for k in s]
        return "the " + ", ".join(names[:-1]) + " and " + names[-1] + " tyres", False

    def _tyre_heat(self, cores, speed, now):
        out, due = [], {1: [], 2: []}
        R = config.TYRE_HEAT_REARM_C
        for i in range(4):
            c = cores[i]
            if not math.isfinite(c) or self.sev[i] >= SEV_LEAK or self.blown_since[i] is not None:
                continue
            if c < config.TYRE_WARM_C - R:
                self.heat_lvl[i] = 0
            elif c < config.TYRE_HOT_C - R and self.heat_lvl[i] == 2:
                self.heat_lvl[i] = 1
            cur = 2 if c >= config.TYRE_HOT_C else 1 if c >= config.TYRE_WARM_C else 0
            if cur == 1 and speed <= 30:
                cur = 0                      # normal-warm tyres while stopped / on blankets: not worth a call. Severe heat always is.
            lvl = self.heat_lvl[i]
            cooldown = config.TYRE_HOT_COOLDOWN if cur == 2 else config.TYRE_WARM_COOLDOWN
            if cur > lvl or (cur and cur == lvl and now - self.heat_at[i] >= cooldown):
                self.heat_lvl[i], self.heat_at[i] = cur, now
                due[cur].append(i)
        for stage in (2, 1):
            if due[stage]:
                words, single = self._group(due[stage])
                out.append((0 if stage == 2 else 1, msg_heat(words, single, stage)))
        return out

    def _tyre_warmup(self, cores, speed, now):
        good = [c for c in cores if math.isfinite(c)]
        if len(good) < 4 or speed < 40:
            self.fast_since = None
            return []
        self.fast_since = self.fast_since or now
        if now - self.fast_since < 8:
            return []
        avg = sum(good) / 4
        if not self.cold_told and avg < config.TYRE_COLD_C:
            self.cold_told = True
            return [(2, "Tyres are cold. Build the temperature gently.")]
        if self.cold_told and not self.window_told and avg >= config.TYRE_WINDOW_C:
            self.window_told = True
            return [(2, "Tyres are up to temperature.")]
        return []

    def _tyre_surface(self, physics, cores, speed, dt, now):
        """Setup hints from the inner / middle / outer tread temperatures, like reading the Tyres app."""
        out = []
        if speed < 60:
            return out
        k = min(1.0, dt / max(1.0, config.TYRE_SURFACE_TAU_S))
        spread = config.TYRE_SURFACE_SPREAD_C
        for i in range(4):
            I, M, O = (float(physics.tyreTempI[i]), float(physics.tyreTempM[i]), float(physics.tyreTempO[i]))
            if not (math.isfinite(I) and math.isfinite(M) and math.isfinite(O)) or not (cores[i] >= 70):
                continue
            s = self.surf[i]
            s["mid"] += ((M - (I + O) / 2) - s["mid"]) * k
            s["cam"] += ((I - O) - s["cam"]) * k
            name = TYRE_NAMES[i]
            tip = None
            if s["mid"] >= spread * 0.8:
                tip = f"The {name} tyre's middle is running hotter than its edges. The pressure may be too high."
            elif s["mid"] <= -spread * 0.8:
                tip = f"The {name} tyre's edges are hotter than its middle. The pressure may be too low."
            elif s["cam"] >= spread:
                tip = f"The {name} tyre's inside edge is much hotter than the outside. It may have too much camber."
            elif s["cam"] <= -spread:
                tip = f"The {name} tyre's outside edge is much hotter than the inside. It may need more camber."
            if tip and self._ok(f"surf{i}", 600):
                out.append((2, tip))
        return out

    def _tyre_log(self, psis, cores, wear, speed):
        """Writes the raw tyre numbers to a file whenever a tyre looks broken, so a missed call can be investigated."""
        line = (f"speed={speed:.0f}km/h psi FL/FR/RL/RR={[round(p, 1) for p in psis]} "
                f"core={[round(c) if math.isfinite(c) else c for c in cores]} wear={[round(w, 3) for w in wear]}")
        print(f"[TYRE] {line}")
        try:
            import datetime
            import os
            os.makedirs(os.path.dirname(config.TYRE_LOG), exist_ok=True)
            with open(config.TYRE_LOG, "a", encoding="utf-8") as f:
                f.write(datetime.datetime.now().strftime("%H:%M:%S ") + line + "\n")
        except Exception:
            pass

    def update(self, physics, graphics, static):
        now = time.time()
        dt = min(now - self.last_t, 0.5)
        self.last_t = now
        out = []

        # ----- flags & penalties -----
        flag = int(graphics.flag)
        pen = float(graphics.penaltyTime)
        if config.FLAG_DEBUG and (flag != self.last_flag or abs(pen - self.last_penalty) > 0.01):
            print(f"[FLAG] game reports flag={flag} ({FLAG_LABELS.get(flag, '?')}), penalty seconds={pen:.1f}")
        if flag != self.last_flag and flag in FLAGS:
            out.append(FLAGS[flag])
        self.last_flag = flag
        if pen > self.last_penalty + 0.1:
            out.append((0, "Penalty. %.0f seconds." % pen))
        self.last_penalty = pen

        # ----- track limits -----
        if int(physics.numberOfTyresOut) >= config.TYRES_OUT_FOR_INVALID and not self.lap_invalid:
            self.lap_invalid = True
            if int(graphics.session) in QUALI_LIKE:
                out.append((0, "Track limits. This lap won't count."))
            else:
                out.append((1, "Track limits. Watch for a penalty."))

        # ----- body damage, any size -----
        dmg = list(physics.carDamage)
        if config.DAMAGE_DEBUG and any(abs(a - b) > 0.01 for a, b in zip(dmg, self.zones)):
            print(f"[DAMAGE] {[round(d, 1) for d in dmg]}")
        hits = [i for i in range(5) if dmg[i] - self.zones[i] >= config.DAMAGE_MIN_STEP]
        if hits and now - self.damage_time > 3:
            self.damage_time = now
            zone = ZONE_NAMES[max(hits, key=lambda i: dmg[i] - self.zones[i])]
            if sum(dmg[:4]) >= config.DAMAGE_HEAVY:        # the 5th slot repeats the worst zone, so skip it
                out.append((0, f"Heavy damage, {zone}. Check the car and think about boxing."))
            else:
                out.append((1, f"Contact, {zone}. Light damage."))
        self.zones = dmg

        # ----- engine: dead, or being pushed too hard -----
        rpm, gear, speed = physics.rpms, physics.gear, physics.speedKmh
        if gear >= 2 and speed > 40 and rpm < 300:
            self.dead_since = self.dead_since or now
            if now - self.dead_since > config.ENGINE_DEAD_SECONDS and not self.engine_dead:
                self.engine_dead = True
                out.append((0, "Engine failure! We've lost the engine. Coast to the pits if you can."))
        else:
            self.dead_since = None
            if rpm > 1000:
                self.engine_dead = False

        if static.maxRpm > 0:
            hi = config.ENGINE_HIGH_RPM_FRACTION * static.maxRpm
            self.engine_load += ((1.0 if rpm > hi else 0.0) - self.engine_load) * min(1.0, dt / 30.0)
            if self.engine_load > config.ENGINE_STRESS_LIMIT and self._ok("engine_stress", 120):
                out.append((1, f"You're pushing the engine too hard. Keep the revs under {hi / 1000:.1f} thousand."))

        # ----- tyres. Every SAFETY call is decided by live PSI; temperatures only give heads-ups and setup hints -----
        in_pit_lane = bool(graphics.isInPitLane)
        psis = [float(p) for p in physics.wheelsPressure]
        cores = [float(c) for c in physics.tyreCoreTemperature]
        warn_psi, box_psi = config.TYRE_PSI_BY_CAR.get(str(static.carModel),
                                                       (config.TYRE_PSI_WARN, config.TYRE_PSI_BOX))
        gone = [(not math.isfinite(cores[i]) or cores[i] <= 1.0)
                and any(math.isfinite(cores[j]) and cores[j] > 30 for j in range(4) if j != i) for i in range(4)]
        broken = any(gone) or any(self.ever_ok[i] and ((not math.isfinite(psis[i])) or psis[i] <= 5) for i in range(4))
        if (config.TYRE_DEBUG
                and (any(p >= warn_psi for p in psis) or any(self.ever_ok[i] and not psis[i] > 12 for i in range(4)) or any(gone))
                and self._ok("psi_debug", 2)):
            print(f"[TYRE] psi FL/FR/RL/RR = {[round(p, 1) for p in psis]}  core = "
                  f"{[round(c) if math.isfinite(c) else c for c in cores]}")
        if broken and self._ok("tyre_log", 1.0):
            self._tyre_log(psis, cores, list(physics.tyreWear), speed)
        for i in range(4):
            out += self._tyre_pressure(i, psis[i], speed, in_pit_lane, warn_psi, box_psi, now, gone[i])
        if in_pit_lane and not self.in_pit_before:        # a pit stop: a fresh set of tyres is coming
            self.cold_told = self.window_told = False
            self.fast_since = None
        self.in_pit_before = in_pit_lane
        if config.TYRE_TEMP_WARNINGS and not in_pit_lane:
            out += self._tyre_heat(cores, speed, now)
            if speed > 30:
                out += self._tyre_warmup(cores, speed, now)
                if config.TYRE_SURFACE_TIPS:
                    out += self._tyre_surface(physics, cores, speed, dt, now)

        # ----- track grip -----
        grip = float(graphics.surfaceGrip)
        if grip > 0:
            if self.grip_ref is None:
                self.grip_ref = grip
            elif abs(grip - self.grip_ref) >= config.GRIP_CALL_STEP and self._ok("grip", 60):
                out.append((2, "Track grip is %s to %.0f percent." %
                            ("up" if grip > self.grip_ref else "down", grip * 100)))
                self.grip_ref = grip

        # ----- pit limiter -----
        if (config.PIT_LIMITER_REMINDER and in_pit_lane and not physics.pitLimiterOn
                and speed > 70 and self._ok("limiter", 8)):
            out.append((0, "Pit limiter!"))
        return out
