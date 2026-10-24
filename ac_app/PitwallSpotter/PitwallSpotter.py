# PitwallSpotter - a tiny Assetto Corsa app. It does ONE thing: about 25 times a second it writes the world position of
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
