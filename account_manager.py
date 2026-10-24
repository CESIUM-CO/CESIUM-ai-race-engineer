"""
account_manager.py - priority failover across credentials, with per-key cooldowns.

  * Tries credentials in priority order; a key that hits 429 / 529 / quota is rested until its retry-after passes.
  * 401 / 403 disables that key until the user re-saves it (no point retrying a bad key).
  * Never raises into the caller: call() returns (ok, result). When nothing is usable the caller falls back
    to the local LLM / rules, exactly like the rest of the app.
  * Rate limits are per ORGANIZATION: extra keys from the same org will not add capacity. Use this for resilience
    (revoked key, second provider, personal + team key), not for pooling quota.
Standard library only.
"""
import threading
import time
from dataclasses import dataclass

TAG = "[ACCOUNTS]"
MAX_COOLDOWN_S = 3600
QUOTA_COOLDOWN_S = 1800          # credit balance / spend cap: check back in 30 min
DEFAULT_429_COOLDOWN_S = 30


class ApiError(Exception):
    """Raise this from your request function. status = HTTP code, retry_after = seconds from the header (or None)."""

    def __init__(self, status, message="", retry_after=None, quota=False):
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.retry_after, self.quota = status, retry_after, quota


@dataclass
class Slot:
    label: str
    key: str
    priority: int = 100
    cooldown_until: float = 0.0
    disabled: bool = False
    fails: int = 0
    last_error: str = ""
    last_ok: float = 0.0

    def available(self, now):
        return not self.disabled and now >= self.cooldown_until


class AccountManager:
    def __init__(self, creds=(), log=print):
        """creds: iterable of (label, key, priority)."""
        self._lock = threading.Lock()
        self._log = log
        self.slots = []
        self.reload(creds)

    def reload(self, creds):
        """Call after the user adds / removes a key. Keeps cooldown state for keys that are still present;
        a re-saved key that was disabled gets a fresh start only if its secret changed."""
        with self._lock:
            old = {s.key: s for s in self.slots}
            slots = []
            for label, key, prio in creds:
                s = old.get(key) or Slot(label, key, prio)
                s.label, s.priority = label, prio
                slots.append(s)
            self.slots = sorted(slots, key=lambda s: s.priority)

    @property
    def available(self):
        now = time.monotonic()
        with self._lock:
            return any(s.available(now) for s in self.slots)

    def status(self):
        """For a settings screen: [(label, 'ready' | 'cooling 42s' | 'disabled: ...')]"""
        now = time.monotonic()
        with self._lock:
            out = []
            for s in self.slots:
                if s.disabled:
                    out.append((s.label, f"disabled: {s.last_error}"))
                elif now < s.cooldown_until:
                    out.append((s.label, f"cooling {int(s.cooldown_until - now)}s"))
                else:
                    out.append((s.label, "ready"))
            return out

    # ------------------------------------------------------------ the one entry point
    def call(self, fn):
        """fn(api_key) -> result, raising ApiError on HTTP problems. Returns (True, result) or (False, None)."""
        tried = set()
        while True:
            slot = self._pick(tried)
            if slot is None:
                return False, None
            tried.add(slot.key)
            try:
                result = fn(slot.key)
            except ApiError as e:
                self._report(slot, e)
                continue                                   # next credential
            except Exception as e:                         # network error, timeout: brief rest, then try the next one
                self._rest(slot, 10, f"{type(e).__name__}: {e}")
                continue
            with self._lock:
                slot.fails, slot.last_ok, slot.last_error = 0, time.monotonic(), ""
            return True, result

    # ------------------------------------------------------------ internals
    def _pick(self, tried):
        now = time.monotonic()
        with self._lock:
            for s in self.slots:
                if s.key not in tried and s.available(now):
                    return s
        return None

    def _report(self, slot, e):
        if e.status in (401, 403):
            with self._lock:
                slot.disabled, slot.last_error = True, f"rejected (HTTP {e.status})"
            self._log(f"{TAG} '{slot.label}' was rejected (HTTP {e.status}). Disabled until you re-save it.")
        elif e.status in (429, 529) or e.quota:
            if e.retry_after:
                secs = e.retry_after
            elif e.quota:
                secs = QUOTA_COOLDOWN_S
            else:
                secs = DEFAULT_429_COOLDOWN_S * (2 ** min(slot.fails, 4))   # exponential backoff
            self._rest(slot, secs, "quota exhausted" if e.quota else "rate limited")
        else:
            self._rest(slot, 15, str(e))

    def _rest(self, slot, seconds, why):
        seconds = min(MAX_COOLDOWN_S, max(1.0, float(seconds)))
        with self._lock:
            slot.fails += 1
            slot.cooldown_until = time.monotonic() + seconds
            slot.last_error = why
        self._log(f"{TAG} '{slot.label}' resting {seconds:.0f}s ({why}).")
