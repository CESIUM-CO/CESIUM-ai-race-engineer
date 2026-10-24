"""
ollama_client.py - talks to a local Ollama server (default http://localhost:11434). Standard library only.

OPTIONAL BY DESIGN: if Ollama is not installed / not running, chat() just returns None and brain.py falls back to the
built-in model (local_llm.py) exactly as before. Nothing here can raise into the race program or block the telemetry
loop: every request has a timeout, and three failures in a row switch it off until the next quiet re-check.

States (shown in the control window through Brain.ai_state()):  idle | loading | ready | failed

  start()                    background check + model warm-up (so the first answer is not slow)
  available() -> bool        True when it can answer right now. Also re-checks a failed server every OLLAMA_RETRY_S
  chat(system, user, history, max_tokens) -> str or None
  unload()                   frees the model's memory (called when the program quits, so the game gets its VRAM back)

Settings in config.py: USE_OLLAMA, OLLAMA_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT, OLLAMA_LOAD_TIMEOUT, OLLAMA_KEEP_ALIVE,
OLLAMA_NUM_CTX, OLLAMA_NUM_GPU, OLLAMA_MAX_PROMPT_CHARS, OLLAMA_RETRY_S.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import config

TAG = "[OLLAMA]"


def _cfg(name, default):
    return getattr(config, name, default)


class OllamaClient:
    def __init__(self):
        self.state = "idle"
        self.error = ""
        self._fails = 0
        self._last_probe = 0.0
        self._probing = False
        self._plock = threading.Lock()          # guards "only one probe at a time"
        self._lock = threading.Lock()           # one question at a time (the model answers one at a time anyway)
        self._last_logged = ""
        # localhost must never go through a system proxy
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ------------------------------------------------------------------ settings
    def _base(self):
        return str(_cfg("OLLAMA_URL", "") or "").rstrip("/")

    def _model(self):
        return str(_cfg("OLLAMA_MODEL", "llama3.2:3b"))

    def enabled(self):
        return bool(_cfg("USE_OLLAMA", True) and self._base())

    # ------------------------------------------------------------------ plumbing
    def _http(self, path, payload=None, timeout=5.0):
        data, headers = None, {"User-Agent": "cesium-engineer"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._base() + path, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        with self._opener.open(req, timeout=timeout) as r:
            return json.loads(r.read(2_000_000).decode("utf-8", "replace") or "{}")

    def _explain(self, e):
        if isinstance(e, urllib.error.HTTPError):
            if e.code == 404:
                return "model '%s' was not found. Install it once with:  ollama pull %s" % (self._model(), self._model())
            return "Ollama answered HTTP %d" % e.code
        if isinstance(e, urllib.error.URLError):
            return "Ollama is not running at %s (start the Ollama app, or set USE_OLLAMA = False)" % self._base()
        if isinstance(e, (TimeoutError, OSError)):
            return "no answer in time (%s)" % type(e).__name__
        return "%s: %s" % (type(e).__name__, e)

    def _fail(self, why):
        self.state, self.error = "failed", why
        if why != self._last_logged:                           # say it once, not on every retry
            self._last_logged = why
            print("%s Not available: %s.\n%s The built-in AI is used instead; nothing else changes." % (TAG, why, TAG))

    # ------------------------------------------------------------------ start / re-check
    def start(self):
        if not self.enabled() or self.state != "idle":
            return
        self.state = "loading"
        self._spawn_probe()

    def _spawn_probe(self):
        with self._plock:
            if self._probing:
                return
            self._probing = True
        threading.Thread(target=self._probe, name="OllamaProbe", daemon=True).start()

    def _probe(self):
        t0 = time.time()
        try:
            tags = self._http("/api/tags", timeout=2.5)
            names = {str(m.get("name", "")) for m in tags.get("models", []) if isinstance(m, dict)}
            model = self._model()
            if (model if ":" in model else model + ":latest") not in names:
                self._fail("model '%s' is not installed. Install it once with:  ollama pull %s" % (model, model))
                return
            # an empty prompt only LOADS the model into memory, so the first real question is fast
            self._http("/api/generate", {"model": model, "prompt": "", "stream": False,
                                         "keep_alive": _cfg("OLLAMA_KEEP_ALIVE", "10m")},
                       timeout=float(_cfg("OLLAMA_LOAD_TIMEOUT", 120)))
            self.state, self.error, self._fails, self._last_logged = "ready", "", 0, ""
            print("%s Ready in %.1fs (%s at %s). Chat, banter and lore questions go here." %
                  (TAG, time.time() - t0, model, self._base()))
        except Exception as e:
            self._fail(self._explain(e))
        finally:
            self._last_probe = time.time()
            self._probing = False

    def available(self):
        if not self.enabled():
            return False
        if self.state == "ready":
            return True
        if self.state == "failed" and time.time() - self._last_probe >= float(_cfg("OLLAMA_RETRY_S", 60)):
            self.state = "loading"                             # the user may have started Ollama since: look again, quietly
            self._spawn_probe()
        return False

    # ------------------------------------------------------------------ chatting
    @staticmethod
    def _messages(system, user, history):
        msgs = [{"role": "system", "content": system}]
        for q, a in history or []:
            msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": user})
        limit = int(_cfg("OLLAMA_MAX_PROMPT_CHARS", 6000))
        while sum(len(m["content"]) for m in msgs) > limit and len(msgs) > 2:
            del msgs[1:3]                                      # drop the oldest exchange first
        return msgs

    def chat(self, system, user, history=(), max_tokens=80, timeout=None):
        """A reply, or None (= use the next AI / the rules). Never raises."""
        try:
            if not self.available():
                return None
            timeout = float(timeout or _cfg("OLLAMA_TIMEOUT", 8))
            options = {"temperature": 0.7, "top_p": 0.9, "repeat_penalty": 1.1,
                       "num_predict": int(max_tokens), "num_ctx": int(_cfg("OLLAMA_NUM_CTX", 2048))}
            if _cfg("OLLAMA_NUM_GPU", None) is not None:       # 0 = CPU only: keeps the graphics card free for the game
                options["num_gpu"] = int(_cfg("OLLAMA_NUM_GPU", 0))
            payload = {"model": self._model(), "messages": self._messages(system, user, history), "stream": False,
                       "keep_alive": _cfg("OLLAMA_KEEP_ALIVE", "10m"), "options": options}
            if not self._lock.acquire(timeout=timeout):
                return None
            t0 = time.time()
            try:
                data = self._http("/api/chat", payload, timeout=timeout)
                text = str(((data.get("message") or {}).get("content")) or "").strip()
                if not text:
                    raise ValueError("empty answer")
                self._fails = 0
                print("%s answered in %.1fs" % (TAG, time.time() - t0))
                return text
            except Exception as e:
                self._fails += 1
                print("%s request failed: %s" % (TAG, self._explain(e)))
                if self._fails >= 3 or (isinstance(e, urllib.error.HTTPError) and e.code == 404):
                    self._fail(self._explain(e))
                return None
            finally:
                self._lock.release()
        except Exception as e:                                 # nothing may escape
            print("%s error ignored: %s: %s" % (TAG, type(e).__name__, e))
            return None

    def unload(self):
        """Free the model's memory (the game gets its graphics memory back). Safe to call at any time."""
        if self.state != "ready":
            return
        try:
            self._http("/api/generate", {"model": self._model(), "prompt": "", "stream": False, "keep_alive": 0}, timeout=2.0)
        except Exception:
            pass
