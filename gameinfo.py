import json
import os
import re

import config

ZONE_NAMES = ["front", "rear", "left side", "right side", "centre"]
_UPPER = {"gt3", "gt2", "gt4", "lms", "gtr", "gte", "dtm", "bmw", "amg", "rs", "tt", "tdi", "gt"}


def _pretty(raw):
    raw = re.sub(r"^(ks|acc|rss)_", "", raw or "", flags=re.I)
    words = [w for w in re.split(r"[_\-\s]+", raw) if w]
    return " ".join(w.upper() if (w.lower() in _UPPER or len(w) <= 2 or any(c.isdigit() for c in w))
                    else w.capitalize() for w in words)


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            return json.loads(f.read(), strict=False)
    except Exception:
        return None


def car_name(car_id):
    data = _read_json(os.path.join(config.AC_PATH, "content", "cars", car_id, "ui", "ui_car.json"))
    return (data or {}).get("name") or _pretty(car_id)


def track_name(track_id, layout):
    base = os.path.join(config.AC_PATH, "content", "tracks", track_id, "ui")
    path = os.path.join(base, layout, "ui_track.json") if layout else os.path.join(base, "ui_track.json")
    name = (_read_json(path) or {}).get("name")
    return name or (_pretty(track_id) + (f" {_pretty(layout)}" if layout else ""))


def weather_line(st):
    air, road, wind, grip = st.get("air_temp"), st.get("road_temp"), st.get("wind"), st.get("grip")
    parts = []
    if air is not None and road is not None:
        parts.append(f"Air {air:.0f} degrees, track {road:.0f}.")
    if wind is not None:
        parts.append(f"Wind around {wind:.0f}.")
    if grip is not None:
        parts.append(f"Grip {grip * 100:.0f} percent." + (" That's low, the track may be damp." if grip < 0.93 else ""))
    return " ".join(parts) or "No weather data yet."
