"""
tyre_probe.py - run this while you are DRIVING in Assetto Corsa (not in the menu).

It prints what the game really puts in shared memory for tyres and flags,
so we can see what is wrong instead of guessing.   Usage:  python tyre_probe.py
Stop early with Ctrl+C.  Takes about 30 seconds.
"""
import faulthandler
import struct
import sys
import time

faulthandler.enable()
CORNERS = ["FL", "FR", "RL", "RR"]

# ---- AC physics page: every field is 4 bytes, (name, how many values) ----
PHYSICS_FIELDS = [
    ("packetId", 1), ("gas", 1), ("brake", 1), ("fuel", 1), ("gear", 1), ("rpms", 1),
    ("steerAngle", 1), ("speedKmh", 1), ("velocity", 3), ("accG", 3), ("wheelSlip", 4),
    ("wheelLoad", 4), ("wheelsPressure", 4), ("wheelAngularSpeed", 4), ("tyreWear", 4),
    ("tyreDirtyLevel", 4), ("tyreCoreTemperature", 4), ("camberRAD", 4),
    ("suspensionTravel", 4), ("drs", 1), ("tc", 1), ("heading", 1), ("pitch", 1),
    ("roll", 1), ("cgHeight", 1), ("carDamage", 5), ("numberOfTyresOut", 1),
    ("pitLimiterOn", 1), ("abs", 1), ("kersCharge", 1), ("kersInput", 1),
    ("autoShifterOn", 1), ("rideHeight", 2), ("turboBoost", 1), ("ballast", 1),
    ("airDensity", 1), ("airTemp", 1), ("roadTemp", 1), ("localAngularVel", 3),
    ("finalFF", 1), ("performanceMeter", 1), ("engineBrake", 1), ("ersRecoveryLevel", 1),
    ("ersPowerLevel", 1), ("ersHeatCharging", 1), ("ersIsCharging", 1),
    ("kersCurrentKJ", 1), ("drsAvailable", 1), ("drsEnabled", 1), ("brakeTemp", 4),
    ("clutch", 1), ("tyreTempI", 4), ("tyreTempM", 4), ("tyreTempO", 4),
]
OFFSETS = {}
_pos = 0
for _name, _count in PHYSICS_FIELDS:
    OFFSETS[_name] = _pos
    _pos += 4 * _count
PHYSICS_NEEDED = _pos

# ---- AC graphics page: fixed part, then TWO known layouts for the tail ----
G_TYRE_COMPOUND = 176          # 33 UTF-16 characters
G_AFTER_COMPOUND = 244         # replayTimeMultiplier starts here
# Layout A (older AC):  carCoordinates[3] then penaltyTime ...
# Layout B (newer AC):  activeCars, carCoordinates[60][3], carID[60], playerCarID, penaltyTime ...
G_TAIL = {"A": 264, "B": 1220}
GRAPHICS_NEEDED = 1300


def parse_physics(buf):
    out = {}
    for name, count in PHYSICS_FIELDS:
        fmt = "<%d%s" % (count, "i" if name in ("packetId", "gear", "rpms", "numberOfTyresOut",
                                                "pitLimiterOn", "autoShifterOn") else "f")
        vals = struct.unpack_from(fmt, buf, OFFSETS[name])
        out[name] = vals if count > 1 else vals[0]
    return out


def parse_graphics(buf):
    g = {}
    g["packetId"], g["status"], g["session"] = struct.unpack_from("<3i", buf, 0)
    g["completedLaps"], g["position"] = struct.unpack_from("<2i", buf, 132)
    g["iLastTime"] = struct.unpack_from("<i", buf, 144)[0]
    g["isInPit"] = struct.unpack_from("<i", buf, 160)[0]
    raw = bytes(buf[G_TYRE_COMPOUND:G_TYRE_COMPOUND + 66])
    g["tyreCompound"] = raw.decode("utf-16-le", errors="ignore").split("\x00")[0]
    for layout, off in G_TAIL.items():
        pen, flag, ideal, pitlane, grip = struct.unpack_from("<fiiif", buf, off)
        g["layout_" + layout] = {"penaltyTime": pen, "flag": flag,
                                 "isInPitLane": pitlane, "surfaceGrip": grip}
    return g


def run():
    if sys.platform != "win32":
        print("This probe only works on Windows, on the PC that runs Assetto Corsa.")
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t]
    k32.MapViewOfFile.restype = ctypes.c_void_p

    views = {}
    for key, name in (("physics", "Local\\acpmf_physics"), ("graphics", "Local\\acpmf_graphics")):
        h = k32.OpenFileMappingW(0x0004, False, name)
        if not h:
            print("FAIL: cannot open %s. Start Assetto Corsa and get into the car first." % name)
            return
        v = k32.MapViewOfFile(h, 0x0004, 0, 0, 0)   # 0 = map the whole page
        if not v:
            print("FAIL: cannot map %s" % name)
            return
        views[key] = v
    print("Connected. Drive around - printing once a second...\n")

    wear_seen = {i: set() for i in range(4)}
    last_g = None
    try:
        for n in range(30):
            phys = parse_physics(ctypes.string_at(views["physics"], PHYSICS_NEEDED))
            gfx = parse_graphics(ctypes.string_at(views["graphics"], GRAPHICS_NEEDED))
            last_g = gfx
            print("--- sample %d | speed %.0f km/h | status %d (2 = live) | compound %r" %
                  (n + 1, phys["speedKmh"], gfx["status"], gfx["tyreCompound"]))
            print("     psi    core   I/M/O temp       wear(raw)   dirt")
            for i, c in enumerate(CORNERS):
                wear_seen[i].add(round(phys["tyreWear"][i], 3))
                print("  %s %5.1f  %5.1f  %3.0f/%3.0f/%3.0f   %10.3f   %.2f" % (
                    c, phys["wheelsPressure"][i], phys["tyreCoreTemperature"][i],
                    phys["tyreTempI"][i], phys["tyreTempM"][i], phys["tyreTempO"][i],
                    phys["tyreWear"][i], phys["tyreDirtyLevel"][i]))
            for layout in ("A", "B"):
                L = gfx["layout_" + layout]
                print("  graphics layout %s: flag=%d penalty=%.1f grip=%.3f pitlane=%d" % (
                    layout, L["flag"], L["penaltyTime"], L["surfaceGrip"], L["isInPitLane"]))
            print()
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass

    print("=" * 60)
    print("VERDICT")
    flat = all(len(v) == 1 for v in wear_seen.values())
    if flat:
        print("* Tyre wear never changed during this test. That is normal for 30 seconds,")
        print("  but if it still reads exactly 100 (or 1.0) after several laps, tyre wear is")
        print("  switched OFF for the session. In Content Manager set the tyre wear rate above 0x.")
    else:
        print("* Tyre wear is changing - the engineer CAN read it.")
    if last_g:
        for layout in ("A", "B"):
            L = last_g["layout_" + layout]
            sane = 0 <= L["flag"] <= 6 and 0.3 <= L["surfaceGrip"] <= 1.2
            print("* Graphics layout %s looks %s" % (layout, "SANE" if sane else "wrong"))
        print("  Use the layout that looks SANE for flags/penalties. Paste this output to me.")


if __name__ == "__main__":
    run()
