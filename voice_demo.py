"""
voice_demo.py - hear every engineer voice, then pick one.

Run:   python voice_demo.py
It plays the same radio call in each voice. When you hear the one you like, open config.py and either set
    ENGINEER_ACCENT = "british"   or   "italian"
or, to choose one exact voice, replace the KOKORO_VOICE line with its name, for example  KOKORO_VOICE = "bm_lewis"
(Run it again after changing the voice. The first call in a new voice takes a moment while it is recorded.)
"""
import os
import time

import config

SENTENCE = "Box, box, box. Rear left pressure critical at forty seven PSI. Car on your left, three wide."

VOICES = [
    ("bm_george", "British man (the default)"),
    ("bm_lewis", "British man, a bit deeper"),
    ("bf_emma", "British woman"),
    ("bf_isabella", "British woman, softer"),
    ("im_nicola", "ITALIAN man"),
    ("if_sara", "ITALIAN woman"),
    ("am_michael", "American man"),
]


def main():
    import fetch
    for path, min_bytes in ((config.KOKORO_MODEL, config.KOKORO_MODEL_MIN_BYTES),
                            (config.KOKORO_VOICES, config.KOKORO_VOICES_MIN_BYTES)):
        if not (os.path.isfile(path) and os.path.getsize(path) >= min_bytes):
            print(f"First time: downloading {os.path.basename(path)} (one time, no account)...")
            if fetch.download(config.KOKORO_BASE_URL + os.path.basename(path), path, min_bytes,
                              os.path.basename(path), print) is None:
                print("Could not download. Check your internet and try again.")
                return
    import sounddevice as sd
    from kokoro_onnx import Kokoro
    k = Kokoro(config.KOKORO_MODEL, config.KOKORO_VOICES)
    try:
        available = set(k.get_voices())
    except Exception:
        available = None
    print("\nIf you hear nothing, check that your laptop is not muted.\n")
    for name, label in VOICES:
        if available is not None and name not in available:
            print(f"  (skipping {name}: not in this voice pack)")
            continue
        print(f"  {name:12} {label}")
        lang = "en-gb" if name.startswith("b") else "en-us"       # Italian voices speak English with an Italian voice
        samples, rate = k.create(SENTENCE, voice=name, speed=config.VOICE_SPEED, lang=lang)
        sd.play(samples, rate)
        sd.wait()
        time.sleep(0.6)
    print("\nPick one, then edit config.py:  ENGINEER_ACCENT = \"british\" / \"italian\"   or   KOKORO_VOICE = \"<name>\"")


if __name__ == "__main__":
    main()
