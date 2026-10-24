"""
user_settings.py - the driver's own settings, saved as JSON in
    %APPDATA%\\CesiumAIRaceEngineer\\user_settings.json

Standard library only. It never imports config at the top (that would be a circular import): every function that
changes the program's behaviour receives the config MODULE as an argument (`cfg`).

  load()                 -> dict (defaults + whatever the user saved, cleaned and clamped)
  save(data)             -> True / False
  list_devices(kind)     -> device names for the dropdowns ("input" / "output")
  detect_class(car_id)   -> "race" | "road" | "drift"   (keyword / regex match on the car's folder name)
  apply_all(cfg)         -> called ONCE by config.py when the program starts
  apply_hardware(...)    -> microphone, speaker, voice
  apply_car_class(...)   -> blind-spot, fuel and engine thresholds for the car's class
  apply_for_session(...) -> what main_pitwall calls when a new car/track session begins

Everything else in the program reads config.X while it runs, so changing config.X here changes the behaviour at once.
Nothing is applied until the user has pressed Save at least once (no file = the values in config.py stay as written).

Try it:   python user_settings.py
"""
import copy
import json
import math
import os
import re

APP_DIR_NAME = "CesiumAIRaceEngineer"
TAG = "[SETTINGS]"

# ---------------------------------------------------------------- car classes
CLASS_ORDER = ("race", "road", "drift")
CLASS_LABELS = {"race": "GT3 / race cars", "road": "Road / civilian cars", "drift": "Drift cars"}

# Whole words inside the car's folder name, e.g. ks_audi_r8_lms_2016 -> "lms", ks_ferrari_488_gt3 -> "gt3".
# Add your own words here (mods): anything not matched is treated as a road car. Or force a class in the Settings tab.
RACE_WORDS = ("gt2", "gt3", "gt4", "gte", "lms", "lmp", "lmp1", "lmp2", "dtm", "tcr", "formula", "f1")
DRIFT_WORDS = ("drift",)                 # matched anywhere in the name (ks_toyota_ae86_drift, mycar_driftspec ...)
_RACE_RE = re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % "|".join(RACE_WORDS))
_DRIFT_RE = re.compile("|".join(DRIFT_WORDS))

# key: (lowest, highest, step). Used for clamping here and for the slider ranges in gui.py
LIMITS = {
    "spotter_level":   (1, 10, 1),          # blind-spot sensitivity
    "spotter_enter_s": (0.05, 0.50, 0.01),  # how long a car must stay alongside before he calls it
    "fuel_margin":     (0.7, 3.0, 0.1),     # laps of fuel left when he tells you to box
    "engine_rpm_pct":  (80, 100, 1),        # rpm (% of max) above which he counts "pushing the engine"
    "engine_patience": (10, 95, 5),         # % of the last 30 s spent up there before he complains
    "tire_target_psi": (20.0, 40.0, 0.5),   # the HOT tyre pressure you aim for; the warn / box limits move with it
}

DEFAULT_CLASSES = {
    # race = exactly the values already in config.py
    "race":  {"spotter_on": True,  "spotter_level": 5, "spotter_enter_s": 0.12,
              "fuel_margin": 1.5, "engine_rpm_pct": 93, "engine_patience": 40, "tire_target_psi": 26.5},
    "road":  {"spotter_on": True,  "spotter_level": 4, "spotter_enter_s": 0.20,
              "fuel_margin": 1.5, "engine_rpm_pct": 90, "engine_patience": 40, "tire_target_psi": 26.5},
    # drift: cars run side by side at close range, so blind-spot calls start OFF; the engine lives near the limiter
    "drift": {"spotter_on": False, "spotter_level": 2, "spotter_enter_s": 0.35,
              "fuel_margin": 1.0, "engine_rpm_pct": 98, "engine_patience": 85, "tire_target_psi": 26.5},
}

DEFAULTS = {"version": 2, "mic": None, "speaker": None, "voice": None,
            "forced_class": "auto", "fuel_learning": True, "classes": DEFAULT_CLASSES}

# (kokoro voice id, name shown in the dropdown). Same list as voice_demo.py
VOICES = [
    ("bm_george", "British man (default)"), ("bm_lewis", "British man, deeper"),
    ("bf_emma", "British woman"), ("bf_isabella", "British woman, softer"),
    ("im_nicola", "Italian man"), ("if_sara", "Italian woman"),
    ("am_michael", "American man"),
]


def voice_label(voice_id):
    return next((label for vid, label in VOICES if vid == voice_id), str(voice_id))


# ---------------------------------------------------------------- file
def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME, "user_settings.json")


def _merge(base, saved):
    """Copy saved values onto base, but only keys that exist in base (unknown keys are ignored)."""
    for key, value in saved.items():
        if key not in base:
            continue
        if isinstance(base[key], dict):
            if isinstance(value, dict):
                _merge(base[key], value)
        else:
            base[key] = value


def _clean(profile, fallback):
    """One car class: right types, numbers clamped to LIMITS, so a hand-edited file can't break anything."""
    out = {"spotter_on": bool(profile.get("spotter_on", fallback["spotter_on"]))}
    for key, (lo, hi, _step) in LIMITS.items():
        try:
            v = float(profile.get(key, fallback[key]))
        except (TypeError, ValueError):
            v = float(fallback[key])
        out[key] = min(hi, max(lo, v))
    return out


def load():
    data = copy.deepcopy(DEFAULTS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            _merge(data, saved)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"{TAG} Could not read {settings_path()} ({type(e).__name__}: {e}). Using defaults.")
    data["version"] = DEFAULTS["version"]          # v1 files (no tire_target_psi) are upgraded on the fly
    for key in ("mic", "speaker", "voice"):
        if not isinstance(data[key], str) or not data[key]:
            data[key] = None
    if data["forced_class"] not in CLASS_ORDER:
        data["forced_class"] = "auto"
    data["fuel_learning"] = bool(data["fuel_learning"])
    for k in CLASS_ORDER:
        data["classes"][k] = _clean(data["classes"][k], DEFAULT_CLASSES[k])
    return data


def save(data):
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)     # the folder is created for you
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)             # swapped in whole: a crash can never leave half a file
        print(f"{TAG} saved {path}  (voice={data.get('voice')}, mic={data.get('mic')}, speaker={data.get('speaker')})")
        return True
    except OSError as e:
        print(f"{TAG} Could not save {path} ({e})")
        return False


def save_audio(**fields):
    """Writes ONLY mic / speaker / voice to disk; every other saved value stays as it is.
    save_audio(speaker="Headset (USB Audio)")   save_audio(mic=None)  (None = the Windows default)"""
    data = load()
    for key in ("mic", "speaker", "voice"):
        if key in fields:
            data[key] = fields[key] or None
    return save(data)


def probe_device(index, kind, samplerate):
    """(ok, reason). Can this device be opened the way the program will open it? index None = Windows default = always ok."""
    if index is None:
        return True, ""
    try:
        import sounddevice as sd
        check = sd.check_input_settings if kind == "input" else sd.check_output_settings
        check(device=index, channels=1, samplerate=samplerate, dtype="float32")
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- audio devices
def _devices(kind):
    """[(index, name)] for the default Windows audio API only (otherwise every device appears 3-4 times)."""
    import sounddevice as sd
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    out = []
    for i in sd.query_hostapis(sd.default.hostapi)["devices"]:
        d = sd.query_devices(i)
        if d[key] > 0:
            out.append((i, d["name"]))
    return out


def list_devices(kind):
    """Device names for a dropdown. kind = "input" or "output"."""
    names = []
    for _i, name in _devices(kind):
        if name not in names:
            names.append(name)
    return names


def resolve_device(name, kind):
    """Saved NAME -> index sounddevice can use. Names survive reboots, indexes do not.
    None (= the Windows default) if nothing was chosen or the device is unplugged."""
    if not name:
        return None
    try:
        for i, n in _devices(kind):
            if n == name:
                return i
    except Exception as e:
        print(f"{TAG} could not look up the {kind} device '{name}' ({type(e).__name__}: {e}).")
        return None
    print(f"{TAG} {kind} device '{name}' is not connected right now, using the Windows default.")
    return None


# ---------------------------------------------------------------- car class
def detect_class(car_id):
    c = (car_id or "").lower()
    if not c:
        return "race"                    # no car yet (program just started): the defaults of config.py
    if _DRIFT_RE.search(c):
        return "drift"
    if _RACE_RE.search(c):
        return "race"
    return "road"


def active_class(data, car_id):
    forced = data.get("forced_class")
    return forced if forced in CLASS_ORDER else detect_class(car_id)


def spotter_geometry(level):
    """Sensitivity 1..10 -> (metres ahead/behind, metres to the side). 5 gives 5.5 / 4.5, the old config.py values."""
    level = max(1, min(10, int(round(level))))
    return round(3.0 + (level - 1) * 0.625, 2), round(3.0 + (level - 1) * 0.375, 2)


def psi_limits(target, warn_offset=8.5, box_offset=13.5):
    """Tyre-pressure target -> (warn psi, box psi) as WHOLE numbers (voice_radio records one phrase per whole psi).
    With the default target (26.5) and offsets (8.5 / 13.5) this gives the old 35 / 40."""
    up = lambda x: int(math.floor(x + 0.5))          # plain "round half up" (Python's round() would give 36 for 35.5, 40 for 40.5)
    warn = up(target + warn_offset)
    return warn, max(warn + 1, up(target + box_offset))


# ---------------------------------------------------------------- apply
def apply_hardware(cfg, data):
    cfg.MIC_DEVICE = resolve_device(data.get("mic"), "input")           # listener.py reads this every time you talk
    cfg.SPEAKER_DEVICE = resolve_device(data.get("speaker"), "output")  # voice_radio.py reads this for every call
    voice = data.get("voice")
    if voice:
        cfg.KOKORO_VOICE = voice
        cfg.ENGINEER_ACCENT = {"b": "british", "i": "italian", "a": "american"}.get(voice[0], "british")


def apply_car_class(cfg, data, car_id=None):
    """Copies the thresholds of the car's class into config. Returns the class used."""
    klass = active_class(data, car_id)
    p = _clean(data["classes"][klass], DEFAULT_CLASSES[klass])
    cfg.SPOTTER = p["spotter_on"]
    cfg.SPOTTER_LONG_M, cfg.SPOTTER_LAT_MAX_M = spotter_geometry(p["spotter_level"])
    cfg.SPOTTER_ENTER_S = p["spotter_enter_s"]
    cfg.FUEL_CALL_LAPS = round(p["fuel_margin"], 2)
    cfg.FUEL_LEARNING = bool(data.get("fuel_learning", True))
    cfg.ENGINE_HIGH_RPM_FRACTION = p["engine_rpm_pct"] / 100.0
    cfg.ENGINE_STRESS_LIMIT = p["engine_patience"] / 100.0
    cfg.TYRE_PSI_TARGET = p["tire_target_psi"]
    cfg.TYRE_PSI_WARN, cfg.TYRE_PSI_BOX = psi_limits(     # alerts.py reads these two on every frame
        p["tire_target_psi"], getattr(cfg, "TYRE_PSI_WARN_OFFSET", 8.5), getattr(cfg, "TYRE_PSI_BOX_OFFSET", 13.5))
    return klass


def apply_all(cfg):
    """config.py calls this once at start-up. No saved file = nothing changes."""
    if not os.path.isfile(settings_path()):
        return False
    data = load()
    apply_hardware(cfg, data)
    apply_car_class(cfg, data, None)
    return True


def apply_for_session(cfg, car_id):
    """main_pitwall calls this when a new car/track session starts. Returns the class, or None if nothing saved yet."""
    if not os.path.isfile(settings_path()):
        return None
    return apply_car_class(cfg, load(), car_id)


if __name__ == "__main__":      # python user_settings.py  -> quick self-test
    print("Settings file :", settings_path())
    for kind in ("input", "output"):
        try:
            print(f"{kind:6} devices:", list_devices(kind))
        except Exception as e:
            print(f"{kind} devices unavailable: {e}")
    for car in ("ks_audi_r8_lms_2016", "ks_ferrari_488_gt3", "ks_toyota_ae86_drift", "ks_mazda_mx5_miata", "ks_nissan_gtr"):
        print(f"{car:24} -> {detect_class(car)}")
