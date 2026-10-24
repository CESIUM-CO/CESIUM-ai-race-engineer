"""Run ONCE:  python setup_check.py
Downloads what's missing and tests voice out, voice in, and the local AI, one by one."""
import os
import time

import config

def header(t):
    print(f"\n{'=' * 8} {t} {'=' * 8}")


def test_voice_out():
    header("1/3  NATURAL VOICE (Kokoro)")
    try:
        import fetch
        for path, min_bytes in ((config.KOKORO_MODEL, config.KOKORO_MODEL_MIN_BYTES),
                                (config.KOKORO_VOICES, config.KOKORO_VOICES_MIN_BYTES)):
            if not (os.path.isfile(path) and os.path.getsize(path) >= min_bytes):
                got = fetch.download(config.KOKORO_BASE_URL + os.path.basename(path), path, min_bytes,
                                     os.path.basename(path), print)
                if got is None:
                    raise IOError(f"could not download {os.path.basename(path)}. Check your internet.")
        import sounddevice as sd
        from kokoro_onnx import Kokoro
        k = Kokoro(config.KOKORO_MODEL, config.KOKORO_VOICES)
        lang = "en-gb" if config.KOKORO_VOICE.startswith("b") else "en-us"
        t0 = time.time()
        samples, rate = k.create("Alright. Engineer online. Box this lap, we're going to fuel and fresh tyres.",
                                 voice=config.KOKORO_VOICE, speed=config.VOICE_SPEED, lang=lang)
        print(f"PASS  generated in {time.time() - t0:.1f}s. Playing now, do you hear a human-sounding voice?")
        print("      (If you hear nothing, check that your laptop is not muted.)")
        sd.play(samples, rate)
        sd.wait()
    except Exception as e:
        print(f"FAIL  {type(e).__name__}: {e}")
        print("      Common fixes:  pip install kokoro-onnx   |   check your internet   |   copy this message to Claude")


def test_voice_in():
    header("2/3  VOICE INPUT (Whisper)")
    try:
        import numpy as np
        import sounddevice as sd
        from faster_whisper import WhisperModel
        print(f"Loading '{config.WHISPER_MODEL}' (first time downloads it, can take a few minutes)...")
        t0 = time.time()
        model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
        print(f"PASS  model loaded in {time.time() - t0:.1f}s")
        input("Press Enter, then say:  'what car am I driving' (you have 4 seconds) ")
        audio = sd.rec(int(4 * 16000), samplerate=16000, channels=1, dtype="float32", device=config.MIC_DEVICE)
        sd.wait()
        t0 = time.time()
        segs, _ = model.transcribe(np.asarray(audio).flatten(), language="en", initial_prompt=config.WHISPER_HINT)
        print(f"PASS  heard: {' '.join(s.text for s in segs).strip()!r}  ({time.time() - t0:.1f}s)")
    except Exception as e:
        print(f"FAIL  {type(e).__name__}: {e}")
        print("      Common fixes:  pip install faster-whisper   |   check your internet   |   copy this message to Claude")


def test_ai():
    header("3/3  BUILT-IN AI")
    import subprocess
    import sys
    try:
        import llama_cpp  # noqa: F401
    except Exception:
        print("The built-in AI engine (llama-cpp-python) is not installed yet.")
        ans = input("Install it now? It is a pre-built package, no compiler needed. [Y/n] ").strip().lower()
        if ans in ("", "y", "yes"):
            code = subprocess.call([sys.executable, "-m", "pip", "install", "-r",
                                    os.path.join(config.BASE_DIR, "requirements_ai.txt")])
            if code != 0:
                print("FAIL  the install did not work. The engineer still runs without it (rules + online fallback).")
                print("      Send the red text above to Claude.")
                return
        else:
            print("SKIPPED  the engineer still runs without it (rules only, plus the online fallback if you set one).")
            return
    try:
        from local_llm import LocalLLM, find_model
        if find_model():
            print(f"Model file found: {find_model()}")
        else:
            print("Model file not found yet. It downloads once (about 1 GB, no account). Be patient...")
        t0 = time.time()
        llm = LocalLLM()
        llm._load()
        if llm.state != "ready":
            print(f"FAIL  {llm.error}")
            return
        print(f"PASS  loaded in {time.time() - t0:.1f}s ({llm.backend})")
        t0 = time.time()
        text = llm.chat("You are a friendly race engineer.", "Say hello to your driver in six words.", timeout=60)
        print(f"PASS  {text!r}  ({time.time() - t0:.1f}s)")
    except Exception as e:
        print(f"FAIL  {type(e).__name__}: {e}")
    if config.LLM_ONLINE_URL:
        try:
            from local_llm import OnlineClient
            text = OnlineClient().chat([{"role": "user", "content": "Say hello in six words."}], 20)
            print("PASS  online fallback:" if text else "FAIL  online fallback did not answer", repr(text) if text else "")
        except Exception as e:
            print(f"FAIL  online fallback: {type(e).__name__}: {e}")


if __name__ == "__main__":
    test_voice_out()
    test_voice_in()
    test_ai()
    print("\nDone. Copy anything marked FAIL to Claude.")
