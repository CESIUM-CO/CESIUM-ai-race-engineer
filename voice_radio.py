import itertools
import os
import random
import re
import threading
import time

import config


def split_sentences(text):
    """'Box now! Tyres gone.' -> ['Box now!', 'Tyres gone.']  Each sentence is cached on its own, so they get reused."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if p.strip()]
    return parts or [text]


class _Msg:
    """One thing to say. kind 'alert' = his own warnings and calls; kind 'answer' = a reply to the driver's question."""
    __slots__ = ("text", "prio", "kind", "ts", "max_age", "seq", "restarts", "start", "voice")

    def __init__(self, text, prio, kind, max_age, seq, voice=None):
        self.text, self.prio, self.kind, self.max_age, self.seq = text, prio, kind, max_age, seq
        self.voice = voice                   # set only for a Settings-tab PREVIEW: say it in this voice, never in another
        self.ts, self.restarts, self.start = time.time(), 0, 0

    @property
    def qprio(self):
        return 1.5 if self.kind == "answer" else self.prio       # alerts 0/1 go before answers, low alerts (2) after


class _Queue:
    """Tiny priority queue that can also be searched (the speaking thread asks: is an important alert waiting?)."""

    def __init__(self):
        self.items = []
        self.cv = threading.Condition()

    def put(self, m):
        with self.cv:
            self.items.append(m)
            self.cv.notify()

    def get(self):
        with self.cv:
            while not self.items:
                self.cv.wait()
            m = min(self.items, key=lambda x: (x.qprio, x.seq))
            self.items.remove(m)
            return m

    def has(self, pred):
        with self.cv:
            return any(pred(m) for m in self.items)

    def empty(self):
        with self.cv:
            return not self.items

    def clear(self):
        with self.cv:
            self.items.clear()


class PitwallVoiceEngine:
    """Priority radio. Alerts: 0 = urgent, 1 = normal, 2 = low. Answers to the driver are their own kind.

    SPEAKING ORDER (nobody is ever cut off mid-sentence):
      * An alert is never interrupted by another alert or by an answer. It finishes, then the next one plays.
      * An answer to the driver's question waits for any alert that is already speaking.
      * If an IMPORTANT alert (priority 0 or 1) arrives while an answer is being spoken, the answer pauses at the end of
        its current sentence, the alert plays in full, then the answer is said again from the start.
      * An answer that has been spoken completely is never repeated.
      * An answer that is still waiting in line simply lets the alert go first.
      * The driver pressing the talk key silences him at once, as before.

    HOW THE VOICE STARTS (nothing for the driver to set up):
      1. The Windows voice speaks immediately, so he is never silent.
      2. In the background the natural voice (Kokoro) downloads once, loads, and takes over by itself.

    HOW IT STAYS FAST:
      * Every sentence he says is saved to disk the first time (data/voice_cache), so it plays instantly forever after.
      * His common calls (flags, spotter, tyre pressure calls...) are recorded in the background right after start-up.
      * Long calls are spoken sentence by sentence: the next sentence is generated while the first one is playing.
      * Silence at the start of each clip is trimmed and the audio output runs in low-latency mode."""

    def __init__(self):
        self.q = _Queue()
        self.counter = itertools.count()
        self.stop_now = threading.Event()
        self._sapi = None
        self.busy = False
        self.quiet = False
        self.last_text = ""
        self.cache = {}                      # sentence -> (samples, rate), this run
        self._kokoro = None
        self._prerender_list = []
        self._prerender_started = False
        self.kokoro_state = "off"            # off | loading | ready | failed
        self.kokoro_seconds = None           # how long one short sentence takes to generate
        self._voice_name = config.KOKORO_VOICE         # the voice in use (the Settings tab can change it while running)
        self._tts_lock = threading.Lock()        # Kokoro / espeak are not thread-safe: ONE sentence is generated at a time
        self._prerender_gen = 0                  # bumped when the voice changes, so an old recording job stops itself
        self._sapi_output = None                 # speaker NAME the Windows voice must use (None = Windows default)
        self._sapi_dirty = False                 # True = rebuild the Windows voice on the speaking thread before the next message
        self._warned_device = False
        self._current = None                     # the message being spoken right now (the preview code looks at it)
        threading.Thread(target=self._worker, daemon=True).start()
        if config.TTS_ENGINE == "kokoro":
            self.kokoro_state = "loading"
            print("[VOICE] Speaking with the Windows voice for now. The natural voice is getting ready in the "
                  "background and will take over by itself.")
            threading.Thread(target=self._kokoro_loader, daemon=True).start()

    def _lang(self, voice=None):
        return "en-gb" if (voice or config.KOKORO_VOICE).startswith("b") else "en-us"    # Italian voices speak English phonemes

    # ------------------------------------------------------------------ natural voice (background)
    def _kokoro_loader(self):
        try:
            from kokoro_onnx import Kokoro
            import sounddevice as sd
        except Exception as e:
            self.kokoro_state = "failed"
            print(f"[VOICE] The natural voice needs one install ({type(e).__name__}: {e}).\n"
                  "        Run this once in the terminal:   pip install -r requirements.txt\n"
                  "        He keeps using the Windows voice until then.")
            return
        try:
            import fetch
            files = [(config.KOKORO_MODEL, config.KOKORO_MODEL_MIN_BYTES),
                     (config.KOKORO_VOICES, config.KOKORO_VOICES_MIN_BYTES)]
            for path, min_bytes in files:
                if os.path.isfile(path) and os.path.getsize(path) >= min_bytes:
                    continue
                if not config.KOKORO_AUTO_DOWNLOAD:
                    raise FileNotFoundError(f"missing file: {path}")
                got = fetch.download(config.KOKORO_BASE_URL + os.path.basename(path), path, min_bytes,
                                     f"the natural voice ({os.path.basename(path)})",
                                     lambda m: print(f"[VOICE] {m}"))
                if got is None:
                    raise IOError(f"could not download {os.path.basename(path)}")
            k = Kokoro(config.KOKORO_MODEL, config.KOKORO_VOICES)
            try:
                if config.KOKORO_VOICE not in set(k.get_voices()):
                    print(f"[VOICE] Voice '{config.KOKORO_VOICE}' isn't in this voice pack. Using bm_george instead.")
                    config.KOKORO_VOICE = "bm_george"
                    self._voice_name = "bm_george"
            except Exception:
                pass
            try:
                sd.default.latency = "low"                       # shorter gap between "decided to speak" and sound
            except Exception:
                pass
            t0 = time.time()
            k.create("Box, box, box now.", voice=config.KOKORO_VOICE, speed=config.VOICE_SPEED, lang=self._lang())
            self.kokoro_seconds = time.time() - t0
            self._kokoro = k
            self.kokoro_state = "ready"
            print(f"[VOICE] Natural voice ready ({config.KOKORO_VOICE}, {config.ENGINEER_ACCENT} accent). "
                  "From now on he speaks with it.")
            if self.kokoro_seconds > 1.5 and config.KOKORO_QUALITY == "high":
                print(f"[VOICE] A new sentence takes {self.kokoro_seconds:.1f}s to generate on this PC. Common calls are "
                      "pre-recorded, but if new ones feel late, set KOKORO_QUALITY = \"fast\" in config.py.")
            self._maybe_prerender()
        except Exception as e:
            self.kokoro_state = "failed"
            print(f"[VOICE] The natural voice couldn't start ({type(e).__name__}: {e}).\n"
                  "        He keeps using the Windows voice.")

    def set_voice(self, name):
        """Switch the natural voice while the program runs (the Settings tab calls this). True if it is in use."""
        if name == self._voice_name:
            return True
        k = self._kokoro
        if k is not None:
            try:
                if name not in set(k.get_voices()):
                    print(f"[VOICE] Voice '{name}' isn't in this voice pack.")
                    return False
            except Exception:
                pass
        config.KOKORO_VOICE = name
        self._voice_name = name
        self.cache.clear()                       # the in-memory sentences belong to the old voice
        self._prerender_gen += 1
        self._prerender_started = False          # record the common calls again, in the new voice
        self._maybe_prerender()
        print(f"[VOICE] Voice changed to {name}. Recording the common calls in the new voice in the background...")
        return True

    # ------------------------------------------------------------------ cache + pre-recording
    def _cache_path(self, sentence, voice=None):
        key = f"{voice or config.KOKORO_VOICE}|{config.VOICE_SPEED}|{config.KOKORO_QUALITY}|{sentence}"
        import hashlib                       # only needed once a sentence is cached
        return os.path.join(config.VOICE_CACHE_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".npz")

    @staticmethod
    def _trim(np, samples):
        """Cut the silent lead-in and tail so the words start the moment the audio does."""
        x = np.asarray(samples, dtype=np.float32)
        loud = np.nonzero(np.abs(x) > 0.01)[0]
        if len(loud) == 0:
            return x
        return x[max(0, loud[0] - 240):min(len(x), loud[-1] + 960)]          # ~10 ms before, ~40 ms after at 24 kHz

    def _audio_for(self, kokoro, sentence, remember=True, voice=None):
        """(samples, rate) for one sentence: memory, then disk, then generate (and save).
        voice=None means the voice in use; a preview passes its own voice, and the cache keeps them apart."""
        voice = voice or config.KOKORO_VOICE
        ckey = (voice, sentence)
        hit = self.cache.get(ckey)
        if hit is not None:
            return hit
        import numpy as np
        path = self._cache_path(sentence, voice)
        try:
            if os.path.isfile(path):
                with np.load(path) as z:
                    audio = (z["s"], int(z["r"]))
                if remember:
                    self.cache[ckey] = audio
                return audio
        except Exception:
            pass                                                          # unreadable cache file: just regenerate
        t0 = time.time()
        with self._tts_lock:                                   # the speaking thread and the recorder thread share one Kokoro
            samples, rate = kokoro.create(sentence, voice=voice, speed=config.VOICE_SPEED, lang=self._lang(voice))
        audio = (self._trim(np, samples), int(rate))
        took = time.time() - t0
        if took > 0.8:
            print(f"[VOICE] new sentence took {took:.1f}s to generate (it is saved, so next time it is instant)")
        try:
            os.makedirs(config.VOICE_CACHE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                np.savez(f, s=audio[0], r=audio[1])
            os.replace(tmp, path)
        except Exception:
            pass
        if remember:
            self.cache[ckey] = audio
        return audio

    def prerender(self, phrases):
        """Record these calls in the background (once; they are saved to disk for every later run)."""
        if not config.VOICE_PRERENDER or config.TTS_ENGINE != "kokoro":
            return
        seen, todo = set(), []
        for p in phrases:
            for s in split_sentences(p):
                if s not in seen:
                    seen.add(s)
                    todo.append(s)
        self._prerender_list = todo
        self._prerender_gen += 1
        self._prerender_started = False          # a second call (new voice / new PSI limits) records what is missing
        self._maybe_prerender()

    def _maybe_prerender(self):
        if self._kokoro is not None and self._prerender_list and not self._prerender_started:
            self._prerender_started = True
            threading.Thread(target=self._prerender_worker, args=(self._kokoro, list(self._prerender_list), self._prerender_gen),
                             daemon=True).start()

    def _prerender_worker(self, kokoro, sentences, gen):
        try:
            todo = [s for s in sentences if not os.path.isfile(self._cache_path(s))]
            if not todo:
                return
            print(f"[VOICE] Recording {len(todo)} of his common calls so they play instantly "
                  "(one time only, in the background)...")
            for n, s in enumerate(todo, 1):
                if gen != self._prerender_gen:
                    return                                         # the voice changed: a newer recording job took over
                while self.busy or not self.q.empty():            # never compete with a call being spoken
                    time.sleep(0.2)
                if gen != self._prerender_gen:
                    return
                self._audio_for(kokoro, s, remember=False)
                time.sleep(0.15)                                   # stay gentle on the CPU while you drive
                if n % max(1, len(todo) // 4) == 0 and n < len(todo):
                    print(f"[VOICE] ...{n * 100 // len(todo)}% recorded")
            print("[VOICE] Common calls recorded. They now play instantly.")
        except Exception as e:
            print(f"[VOICE] Recording stopped ({type(e).__name__}: {e}). Calls are still generated when needed.")

    # ------------------------------------------------------------------ Windows voice
    def _load_sapi(self, output_name=None):
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            sp = win32com.client.Dispatch("SAPI.SpVoice")
            sp.Rate, sp.Volume = config.VOICE_RATE, config.VOICE_VOLUME
            wanted = config.VOICE_NAME_CONTAINS.lower()
            if wanted:
                voices = sp.GetVoices()
                for i in range(voices.Count):
                    if wanted in voices.Item(i).GetDescription().lower():
                        sp.Voice = voices.Item(i)
                        break
            if output_name:                                   # send the Windows voice to the speaker the driver picked
                outs = sp.GetAudioOutputs()
                want = output_name.lower()
                for i in range(outs.Count):
                    desc = outs.Item(i).GetDescription().lower()
                    if desc.startswith(want) or want.startswith(desc) or want in desc:   # PortAudio cuts names at 31 characters
                        sp.AudioOutput = outs.Item(i)
                        break
                else:
                    print(f"[VOICE] The Windows voice could not find the speaker '{output_name}', using the default one.")
            return sp
        except Exception as e:
            print(f"[VOICE] Windows voice not available ({type(e).__name__}: {e}). "
                  "It needs:  pip install pywin32")
            return None

    # ------------------------------------------------------------------ playing one message
    def _should_preempt(self, m):
        """True when an answer should pause because an important alert is waiting."""
        return (m.kind == "answer" and m.restarts < config.ANSWER_MAX_PREEMPTS
                and self.q.has(lambda x: x.kind == "alert" and x.prio <= config.ANSWER_PREEMPT_MAX_PRIO))

    def _run_sapi(self, m, sp):
        """Windows voice, a sentence at a time. Returns (finished, next_sentence_index)."""
        parts = split_sentences(m.text)
        for i in range(m.start, len(parts)):
            if self.stop_now.is_set():
                return True, i
            if self._should_preempt(m):
                return False, i
            sp.Speak(parts[i], 1)                         # 1 = speak in the background so we can still be stopped
            while not sp.WaitUntilDone(50):
                if self.stop_now.is_set():
                    sp.Speak("", 2)                       # 2 = purge
                    return True, i
        return True, len(parts)

    def _run_kokoro(self, m, kokoro):
        """Natural voice: the next sentence is generated while the current one plays. Returns (finished, next_index)."""
        import sounddevice as sd
        parts = split_sentences(m.text)
        if m.start >= len(parts):
            return True, len(parts)
        nxt = self._audio_for(kokoro, parts[m.start], voice=m.voice)      # the only wait: the first sentence
        for i in range(m.start, len(parts)):
            if self.stop_now.is_set():
                return True, i
            if self._should_preempt(m):
                return False, i
            samples, rate = nxt
            self._play(sd, samples, rate)      # None = the Windows default speaker
            nxt = self._audio_for(kokoro, parts[i + 1], voice=m.voice) if i + 1 < len(parts) else None
            while sd.get_stream().active:
                if self.stop_now.is_set():
                    sd.stop()
                    return True, i
                time.sleep(0.01)
        return True, len(parts)

    def _play(self, sd, samples, rate):
        """Play on the speaker chosen in Settings. If Windows refuses that device, play on the default one (and say so once)
        instead of dropping to the Windows voice."""
        dev = config.SPEAKER_DEVICE
        try:
            sd.play(samples, rate, device=dev)
        except Exception as e:
            if dev is None:
                raise
            if not self._warned_device:
                self._warned_device = True
                print(f"[VOICE] The chosen speaker (device {dev}) could not be opened ({type(e).__name__}: {e}). "
                      "Using the Windows default speaker. Pick another one in Settings.")
            sd.play(samples, rate, device=None)

    def set_output_device(self, index, name=None):
        """Settings tab: another speaker / headset was picked. The natural voice reads config.SPEAKER_DEVICE for every
        sentence, so it moves at once. The Windows voice is rebuilt on the speaking thread before the next message."""
        config.SPEAKER_DEVICE = index
        self._warned_device = False
        if name != self._sapi_output:
            self._sapi_output = name
            self._sapi_dirty = True

    @staticmethod
    def _saved_speaker_name():
        """The speaker saved in Settings (only when one was really chosen)."""
        if getattr(config, "SPEAKER_DEVICE", None) is None:
            return None
        try:
            import user_settings
            return user_settings.load().get("speaker")
        except Exception:
            return None

    def _wait_for_natural(self, m, age):
        """True while the natural voice is still loading and this message may wait for it. Urgent alerts never wait.
        (Without this, the first radio call after every start is spoken by the Windows voice.)"""
        limit = getattr(config, "VOICE_WAIT_FOR_NATURAL_S", 15)
        if m.voice:                                   # a preview is only queued once the natural voice is ready
            return False
        return (limit > 0 and config.TTS_ENGINE == "kokoro" and self._kokoro is None
                and self.kokoro_state == "loading" and age < limit
                and not (m.kind == "alert" and m.prio == 0))

    def _has_voice(self):
        return self._kokoro is not None or self._sapi is not None

    def _run(self, m):
        kokoro = self._kokoro                              # picked per message: switches over the moment it is ready
        if kokoro is not None:
            for attempt in (1, 2):                         # one quiet retry before the Windows voice is used
                try:
                    return self._run_kokoro(m, kokoro)
                except Exception as err:
                    print(f"[VOICE ERROR] natural voice ({config.KOKORO_VOICE}) failed, attempt {attempt} of 2: "
                          f"{type(err).__name__}: {err}")
                    m.start = 0
                    time.sleep(0.2)
            if m.voice:                                    # a preview must NEVER turn into the Windows voice
                print(f"[VOICE] Preview in '{m.voice}' failed (see the error above). Nothing was played.")
                return True, 0
            if self._sapi is None:
                return True, 0
            print("[VOICE] Using the Windows voice for this one message only. The natural voice stays selected.")
        return self._run_sapi(m, self._sapi)

    # ------------------------------------------------------------------ the speaking thread
    def _worker(self):
        self._sapi_output = self._saved_speaker_name()
        self._sapi = self._load_sapi(self._sapi_output)            # must live on this thread (Windows COM rule)
        while True:
            m = self.q.get()
            if self._sapi_dirty:                   # the speaker changed: rebuild the Windows voice on THIS thread (COM rule)
                self._sapi_dirty = False
                self._sapi = self._load_sapi(self._sapi_output)
            age = time.time() - m.ts
            if m.max_age is not None and age > m.max_age:
                continue                           # a spotter call that is out of date is worse than none
            if m.kind == "answer" and age > config.ANSWER_MAX_AGE_S:
                continue
            if self._wait_for_natural(m, age):
                self.q.put(m)                      # not yet: give the natural voice a moment to load
                time.sleep(0.3)
                continue
            if not self._has_voice():
                if self.kokoro_state == "loading" and age < 15:
                    self.q.put(m)                  # nothing can speak yet: try again shortly
                    time.sleep(0.5)
                continue
            self.stop_now.clear()
            self._current = m
            self.busy = True
            finished, index = True, 0
            try:
                finished, index = self._run(m)
            except Exception as err:
                print(f"[VOICE ERROR] {type(err).__name__}: {err}")
            finally:
                self.busy = False
                self._current = None
            if not finished:                       # an answer paused for an important alert: say it again afterwards
                m.restarts += 1
                m.start = index if config.VOICE_RESUME_ANSWER == "continue" else 0
                self.q.put(m)

    # ------------------------------------------------------------------ public
    def speak(self, text, priority=1, kind="alert", max_age=None, voice=None):
        """kind='alert' for his own calls, kind='answer' for replies to the driver. max_age: drop it if it waits longer."""
        if isinstance(text, (list, tuple)):
            text = random.choice(text)
        self.last_text = text
        muted = kind == "alert" and self.quiet and priority > 0
        waiting = " (waiting for him to finish)" if (self.busy or not self.q.empty()) and not muted else ""
        print(f"\n🎙️ [PITWALL RADIO]{' (muted)' if muted else ''}{waiting}: \"{text}\"")
        if muted:
            return
        if self.q.has(lambda m: m.text == text and m.kind == kind and m.voice == voice):
            return                                 # the same call is already waiting: don't pile it up
        self.q.put(_Msg(text, priority, kind, max_age, next(self.counter), voice))

    def preview(self, voice_id, text):
        """Settings tab "Preview": say `text` in `voice_id` WITHOUT changing the voice the engineer really uses
        (Save does that). Always the natural voice: if it is not ready, nothing is played and the log says why,
        instead of quietly speaking with the Windows voice. Returns True if the preview was queued."""
        k = self._kokoro
        if k is None:
            print(f"[VOICE] Preview: the natural voice is not ready yet (state: {self.kokoro_state}). Try again in a moment.")
            return False
        try:
            if voice_id not in set(k.get_voices()):
                print(f"[VOICE] Preview: voice '{voice_id}' isn't in this voice pack.")
                return False
        except Exception:
            pass
        with self.q.cv:                                    # a newer preview replaces any older one still waiting
            self.q.items = [m for m in self.q.items if m.voice is None]
        cur = self._current
        if self.busy and cur is not None and cur.voice is not None:
            self.stop_now.set()                            # ...and one that is playing right now
        self.speak(text, 1, kind="answer", voice=voice_id)
        return True

    def shush(self):
        """Driver started talking: stop the engineer immediately."""
        self.q.clear()
        if self.busy:
            self.stop_now.set()

    def wait_until_done(self, timeout=8):
        end = time.time() + timeout
        while (not self.q.empty() or self.busy) and time.time() < end:
            time.sleep(0.1)
