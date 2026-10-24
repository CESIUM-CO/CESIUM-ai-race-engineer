"""
local_llm.py - the engineer's AI brain. Zero setup for the driver: no Ollama, no other app, no account.

ORDER OF PREFERENCE for every question
  1. BUILT-IN MODEL   a small GGUF model (default Qwen2.5-1.5B) run by llama-cpp-python inside a helper process.
                      The .gguf can ship next to the program (models/ folder, also inside a packaged .exe folder);
                      if it is missing it downloads itself once (resumable, no account).
  2. ONLINE FALLBACK  optional. A tiny proxy worker you deploy once (cloud_worker/) so drivers need no account.
                      Only used while the built-in model is loading or has failed. Off while LLM_ONLINE_URL is empty.
  3. RULES ONLY       chat() returns None and the engineer uses his deterministic logic, memory and trusted web search.

NOTHING here can raise into the race program, block the telemetry loop, or crash it: the model lives in its own
process (llm_worker.py), every network call has a timeout, and every failure just means "return None".
"""
import json
import os
import queue
import re
import sys
import threading
import time

import config
import fetch
import paths          # NEW: finds files and the helper program in source runs AND Nuitka builds


def _clean(text):
    return re.sub(r"\s+", " ", re.sub(r"[*_#`]", "", text or "")).strip()


# ====================================================================== model file
def find_model():
    """Path of the model file if it is already on disk (including next to a packaged .exe), else None."""
    dirs = [config.MODELS_DIR]
    for d in paths.resource_candidates("models"):      # next to the .exe / in the .dist folder / in the project
        if d not in dirs:
            dirs.append(d)
    for d in dirs:
        path = os.path.join(d, config.LLM_FILE)
        if os.path.isfile(path) and os.path.getsize(path) >= config.LLM_MIN_BYTES:
            return path
    return None


def download_model(log=print):
    """One-time, resumable download into MODELS_DIR. Returns the path, or None."""
    final = os.path.join(config.MODELS_DIR, config.LLM_FILE)
    return fetch.download(config.LLM_URL, final, config.LLM_MIN_BYTES, "the built-in AI model",
                          lambda m: log(f"[AI] {m}"))


# ====================================================================== the helper process
class EngineError(Exception):
    """The model answered with an error, but is still alive."""


class EngineDead(Exception):
    """The helper process died or hung (it has been stopped)."""


class _Engine:
    def __init__(self, model_path):
        self.model_path = model_path
        self.proc = None
        self.out = queue.Queue()
        self.error = ""
        self._id = 0

    def start(self):
        import subprocess                       # only when the model is really started
        try:
            cmd = paths.worker_command()                # compiled: this exe + --llm-worker. Source: python llm_worker.py
        except FileNotFoundError as e:
            self.error = f"could not find the AI helper ({e})"
            return False
        cmd += [self.model_path, str(config.LLM_CONTEXT), str(config.LLM_THREADS)]
        print(f"[AI] Starting the AI helper: {cmd[0]} {' '.join(cmd[1:2])}")
        flags = 0
        if os.name == "nt":      # low priority + no console window: the game keeps its frame rate
            flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                         bufsize=1, creationflags=flags)
        except Exception as e:
            self.error = f"could not start the AI helper ({type(e).__name__}: {e})"
            return False
        threading.Thread(target=self._reader, args=(self.proc, self.out), daemon=True).start()
        end = time.time() + config.LLM_LOAD_TIMEOUT
        while True:
            try:
                msg = self.out.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                self.error = "the AI model took too long to load"
                self.kill()
                return False
            if msg.get("event") == "ready":
                return True
            if msg.get("event") in ("error", "exit"):
                self.error = msg.get("error") or "the AI helper stopped while loading"
                self.kill()
                return False

    @staticmethod
    def _reader(proc, out):
        try:
            for line in proc.stdout:
                try:
                    out.put(json.loads(line))
                except ValueError:
                    pass
        except Exception:
            pass
        out.put({"event": "exit"})

    def ask(self, messages, max_tokens, timeout):
        self._id += 1
        rid = self._id
        try:
            self.proc.stdin.write(json.dumps({"id": rid, "messages": messages, "max_tokens": max_tokens,
                                              "temperature": 0.5}) + "\n")
            self.proc.stdin.flush()
        except Exception:
            self.kill()
            raise EngineDead("the AI helper is not running")
        end = time.time() + timeout
        while True:
            try:
                msg = self.out.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                self.kill()                               # too slow: stop it for real, it restarts in the background
                raise EngineDead(f"no answer within {timeout}s")
            if msg.get("event") == "exit":
                raise EngineDead("the AI helper exited")
            if msg.get("id") == rid:
                if "error" in msg:
                    raise EngineError(msg["error"])
                return msg.get("text")

    def kill(self):
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass


# ====================================================================== optional online fallback
class OnlineClient:
    FAILS_BEFORE_PAUSE, PAUSE_SECONDS = 3, 300

    def __init__(self):
        self.fails, self.paused_until, self.announced = 0, 0.0, False

    def available(self):
        return bool(config.LLM_ONLINE_FALLBACK and config.LLM_ONLINE_URL and time.time() >= self.paused_until)

    def chat(self, messages, max_tokens):
        try:
            import urllib.request                   # only loaded if the online fallback is actually used
            from urllib.parse import urlparse
            if not self.announced:
                self.announced = True
                print(f"[AI] Using the online fallback ({urlparse(config.LLM_ONLINE_URL).netloc}). "
                      "The question and race data you ask about are sent there.")
            headers = {"Content-Type": "application/json", "User-Agent": "pitwall-engineer"}
            if config.LLM_ONLINE_KEY:
                headers["Authorization"] = "Bearer " + config.LLM_ONLINE_KEY
            body = json.dumps({"model": config.LLM_ONLINE_MODEL, "messages": messages,
                               "max_tokens": max_tokens, "temperature": 0.5}).encode("utf-8")
            req = urllib.request.Request(config.LLM_ONLINE_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=config.LLM_ONLINE_TIMEOUT) as r:
                data = json.loads(r.read(1_000_000).decode("utf-8", "replace"))
            text = (data.get("choices", [{}])[0].get("message", {}).get("content")
                    or data.get("response") or data.get("reply") or "")
            if not str(text).strip():
                raise ValueError("empty answer")
            self.fails = 0
            return str(text)
        except Exception as e:
            self.fails += 1
            print(f"[AI] online fallback failed ({type(e).__name__}: {e})")
            if self.fails >= self.FAILS_BEFORE_PAUSE:
                self.fails = 0
                self.paused_until = time.time() + self.PAUSE_SECONDS
                print("[AI] online fallback paused for 5 minutes (rules and the built-in model still work).")
            return None


# ====================================================================== the public face
class LocalLLM:
    """Same interface the rest of the program has always used: start(), ready, chat(), spoken_status()."""

    def __init__(self):
        self.state = "idle"          # idle | loading | ready | failed      (the BUILT-IN model)
        self.error = ""
        self.backend = None
        self.online = OnlineClient()
        self._engine = None
        self._lock = threading.Lock()
        self._restarts = 0

    # ------------------------------------------------------------------ status
    @property
    def ready(self):
        """True when ANY AI route can answer right now (built-in model or online fallback)."""
        return self.state == "ready" or self.online.available()

    def spoken_status(self):
        if self.ready:
            return ""
        if self.state == "loading":
            return "My AI brain is still loading. The race calls all work, ask me again in a minute."
        return "My AI brain isn't available, but I still handle fuel, tyres, flags and the race basics."

    # ------------------------------------------------------------------ loading (always in the background)
    def start(self):
        if self.state != "idle" or not config.USE_LLM:
            return
        self.state = "loading"
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        t0 = time.time()
        self.state = "loading"
        try:
            model = find_model()
            if model is None:
                if not config.LLM_AUTO_DOWNLOAD:
                    raise RuntimeError(f"model file {config.LLM_FILE} not found in {config.MODELS_DIR}")
                print(f"[AI] First run: downloading the built-in AI model (about 1 GB, one time, no account).\n"
                      "     Race calls already work while this downloads.")
                model = download_model()
                if model is None:
                    raise RuntimeError("the model could not be downloaded")
            engine = _Engine(model)
            if not engine.start():
                raise RuntimeError(engine.error)
            with self._lock:
                self._engine = engine
            self.backend = f"built-in, {config.LLM_FILE}"
            self.state, self.error = "ready", ""
            print(f"[AI] Built-in AI ready in {time.time() - t0:.1f}s ({config.LLM_FILE}).")
        except Exception as e:
            self.state, self.error = "failed", str(e)
            hint = ("   Fix (one time):  pip install -r requirements_ai.txt"
                    if "not installed" in self.error else "")
            route = "the online fallback" if self.online.available() else "rules only"
            print(f"[AI] Built-in AI unavailable: {self.error}\n[AI] The race engineer keeps working ({route}).{hint}")

    def _engine_died(self, reason):
        print(f"[AI] The AI helper stopped ({reason}).")
        self._restarts += 1
        if self._restarts > config.LLM_MAX_RESTARTS:
            self.state, self.error = "failed", "the built-in AI kept crashing, so it is switched off for this session"
            print(f"[AI] {self.error}.")
            return
        self.state = "loading"
        threading.Thread(target=self._load, daemon=True).start()

    def shutdown(self):
        """Stop the model's helper process (called when the program quits)."""
        self.state = "failed"
        engine, self._engine = self._engine, None
        if engine is not None:
            engine.kill()

    # ------------------------------------------------------------------ chatting
    @staticmethod
    def _messages(system, user, history):
        msgs = [{"role": "system", "content": system}]
        for q, a in history or []:
            msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": user})
        limit = config.LLM_MAX_PROMPT_CHARS
        while sum(len(m["content"]) for m in msgs) > limit and len(msgs) > 2:
            del msgs[1:3]                                   # drop the oldest exchange
        over = sum(len(m["content"]) for m in msgs) - limit
        if over > 0:
            msgs[0]["content"] = msgs[0]["content"][:max(200, len(msgs[0]["content"]) - over)]
        return msgs

    def chat(self, system, user, history=(), max_tokens=70, timeout=None):
        """A short clean answer, or None (meaning: use the rules). Never raises, never blocks longer than the timeouts."""
        try:
            if not config.USE_LLM:
                return None
            messages = self._messages(system, user, history)
            text = None
            if self.state == "ready":
                text = self._chat_local(messages, max_tokens, timeout or config.LLM_TIMEOUT)
            if text is None and self.online.available():
                text = self.online.chat(messages, max_tokens)
            return _clean(text) or None
        except Exception as e:
            print(f"[AI] error ignored: {type(e).__name__}: {e}")
            return None

    def _chat_local(self, messages, max_tokens, timeout):
        if not self._lock.acquire(timeout=timeout):
            return None
        try:
            engine = self._engine
            if engine is None:
                return None
            t0 = time.time()
            try:
                text = engine.ask(messages, max_tokens, timeout)
            except EngineError as e:
                print(f"[AI] model error: {e}")
                return None
            except EngineDead as e:
                self._engine = None
                self._engine_died(str(e))
                return None
            print(f"[AI] answered in {time.time() - t0:.1f}s")
            return text
        finally:
            self._lock.release()
