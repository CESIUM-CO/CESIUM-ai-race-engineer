"""
cloud_coach.py - optional background coach. After each pit stop it sends a small anonymous stint summary to Claude
and gets back short lessons plus safe tuning suggestions (tyre cliff, fuel margin, tyre call distance).

  * Never blocks the race loop: the request runs on its own thread.
  * Never raises into the caller: any failure just means "no advice this stint".
  * Suggestions are clamped to SAFE_LIMITS before they are returned, so a bad answer can't break the engineer.
  * Lessons are remembered per car+track in %APPDATA%\\CesiumAIRaceEngineer\\data\\cloud_lessons.json (packaged build).
"""
import json
import os
import threading
import urllib.error
import urllib.request

import config
import credentials
from account_manager import AccountManager, ApiError

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = getattr(config, "CLOUD_MODEL", "claude-haiku-5-5")
LESSONS_FILE = os.path.join(config.USER_ROOT, "data", "cloud_lessons.json")
MAX_LESSONS_KEPT = 5
MAX_LESSONS_SHOWN = 3

# (lowest, highest) for every value the coach may suggest
SAFE_LIMITS = {
    "tyre_cliff": (0.05, 0.60),
    "fuel_margin_laps": (0.7, 3.0),
    "tyre_call_laps": (0.5, 4.0),
}

SYSTEM = (
    "You are a motorsport data engineer reviewing one finished stint from a sim-racing session (Assetto Corsa). "
    "You receive JSON: car, track, tyre compound, the last laps (time, fuel used, tyre loss percent, validity), "
    "average fuel per lap, tyre loss per lap for each wheel, and the engineer's current settings. "
    "Reply with ONLY one JSON object and nothing else, no markdown, in exactly this shape: "
    '{"lessons": ["short sentence", "short sentence"], '
    '"suggestions": {"tyre_cliff": 0.30, "fuel_margin_laps": 1.5, "tyre_call_laps": 1.5}}. '
    "lessons: at most 3 plain sentences about what happened (fuel use, tyre wear, pace drop). "
    "suggestions: include ONLY the keys you really want to change. "
    "tyre_cliff is the average tyre health fraction (0.05 to 0.60) at which to call the pit. "
    "fuel_margin_laps is the laps of fuel left when to call the pit (0.7 to 3.0). "
    "tyre_call_laps is how many laps before the cliff to call (0.5 to 4.0). "
    "If the stint tells you nothing new, return an empty suggestions object. Never invent data."
)

_file_lock = threading.Lock()


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


class CloudCoach:
    def __init__(self, on_result=None, model=MODEL, timeout=25):
        self.model, self.timeout, self.on_result = model, timeout, on_result
        self.accounts = AccountManager(credentials.load_keys("anthropic"))
        self._busy = False
        self._busy_lock = threading.Lock()

    # ------------------------------------------------------------------ status / keys
    @property
    def available(self):
        return self.accounts.available          # False while every key is cooling or disabled -> silently skipped

    def refresh_keys(self):                     # call from the Settings tab after Save
        self.accounts.reload(credentials.load_keys("anthropic"))

    # ------------------------------------------------------------------ public: called by main_pitwall at pit entry
    def submit(self, summary, current):
        """Start one background request. Ignored if one is already running or no key is usable."""
        with self._busy_lock:
            if self._busy or not self.available:
                return False
            self._busy = True
        threading.Thread(target=self._work, args=(summary, current), name="CloudCoach", daemon=True).start()
        return True

    # ------------------------------------------------------------------ the HTTP request
    def _request(self, summary, api_key):
        body = json.dumps({"model": self.model, "max_tokens": 400, "system": SYSTEM,
                           "messages": [{"role": "user", "content": json.dumps(summary)}]}).encode()
        req = urllib.request.Request(API_URL, data=body, headers={
            "content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read(2000).decode("utf-8", "replace")
            ra = e.headers.get("retry-after")
            raise ApiError(e.code, text[:200], float(ra) if ra and ra.replace(".", "").isdigit() else None,
                           quota="credit balance" in text.lower() or "spend limit" in text.lower())
        return "".join(b.get("text", "") for b in data.get("content", []))

    def _work(self, summary, current):
        try:
            ok, text = self.accounts.call(lambda key: self._request(summary, key))
            if not ok:
                print("[CLOUD] no usable key right now, skipping this stint summary")
                return
            result = self.parse(text, current)
            self._store(summary, result)
            if self.on_result:
                self.on_result(result)
        except Exception as e:
            print("[CLOUD] skipped: %s" % e)
        finally:
            self._busy = False

    # ------------------------------------------------------------------ the answer -> a safe result
    @staticmethod
    def parse(text, current):
        """Returns {"lessons": [str], "suggestions": {key: number}}. Always safe: bad input gives empty lists."""
        out = {"lessons": [], "suggestions": {}}
        try:
            start, end = (text or "").find("{"), (text or "").rfind("}")
            data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
        except ValueError:
            print("[CLOUD] the answer was not valid JSON, ignoring it")
            return out
        if not isinstance(data, dict):
            return out
        lessons = data.get("lessons")
        if isinstance(lessons, str):
            lessons = [lessons]
        if isinstance(lessons, list):
            out["lessons"] = [str(x).strip()[:200] for x in lessons if str(x).strip()][:MAX_LESSONS_SHOWN]
        sugg = data.get("suggestions")
        if isinstance(sugg, dict):
            for key, (lo, hi) in SAFE_LIMITS.items():
                try:
                    value = _clamp(float(sugg[key]), lo, hi)
                except (KeyError, TypeError, ValueError):
                    continue
                old = (current or {}).get(key)
                if isinstance(old, (int, float)) and abs(value - old) < 0.01:
                    continue                                   # nothing to change
                out["suggestions"][key] = round(value, 3)
        return out

    # ------------------------------------------------------------------ remembered lessons (per car + track)
    @staticmethod
    def _key(car, track):
        return "%s|%s" % (car, track)

    @staticmethod
    def _read_all():
        try:
            with open(LESSONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _store(self, summary, result):
        if not result["lessons"]:
            return
        key = self._key(summary.get("car"), summary.get("track"))
        try:
            with _file_lock:
                data = self._read_all()
                kept = [x for x in data.get(key, []) if x not in result["lessons"]]
                data[key] = (kept + result["lessons"])[-MAX_LESSONS_KEPT:]
                os.makedirs(os.path.dirname(LESSONS_FILE), exist_ok=True)
                tmp = LESSONS_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp, LESSONS_FILE)
        except OSError as e:
            print("[CLOUD] could not save lessons (%s)" % e)

    @classmethod
    def lessons_for(cls, car, track):
        """Newest remembered lessons for this car and track (a list, or [] if none). Used by main_pitwall."""
        with _file_lock:
            return cls._read_all().get(cls._key(car, track), [])[-MAX_LESSONS_SHOWN:]
