AUDIO SETTINGS FIX  -  what changed and where to put it
=======================================================

HOW TO USE
  1. Close the engineer.
  2. Copy ALL the files from this folder into your project folder AI_RACE_ENGINNER_3.12\ and say "Replace".
     Do NOT delete anything of yours: keep cesium.ico, .venv\, models\ and data\ exactly as they are.
  3. Run it (PyCharm) or rebuild with build_nuitka.bat.

FOLDER LAYOUT (every folder the program and the build script expect)
  AI_RACE_ENGINNER_3.12\
  |-- main_pitwall.py        CHANGED
  |-- gui.py                 CHANGED
  |-- user_settings.py       CHANGED
  |-- voice_radio.py         CHANGED
  |-- listener.py            CHANGED
  |-- (all other .py / .bat / .txt files: unchanged)
  |-- cesium.ico             <- yours, not in this zip, keep it
  |-- .venv\                 <- yours, keep it
  |-- ac_app\PitwallSpotter\PitwallSpotter.py + manifest.ini   (current version, needed by the build scripts)
  |-- cloud_worker\worker.js
  |-- models\   data\        (filled automatically)

WHAT TO LOOK FOR IN THE LOG WINDOW (the "Log" button) AFTER YOU PRESS SAVE
  [SETTINGS] saved ...user_settings.json (voice=..., mic=..., speaker=...)   <- proves what was written
  [SETTINGS] applied (car class: ..., voice: ..., mic: ..., speaker: ...)    <- proves what is now in use
  [AUDIO] speaker is now: ...                                                <- after picking a device
If you ever hear the Windows voice, the log now says why ("[VOICE ERROR] natural voice ... failed").
