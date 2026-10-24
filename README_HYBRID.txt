HYBRID CHAT UPDATE  -  Ollama + speech repair + pit-wall persona
================================================================

HOW TO USE
  1. Close the engineer.
  2. Copy ALL the files from this folder into your project folder AI_RACE_ENGINNER_3.12\ and say "Replace".
     Do NOT delete anything of yours: keep cesium.ico, .venv\, models\, data\, ac_app\ and cloud_worker\ exactly as they are.
  3. (Optional but recommended) install Ollama, once:
        - download it from https://ollama.com and install it
        - open a terminal and run:    ollama pull llama3.2:3b
     No Ollama? Skip this. The engineer works exactly as before (built-in model + rules).
  4. Run  python test_hybrid.py  in PyCharm's terminal. Every line must say PASS.
  5. Run the engineer (PyCharm) or rebuild with build_nuitka.bat. The new files are imported by brain.py and
     main_pitwall.py, so Nuitka finds them by itself. The self-test (CesiumAIRaceEngineer.exe --selftest) now checks them too.

FOLDER LAYOUT
  AI_RACE_ENGINNER_3.12\
  |-- speech_repair.py       NEW      fixes what Whisper mishears, before the engineer logic
  |-- intent_router.py       NEW      decides: instant rules, or conversation?
  |-- ollama_client.py       NEW      talks to Ollama at http://localhost:11434 (optional)
  |-- persona.py             NEW      the GT3 / Formula pit-wall persona prompt + reply clean-up
  |-- test_hybrid.py         NEW      offline test (uses a fake Ollama, nothing is downloaded)
  |-- brain.py               CHANGED  router + Ollama in the answer path
  |-- main_pitwall.py        CHANGED  speech repair in Core._heard, AI status, Ollama unload on quit
  |-- config.py              CHANGED  new block "HYBRID CHAT"; the "whether" entry was removed from WORD_FIXES
  |-- boot_guard.py          CHANGED  --selftest also checks the 4 new modules
  |-- README_HYBRID.txt      this file
  |-- (all other .py / .bat / .txt files: unchanged)
  |-- cesium.ico, .venv\, models\, data\, ac_app\, cloud_worker\     <- yours, keep them

IF YOU KEEP YOUR OWN config.py
  Copy only the block that starts with "HYBRID CHAT: Ollama + SPEECH REPAIR" (it sits just above "TRUSTED WEB HELP").
  Also delete  "whether": "weather",  from your WORD_FIXES. That entry rewrote "whether or not" too; speech_repair.py now
  fixes "whether" only where it means the weather ("what's the whether"). Without the block everything still works:
  every new setting has a built-in default.

HOW A SENTENCE TRAVELS NOW
  microphone -> Whisper -> listener.py (WORD_FIXES)
     -> speech_repair.py        "okay alix what's the whether"  ->  "what's the weather"
     -> Brain.handle
          1. control commands (quiet, repeat, set cliff, thanks ...)       instant rules
          2. IntentRouter.prefers_chat?  banter must not trip a one-word trigger
          3. telemetry questions (fuel, tyres, temps, pit ...)             instant rules, NO AI involved
          4. memory (things you taught him with "remember that ...")
          5. IntentRouter.classify:  "chat"      -> Ollama with the persona, no web search allowed
                                     "knowledge" -> Ollama with the persona, may answer "SEARCH: ..." -> trusted web
          6. Ollama not running / too slow -> the built-in model -> the old rules + web
  The AI only ever sees the live data as text. It never controls pit calls, fuel calls or alerts.

WHAT TO LOOK FOR IN THE LOG WINDOW (the "Log" button)
  [OLLAMA] Ready in 3.4s (llama3.2:3b at http://localhost:11434) ...      <- Ollama found and warmed up
  [OLLAMA] Not available: Ollama is not running ...                         <- fine, the built-in AI is used
  [OLLAMA] Not available: model 'llama3.2:3b' is not installed ...          <- run:  ollama pull llama3.2:3b
  [STT] heard 'okay alix what's the whether' -> "what's the weather"  (...)  <- speech repair changed something
  [ROUTER] chat -> 'do you think senna was faster than prost'               <- which route a sentence took
  [OLLAMA] answered in 1.2s
  Start Ollama any time: he looks again every OLLAMA_RETRY_S seconds (60), no restart needed.

SETTINGS (config.py, block "HYBRID CHAT")
  USE_OLLAMA = False           never use Ollama
  OLLAMA_MODEL                 "llama3.2:3b" (default). "qwen2.5:3b" is a good alternative. Bigger = smarter but slower.
  OLLAMA_NUM_GPU = 0           run the model on the CPU only. Try this if Assetto Corsa loses frames while he answers.
                               (None lets Ollama use your graphics card: faster answers, but it takes video memory.)
  OLLAMA_KEEP_ALIVE = "10m"    the model leaves memory this long after the last question; it is also unloaded on quit.
  OLLAMA_TIMEOUT = 8           seconds before the built-in model takes over
  DRIVER_NAME = "Marco"        what the engineer calls you
  TEAM_LORE = [...]            short TRUE facts and in-jokes, e.g.
                               "The team nickname for the Audi R8 is 'the Silver Bullet'."
                               He plays along naturally and never explains them.
  CHAT_KEYWORDS = ["marco"]    extra lower-case words that mean "banter" (friends, team mates). They are also
                               phonetically corrected by speech_repair.py.
  SPEECH_REPAIR = False        switch the repair layer off

WHERE TO ADD YOUR OWN FIXES
  speech_repair.py  _SIMPLE   words that are always wrong:        "kliff": "cliff"
  speech_repair.py  _CTX      words that depend on the neighbours: ("books", "box", {"should", "i"}, {"now", ""})
  speech_repair.py  VOCAB / NAMES   words and people to match fuzzily (same first letter + very close spelling only)
  intent_router.py  CHAT_PHRASES / CHAT_WORDS   what counts as banter
  persona.py        _PROMPT     the engineer's character
Try a sentence without starting anything:     python speech_repair.py "okay alex what's the whether"

GOOD TO KNOW
  * Ollama is optional, so the "plug and play for every player" idea still holds: players without Ollama get the
    built-in model. The race calls never depend on any AI.
  * Web search ("SEARCH:") is only allowed for setup / rules questions, never for banter.
  * Ollama is local: the live race data is sent to localhost only, nothing leaves the PC. If you ever point
    OLLAMA_URL at another computer, the data goes there instead.
  * Known limit: the old "quiet" command still fires on the word "quiet" in any sentence ("it's been quiet today").
