# =====================================================
#  config.py  -  EVERY setting you may want to change
#  Edit a value, save, restart main_pitwall.py. Done.
# =====================================================

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # the folder this project lives in (read-only resources)

# A packaged .exe unpacks itself into a temporary folder that is wiped when it closes (PyInstaller / Nuitka one-file),
# and an installed program folder may not be writable either. So everything the program WRITES (database, voice cache,
# the AI model, logs) goes to USER_ROOT: the user profile in a packaged build, the project folder when run from source.
FROZEN = bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()
USER_ROOT = (os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "CesiumAIRaceEngineer")
             if FROZEN else BASE_DIR)

# ---------- THE APP ----------
APP_VERSION = "v0.1.0"         # updater.py compares this with the newest GitHub release; gui.py shows it. Bump it with every build.
USE_UPDATER = True                  # False = never check for updates (CESIUM_NO_UPDATE=1 or --no-update also switch it off)
LANGUAGE = "en"                     # set by the language selector in gui.py. Only the window is translated so far; the engineer speaks English.
# Optional updater settings (see updater.py):
# UPDATE_PRESERVE = ("data/", "models/", "logs/", "voice_cache/", "*.db", "*.sqlite", "*.log", "config.py", "ptt_binding.json")
# UPDATE_ASSET_PATTERN = "*win*.zip"
# UPDATE_INCLUDE_PRERELEASES = True
# Optional push-to-talk setting (see ptt_manager.py):
# PTT_SETTINGS_FILE = os.path.join(USER_ROOT, "data", "ptt_binding.json")

# ---------- YOUR GAME ----------
AC_PATH = r"C:\Program Files (x86)\Steam\steamapps\common\assettocorsa"   # your comment: used to show real car/track names
ENGINEER_NAME = "Alex"              # your comment: his name (he uses it in his personality)

# ---------- FUEL ----------
FUEL_CALL_LAPS = 1.5      # asks you to pit when fuel left is BELOW this many laps
FUEL_LEARNING = True      # True = he adjusts that number himself from what you do
FUEL_MARGIN_MIN = 0.7
FUEL_MARGIN_MAX = 3.0

# Fuel WARNING LIGHT fallback. Used when there is no trustworthy fuel-per-lap number (a car/track he has never seen,
# only one lap measured so far, or laps that used very different amounts of fuel). The game does not publish the icon
# colour, so yellow / orange / red are worked out from how full the tank is. Orange = "box this lap", red = "box, box, box".
FUEL_WARN_FALLBACK = True           # False = no fuel calls at all until he has fuel numbers (the old behaviour)
FUEL_MIN_LAPS = 2                   # clean laps needed before the fuel-per-lap number is trusted (no saved history)
FUEL_BURN_MAX_SPREAD = 0.5          # laps whose fuel use differs by more than 50% of the average are not trusted
FUEL_WARN_YELLOW = 0.15             # fraction of a FULL TANK left at which the light is yellow (heads-up only)
FUEL_WARN_ORANGE = 0.08             # ...orange: he calls "box this lap"
FUEL_WARN_RED = 0.04                # ...red: he calls "box, box, box"
FUEL_WARN_BY_CAR = {}               # per-car override, e.g. {"ks_audi_r8_lms_2016": (0.20, 0.10, 0.05)}  (yellow, orange, red)
FUEL_WARN_YELLOW_CALL = True        # False = stay quiet at yellow
FUEL_WARN_ORANGE_COOLDOWN = 90      # seconds between "orange" reminders
FUEL_WARN_RED_COOLDOWN = 20         # seconds between "red" reminders
FUEL_WARN_REARM = 0.02              # the tank must be this much fuller than a threshold before that call can fire again
FUEL_WARN_DEBUG = True              # prints "[FUEL] light level -> orange: 4.2 L left" so you can compare with the in-game icon

# ---------- TYRES (each tyre is watched separately AND as a group) ----------
TYRE_CLIFF_DEFAULT = 0.30           # cliff for the AVERAGE of the 4 tyres (0.30 = 30% life left)
TYRE_CLIFF_BY_TRACK = {}            # per-track override, e.g. {"ks_nordschleife": 0.35}
TYRE_SINGLE_CLIFF = 0.15            # cliff for ONE tyre. Lower than the group cliff on purpose:
                                    # one tyre at 25% can still run, one at 12% is done
TYRE_IMBALANCE = 0.20               # heads-up if the worst tyre is this far below the average
TYRE_CALL_LAPS = 1.5                # call the pit when a cliff is this many laps away (Nordschleife: try 2.0)
# Tyre temperature heads-up. This is ONLY a "you're cooking it" reminder. It is not a safety call and never says "about to blow":
# safety calls (pressure too high, puncture, blown tyre) use PSI only, see below.
TYRE_TEMP_WARNINGS = True           # False = no temperature talk at all
TYRE_WARM_C = 90                    # core temperature (deg C) from here: "overheating" (only while driving; he keeps saying it every TYRE_WARM_COOLDOWN seconds)
TYRE_HOT_C = 125                    # core temperature from here: "way too hot, you must slow down" (even when stopped)
TYRE_WARM_COOLDOWN = 120            # seconds between "overheating" reminders for the same tyres
TYRE_HOT_COOLDOWN = 45
TYRE_HEAT_REARM_C = 8               # a tyre must cool this far below a threshold before that warning can fire again
TYRE_COLD_C = 65                    # average core temperature below this while driving = "tyres are cold"
TYRE_WINDOW_C = 80                  # ...and when they reach this he says "up to temperature"
TYRE_SURFACE_TIPS = True            # setup hints from the inner / middle / outer tread temperatures (like the Tyres app)
TYRE_SURFACE_SPREAD_C = 15          # how far apart those temperatures must stay (for ~20 s) before he mentions it
TYRE_SURFACE_TAU_S = 20
TYRE_LOG = os.path.join(USER_ROOT, "data", "tyre_events.log")   # raw tyre numbers whenever something looks broken

# Tyre safety uses live PSI ONLY (core temperature is deliberately not used for any warning).
TYRE_PSI_WARN = 35                  # Stage 1: from this psi, "you must slow down" (until TYRE_PSI_BOX)
TYRE_PSI_BOX = 40                   # Stage 2: from this psi, CRITICAL: "box now, it's about to blow up"
TYRE_PSI_TARGET = 26.5              # "Tire Pressure Target": the HOT pressure you aim for (Settings tab, per car class).
                                    # The warn / box limits above move with it: warn = target + offset, box = target + offset.
TYRE_PSI_WARN_OFFSET = TYRE_PSI_WARN - TYRE_PSI_TARGET     # 8.5, worked out from YOUR warn / box numbers above,
TYRE_PSI_BOX_OFFSET = TYRE_PSI_BOX - TYRE_PSI_TARGET       # 13.5. At the default target nothing changes.
TYRE_PSI_BY_CAR = {}                # per-car override, e.g. {"ks_audi_r8_lms_2016": (40, 45)}  (warn, box)
TYRE_PSI_HYSTERESIS = 2.0           # pressure must fall this far below TYRE_PSI_WARN before a warning can re-arm
TYRE_PSI_WARN_COOLDOWN = 18         # seconds between Stage 1 prompts for the same wheel (15-20 recommended)
TYRE_PSI_BOX_COOLDOWN = 15          # seconds between Stage 2 repeats for the same wheel
TYRE_BLOWN_PSI = 1.0                # Stage 3: at or below this psi (after the tyre was healthy) = the tyre HAS blown, at ANY speed
TYRE_BLOWN_CONFIRM_S = 0.3          # ...for this long (ignores one-frame glitches)
TYRE_BLOWN_REPEAT = 20              # seconds between "still blown" reminders until you reach the pit lane (0 = off)
TYRE_BLOWN_PENALTY = 0.5            # in his tyre STATUS a blown tyre counts this much less (0.5 = minus 50 percentage points; 1.0 = counts as 0%)
PRESSURE_DROP_FRACTION = 0.35       # extra: pressure falling this fraction within PRESSURE_DROP_SECONDS = "dropping fast" (0 = off)
PRESSURE_DROP_SECONDS = 2.0
TYRE_LEARN_CLIFF = True
TYRE_DEBUG = True                   # prints tyre numbers after every lap so we can calibrate
CLIFF_SLOWDOWN = 0.03               # 3% slower than your early laps = tyres fading
ROLLING_LAPS = 3

# ---------- SPOTTER (cars alongside) ----------
SPOTTER = True                      # "car on your left / right / three wide / clear"
SPOTTER_MIN_KMH = 40                # silent below this speed (grid, pit lane, crawling)
SPOTTER_LONG_M = 5.5                # a car counts as alongside when its centre is within this many metres ahead/behind
SPOTTER_LAT_MAX_M = 4.5             # ...and no more than this many metres to the side
SPOTTER_ENTER_S = 0.12              # a side must be occupied this long before he calls it
SPOTTER_EXIT_S = 0.5                # ...and free this long before he says "clear"
SPOTTER_SELF_MATCH_M = 4.0          # data sanity check: my own wheels must sit on one of the car positions
# Vanilla Assetto Corsa does not put the other cars into its shared memory. A tiny in-game app (apps/python/PitwallSpotter,
# installed by install_spotter.py) writes their positions to this file, and the spotter reads it. Same path on both sides.
SPOTTER_FILE = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~"), "pitwall_cars.txt")
SPOTTER_FILE_MAX_AGE_S = 1.0        # a position file older than this is ignored (game paused or app switched off)

# ---------- LAPS, FLAGS, TRACK ----------
LAP_TIME_CALLS = "pb"               # "all", "pb", "off"
TYRES_OUT_FOR_INVALID = 4           # tyres off track before "lap won't count" (3 = stricter)
PIT_LIMITER_REMINDER = True
GRIP_CALL_STEP = 0.03               # he tells you when track grip moves by this much (0.03 = 3%)

FLAG_DEBUG = True                   # prints every flag / penalty change the game reports, so we can verify them

# ---------- DAMAGE & ENGINE ----------
DAMAGE_MIN_STEP = 1.0               # smallest damage increase that counts as a hit (raise if he's too sensitive)
DAMAGE_HEAVY = 25.0                 # total damage where "light" becomes "heavy"
DAMAGE_DEBUG = True                 # prints raw damage numbers in the terminal so you can calibrate the two above
ENGINE_HIGH_RPM_FRACTION = 0.93     # RPM above this % of max counts as "pushing the engine"
ENGINE_STRESS_LIMIT = 0.40          # 0-1. Lower = he complains sooner
ENGINE_DEAD_SECONDS = 1.5           # engine dead this long (in gear, moving) = "engine failure"

# ---------- VOICE OUTPUT ----------
# He speaks at once with the Windows voice. The natural, human-sounding voice (Kokoro) downloads ITSELF the first
# time (one time, no account) and takes over by itself as soon as it is ready. Nothing to set up except:
#     pip install -r requirements.txt
TTS_ENGINE = "kokoro"               # "kokoro" = natural voice (recommended), "sapi" = Windows voice only
KOKORO_QUALITY = "high"             # "high" = best sound (310 MB download). "fast" = smaller and quicker (88 MB)
ENGINEER_ACCENT = "italian"         # "british" or "italian". Run  python voice_demo.py  to hear every voice and pick one.
KOKORO_VOICE = {"british": "bm_george", "italian": "im_nicola"}.get(ENGINEER_ACCENT, "bm_george")
                                    # to pick a specific voice instead, type its name here, e.g. "bm_lewis", "bf_emma" (British),
                                    # "im_nicola" (Italian man), "if_sara" (Italian woman), "am_michael" (American man)
VOICE_PRERENDER = True              # speaks his common calls into a saved cache in the background, so they play instantly
VOICE_CACHE_DIR = os.path.join(USER_ROOT, "data", "voice_cache")
KOKORO_AUTO_DOWNLOAD = True         # False = never download (you copy the two files into the models folder yourself)
KOKORO_BASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_MODEL = os.path.join(USER_ROOT, "models", "kokoro-v1.0.onnx" if KOKORO_QUALITY == "high" else "kokoro-v1.0.int8.onnx")
KOKORO_VOICES = os.path.join(USER_ROOT, "models", "voices-v1.0.bin")
KOKORO_MODEL_MIN_BYTES = 60_000_000
KOKORO_VOICES_MIN_BYTES = 5_000_000
# Speaking order. Nobody is ever cut off mid-sentence: an alert always finishes before the next call starts.
ANSWER_PREEMPT_MAX_PRIO = 1         # an alert of this priority or more important (0 urgent, 1 normal) may pause an ANSWER to you
ANSWER_MAX_PREEMPTS = 2             # ...at most this many times per answer, so it can't be pushed back forever
VOICE_RESUME_ANSWER = "restart"     # "restart" = after the alert he says the whole answer again, "continue" = only what is left
ANSWER_MAX_AGE_S = 40               # an answer that had to wait longer than this is dropped (it would be out of date)
SPOTTER_MAX_AGE_S = 2.5             # a "car on your left" call that waited longer than this is dropped (the car has moved)
VOICE_SPEED = 1.05                  # 1.0 = relaxed, 1.2 = quick radio talk
VOICE_NAME_CONTAINS = ""            # Windows voice only: part of a voice name, e.g. "David" or "Zira"
VOICE_RATE = 2
VOICE_VOLUME = 100

# ---------- KEYS ----------
# '#' is the symbol that starts these comment lines. The buttons YOU press are named right here:
PTT_KEY = "f"                       # PUSH TO TALK (first run only): hold the F key, speak, let go. Pressing it also silences him.
                                    # Once you pick a key / mouse button / wheel button with "Change..." in the control window,
                                    # that choice is saved (ptt_binding.json) and this line is no longer used.
QUIT_KEY = "ctrl+shift+q"           # QUIT: hold Ctrl and Shift, press Q. Shuts the engineer down cleanly.
                                    # (A single key works too, e.g. "f12". Not "#": on most keyboards that is Shift+3,
                                    #  and the keyboard library would then quit every time you press 3.)
                                    # NOTE: the key press still reaches the game. If F does something in your Assetto Corsa
                                    # controls, unbind it there, or set PTT_KEY to the key a wheel button is mapped to.
PTT_BEEP = True                     # short beep when the mic opens/closes so you KNOW it registered
PTT_REJECT_BEEP = False              # False (default) = pressing PTT while the radio is OFF is completely silent.
                                    # True = play a short falling two-tone "refused" cue instead. No mic, no recording either way.
WHISPER_MODEL = "base.en"           # "base.en" = fast (default), "small.en" = more accurate but about 3x slower to answer
WHISPER_HINT = ("Race engineer radio in Assetto Corsa. car, tyres, fuel, pit, box, cliff, grip, weather, "
                "track, lap time, damage, engine, RPM, position, penalty.")
WORD_FIXES = {                      # your comment: add words he keeps mishearing: "heard": "meant"
    "cart": "car", "card": "car", "clef": "cliff", "cliffs": "cliff",     # "whether" removed: speech_repair.py fixes it only in context
}
MIC_DEVICE = None
SPEAKER_DEVICE = None               # output device index. The Settings tab fills this in (None = Windows default)

# ---------- THE AI BRAIN (nothing to install by hand, no account for the driver) ----------
# Order of preference:  1) built-in model  ->  2) online fallback (optional)  ->  3) rules only.
# Fuel, tyres, flags, pressures and every voice command are plain rules and never depend on any AI.
USE_LLM = True
MODELS_DIR = os.path.join(USER_ROOT, "models")     # a release build can simply ship the .gguf file in this folder
LLM_FILE = "qwen2.5-1.5b-instruct-q4_k_m.gguf"     # small, fast, Apache-2.0 (about 1.1 GB)
LLM_URL = ("https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/"
           "qwen2.5-1.5b-instruct-q4_k_m.gguf")    # used once, only if the file is not already in MODELS_DIR
LLM_AUTO_DOWNLOAD = True                           # False = never download (release build with the model bundled)
LLM_MIN_BYTES = 500_000_000                        # smaller than this = broken download
LLM_THREADS = 4                                    # CPU threads for the model. Low on purpose so the game keeps its frame rate
LLM_CONTEXT = 2048
LLM_MAX_PROMPT_CHARS = 6000                        # older chat history is dropped beyond this
LLM_TIMEOUT = 12                                   # seconds before he gives up on the model (it is then restarted)
LLM_LOAD_TIMEOUT = 180                             # seconds allowed for the model to load
LLM_MAX_RESTARTS = 3                               # after this many crashes/hangs the built-in model is switched off

# Optional online fallback: a tiny proxy worker YOU deploy once (see cloud_worker/), so drivers need no account.
# Leave the URL empty to stay 100% offline. Used only while the built-in model isn't ready or has failed.
LLM_ONLINE_FALLBACK = True
LLM_ONLINE_URL = ""                                # e.g. "https://pitwall-ai.YOURNAME.workers.dev"
LLM_ONLINE_MODEL = "default"
LLM_ONLINE_KEY = ""                                # normally empty (keyless). Sent as a Bearer token if you set one
LLM_ONLINE_TIMEOUT = 8

# ---------- HYBRID CHAT: Ollama + SPEECH REPAIR + PIT-WALL PERSONA ----------
# Fuel, tyres, flags, pressures and every voice command stay instant rules. Only conversation (banter, real people,
# racing lore, setups) goes to a local LLM. Ollama is OPTIONAL: no Ollama = the built-in model answers, as before.
#   One-time setup:  install Ollama from ollama.com, then in a terminal:   ollama pull llama3.2:3b
USE_OLLAMA = True
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2:3b"        # small and quick. Others to try: "qwen2.5:3b", "llama3.1:8b" (slower, needs more memory)
OLLAMA_TIMEOUT = 8                  # seconds he waits for an answer before the built-in model takes over
OLLAMA_LOAD_TIMEOUT = 120           # seconds allowed for the model to load at start-up
OLLAMA_KEEP_ALIVE = "10m"           # how long the model stays in memory after the last question
OLLAMA_NUM_CTX = 2048
OLLAMA_MAX_TOKENS = 80              # enough for two radio sentences
OLLAMA_NUM_GPU = None               # None = Ollama decides. 0 = CPU only: keeps the graphics card free for Assetto Corsa
OLLAMA_MAX_PROMPT_CHARS = 6000
OLLAMA_RETRY_S = 60                 # if Ollama was not running, look again this often (start it any time, no restart)

SPEECH_REPAIR = True                # fixes Whisper mishearings before the engineer logic sees them (speech_repair.py)
DRIVER_NAME = ""                    # optional: what the engineer calls you, e.g. "Marco"
TEAM_LORE = [                       # short TRUE facts and in-jokes. He plays along naturally and never explains them.
    # "The team nickname for the Audi R8 is 'the Silver Bullet'.",
    # "Marco never lets anyone forget the Brands Hatch spin of last season.",
]
CHAT_KEYWORDS = []                  # extra lower-case words that mean "banter", e.g. names of friends / team mates: ["marco", "luca"]

# ---------- TRUSTED WEB HELP ----------
USE_WEB = True
WEB_TIMEOUT = 6
TRUSTED_SITES = [
    "assettocorsa.net", "racedepartment.com", "overtake.gg", "fia.com", "wikipedia.org",
]
LEARN_INTERVAL = 90                 # seconds between background study sessions

# ---------- CLOUD COACH (optional, background only) ----------
USE_CLOUD = True                    # needs the ANTHROPIC_API_KEY environment variable. No key = silently off.
BAN_LIST_URL = "https://pitwall-ai.cesium-ai.workers.dev"                   # https address of your ban proxy worker (cloud_worker/ban_proxy). Empty = no ban check.

# ---------- GAME CONNECTION (runs silently on its own thread) ----------
RECONNECT_STALE_SECONDS = 3         # game says LIVE but its data is frozen this long = disconnected
REATTACH_SECONDS = 1.5              # while disconnected, quietly re-open the game's memory this often (1-2 s)
DISCONNECT_MESSAGE = "Telemetry disconnected. Waiting for reconnection."   # spoken ONCE per disconnect
DISCONNECT_VOICE_COOLDOWN = 10      # never repeat that message more often than this many seconds

# ---------- MEMORY ----------
DB_PATH = os.path.join(USER_ROOT, "data", "engineer.db")

# ---------- YOUR SAVED SETTINGS (from the Settings tab). KEEP THIS AT THE VERY END OF THE FILE ----------
# Reads %APPDATA%\CesiumAIRaceEngineer\user_settings.json and overrides the values above.
# No file yet (Save never pressed) = every value in this file stays exactly as written.
try:
    import user_settings
    user_settings.apply_all(sys.modules[__name__])
except Exception as _e:
    print(f"[SETTINGS] could not load your saved settings ({type(_e).__name__}: {_e}). Using config.py values.")
