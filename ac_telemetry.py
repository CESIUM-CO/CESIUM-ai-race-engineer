import ctypes
import math
import os
import struct
from ctypes import c_float, c_int32, c_wchar
from types import SimpleNamespace

FILE_MAP_READ = 0x0004

if os.name == "nt":
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    kernel32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p
    kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    kernel32.UnmapViewOfFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
else:                       # lets the decoding code below be tested on any computer
    kernel32 = None


class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packetId', c_int32), ('gas', c_float), ('brake', c_float), ('fuel', c_float),
        ('gear', c_int32), ('rpms', c_int32), ('steerAngle', c_float), ('speedKmh', c_float),
        ('velocity', c_float * 3), ('accG', c_float * 3),
        ('wheelSlip', c_float * 4), ('wheelLoad', c_float * 4), ('wheelsPressure', c_float * 4),
        ('wheelAngularSpeed', c_float * 4), ('tyreWear', c_float * 4), ('tyreDirtyLevel', c_float * 4),
        ('tyreCoreTemperature', c_float * 4), ('camberRAD', c_float * 4), ('suspensionTravel', c_float * 4),
        ('drs', c_float), ('tc', c_float), ('heading', c_float), ('pitch', c_float), ('roll', c_float),
        ('cgHeight', c_float), ('carDamage', c_float * 5),
        ('numberOfTyresOut', c_int32), ('pitLimiterOn', c_int32), ('abs', c_float),
        ('kersCharge', c_float), ('kersInput', c_float), ('autoShifterOn', c_int32),
        ('rideHeight', c_float * 2), ('turboBoost', c_float), ('ballast', c_float), ('airDensity', c_float),
        ('airTemp', c_float), ('roadTemp', c_float), ('localAngularVel', c_float * 3),
        ('finalFF', c_float), ('performanceMeter', c_float), ('engineBrake', c_int32),
        ('ersRecoveryLevel', c_int32), ('ersPowerLevel', c_int32),
        ('ersHeatCharging', c_int32), ('ersIsCharging', c_int32),
        ('kersCurrentKJ', c_float), ('drsAvailable', c_int32), ('drsEnabled', c_int32),
        ('brakeTemp', c_float * 4), ('clutch', c_float),
        ('tyreTempI', c_float * 4), ('tyreTempM', c_float * 4), ('tyreTempO', c_float * 4),
        ('isAIControlled', c_int32),
        ('tyreContactPoint', c_float * 12), ('tyreContactNormal', c_float * 12), ('tyreContactHeading', c_float * 12),
        ('brakeBias', c_float), ('localVelocity', c_float * 3),
    ]


class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('smVersion', c_wchar * 15), ('acVersion', c_wchar * 15),
        ('numberOfSessions', c_int32), ('numCars', c_int32),
        ('carModel', c_wchar * 33), ('track', c_wchar * 33),
        ('playerName', c_wchar * 33), ('playerSurname', c_wchar * 33), ('playerNick', c_wchar * 33),
        ('sectorCount', c_int32), ('maxTorque', c_float), ('maxPower', c_float),
        ('maxRpm', c_int32), ('maxFuel', c_float),
        ('suspensionMaxTravel', c_float * 4), ('tyreRadius', c_float * 4),
        ('maxTurboBoost', c_float), ('deprecated_1', c_float), ('deprecated_2', c_float),
        ('penaltiesEnabled', c_int32), ('aidFuelRate', c_float), ('aidTireRate', c_float),
        ('aidMechanicalDamage', c_float), ('aidAllowTyreBlankets', c_int32),
        ('aidStability', c_float), ('aidAutoClutch', c_int32), ('aidAutoBlip', c_int32),
        ('hasDRS', c_int32), ('hasERS', c_int32), ('hasKERS', c_int32), ('kersMaxJ', c_float),
        ('engineBrakeSettingsCount', c_int32), ('ersPowerControllerCount', c_int32),
        ('trackSPlineLength', c_float), ('trackConfiguration', c_wchar * 15),
    ]


# ------------------------------------------------------------------
#  GRAPHICS PAGE. The first part never changes between AC versions.
#  The tail (penalty, FLAG, grip...) exists in two layouts, so we try both
#  and lock onto the one that gives believable numbers.
# ------------------------------------------------------------------
GRAPHICS_READ_BYTES = 1300
TAIL_OFFSETS = {"A": 264, "B": 1220}      # A = older AC, B = newer AC (activeCars + 60 car positions)


def _finite(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


def _decode_tail(raw, off):
    pen, flag, ideal, pitlane, grip, mand, wind, wdir = struct.unpack_from("<fiiififf", raw, off)
    return dict(penaltyTime=pen, flag=flag, idealLineOn=ideal, isInPitLane=pitlane,
                surfaceGrip=grip, mandatoryPitDone=mand, windSpeed=wind, windDirection=wdir)


def _tail_is_sane(t):
    return (_finite(t["penaltyTime"]) and 0 <= t["penaltyTime"] < 10000 and
            0 <= t["flag"] <= 6 and t["isInPitLane"] in (0, 1) and
            _finite(t["surfaceGrip"]) and 0.3 <= t["surfaceGrip"] <= 1.2 and
            _finite(t["windSpeed"]) and 0 <= t["windSpeed"] < 300)


def decode_graphics(raw, layout=None):
    """raw bytes -> (graphics object, layout letter or None if undecided)."""
    g = SimpleNamespace()
    g.packetId, g.status, g.session = struct.unpack_from("<3i", raw, 0)
    g.completedLaps, g.position, g.iCurrentTime, g.iLastTime, g.iBestTime = struct.unpack_from("<5i", raw, 132)
    g.sessionTimeLeft, g.distanceTraveled = struct.unpack_from("<2f", raw, 152)
    g.isInPit, g.currentSectorIndex, g.lastSectorTime, g.numberOfLaps = struct.unpack_from("<4i", raw, 160)
    g.tyreCompound = bytes(raw[176:242]).decode("utf-16-le", errors="ignore").split("\x00")[0]
    g.replayTimeMultiplier, g.normalizedCarPosition = struct.unpack_from("<2f", raw, 244)

    tails = {k: _decode_tail(raw, off) for k, off in TAIL_OFFSETS.items()}
    locked = layout
    if locked is None:
        ok = [k for k, t in tails.items() if _tail_is_sane(t)]
        if len(ok) == 1:
            locked = ok[0]
    chosen = locked or "B"
    g.__dict__.update(tails[chosen])
    # Positions of all cars on track (needed by the spotter). Only the newer layout carries them.
    g.activeCars, g.carCoordinates, g.carID, g.playerCarID = 0, [], [], 0
    if chosen == "B":
        n = max(0, min(60, struct.unpack_from("<i", raw, 252)[0]))
        flat = struct.unpack_from("<180f", raw, 256)
        g.activeCars = n
        g.carCoordinates = [flat[i * 3:i * 3 + 3] for i in range(n)]
        g.carID = list(struct.unpack_from("<60i", raw, 976))[:n]
        g.playerCarID = struct.unpack_from("<i", raw, 1216)[0]
    return g, locked


class ACTelemetryReader:
    def __init__(self):
        self.handles, self.views = {}, {}
        self.is_connected = False
        self.layout = None          # "A" or "B" once detected

    def close(self):
        for view in self.views.values():
            if view:
                kernel32.UnmapViewOfFile(view)
        for handle in self.handles.values():
            if handle:
                kernel32.CloseHandle(handle)
        self.handles, self.views = {}, {}
        self.is_connected = False

    def connect(self) -> bool:
        self.close()
        for key, name in (("physics", "Local\\acpmf_physics"), ("graphics", "Local\\acpmf_graphics"),
                          ("static", "Local\\acpmf_static")):
            handle = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
            if not handle:
                self.close()
                return False
            view = kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)   # 0 = whole page
            if not view:
                kernel32.CloseHandle(handle)
                self.close()
                return False
            self.handles[key], self.views[key] = handle, view
        self.is_connected = True
        return True

    def _snapshot(self, key, struct_type):
        raw = ctypes.string_at(self.views[key], ctypes.sizeof(struct_type))
        return struct_type.from_buffer_copy(raw)

    def get_data(self):
        if not self.is_connected:
            return None, None, None
        try:
            physics = self._snapshot("physics", SPageFilePhysics)
            static = self._snapshot("static", SPageFileStatic)
            raw = ctypes.string_at(self.views["graphics"], GRAPHICS_READ_BYTES)
            graphics, locked = decode_graphics(raw, self.layout)
            if locked and not self.layout:
                self.layout = locked
                print(f"[STATUS] Game data layout detected: {locked} "
                      f"({'newer' if locked == 'B' else 'older'} Assetto Corsa). Flags and penalties are now readable.")
            return physics, graphics, static
        except Exception:
            self.close()
            return None, None, None
