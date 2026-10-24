"""
install_spotter.py - installs the little in-game app that lets the engineer see the other cars (for the spotter).

RUN ONCE, from the project folder:      python install_spotter.py

FOLDER LAYOUT (this script creates anything that is missing, so you do not have to):

    AI_RACE_ENGINNER_3.12/                 <- your project folder
    |-- install_spotter.py                 <- this file
    |-- config.py                          <- has AC_PATH (your game folder) and SPOTTER_FILE (the shared file)
    `-- ac_app/
        `-- PitwallSpotter/
            |-- PitwallSpotter.py          <- the app (the game requires the .py to be named like its folder)
            `-- manifest.ini               <- optional: only used by Content Manager for its app list

    <Assetto Corsa>/apps/python/PitwallSpotter/     <- where it gets installed
        |-- PitwallSpotter.py
        |-- manifest.ini
        `-- spotter_path.txt               <- written here: tells the app which file to write (config.SPOTTER_FILE)

Then, in the game: move the mouse to the RIGHT edge of the screen, open the app bar and click  PitwallSpotter.
It should say "Pitwall spotter: sending N cars".
"""
import os
import shutil
import sys

import config

APP_SOURCE = r'''# PitwallSpotter - a tiny Assetto Corsa app. It does ONE thing: about 25 times a second it writes the world position of
# every car to a small text file, so the Pitwall Race Engineer can say "car on your left / right / three wide / clear".
#
# IMPORTANT: this runs INSIDE the game, which embeds an old Python (3.3). So: no f-strings, no fancy syntax.
#
# WHERE IT WRITES: the path is read from spotter_path.txt, a file that install_spotter.py puts next to this one and fills
# with config.SPOTTER_FILE from the engineer's config.py. That is how both programs always agree on the same file.
# If spotter_path.txt is missing it falls back to  %TEMP%\pitwall_cars.txt  (which is also the default in config.py).
import os
import time

import ac
import acsys

APP_NAME = "PitwallSpotter"
INTERVAL = 0.04            # seconds between writes = 25 per second
_state = {"owed": 0.0, "label": None, "sent": 0, "errors": 0}


def _find_path():
    here = ""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        pass
    if here:
        try:
            with open(os.path.join(here, "spotter_path.txt"), "r") as f:
                wanted = f.read().strip()
            if wanted:
                return wanted
        except (OSError, IOError):
            pass
    folder = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
    return os.path.join(folder, "pitwall_cars.txt")


PATH = _find_path()


def acMain(ac_version):
    window = ac.newApp(APP_NAME)
    ac.setSize(window, 230, 70)
    _state["label"] = ac.addLabel(window, "Pitwall spotter: starting...")
    ac.setPosition(_state["label"], 10, 35)
    ac.log("PitwallSpotter writing to " + PATH)
    return APP_NAME


def _write():
    count = ac.getCarsCount()
    # first line: time in milliseconds, index of the car the camera is on. Then one line per car: index,x,y,z
    lines = ["%d,%d" % (int(time.time() * 1000), ac.getFocusedCar())]
    for i in range(count):
        x, y, z = ac.getCarState(i, acsys.CS.WorldPosition)
        lines.append("%d,%.2f,%.2f,%.2f" % (i, x, y, z))
    text = "\n".join(lines)
    tmp = PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, PATH)              # swap in whole, so the engineer never reads half a file
    except (OSError, IOError):
        with open(PATH, "w") as f:         # if the engineer was reading it at that instant, write directly
            f.write(text)
    return count


def acUpdate(delta_t):
    # a timer that keeps the long-run rate at 25 per second whatever the frame rate is
    _state["owed"] += delta_t
    if _state["owed"] < INTERVAL:
        return
    _state["owed"] = 0.0 if _state["owed"] > 3 * INTERVAL else _state["owed"] - INTERVAL
    try:
        count = _write()
        _state["sent"] += 1
        if _state["sent"] % 25 == 1:
            ac.setText(_state["label"], "Pitwall spotter: sending %d cars" % count)
    except Exception as e:                 # never let a problem here disturb the game
        _state["errors"] += 1
        if _state["errors"] <= 3:
            ac.log("PitwallSpotter error: " + str(e))
        ac.setText(_state["label"], "Pitwall spotter: error (see log)")
'''

MANIFEST = r'''[ABOUT]
NAME = Pitwall Spotter
AUTHOR = Pitwall Race Engineer
VERSION = 1.0
DESCRIPTION = Sends the position of every car to the Pitwall Race Engineer so it can call "car on your left / right / three wide / clear".
'''


def make_source_folder():
    """Make sure ac_app/PitwallSpotter exists next to this file. Rebuilds it from the copy inside this script if needed."""
    src = os.path.join(config.BASE_DIR, "ac_app", "PitwallSpotter")
    os.makedirs(src, exist_ok=True)
    rebuilt = []
    for name, text in (("PitwallSpotter.py", APP_SOURCE), ("manifest.ini", MANIFEST)):
        path = os.path.join(src, name)
        if not os.path.isfile(path):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            rebuilt.append(name)
    if rebuilt:
        print(f"The ac_app folder was missing some files, so I created them in:\n   {src}\n   ({', '.join(rebuilt)})")
    return src


def main():
    src = make_source_folder()
    apps = os.path.join(config.AC_PATH, "apps", "python")
    if not os.path.isdir(apps):
        print(f"I can't find Assetto Corsa at:\n   {config.AC_PATH}\n"
              "Open config.py and set AC_PATH to your game folder. To find it: Steam > right-click Assetto Corsa >\n"
              "Manage > Browse local files. The right folder contains 'acs.exe'. Then run this again.")
        return 1
    dest = os.path.join(apps, "PitwallSpotter")
    try:
        shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with open(os.path.join(dest, "spotter_path.txt"), "w", encoding="utf-8") as f:
            f.write(config.SPOTTER_FILE)                 # the app reads its target file from here
    except PermissionError:
        print(f"Windows wouldn't let me write to:\n   {dest}\nRun this from a terminal opened as Administrator "
              f"(right-click PowerShell > Run as administrator),\nor copy the folder\n   {src}\ninto\n   {apps}\n"
              f"yourself and put a file called spotter_path.txt inside it that contains this one line:\n   {config.SPOTTER_FILE}")
        return 1
    print(f"Installed to {dest}\n\nNow, in Assetto Corsa:\n"
          "  1. Start a session and drive out.\n"
          "  2. Move the mouse to the RIGHT edge of the screen to open the app bar.\n"
          "  3. Click  PitwallSpotter.  A small window appears saying 'Pitwall spotter: sending N cars'.\n"
          "     (It can stay open, or you can drag it somewhere harmless. It must stay switched on.)\n"
          f"\nThe cars' positions are shared through this file:\n   {config.SPOTTER_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
