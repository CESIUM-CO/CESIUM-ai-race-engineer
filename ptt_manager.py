"""
ptt_manager.py - Cesium AI Race Engineer: universal push-to-talk input (v0.1.0-beta).

ONE push-to-talk button, from ANY device, even while Assetto Corsa has the focus:
  * keyboards
  * mice                    (middle / side buttons; left and right are not offered when rebinding)
  * steering wheels         (cheap USB wheels up to Fanatec, Logitech, Thrustmaster, Simucube, Moza ...)
  * button boxes, shifters, handbrakes, gamepads   (DirectInput and XInput)

HOW IT WORKS
  keyboard   the `keyboard` package (global hook, already in requirements.txt)
  mouse      the `mouse` package   (same author, same style) AND, on Windows, a GetAsyncKeyState poll of
             middle / side buttons, so Mouse 4 / 5 work even if the package or its hook does not
  wheels,    pygame 2 / SDL2. SDL sees DirectInput devices AND XInput pads through one API, and we ask it to keep
  pads       delivering input while the game window has focus (SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS).
  Each source is optional: if a package is missing, only that source is switched off and the rest keeps working.

JOYSTICK BACKEND (what changed in this version, and why)
  * Every loop iteration now runs, in this order:  Windows message pump -> pygame.event.pump() -> pygame.event.get()
    -> state polling.  SDL is told to use its "dummy" video driver (we never open a window), and the dummy driver does
    NOT pump the Windows message queue of its thread. SDL's hidden helper window (used for device notifications and for
    background input) therefore never received its messages. We now pump that queue ourselves (PeekMessageW /
    DispatchMessageW) and call pygame.event.pump() explicitly before anything is read.
  * SDL's RAWINPUT and WGI joystick drivers are switched off (setdefault, so an environment variable still wins).
    Both only deliver input reliably to a program that owns a visible window with a message loop. DirectInput and
    XInput are polled by SDL itself, work with no window, and keep working while a game has the focus.
  * Devices are stored by their SDL INSTANCE id (Joystick.get_instance_id()), never by the device index. Events are
    matched through one function (_dev_for) that understands instance_id, the legacy `joy` field and the device index.
    An event for a device we do not know triggers an immediate rescan, so a controller can never be "lost".
  * Axes (paddles wired as axes, triggers, analogue handbrakes) and hats (d-pads) work for live push-to-talk AND for
    rebind. In rebind, an axis is only accepted when it SNAPS from rest to pressed in a fraction of a second, so slowly
    turning the steering wheel is not mistaken for a button. Pedals can still be captured: press Esc if that happens.

BINDINGS are short text "specs" saved in a small JSON file:
    kb:f                      keyboard key (name as the keyboard package reports it)
    mouse:x2                  mouse button (left, right, middle, x, x2)
    joy:0eb7-0e03:b5          button 5 (shown to the user as "Button 6") on the device with vendor 0eb7 / product 0e03
    joy:0eb7-0e03:h0up        hat / d-pad direction (up, down, left, right)
    joy:045e-028e:a5+         axis 5, pushed in the + direction
  Controllers are matched by USB vendor/product id, so a binding survives a reboot, a new USB port or a firmware
  update. Two identical devices share one binding. If an old config.py still has PTT_KEY = "f", that is used until
  the user rebinds.

THREADING
  Nothing here blocks the caller. The hooks only drop events into a queue; ONE dispatcher thread ("PTT-dispatch") owns
  all state and runs your callbacks, so press/release always arrive in order. Keep callbacks short (start a thread for
  slow work). Unplugging a controller while the button is held counts as a release, so the mic can never get stuck on.
  The joystick backend owns ONE thread ("PTT-joystick"): pygame is initialised, pumped and shut down on that thread only.

USAGE
    from ptt_manager import PTTManager

    ptt = PTTManager(on_press=start_recording, on_release=stop_recording)
    ptt.start()                                    # returns at once
    session = ptt.rebind(on_done=lambda s: print("PTT is now", s.binding))   # next key/button on ANY device
    ptt.stop()

  Try it on real hardware:     python ptt_manager.py --list
                               python ptt_manager.py --rebind
                               python ptt_manager.py --raw        (prints every button the wheel sends)
                               python ptt_manager.py --diag       (pygame / SDL versions, drivers, every device's raw state)

NOTES
  * The key press still reaches the game (nothing is suppressed). If your PTT is bound in Assetto Corsa too, unbind it there.
  * Windows hides input going to an ELEVATED window from a normal program. If you run Assetto Corsa as administrator,
    run the engineer as administrator too, or keyboard/mouse PTT will not be seen (wheel buttons are not affected).
  * Packaging: all optional imports are inside functions and visible to PyInstaller / Nuitka. See the end of the file.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import config
except Exception:                      # lets this file run on its own (tests, --list)
    config = None

# Wheels, gamepads and button boxes must keep working while Assetto Corsa has the focus. SDL only allows that when this
# is set BEFORE pygame is imported and a joystick is opened, so it is set here, at import time, unconditionally.
# (_JoystickBackend.start() sets it again, and gui.py sets it first of all.)
os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
# Drivers that need a window with a live message loop do not deliver input to a windowless background program.
# DirectInput + XInput (polled by SDL itself) do. Set the variable yourself (to "1") to override.
os.environ.setdefault("SDL_JOYSTICK_RAWINPUT", "0")
os.environ.setdefault("SDL_JOYSTICK_WGI", "0")

__all__ = ["PTTManager", "Binding", "InputEvent", "RebindSession", "describe_spec", "normalize_spec"]

TAG = "[PTT]"
DEFAULT_SPEC = "kb:f"
CANCEL_SPEC = "kb:esc"                 # Esc cancels a rebind
PRIMARY_MOUSE = ("mouse:left", "mouse:right")
REBIND_ACCEPTS_AXES = True             # rebind() accepts snapping axes (paddles, triggers) unless told otherwise
_STOP = object()


# =====================================================================================
#  Specs, events, bindings
# =====================================================================================
def _pretty_key(name: str) -> str:
    return name.upper() if len(name) == 1 else name.title()


def normalize_spec(text: str) -> str:
    """'F' -> 'kb:f'.  'kb:F' -> 'kb:f'.  'joy:...' / 'mouse:...' are lower-cased and kept."""
    t = (text or "").strip().lower()
    if t == "mouse:x":                  # the `mouse` package calls the first side button "x"; we always say "x1"
        return "mouse:x1"
    if t.startswith(("kb:", "mouse:", "joy:")):
        return t
    return "kb:" + t


_JOY_CTL = re.compile(r"^(?:b(\d+)|h(\d+)(up|down|left|right)|a(\d+)([+-]))$")
_MOUSE_NAMES = {"left": "Left button", "right": "Right button", "middle": "Middle button",
                "x": "Side button 1", "x1": "Side button 1", "x2": "Side button 2"}


def describe_spec(spec: str) -> str:
    """Human text for a spec when no better label is known (a label saved from a real device is nicer)."""
    kind, _, rest = spec.partition(":")
    if kind == "kb":
        return "Keyboard: " + _pretty_key(rest)
    if kind == "mouse":
        return "Mouse: " + _MOUSE_NAMES.get(rest, rest)
    if kind == "joy":
        dev, _, ctl = rest.partition(":")
        m = _JOY_CTL.match(ctl)
        if m:
            if m.group(1) is not None:
                what = "Button %d" % (int(m.group(1)) + 1)
            elif m.group(2) is not None:
                what = "Hat %d %s" % (int(m.group(2)) + 1, m.group(3).title())
            else:
                what = "Axis %d (%s)" % (int(m.group(4)) + 1, m.group(5))
            return "Controller %s: %s" % (dev, what)
    return spec


@dataclass(frozen=True)
class InputEvent:
    """One raw press or release from any device."""
    kind: str          # "down" | "up"
    spec: str          # what was pressed, e.g. "kb:f"
    label: str         # human text, e.g. "Fanatec ClubSport Wheel Base: Button 6"
    origin: str        # which physical device: "kb", "mouse", "joy:3"
    control: str       # "key" | "mouse" | "button" | "hat" | "axis"
    snap: bool = True  # axes only: True when the axis jumped from rest to pressed quickly (a paddle / trigger, not a steering turn)


@dataclass(frozen=True)
class Binding:
    spec: str
    label: str = ""

    @property
    def source(self) -> str:           # "kb" | "mouse" | "joy"
        return self.spec.split(":", 1)[0]

    def __str__(self) -> str:
        return self.label or describe_spec(self.spec)


class RebindSession:
    """Returned by PTTManager.rebind(). wait() blocks until the next input is captured, or until timeout / cancel."""

    def __init__(self, manager, timeout, include_axes, allow_primary_mouse, on_done):
        self._manager = manager
        self.timeout = float(timeout) if timeout and timeout > 0 else float("inf")
        self.include_axes = include_axes
        self.allow_primary_mouse = allow_primary_mouse
        self.on_done = on_done
        self.deadline = float("inf")   # set when listening actually begins
        self.binding: Optional[Binding] = None
        self.reason = ""               # captured | timeout | cancelled | replaced | stopped
        self.saved = False             # True when the new binding was written to disk
        self.last_seen = ""            # label of the last key / button pressed while listening (the popup shows it)
        self._event = threading.Event()

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> Optional[Binding]:
        """The captured Binding, or None (timeout / cancelled). Never call this from a GUI thread without a timeout."""
        self._event.wait(timeout)
        return self.binding

    def cancel(self) -> None:
        self._manager.cancel_rebind(self)


# =====================================================================================
#  Input sources. Each one only translates raw input into InputEvents and calls feed().
# =====================================================================================
class _KeyboardBackend:
    name = "keyboard"
    hint = "Run:  pip install keyboard"

    def __init__(self, feed, log):
        self._feed, self._kb, self._remove = feed, None, None

    def start(self):
        import keyboard
        self._kb = keyboard
        self._remove = keyboard.hook(self._on_event)

    def stop(self):
        if self._kb is not None and self._remove is not None:
            try:
                self._kb.unhook(self._remove)
            except Exception:
                pass
            self._remove = None

    def devices(self):
        return []

    def _on_event(self, e):                      # runs in the keyboard package's thread: be quick
        try:
            name = (getattr(e, "name", None) or "scan %s" % getattr(e, "scan_code", "?")).lower()
            if getattr(e, "is_keypad", False) and not name.startswith("num"):
                name = "num " + name              # keep keypad 5 apart from the 5 above the letters
            self._feed(InputEvent("up" if e.event_type == "up" else "down", "kb:" + name,
                                  "Keyboard: " + _pretty_key(name), "kb", "key"))
        except Exception:
            pass


class _MouseBackend:
    """Mouse buttons, from TWO sources that feed the same events (a repeat of the same press is ignored downstream):
      * the `mouse` package (a global hook), and
      * on Windows, a poll of GetAsyncKeyState for left / right / middle / side buttons: no package, no hook, and it
        works whatever window has the focus.
    So Mouse 4 / Mouse 5 (side buttons) still work if the `mouse` package is missing or its hook is blocked.
    Side buttons are always named x1 and x2 (the `mouse` package calls the first one "x")."""
    name = "mouse"
    hint = "Run:  pip install mouse"
    POLL_S = 0.01
    VK = {"left": 0x01, "right": 0x02, "middle": 0x04, "x1": 0x05, "x2": 0x06}     # Windows virtual-key codes

    def __init__(self, feed, log):
        self._feed, self._log = feed, log
        self._mouse = self._handler = self._thread = None
        self._stop = threading.Event()
        self.note = ""                           # what is not working (shown in the popup); "" = everything is

    def start(self):
        problem = ""
        try:
            import mouse
            self._mouse = mouse
            self._handler = mouse.hook(self._on_event)
        except Exception as e:
            self._mouse = self._handler = None
            problem = "%s: %s" % (type(e).__name__, e)
        polling = False
        if os.name == "nt":
            try:
                reader = self._windows_reader()
                self._thread = threading.Thread(target=self._poll_loop, args=(reader,), name="PTT-mouse-poll", daemon=True)
                self._thread.start()
                polling = True
            except Exception as e:
                problem = problem or "%s: %s" % (type(e).__name__, e)
        if self._mouse is None and not polling:
            raise RuntimeError(problem or "no way to read the mouse on this system")
        if self._mouse is None:
            self.note = "polling only, the mouse package is not working (%s)" % problem

    def stop(self):
        self._stop.set()
        if self._mouse is not None and self._handler is not None:
            try:
                self._mouse.unhook(self._handler)
            except Exception:
                pass
            self._handler = None
        if self._thread is not None:
            self._thread.join(0.5)

    def devices(self):
        return []

    # ------------------------------------------------------------------ source 1: the `mouse` package
    def _on_event(self, e):                      # also receives every mouse move: filter first, quickly
        try:
            if not isinstance(e, self._mouse.ButtonEvent):
                return
            button = str(e.button).lower()
            if button == "x":
                button = "x1"
            spec = "mouse:" + button
            # a double-click replaces the second "down", so it counts as a press
            self._feed(InputEvent("up" if e.event_type == "up" else "down", spec, describe_spec(spec), "mouse", "mouse"))
        except Exception:
            pass

    # ------------------------------------------------------------------ source 2: polling (Windows)
    @staticmethod
    def _windows_reader():
        import ctypes
        fn = ctypes.windll.user32.GetAsyncKeyState
        fn.argtypes = [ctypes.c_int]
        fn.restype = ctypes.c_short
        return lambda vk: bool(fn(vk) & 0x8000)   # the top bit = "is down right now"

    def _poll_loop(self, is_down):
        down = set()
        while not self._stop.is_set():
            try:
                for name, vk in self.VK.items():
                    now = is_down(vk)
                    if now != (name in down):
                        (down.add if now else down.discard)(name)
                        spec = "mouse:" + name
                        self._feed(InputEvent("down" if now else "up", spec, describe_spec(spec), "mouse", "mouse"))
            except Exception:
                pass
            self._stop.wait(self.POLL_S)


def _device_id(guid: str, name: str) -> str:
    """Stable id for a controller: 'vvvv-pppp' (USB vendor-product) from its SDL GUID, else the GUID, else its name.
    Vendor/product survive reboots, other USB ports and firmware updates (which change the GUID's version bytes)."""
    g = (guid or "").lower()
    if len(g) == 32 and g[12:16] == "0000" and g[20:24] == "0000":     # SDL layout: bus crc VENDOR 0 PRODUCT 0 version ...
        try:
            vid, pid = int(g[10:12] + g[8:10], 16), int(g[18:20] + g[16:18], 16)
            if vid or pid:
                return "%04x-%04x" % (vid, pid)
        except ValueError:
            pass
    return g or "name_" + (re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "unknown")


class _Dev:
    """One connected controller. Always stored under its SDL INSTANCE id (`iid`)."""

    def __init__(self, joy, iid, index, dev_id, name, armed_at, counts=(0, 0, 0)):
        self.joy, self.iid, self.index, self.dev_id, self.name = joy, iid, index, dev_id, name
        self.nb, self.nh, self.na = counts      # buttons, hats, axes
        self.btn_down = set()       # buttons known to be down (events and polling both update it)
        self.origin = "joy:%s" % iid
        self.armed_at, self.armed = armed_at, False
        self.rest = {}              # resting value of every axis (measured after the warm-up)
        self.axes_on = set()        # {(axis, +1/-1)} currently past the threshold
        self.axis_low = {}          # (axis, sign) -> last time that axis direction was clearly at rest (for snap detection)
        self.hats = {}              # hat -> set of directions currently held
        self.held = {}              # spec -> label, so a disconnect can release everything


class _JoystickBackend:
    """Wheels, pedals, button boxes and gamepads through pygame/SDL2, on its own thread.

    Two ways of reading, used TOGETHER (the dispatcher ignores a repeat of the same press):
      * pygame events    JOYBUTTONDOWN / JOYHATMOTION / JOYAXISMOTION / device added / removed
      * state polling    joy.get_button / get_hat / get_axis every few milliseconds
    Polling is the safety net: SDL keeps a device's state up to date even when it decides not to post events because
    the program is "in the background" (our process has no window, and the game has the focus).

    Every loop iteration: Windows message pump -> pygame.event.pump() -> pygame.event.get() -> state polling.
    (See the module docstring for why the message pump and the explicit pump matter.)

    Devices live in self._devs keyed by SDL INSTANCE id. self._index_to_iid maps a device index to it, and _dev_for()
    resolves any event (instance_id, legacy `joy`, or device index) to a device."""
    name = "joystick"
    hint = "Run:  pip install pygame   (version 2.0 or newer)"

    WARMUP_S = 1.0            # SDL can report a burst of "initial" axis values; ignore axes until they settle
    AXIS_ON, AXIS_OFF = 0.6, 0.3   # axis travel (from its resting value) that counts as pressed / released
    SNAP_S = 0.20             # an axis that goes from "at rest" (< AXIS_OFF) to pressed (>= AXIS_ON) this fast is a paddle / trigger
    POLL_S = 0.005
    RESCAN_S = 2.0
    BAD_DEVICE_S = 5.0        # a device that raised an error while polled is left alone for this long, then tried again

    def __init__(self, feed, log):
        self._feed, self._log = feed, log
        self._stop = threading.Event()
        self._thread = None
        self._pg = None
        self._devs = {}                  # instance id -> _Dev
        self._index_to_iid = {}          # device index -> instance id (only for devices we hold)
        self._skipped = set()            # instance ids of devices with nothing to press (never retried)
        self._bad_until = {}             # instance id -> monotonic time before which it is not re-added
        self._force_scan = False         # an event came from a device we do not know: rescan at once
        self._pump_native = None
        self._snap_lock = threading.Lock()
        self._snapshot = []
        self._last_error = None
        self._none_warned = False
        self._versions_logged = False
        self.note = ""                   # what is wrong right now (shown in the popup); "" = nothing

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"   # BEFORE pygame is imported / a joystick is opened
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        os.environ.setdefault("SDL_JOYSTICK_RAWINPUT", "0")
        os.environ.setdefault("SDL_JOYSTICK_WGI", "0")
        import pygame
        if not hasattr(pygame, "JOYDEVICEADDED"):
            raise RuntimeError("pygame 2.0 or newer is required")
        self._pg = pygame
        self._thread = threading.Thread(target=self._run, name="PTT-joystick", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(1.5)

    def devices(self):
        with self._snap_lock:
            return [dict(d) for d in self._snapshot]

    def _run(self):
        while not self._stop.is_set():
            try:
                self._init_pygame()
                self.note = ""
                self._loop()
            except Exception as e:
                text = "%s: %s" % (type(e).__name__, e)
                self.note = "problem: " + text
                if text != self._last_error:
                    self._last_error = text
                    self._log("%s Controller input hit a problem (%s). Retrying quietly." % (TAG, text))
                self._drop_all()
                self._shutdown_pygame()
                self._stop.wait(2.0)
        self._drop_all()
        self._shutdown_pygame()

    def _init_pygame(self):
        pg = self._pg
        own_driver = "SDL_VIDEODRIVER" not in os.environ
        if own_driver:
            os.environ["SDL_VIDEODRIVER"] = "dummy"       # pygame's event system needs a video system; we never open a window
        try:
            pg.display.init()
            pg.joystick.init()
        finally:
            if own_driver:
                os.environ.pop("SDL_VIDEODRIVER", None)
        self._pump_native = self._make_native_pump()
        if not self._versions_logged:
            self._versions_logged = True
            try:
                self._log("%s pygame %s, SDL %s, joystick drivers: RAWINPUT=%s WGI=%s" % (
                    TAG, getattr(pg, "version", None) and pg.version.ver,
                    ".".join(str(x) for x in pg.version.SDL),
                    os.environ.get("SDL_JOYSTICK_RAWINPUT"), os.environ.get("SDL_JOYSTICK_WGI")))
            except Exception:
                pass
        for i in range(pg.joystick.get_count()):
            self._add(i)

    def _shutdown_pygame(self):
        self._skipped.clear()
        self._index_to_iid.clear()
        self._bad_until.clear()
        for fn in (lambda: self._pg.joystick.quit(), lambda: self._pg.display.quit()):
            try:
                fn()
            except Exception:
                pass

    # ------------------------------------------------------------------ pumping
    @staticmethod
    def _make_native_pump():
        """Windows only: a function that drains THIS thread's message queue.
        SDL's "dummy" video driver never does, but SDL's hidden helper window (device notifications, background input)
        lives on this thread and needs its messages dispatched. Must be created on the joystick thread."""
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            msg = wintypes.MSG()
            peek, translate, dispatch = user32.PeekMessageW, user32.TranslateMessage, user32.DispatchMessageW
            peek.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
            peek.restype = wintypes.BOOL
            translate.argtypes = [ctypes.POINTER(wintypes.MSG)]
            translate.restype = wintypes.BOOL
            dispatch.argtypes = [ctypes.POINTER(wintypes.MSG)]
            dispatch.restype = ctypes.c_ssize_t
            ref = ctypes.byref(msg)

            def pump():
                for _ in range(256):                      # bounded: never starve the loop
                    if not peek(ref, None, 0, 0, 0x0001):  # PM_REMOVE
                        break
                    translate(ref)
                    dispatch(ref)
            return pump
        except Exception:
            return None

    def _pump(self):
        if self._pump_native is not None:
            try:
                self._pump_native()
            except Exception:
                pass
        self._pg.event.pump()                              # refreshes SDL's device state before we read anything

    def _loop(self):
        pg = self._pg
        started = next_scan = time.monotonic()
        while not self._stop.is_set():
            self._pump()
            for e in pg.event.get():
                self._handle(e)
            now = time.monotonic()
            if self._force_scan or now >= next_scan:
                self._force_scan = False
                next_scan = now + self.RESCAN_S
                self._rescan()
                if not self._none_warned and not self._devs and now - started > 3.0:
                    self._none_warned = True
                    self._log("%s pygame / SDL sees no game controller. If a wheel or gamepad is plugged in: check that it is "
                              "listed in Windows 'Set up USB game controllers' (run joy.cpl) and that no other program holds "
                              "it exclusively." % TAG)
            self._arm_ready()
            for dev in list(self._devs.values()):
                self._poll_state(dev)
            self._stop.wait(self.POLL_S)

    # ------------------------------------------------------------------ devices
    def _dev_for(self, e):
        """The _Dev an event belongs to, or None. Understands instance_id (pygame 2), the legacy `joy` field and a plain
        device index, so a mismatch between them can never silently drop a button press."""
        iid = getattr(e, "instance_id", None)
        if iid is not None and iid in self._devs:
            return self._devs[iid]
        joy = getattr(e, "joy", None)
        if joy is not None:
            if joy in self._devs:
                return self._devs[joy]
            mapped = self._index_to_iid.get(joy)
            if mapped in self._devs:
                return self._devs[mapped]
        idx = getattr(e, "device_index", None)
        if idx is not None:
            mapped = self._index_to_iid.get(idx)
            if mapped in self._devs:
                return self._devs[mapped]
        return None

    def _rescan(self):
        try:
            count = self._pg.joystick.get_count()
        except Exception:
            return
        known = len(self._devs) + len(self._skipped)
        now = time.monotonic()
        if count != known or any(t <= now for t in self._bad_until.values()):
            for i in range(count):
                self._add(i)

    def _add(self, index):
        try:
            joy = self._pg.joystick.Joystick(index)
            iid = joy.get_instance_id()
        except Exception:
            return
        if iid in self._devs or iid in self._skipped:
            self._index_to_iid[index] = iid
            return                                       # SDL announces devices that are already present: ignore the repeat
        if self._bad_until.get(iid, 0.0) > time.monotonic():
            return
        self._bad_until.pop(iid, None)
        try:
            name = (joy.get_name() or "Controller").strip()
            nb, nh, na = joy.get_numbuttons(), joy.get_numhats(), joy.get_numaxes()
        except Exception:
            return
        if nb == 0 and nh == 0 and na == 0:
            self._skipped.add(iid)
            return
        try:
            guid = joy.get_guid()
        except Exception:
            guid = ""
        dev = _Dev(joy, iid, index, _device_id(guid, name), name, time.monotonic() + self.WARMUP_S, (nb, nh, na))
        self._devs[iid] = dev
        self._index_to_iid[index] = iid
        self._log("%s Controller connected: %s (index %d, instance %s, %d buttons, %d hats, %d axes)" % (
            TAG, name, index, iid, nb, nh, na))
        self._publish_snapshot()

    def _remove(self, iid, bad=False):
        self._skipped.discard(iid)
        dev = self._devs.pop(iid, None)
        for idx in [k for k, v in self._index_to_iid.items() if v == iid]:
            self._index_to_iid.pop(idx, None)
        if bad:
            self._bad_until[iid] = time.monotonic() + self.BAD_DEVICE_S
        if dev is None:
            return
        for spec, label in list(dev.held.items()):       # unplugged while held = released (never leave the mic open)
            self._feed(InputEvent("up", spec, label, dev.origin, "button"))
        dev.held.clear()
        dev.btn_down.clear()
        self._log("%s Controller disconnected: %s" % (TAG, dev.name))
        self._publish_snapshot()

    def _drop_all(self):
        for iid in list(self._devs):
            self._remove(iid)

    def _publish_snapshot(self):
        snap = []
        for d in self._devs.values():
            snap.append({"name": d.name, "id": d.dev_id, "buttons": d.nb, "hats": d.nh, "axes": d.na})
        with self._snap_lock:
            self._snapshot = snap

    def _arm_ready(self):
        now = time.monotonic()
        for dev in self._devs.values():
            if not dev.armed and now >= dev.armed_at:
                dev.armed = True
                try:
                    dev.rest = {i: float(dev.joy.get_axis(i)) for i in range(dev.na)}
                except Exception:
                    dev.rest = {}
                for i in range(dev.na):                  # every axis starts "at rest" for snap detection
                    dev.axis_low[(i, 1)] = dev.axis_low[(i, -1)] = now

    # ------------------------------------------------------------------ input: events
    def _handle(self, e):
        pg, t = self._pg, e.type
        if t == pg.JOYDEVICEADDED:
            self._add(e.device_index)
        elif t == pg.JOYDEVICEREMOVED:
            self._remove(getattr(e, "instance_id", None))
        elif t in (pg.JOYBUTTONDOWN, pg.JOYBUTTONUP, pg.JOYHATMOTION, pg.JOYAXISMOTION):
            dev = self._dev_for(e)
            if dev is None:
                self._force_scan = True                  # an event from a device we do not hold: go and find it now
                return
            if t == pg.JOYHATMOTION:
                self._hat(dev, e.hat, e.value)
            elif t == pg.JOYAXISMOTION:
                if dev.armed:
                    self._axis(dev, e.axis, float(e.value))
            else:
                self._button(dev, e.button, t == pg.JOYBUTTONDOWN)

    # ------------------------------------------------------------------ input: state polling (same handlers, so no duplicates)
    def _poll_state(self, dev):
        joy = dev.joy
        try:
            for b in range(dev.nb):
                self._button(dev, b, bool(joy.get_button(b)))
            for h in range(dev.nh):
                self._hat(dev, h, joy.get_hat(h))
            if dev.armed:
                for a in range(dev.na):
                    self._axis(dev, a, float(joy.get_axis(a)))
        except Exception:                                # unplugged between two polls, or SDL dropped the handle
            self._remove(dev.iid, bad=True)

    # ------------------------------------------------------------------ one place per kind of control
    def _emit(self, dev, down, suffix, control, text, snap=True):
        spec = "joy:%s:%s" % (dev.dev_id, suffix)
        label = "%s: %s" % (dev.name, text)
        if down:
            dev.held[spec] = label
        else:
            dev.held.pop(spec, None)
        self._feed(InputEvent("down" if down else "up", spec, label, dev.origin, control, snap))

    def _button(self, dev, b, down):
        if (b in dev.btn_down) == down:
            return                                       # already known (the event and the poll both report every press)
        (dev.btn_down.add if down else dev.btn_down.discard)(b)
        self._emit(dev, down, "b%d" % b, "button", "Button %d" % (b + 1))

    def _hat(self, dev, hat, value):
        try:
            x, y = value
        except (TypeError, ValueError):
            return
        now = {d for d, on in (("left", x < 0), ("right", x > 0), ("up", y > 0), ("down", y < 0)) if on}
        before = dev.hats.get(hat, set())
        for d in before - now:
            self._emit(dev, False, "h%d%s" % (hat, d), "hat", "Hat %d %s" % (hat + 1, d.title()))
        for d in now - before:
            self._emit(dev, True, "h%d%s" % (hat, d), "hat", "Hat %d %s" % (hat + 1, d.title()))
        dev.hats[hat] = now

    def _axis(self, dev, axis, value):
        now = time.monotonic()
        delta = value - dev.rest.get(axis, 0.0)
        for sign, mark in ((1, "+"), (-1, "-")):
            travel, key = delta * sign, (axis, sign)
            if travel < self.AXIS_OFF:
                dev.axis_low[key] = now                  # clearly at rest: remember when
            if key not in dev.axes_on and travel >= self.AXIS_ON:
                dev.axes_on.add(key)
                snap = (now - dev.axis_low.get(key, 0.0)) <= self.SNAP_S
                self._emit(dev, True, "a%d%s" % (axis, mark), "axis", "Axis %d (%s)" % (axis + 1, mark), snap)
            elif key in dev.axes_on and travel < self.AXIS_OFF:
                dev.axes_on.discard(key)
                self._emit(dev, False, "a%d%s" % (axis, mark), "axis", "Axis %d (%s)" % (axis + 1, mark))

    # ------------------------------------------------------------------ diagnostics (python ptt_manager.py --diag)
    def diagnostics(self):
        """Human-readable lines about what SDL sees right now. Safe to call from any thread (reads a snapshot)."""
        lines = []
        for d in list(self._devs.values()):
            try:
                btn = [b + 1 for b in range(d.nb) if d.joy.get_button(b)]
                hats = [d.joy.get_hat(h) for h in range(d.nh)]
                axes = [round(d.joy.get_axis(a), 2) for a in range(d.na)]
            except Exception as e:
                lines.append("  %s: could not read (%s)" % (d.name, e))
                continue
            lines.append("  %s [instance %s, index %s, id %s]\n     buttons down: %s   hats: %s\n     axes: %s" % (
                d.name, d.iid, d.index, d.dev_id, btn or "-", hats or "-", axes or "-"))
        return lines


# =====================================================================================
#  The manager
# =====================================================================================
def _default_settings_file() -> str:
    explicit = getattr(config, "PTT_SETTINGS_FILE", None)
    if explicit:
        return explicit
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        # packaged one-file builds unpack into a temp folder that is wiped on exit: keep the setting in the user profile
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "CesiumAIRaceEngineer", "ptt_binding.json")
    base = getattr(config, "BASE_DIR", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "data", "ptt_binding.json")


class PTTManager:
    """Global push-to-talk from any keyboard, mouse, wheel or gamepad. See the module docstring."""

    def __init__(self, on_press: Optional[Callable[[], None]] = None,
                 on_release: Optional[Callable[[], None]] = None,
                 binding: Optional[str] = None, settings_file: Optional[str] = None,
                 on_raw: Optional[Callable[[InputEvent], None]] = None, log: Optional[Callable] = print,
                 use_keyboard: bool = True, use_mouse: bool = True, use_joystick: bool = True):
        self._on_press, self._on_release, self._on_raw = on_press, on_release, on_raw
        self._log = log or (lambda *a, **k: None)
        self.settings_file = settings_file or _default_settings_file()
        self._enabled = ((_KeyboardBackend, use_keyboard), (_MouseBackend, use_mouse), (_JoystickBackend, use_joystick))

        self._q: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._backends = []
        self._status = {cls.name: "starting" if on else "disabled" for cls, on in self._enabled}

        # State below is only touched by the dispatcher thread (see _post / _sync for how other threads reach it).
        self._held = set()                 # {(spec, origin)} of every input that is down right now
        self._ignore = set()               # specs that must not trigger until they are released once
        self._active = False               # is push-to-talk down right now
        self._rebind: Optional[RebindSession] = None
        if binding:
            spec = normalize_spec(binding)
            self._binding = Binding(spec, describe_spec(spec))
        else:
            self._binding = self._load_binding()

    # ------------------------------------------------------------------ public: lifecycle
    def start(self) -> "PTTManager":
        """Returns immediately. Hooks and controller support start in the background."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stopping.clear()
        self._thread = threading.Thread(target=self._pump, name="PTT-dispatch", daemon=True)
        self._thread.start()
        threading.Thread(target=self._boot, name="PTT-boot", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stopping.set()
        for b in list(self._backends):
            try:
                b.stop()
            except Exception:
                pass
        self._backends.clear()
        self._q.put(_STOP)
        if self._thread is not None:
            self._thread.join(2.0)

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """True once every input source has either started or reported why it can't."""
        return self._ready.wait(timeout)

    # ------------------------------------------------------------------ public: state
    @property
    def binding(self) -> Binding:
        return self._binding

    @property
    def active(self) -> bool:
        """True while push-to-talk is held down."""
        return self._active

    def status(self) -> dict:
        """{'keyboard': 'ok', 'mouse': 'ok (polling only, ...)', 'joystick': 'unavailable: ...'} for a settings screen."""
        out = dict(self._status)
        for b in list(self._backends):
            note = getattr(b, "note", "")
            out[b.name] = "ok" + (" (%s)" % note if note else "")
        return out

    def devices(self) -> list:
        """Connected controllers: [{'name', 'id', 'buttons', 'hats', 'axes'}, ...]"""
        out = []
        for b in list(self._backends):
            out += b.devices()
        return out

    def diagnostics(self) -> list:
        """Raw live state of every controller SDL sees (buttons down, hats, axes), as text lines."""
        out = []
        for b in list(self._backends):
            fn = getattr(b, "diagnostics", None)
            if fn:
                out += fn()
        return out

    def feed(self, event: InputEvent) -> None:
        """Hand in an input event (used by the sources; also handy for tests or for a custom device)."""
        self._q.put(event)

    # ------------------------------------------------------------------ public: bindings
    def set_binding(self, spec: str, label: str = "", save: bool = True) -> Binding:
        """Bind directly, e.g. set_binding("mouse:x2") or set_binding("f")."""
        spec = normalize_spec(spec)
        b = Binding(spec, label or describe_spec(spec))
        self._sync(lambda: self._apply_binding(b, save))
        return b

    def rebind(self, timeout: float = 15.0, on_done: Optional[Callable[[RebindSession], None]] = None,
               include_axes: Optional[bool] = None, allow_primary_mouse: bool = False) -> RebindSession:
        """Listen for the next key / button pressed on ANY connected device, make it the push-to-talk input and save it.

        Returns at once; use session.wait() to block, or on_done(session) to be called (on the dispatcher thread).
        Esc cancels. While listening, push-to-talk itself is paused.
          * Things already held when you call this are ignored until released, so the click or Enter that opened
            your "Rebind" button can't be captured.
          * Left and right mouse buttons are not captured (allow_primary_mouse=True to allow them).
          * Axes (paddles wired as axes, triggers, analogue handbrakes) ARE captured by default, but only when they snap
            from rest to pressed in a fraction of a second, so turning the steering wheel does not count.
            include_axes=False switches axes off completely (pedals can otherwise be captured: press Esc and try again).
        """
        if include_axes is None:
            include_axes = REBIND_ACCEPTS_AXES
        session = RebindSession(self, timeout, include_axes, allow_primary_mouse, on_done)
        self._post(lambda: self._begin_rebind(session))
        return session

    def cancel_rebind(self, session: Optional[RebindSession] = None) -> None:
        def job():
            if self._rebind is not None and (session is None or session is self._rebind):
                self._end_rebind(None, "cancelled")
        self._post(job)

    # ------------------------------------------------------------------ boot / dispatcher thread
    def _boot(self):
        for cls, enabled in self._enabled:
            if not enabled:
                continue
            if self._stopping.is_set():
                break
            backend = cls(self.feed, self._log)
            try:
                backend.start()
            except Exception as e:
                self._status[cls.name] = "unavailable: %s: %s" % (type(e).__name__, e)
                self._log("%s %s input is off (%s: %s). %s" % (TAG, cls.name.capitalize(), type(e).__name__, e, cls.hint))
                continue
            self._backends.append(backend)
            self._status[cls.name] = "ok"
            if getattr(backend, "note", ""):
                self._log("%s %s: %s. %s" % (TAG, cls.name.capitalize(), backend.note, cls.hint))
            if self._stopping.is_set():
                backend.stop()
        self._log("%s Ready. Push-to-talk = %s" % (TAG, self._binding))
        self._ready.set()

    def _pump(self):
        while True:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            try:
                if isinstance(item, InputEvent):
                    self._handle(item)
                elif item is not None:
                    item()
                rb = self._rebind
                if rb is not None and time.monotonic() >= rb.deadline:
                    self._end_rebind(None, "timeout")
            except Exception:
                self._log("%s internal error:\n%s" % (TAG, traceback.format_exc()))
        if self._rebind is not None:
            self._end_rebind(None, "stopped")
        if self._active:                                    # never leave the microphone open
            self._active = False
            self._call(self._on_release)

    def _post(self, fn):
        """Run fn on the dispatcher thread (or right here if it isn't running)."""
        t = self._thread
        if t is not None and t.is_alive() and threading.current_thread() is not t:
            self._q.put(fn)
        else:
            fn()

    def _sync(self, fn):
        """Like _post but waits for the result."""
        t = self._thread
        if t is None or not t.is_alive() or threading.current_thread() is t:
            return fn()
        box, done = {}, threading.Event()

        def job():
            try:
                box["r"] = fn()
            except BaseException as e:                      # handed back to the caller
                box["e"] = e
            finally:
                done.set()
        self._q.put(job)
        done.wait(5.0)
        if "e" in box:
            raise box["e"]
        return box.get("r")

    def _call(self, fn, *args):
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            self._log("%s callback error:\n%s" % (TAG, traceback.format_exc()))

    # ------------------------------------------------------------------ the state machine (dispatcher thread only)
    def _handle(self, ev: InputEvent):
        key = (ev.spec, ev.origin)
        if ev.kind == "down":
            if key in self._held:                           # keyboard auto-repeat: not a new press
                return
            self._held.add(key)
        else:
            if key not in self._held:
                return
            self._held.discard(key)
            if not any(s == ev.spec for s, _ in self._held):
                self._ignore.discard(ev.spec)               # released once: it may trigger again
        self._call(self._on_raw, ev)
        if ev.kind == "down" and self._rebind is not None:
            self._rebind.last_seen = ev.label          # even for inputs that are NOT accepted (left click, pedals)
        if self._rebind is not None:
            if ev.kind == "down":
                self._offer_to_rebind(ev)
            return
        self._refresh_active()

    def _refresh_active(self):
        spec = self._binding.spec
        want = any(s == spec and s not in self._ignore for s, _ in self._held)
        if want != self._active:
            self._active = want
            self._call(self._on_press if want else self._on_release)

    def _apply_binding(self, binding: Binding, save: bool) -> bool:
        if self._active and binding.spec != self._binding.spec:
            self._active = False
            self._call(self._on_release)
        if binding.spec != self._binding.spec and any(s == binding.spec for s, _ in self._held):
            self._ignore.add(binding.spec)                  # still held from before: needs a fresh press
        self._binding = binding
        return self._save() if save else True

    # ------------------------------------------------------------------ rebind
    def _begin_rebind(self, session: RebindSession):
        if self._rebind is not None:
            self._end_rebind(None, "replaced")
        if self._active:
            self._active = False
            self._call(self._on_release)
        self._ignore |= {s for s, _ in self._held}
        session.deadline = time.monotonic() + session.timeout
        self._rebind = session
        self._log("%s Rebind: press the key or button you want for push-to-talk (Esc cancels)..." % TAG)

    def _offer_to_rebind(self, ev: InputEvent):
        rb = self._rebind
        if ev.spec in self._ignore:
            return
        if ev.spec == CANCEL_SPEC:
            self._end_rebind(None, "cancelled")
            return
        if ev.control == "axis" and (not rb.include_axes or not ev.snap):
            return                                          # a slow axis move is a steering turn, not a paddle press
        if ev.spec in PRIMARY_MOUSE and not rb.allow_primary_mouse:
            return
        binding = Binding(ev.spec, ev.label)
        rb.saved = self._apply_binding(binding, save=True)
        self._log("%s Push-to-talk is now: %s%s" % (TAG, binding, "" if rb.saved else " (could not save it to disk)"))
        self._end_rebind(binding, "captured")

    def _end_rebind(self, binding: Optional[Binding], reason: str):
        rb, self._rebind = self._rebind, None
        if rb is None:
            return
        rb.binding, rb.reason = binding, reason
        rb._event.set()
        if reason in ("timeout", "cancelled"):
            self._log("%s Rebind %s. Push-to-talk stays: %s" % (TAG, reason, self._binding))
        self._call(rb.on_done, rb)

    # ------------------------------------------------------------------ persistence
    def _load_binding(self) -> Binding:
        try:
            with open(self.settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)["ptt"]
            spec = normalize_spec(str(data["spec"]))
            return Binding(spec, str(data.get("label") or describe_spec(spec)))
        except FileNotFoundError:
            pass
        except Exception as e:
            self._log("%s Could not read %s (%s: %s). Using the default." % (TAG, self.settings_file, type(e).__name__, e))
        legacy = str(getattr(config, "PTT_KEY", "") or "").strip()         # the old config.py setting
        if legacy:
            if "+" in legacy and legacy != "+":
                legacy = legacy.split("+")[-1]
                self._log("%s PTT_KEY combinations are not supported, using '%s' only." % (TAG, legacy))
            spec = normalize_spec(legacy)
            return Binding(spec, describe_spec(spec))
        return Binding(DEFAULT_SPEC, describe_spec(DEFAULT_SPEC))

    def _save(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.settings_file) or ".", exist_ok=True)
            tmp = self.settings_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "ptt": {"spec": self._binding.spec, "label": self._binding.label}}, f, indent=2)
            os.replace(tmp, self.settings_file)
            return True
        except OSError as e:
            self._log("%s Could not save %s (%s)" % (TAG, self.settings_file, e))
            return False


# =====================================================================================
#  Command line: test your hardware without starting the whole engineer
# =====================================================================================
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Test the Cesium push-to-talk input.")
    ap.add_argument("--list", action="store_true", help="show which input sources work and which controllers are connected")
    ap.add_argument("--rebind", action="store_true", help="pick a new push-to-talk key/button, then keep running")
    ap.add_argument("--axes", action="store_true", help="with --rebind: accept axes (this is now the default)")
    ap.add_argument("--no-axes", action="store_true", help="with --rebind: ignore pedals / triggers / axis paddles")
    ap.add_argument("--raw", action="store_true", help="print every key / button press (find out what your wheel sends)")
    ap.add_argument("--diag", action="store_true", help="print pygame / SDL info and the live raw state of every controller")
    ap.add_argument("--settings", default=None, help="use another settings file")
    a = ap.parse_args(argv)

    def raw(ev):
        extra = "" if ev.control != "axis" else ("  (snap)" if ev.snap else "  (slow move)")
        print("   %-4s %-34s %s%s" % (ev.kind, ev.spec, ev.label, extra))

    mgr = PTTManager(on_press=lambda: print(">>> PUSH-TO-TALK DOWN  (microphone open)"),
                     on_release=lambda: print("<<< PUSH-TO-TALK UP    (microphone closed)"),
                     settings_file=a.settings, on_raw=raw if a.raw else None)
    mgr.start()
    mgr.wait_ready(10)
    time.sleep(1.5)                                  # let the controller thread enumerate devices
    try:
        if a.list:
            for name, state in mgr.status().items():
                print("%-9s %s" % (name, state))
            devs = mgr.devices()
            print("\nControllers: %s" % ("none found" if not devs else ""))
            for d in devs:
                print("  %s   id %s   %d buttons, %d hats, %d axes" % (d["name"], d["id"], d["buttons"], d["hats"], d["axes"]))
            print("\nPush-to-talk is bound to: %s   [%s]" % (mgr.binding, mgr.binding.spec))
            return 0
        if a.diag:
            for name, state in mgr.status().items():
                print("%-9s %s" % (name, state))
            print("\nHold buttons / move axes on your wheel; Ctrl+C to quit.\n")
            while True:
                lines = mgr.diagnostics()
                print("\n".join(lines) if lines else "  (no controllers)")
                print("-" * 60)
                time.sleep(1.0)
        if a.rebind:
            s = mgr.rebind(timeout=20, include_axes=False if a.no_axes else None)
            got = s.wait(25)
            print("Bound to: %s" % got if got else "No change (%s)." % (s.reason or "no input"))
        print("Push-to-talk = %s.  Press it to test, Ctrl+C to quit." % mgr.binding)
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        mgr.stop()


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------------------
# PACKAGING
#   Add to requirements.txt:   mouse   and   pygame   (keyboard is already there; pygame-ce also works)
#
#   PyInstaller: the imports are inside functions but still found by the analysis. If the built exe reports
#                "Joystick input is off", add  --collect-all pygame  (it bundles the SDL2 DLLs).
#   Nuitka:      add  --include-package=pygame --include-package=keyboard --include-package=mouse
#
# Settings are stored in %APPDATA%\CesiumAIRaceEngineer\ptt_binding.json in packaged builds (a one-file build unpacks
# into a temp folder that is deleted on exit) and in <project>/data/ptt_binding.json otherwise.
# Set PTT_SETTINGS_FILE in config.py to put it somewhere else.
# ---------------------------------------------------------------------------------------
