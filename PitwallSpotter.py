# PitwallSpotter - a tiny Assetto Corsa app. It does ONE thing: it writes the position of every car to a small text file
# about 25 times a second, so the Pitwall Race Engineer can say "car on your left / right".
#
# IMPORTANT: this runs INSIDE the game, which embeds an old Python (3.3). So: no f-strings, no fancy syntax.
# Install with  python install_spotter.py  and switch the app on in the game's app bar (right edge of the screen).
import os
import time

import ac
import acsys

APP_NAME = "PitwallSpotter"
INTERVAL = 0.04            # seconds between writes (25 per second)
_state = {"since": 0.0, "label": None, "sent": 0, "errors": 0}


def _file_path():
    # must match SPOTTER_FILE in the engineer's config.py
    folder = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
    return os.path.join(folder, "pitwall_cars.txt")


PATH = _file_path()


def acMain(ac_version):
    window = ac.newApp(APP_NAME)
    ac.setSize(window, 230, 70)
    _state["label"] = ac.addLabel(window, "Pitwall spotter: starting...")
    ac.setPosition(_state["label"], 10, 35)
    ac.log("PitwallSpotter writing to " + PATH)
    return APP_NAME


def _write():
    count = ac.getCarsCount()
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
        with open(PATH, "w") as f:         # if the engineer happened to be reading it at that instant, write directly
            f.write(text)
    return count


def acUpdate(delta_t):
    _state["since"] += delta_t
    if _state["since"] < INTERVAL:
        return
    _state["since"] = 0.0
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
