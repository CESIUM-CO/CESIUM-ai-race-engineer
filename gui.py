"""
gui.py - Cesium AI Race Engineer: control window + system tray (v0.1.0-beta).

WHAT YOU GET
  * One small window: an ON/OFF switch for the telemetry + AI radio loop, a language selector, the push-to-talk binding
    (with a "Change..." button that uses ptt_manager), AI / voice status, a log viewer, and a button that starts the
    in-game spotter setup wizard (installer_gui.py).
  * A tray icon with Show / Radio on-off / Quit. It is optional: it needs `pystray` and `Pillow`. Without them the app
    is window-only and closing the window quits.
  * Runtime helpers for a windowed (--noconsole) build: print() that can never crash (Arabic paths, emoji), a log file,
    and a single-instance lock.

THREADING RULES
  Tk lives on the main thread only. Anything slow (starting / stopping the radio, quitting) runs on a worker thread.
  Tray callbacks and push-to-talk callbacks never touch Tk: they put a message in a queue that the main thread polls
  every 100 ms.

WHAT THE BACKEND MUST PROVIDE (main_pitwall.Engineer does)
    start()                 begin the telemetry/AI loop and return quickly
    stop()                  stop it; may block for seconds (the GUI always calls it from a worker thread)
    is_running() -> bool
  optional (the GUI skips whatever is missing):
    status() -> "waiting" | "live"
    details() -> {"ai": idle|loading|ready|failed, "voice": off|loading|ready|failed}
    ptt_label() -> str                  rebind_ptt(on_done)     on_done(label or None), may be called from any thread
    set_language(code)
    quit_requested() -> bool            (the quit hotkey was pressed)
    shutdown()                          final cleanup when the program exits

TRY IT WITHOUT THE GAME:   python gui.py        (a fake backend, to look at the window and the tray)

LANGUAGE: the selector changes THIS WINDOW. The engineer's spoken phrases and speech recognition are English
(Whisper runs with language="en", every call in alerts.py / strategy.py / spotter.py is an English sentence). The backend
gets set_language(code) so that translated phrases can be plugged in later without touching this file.
"""
from __future__ import annotations

import collections
import copy
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk
from typing import Optional

try:
    import config
except Exception:                      # lets this file run on its own (python gui.py)
    config = None

import bug_reporter
import user_settings

# Wheels, gamepads and side buttons must keep working while the game has the focus: ask SDL for background joystick
# events BEFORE pygame is imported anywhere (ptt_manager imports it later, on its own thread).
os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"

__all__ = ["run_gui", "setup_runtime", "acquire_single_instance", "tell_user", "system_language",
           "Settings", "SettingsTab", "ControlPanel", "LOG", "tr", "LANGUAGES"]

APP_TITLE = "Cesium AI Race Engineer"
APP_DIR_NAME = "CesiumAIRaceEngineer"
DEFAULT_VERSION = "v0.1.0-beta"
TAG = "[GUI]"


# =====================================================================================
#  Environment helpers
# =====================================================================================
def is_frozen():
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def settings_dir():
    """Where settings and logs live. Same rule as ptt_manager / updater: a packaged build unpacks into a temp folder that
    is wiped on exit, so it keeps its files in the user profile (any user name works, Arabic included)."""
    if is_frozen():
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIR_NAME)
    return os.path.join(getattr(config, "BASE_DIR", None) or os.path.dirname(os.path.abspath(__file__)), "data")


def resource_dirs():
    dirs = []
    for d in (getattr(sys, "_MEIPASS", None),
              os.path.dirname(os.path.abspath(__file__)),
              os.path.dirname(os.path.abspath(sys.argv[0] or "")),
              os.path.dirname(os.path.abspath(sys.executable)),
              os.getcwd()):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def system_language():
    """'ar' / 'it' / 'en' from the Windows display language."""
    try:
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        return {0x01: "ar", 0x10: "it"}.get(primary, "en")
    except Exception:
        return "en"


def default_installer_command():
    """How to start the setup wizard: as a separate process, because it needs a Tk window of its own."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--installer"]
    if "__compiled__" in globals():
        return [os.path.abspath(sys.argv[0]), "--installer"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "installer_gui.py")
    return [sys.executable, path] if os.path.isfile(path) else None


def _bmp(text):
    """Tk (before 8.6.10 especially) can choke on characters above U+FFFF, such as the radio emoji."""
    return "".join(c if ord(c) <= 0xFFFF else "?" for c in text)


# =====================================================================================
#  Logging: print() must work (and never crash) in a --noconsole build
# =====================================================================================
class LogHub:
    """Collects everything the program prints: a log file on disk plus a ring buffer for the log window."""

    def __init__(self, max_chunks=4000):
        self._lock = threading.Lock()
        self._chunks = collections.deque(maxlen=max_chunks)
        self._n = 0
        self.file = None
        self.path = None

    def open_file(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            if os.path.getsize(path) > 2 * 1024 * 1024:           # keep one old log, never grow forever
                os.replace(path, path + ".1")
        except OSError:
            pass
        self.file = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        self.path = path
        self.file.write("\n===== %s  %s =====\n" % (APP_TITLE, time.strftime("%Y-%m-%d %H:%M:%S")))

    def write(self, text):
        text = text.replace("\r", "")
        if not text:
            return
        with self._lock:
            self._n += 1
            self._chunks.append((self._n, text))
            if self.file is not None:
                try:
                    self.file.write(text)
                except Exception:
                    pass

    def since(self, n):
        """(newest id, text written after id n)"""
        with self._lock:
            return self._n, "".join(t for k, t in self._chunks if k > n)


LOG = LogHub()


class _Tee:
    """Replaces sys.stdout / sys.stderr: original stream (if there is one) + log file + log window."""
    encoding, errors = "utf-8", "replace"

    def __init__(self, original, sink):
        self._orig, self._sink = original, sink

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        self._sink(text)
        if self._orig is not None:
            try:
                self._orig.write(text)
            except Exception:                     # e.g. a legacy console code page that can't show Arabic or emoji
                pass
        return len(text)

    def flush(self):
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def writable(self):
        return True


_runtime_ready = False


def setup_runtime():
    """Call this first thing in main(). Returns the LogHub."""
    global _runtime_ready
    if _runtime_ready:
        return LOG
    _runtime_ready = True
    for stream in (sys.stdout, sys.stderr):           # both are None in a --noconsole build
        if stream is not None:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    try:
        LOG.open_file(os.path.join(settings_dir(), "logs", "cesium.log"))
    except OSError:
        pass
    sys.stdout = _Tee(sys.stdout, LOG.write)
    sys.stderr = _Tee(sys.stderr, LOG.write)
    if LOG.file is not None:                          # hard crashes (native code) leave a trace too
        try:
            import faulthandler
            faulthandler.enable(file=LOG.file)
        except Exception:
            pass
    return LOG


# =====================================================================================
#  Single instance
# =====================================================================================
def acquire_single_instance(name="Local\\CesiumAIRaceEngineer_SingleInstance", wait_s=6.0):
    """A handle to keep alive, or None if another copy is running.
    It retries for a few seconds so that the copy started by the updater (while the old one is still exiting) gets in."""
    if os.name != "nt":
        return object()
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    deadline = time.monotonic() + wait_s
    while True:
        handle = k32.CreateMutexW(None, False, name)
        already = ctypes.get_last_error() == 183              # ERROR_ALREADY_EXISTS
        if handle and not already:
            return handle
        if handle:
            k32.CloseHandle(handle)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def tell_user(text):
    """A plain message box, usable before the main window exists."""
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_TITLE, text, parent=root)
        root.destroy()
    except Exception:
        print(text)


# =====================================================================================
#  Settings
# =====================================================================================
class Settings:
    DEFAULTS = {"language": None, "auto_start": True, "close_to_tray": True}

    def __init__(self, path=None):
        self.path = path or os.path.join(settings_dir(), "gui_settings.json")
        self.data = dict(self.DEFAULTS)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update({k: v for k, v in loaded.items() if k in self.DEFAULTS})
        except FileNotFoundError:
            pass
        except Exception as e:
            print("%s Could not read %s (%s: %s). Using defaults." % (TAG, self.path, type(e).__name__, e))

    def get(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def set(self, key, value):
        self.data[key] = value
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            print("%s Could not save settings (%s)" % (TAG, e))


# =====================================================================================
#  Languages (this window only, see the module docstring)
# =====================================================================================
LANGUAGES = {                       # code: (name shown in the dropdown, right-to-left)
    "en": ("English", False),
    "ar": ("العربية", True),
    "it": ("Italiano", False),
}

STRINGS = {
    "en": {
        "radio": "Radio", "st_off": "Radio is off", "st_starting": "Starting...",
        "st_waiting": "Radio is on, waiting for Assetto Corsa", "st_live": "Live: your engineer is following the race",
        "st_stopping": "Stopping...", "ai": "AI brain", "voice": "Voice",
        "svc_ready": "ready", "svc_loading": "loading", "svc_failed": "unavailable (rules only)", "svc_off": "off",
        "voice_natural": "natural", "voice_loading": "Windows voice (natural one loading)", "voice_windows": "Windows voice",
        "ptt": "Push-to-talk", "ptt_change": "Change...", "ptt_press": "Press a key or button... (Esc cancels)",
        "language": "Language",
        "lang_note": "The language changes this window. The engineer still speaks and listens in English.",
        "opt_auto": "Switch the radio on when the program starts",
        "opt_tray": "Closing the window keeps it running in the tray",
        "btn_log": "Log", "btn_installer": "Install in-game app...", "btn_quit": "Quit", "btn_bug": "Report a bug",
        "tray_show": "Show window", "tray_radio": "Radio on", "tray_quit": "Quit",
        "tray_hint": "Cesium is still running in the tray.",
        "log_title": "Log", "log_copy": "Copy all", "log_folder": "Open folder",
        "quit_title": "Quit", "quit_ask": "Stop the engineer and quit?",
        "already_running": "Cesium is already running. Look for its icon in the tray.",
        "installer_missing": "The setup wizard could not be found.", "error_title": "Error",
        "tab_radio": "Radio", "tab_settings": "Settings",
        "set_ptt_box": "Push-to-talk", "set_audio_box": "Audio and voice",
        "set_warn_box": "Warnings (per car class)",
        "set_mic": "Microphone", "set_speaker": "Speaker / headset", "set_voice": "Voice",
        "set_default_dev": "System default", "set_preview": "Preview",
        "set_edit_class": "Edit settings for", "set_use_class": "Use in game",
        "set_auto_class": "Auto-detect from the car",
        "set_bsi_on": "Blind-spot calls (car on your left / right)",
        "set_bsi_level": "Blind-spot sensitivity", "set_bsi_delay": "Blind-spot call delay",
        "set_fuel": "Fuel warning margin", "set_eng_rpm": "Engine: rev limit",
        "set_eng_patience": "Engine: patience",
        "set_fuel_learn": "Let the engineer learn my fuel margin by itself",
        "set_hint": ("Fuel: with auto-learning on, the slider is only the starting value. "
                     "Engine: the game gives no engine temperature, so these watch over-revving. "
                     "Lower patience = he complains sooner."),
        "btn_save": "Save", "btn_reset": "Reset to defaults",
        "set_unsaved": "Unsaved changes", "set_saved": "Saved and applied.",
        "set_save_failed": "Could not save (see the log).",
        "set_dev_saved": "Audio device applied and saved.",
        "set_dev_failed": "That device could not be opened. Using the system default.",
        "set_reset": "Reset to defaults",
        "set_reset_ask": "Reset ALL warning thresholds (every car class) to the defaults?\n"
                         "Audio, voice and push-to-talk stay as they are.",
        "set_reset_done": "Defaults restored. Press Save to keep them.",
        "set_tire_psi": "Tire Pressure Target (PSI)",
        "set_psi_hint": "Warns at {warn} PSI, calls you to box at {box} PSI",
        "btn_cancel": "Cancel",
        "ptt_dlg_title": "Push-to-talk",
        "ptt_press_any": "Press any key, mouse button, or controller button...",
        "ptt_dlg_hint": ("Keyboard keys, mouse middle and side buttons, wheel and gamepad buttons, and headset "
                         "buttons that act as media keys all work. Left / right mouse buttons and pedals are "
                         "ignored. Esc cancels."),
        "ptt_last_seen": "Last input seen: %s",
        "ptt_unavailable": ("Push-to-talk input is not running (it is still starting, or the keyboard, numpy or "
                            "sounddevice package is missing). Check the log, then try again."),
    },
    "ar": {
        "radio": "الراديو", "st_off": "الراديو متوقف", "st_starting": "جارٍ التشغيل...",
        "st_waiting": "الراديو يعمل، بانتظار Assetto Corsa", "st_live": "مباشر: المهندس يتابع سباقك",
        "st_stopping": "جارٍ الإيقاف...", "ai": "الذكاء الاصطناعي", "voice": "الصوت",
        "svc_ready": "جاهز", "svc_loading": "جارٍ التحميل", "svc_failed": "غير متاح (القواعد فقط)", "svc_off": "متوقف",
        "voice_natural": "طبيعي", "voice_loading": "صوت ويندوز (الصوت الطبيعي قيد التحميل)", "voice_windows": "صوت ويندوز",
        "ptt": "زر التحدث", "ptt_change": "تغيير...", "ptt_press": "اضغط أي زر... (Esc للإلغاء)",
        "language": "اللغة",
        "lang_note": "تغيّر اللغة هذه النافذة فقط. المهندس ما زال يتحدث ويستمع بالإنجليزية.",
        "opt_auto": "تشغيل الراديو عند فتح البرنامج",
        "opt_tray": "إغلاق النافذة يُبقي البرنامج يعمل في منطقة الإشعارات",
        "btn_log": "السجل", "btn_installer": "تثبيت تطبيق اللعبة...", "btn_quit": "خروج", "btn_bug": "الإبلاغ عن خطأ",
        "tray_show": "إظهار النافذة", "tray_radio": "تشغيل الراديو", "tray_quit": "خروج",
        "tray_hint": "ما زال Cesium يعمل في منطقة الإشعارات.",
        "log_title": "السجل", "log_copy": "نسخ الكل", "log_folder": "فتح المجلد",
        "quit_title": "خروج", "quit_ask": "إيقاف المهندس والخروج؟",
        "already_running": "Cesium يعمل بالفعل. ابحث عن أيقونته في منطقة الإشعارات.",
        "installer_missing": "تعذر العثور على معالج التثبيت.", "error_title": "خطأ",
    },
    "it": {
        "radio": "Radio", "st_off": "Radio spenta", "st_starting": "Avvio...",
        "st_waiting": "Radio accesa, in attesa di Assetto Corsa", "st_live": "In diretta: l'ingegnere segue la gara",
        "st_stopping": "Arresto...", "ai": "Cervello IA", "voice": "Voce",
        "svc_ready": "pronto", "svc_loading": "caricamento", "svc_failed": "non disponibile (solo regole)", "svc_off": "spento",
        "voice_natural": "naturale", "voice_loading": "voce Windows (quella naturale si sta caricando)", "voice_windows": "voce Windows",
        "ptt": "Tasto radio", "ptt_change": "Cambia...", "ptt_press": "Premi un tasto o pulsante... (Esc annulla)",
        "language": "Lingua",
        "lang_note": "La lingua cambia solo questa finestra. L'ingegnere parla e ascolta ancora in inglese.",
        "opt_auto": "Accendi la radio all'avvio del programma",
        "opt_tray": "Chiudendo la finestra resta attivo nella barra di sistema",
        "btn_log": "Log", "btn_installer": "Installa app di gioco...", "btn_quit": "Esci", "btn_bug": "Segnala un bug",
        "tray_show": "Mostra finestra", "tray_radio": "Radio accesa", "tray_quit": "Esci",
        "tray_hint": "Cesium continua a funzionare nella barra di sistema.",
        "log_title": "Log", "log_copy": "Copia tutto", "log_folder": "Apri cartella",
        "quit_title": "Esci", "quit_ask": "Fermare l'ingegnere e uscire?",
        "already_running": "Cesium è già in esecuzione. Cerca la sua icona nella barra di sistema.",
        "installer_missing": "Impossibile trovare la procedura di installazione.", "error_title": "Errore",
    },
}


def tr(lang, key):
    return STRINGS.get(lang, {}).get(key) or STRINGS["en"].get(key, key)


COLORS = {"off": "#9aa0a6", "starting": "#d98e04", "stopping": "#d98e04", "waiting": "#2563eb", "live": "#1a7f37"}


# =====================================================================================
#  The ON/OFF switch (ttk has none)
# =====================================================================================
class ToggleSwitch(tk.Canvas):
    W, H = 64, 32
    ON, OFF, BUSY = "#1a7f37", "#9aa0a6", "#d98e04"

    def __init__(self, master, command=None, bg="SystemButtonFace"):
        super().__init__(master, width=self.W, height=self.H, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=1)
        self._command = command
        self._on = self._busy = self._focus = False
        self.bind("<Button-1>", self._click)
        self.bind("<space>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._draw()

    def _click(self, _event=None):
        self.focus_set()
        if self._busy:
            return
        self._on = not self._on
        self._draw()
        if self._command:
            self._command(self._on)

    def _set_focus(self, value):
        self._focus = value
        self._draw()

    def set(self, on):
        if bool(on) != self._on:
            self._on = bool(on)
            self._draw()

    def set_busy(self, busy):
        if bool(busy) != self._busy:
            self._busy = bool(busy)
            self._draw()

    def _draw(self):
        self.delete("all")
        color = self.BUSY if self._busy else (self.ON if self._on else self.OFF)
        r = self.H / 2
        self.create_oval(0, 0, self.H, self.H, fill=color, outline=color)
        self.create_oval(self.W - self.H, 0, self.W, self.H, fill=color, outline=color)
        self.create_rectangle(r, 0, self.W - r, self.H, fill=color, outline=color)
        knob = r - 4
        cx = self.W / 2 if self._busy else (self.W - r if self._on else r)
        self.create_oval(cx - knob, r - knob, cx + knob, r + knob, fill="white",
                         outline="#3b82f6" if self._focus else "white", width=2)


# =====================================================================================
#  Tray icon (optional: pystray + Pillow)
# =====================================================================================
class Tray:
    RGB = {"off": (154, 160, 166), "starting": (217, 142, 4), "stopping": (217, 142, 4),
           "waiting": (37, 99, 235), "live": (26, 127, 55)}

    @staticmethod
    def available():
        try:
            import pystray  # noqa: F401
            from PIL import Image, ImageDraw  # noqa: F401
            return True
        except Exception:
            return False

    def __init__(self, panel):
        self.panel, self.icon, self._state = panel, None, None

    def _image(self, state):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((2, 2, 62, 62), fill=(32, 36, 44, 255))
        d.ellipse((17, 17, 47, 47), fill=self.RGB.get(state, self.RGB["off"]) + (255,))
        return img

    def start(self):
        try:
            import pystray
            p = self.panel
            menu = pystray.Menu(
                pystray.MenuItem(lambda item: p.t("tray_show"), lambda: p.post("show"), default=True),
                pystray.MenuItem(lambda item: p.t("tray_radio"), lambda: p.post("toggle"), checked=lambda item: p.is_on()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(lambda item: p.t("tray_quit"), lambda: p.post("quit")))
            self.icon = pystray.Icon(APP_DIR_NAME, self._image("off"), APP_TITLE, menu)
            self.icon.run_detached()
            return True
        except Exception as e:
            print("%s Tray icon unavailable (%s: %s). Window only." % (TAG, type(e).__name__, e))
            self.icon = None
            return False

    def update(self, state):
        if self.icon is None or state == self._state:
            return
        self._state = state
        try:
            self.icon.icon = self._image(state)
            self.icon.title = "%s - %s" % (APP_TITLE, self.panel.t("st_" + state))
            self.icon.update_menu()
        except Exception:
            pass

    def refresh_menu(self):
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def notify(self, text):
        if self.icon is not None:
            try:
                self.icon.notify(text, APP_TITLE)
            except Exception:
                pass

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None


# =====================================================================================
#  The push-to-talk popup and the scroll area
# =====================================================================================
class PttDialog(tk.Toplevel):
    """Modal "press a key / button" window shown while push-to-talk is being re-bound.
    The capturing itself is done by ptt_manager (global hooks: it sees the keyboard, mouse buttons, wheels, gamepads and
    media keys even while this window or the game has the focus). This window only shows what is going on."""

    def __init__(self, panel, session, sources):
        super().__init__(panel.root)
        self.panel, self.session, self._closed, self._n = panel, session, False, 0
        t = panel.t
        self.title(t("ptt_dlg_title"))
        self.resizable(False, False)
        self.transient(panel.root)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())
        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=t("ptt_press_any"), font=("Segoe UI", 12, "bold"),
                  wraplength=380, justify="center").pack()
        ttk.Label(body, text=t("ptt_dlg_hint"), wraplength=380, justify="center",
                  foreground="#555").pack(pady=(10, 0))
        self.lbl_src = ttk.Label(body, text=self._sources_text(sources), wraplength=380, justify="center",
                                 foreground="#777", font=("Segoe UI", 8))
        self.lbl_src.pack(pady=(10, 0))
        self.lbl_seen = ttk.Label(body, text="", wraplength=380, justify="center")
        self.lbl_seen.pack(pady=(14, 0))
        self.lbl_left = ttk.Label(body, text="", foreground="#555")
        self.lbl_left.pack(pady=(4, 0))
        ttk.Button(body, text=t("btn_cancel"), command=self.cancel).pack(pady=(14, 0))
        self.update_idletasks()
        r = panel.root
        x = r.winfo_rootx() + (r.winfo_width() - self.winfo_width()) // 2
        y = r.winfo_rooty() + (r.winfo_height() - self.winfo_height()) // 3
        self.geometry("+%d+%d" % (max(0, x), max(0, y)))
        try:
            self.wait_visibility()
            self.grab_set()                     # modal: the main window can't be clicked until this is closed
        except tk.TclError:
            pass
        self.focus_set()                        # focus the window, not the Cancel button (Space would press it)
        self._tick()

    @staticmethod
    def _sources_text(info):
        """Which input sources really run, and WHY one does not, so a silent failure is visible."""
        if not info:
            return ""
        st, devs = info.get("status", {}), info.get("devices", [])
        on = lambda name: str(st.get(name, "")).startswith("ok")
        word = lambda name: "OK" if on(name) else "OFF"
        pads = ", ".join(devs) if devs else ("none found" if on("joystick") else "OFF")
        lines = ["Keyboard: %s    Mouse: %s    Controllers: %s" % (word("keyboard"), word("mouse"), pads)]
        for name in ("keyboard", "mouse", "joystick"):
            s = str(st.get(name, ""))
            if s and s != "ok":
                lines.append("%s: %s" % (name, s[:150]))           # e.g. "unavailable: ModuleNotFoundError: No module named 'pygame'"
        if not all(on(n) for n in ("keyboard", "mouse", "joystick")):
            lines.append("Fix:  pip install -r requirements.txt   (then restart)")
        elif not devs:
            lines.append("No controller found: is it listed in Windows 'Set up USB game controllers' (joy.cpl)?")
        return "\n".join(lines)

    def _tick(self):
        if self._closed:
            return
        self._n += 1
        if self._n % 10 == 0:               # once a second: a controller can show up just after the window opens
            self.lbl_src.configure(text=self._sources_text(self.panel._call("ptt_sources", None)))
        seen = getattr(self.session, "last_seen", "")
        self.lbl_seen.configure(text=(self.panel.t("ptt_last_seen") % seen) if seen else "")
        left = getattr(self.session, "deadline", float("inf")) - time.monotonic()
        self.lbl_left.configure(text=("%d s" % max(0, int(left + 0.99))) if left < 3600 else "")
        self.after(100, self._tick)

    def cancel(self):
        """Esc, the Cancel button or the window's X. The window closes when the backend reports 'done'."""
        try:
            self.session.cancel()
        except Exception:
            self.panel.post("ptt_done", None)

    def close(self):
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass


class _Scrollable(ttk.Frame):
    """A frame whose content scrolls vertically. Put widgets in `.inner`. The mouse wheel works over it."""

    def __init__(self, master):
        super().__init__(master)
        bg = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=bg)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind_all("<MouseWheel>", self._wheel, add="+")
        # a drop-down box would otherwise change its value when the wheel passes over it
        self.bind_class("TCombobox", "<MouseWheel>", self._combo_wheel)

    def _wheel(self, e):
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except Exception:                       # e.g. while a combobox list is open
            return
        if w is None or not str(w).startswith(str(self)):
            return
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

    def _combo_wheel(self, e):
        self._wheel(e)
        return "break"


# =====================================================================================
#  The Settings tab
# =====================================================================================
class SettingsTab(ttk.Frame):
    """Edits a plain dict (user_settings.load()). Save writes it to disk and asks the backend to apply it now
    (Engineer.apply_settings in main_pitwall.py). The boxes live in a scrollable area; Save / Reset stay pinned."""

    SLIDERS = (   # key (see user_settings.LIMITS), text key, number format
        ("spotter_level", "set_bsi_level", "{:.0f}"),
        ("spotter_enter_s", "set_bsi_delay", "{:.2f} s"),
        ("fuel_margin", "set_fuel", "{:.1f} laps"),
        ("tire_target_psi", "set_tire_psi", "{:.1f} PSI"),
        ("engine_rpm_pct", "set_eng_rpm", "{:.0f} %"),
        ("engine_patience", "set_eng_patience", "{:.0f} %"),
    )

    def __init__(self, master, panel):
        super().__init__(master, padding=(14, 10))
        self.panel = panel
        self.data = user_settings.load()
        self.klass = "race"              # the class whose values the sliders show right now
        self.loading = False
        self.sliders = {}
        self.mic_names, self.spk_names = self._device_lists()
        self._build()
        self._fill_class()
        self.retext()

    @staticmethod
    def _device_lists():
        try:
            return user_settings.list_devices("input"), user_settings.list_devices("output")
        except Exception as e:                      # no sounddevice / no audio: the dropdowns just stay empty
            print("%s Could not list audio devices (%s: %s)" % (TAG, type(e).__name__, e))
            return [], []

    # ------------------------------------------------------------------ layout
    def _build(self):
        p = self.panel
        bar = ttk.Frame(self)                       # packed first so a short window never hides Save
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        self.btn_save = ttk.Button(bar, command=self._save)
        self.btn_save.pack(side="right")
        self.btn_reset = ttk.Button(bar, command=self._reset)
        self.btn_reset.pack(side="right", padx=6)
        self.lbl_state = ttk.Label(bar, foreground="#555")
        self.lbl_state.pack(side="left")

        self.scroll = _Scrollable(self)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.inner

        # ---- 1. push-to-talk (the widgets are registered on the panel: its _rebind / _refresh_details use them) ----
        self.box_ptt = ttk.LabelFrame(body, padding=8)
        self.box_ptt.pack(fill="x", padx=(0, 6))
        self.box_ptt.columnconfigure(1, weight=1)
        p.lbl_ptt = ttk.Label(self.box_ptt)
        p.val_ptt = ttk.Label(self.box_ptt, foreground="#555", wraplength=200)
        p.btn_ptt = ttk.Button(self.box_ptt, command=p._rebind, width=12)
        p.lbl_ptt.grid(row=0, column=0, sticky="w", padx=(0, 14))
        p.val_ptt.grid(row=0, column=1, sticky="w")
        if hasattr(p.backend, "rebind_ptt"):
            p.btn_ptt.grid(row=0, column=2, sticky="e")

        # ---- 2. audio devices + voice ----
        a = self.box_audio = ttk.LabelFrame(body, padding=8)
        a.pack(fill="x", padx=(0, 6), pady=(8, 0))
        a.columnconfigure(1, weight=1)
        self.lbl_mic, self.lbl_spk, self.lbl_vox = ttk.Label(a), ttk.Label(a), ttk.Label(a)
        self.cmb_mic = ttk.Combobox(a, state="readonly")
        self.cmb_spk = ttk.Combobox(a, state="readonly")
        self.cmb_voice = ttk.Combobox(a, state="readonly")
        self.btn_preview = ttk.Button(a, command=self._preview, width=12)
        for r, (lbl, cmb) in enumerate(((self.lbl_mic, self.cmb_mic), (self.lbl_spk, self.cmb_spk),
                                        (self.lbl_vox, self.cmb_voice))):
            lbl.grid(row=r, column=0, sticky="w", pady=3, padx=(0, 14))
            cmb.grid(row=r, column=1, sticky="ew", pady=3)
        self.btn_preview.grid(row=2, column=2, padx=(8, 0))
        self.cmb_mic.bind("<<ComboboxSelected>>", lambda e: self._on_device("mic", self.cmb_mic, self.mic_names))
        self.cmb_spk.bind("<<ComboboxSelected>>", lambda e: self._on_device("speaker", self.cmb_spk, self.spk_names))
        self.cmb_voice.bind("<<ComboboxSelected>>", self._on_voice)

        # ---- 3. warning thresholds ----
        w = self.box_warn = ttk.LabelFrame(body, padding=8)
        w.pack(fill="x", padx=(0, 6), pady=(8, 0))
        w.columnconfigure(1, weight=1)
        self.lbl_edit, self.lbl_use = ttk.Label(w), ttk.Label(w)
        self.cmb_edit = ttk.Combobox(w, state="readonly")
        self.cmb_use = ttk.Combobox(w, state="readonly")
        for r, (lbl, cmb) in enumerate(((self.lbl_edit, self.cmb_edit), (self.lbl_use, self.cmb_use))):
            lbl.grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            cmb.grid(row=r, column=1, columnspan=2, sticky="ew", pady=3)
        self.cmb_edit.bind("<<ComboboxSelected>>", self._on_edit_class)
        self.cmb_use.bind("<<ComboboxSelected>>", self._on_use_class)

        self.var_bsi = tk.BooleanVar()
        self.chk_bsi = ttk.Checkbutton(w, variable=self.var_bsi, command=self._on_bsi)
        self.chk_bsi.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        row = 3
        for key, _text_key, fmt in self.SLIDERS:
            lo, hi, step = user_settings.LIMITS[key]
            var = tk.DoubleVar(value=lo)
            lbl, val = ttk.Label(w), ttk.Label(w, width=9, anchor="e")
            scale = ttk.Scale(w, from_=lo, to=hi, variable=var, command=lambda _v, k=key: self._moved(k))
            lbl.grid(row=row, column=0, sticky="w", pady=2, padx=(0, 10))
            scale.grid(row=row, column=1, sticky="ew", pady=2)
            val.grid(row=row, column=2, sticky="e", padx=(6, 0))
            self.sliders[key] = (var, step, fmt, lbl, val)
            row += 1
            if key == "tire_target_psi":                 # the limits this target produces, right under its slider
                self.lbl_psi = ttk.Label(w, foreground="#555", font=("Segoe UI", 8))
                self.lbl_psi.grid(row=row, column=1, sticky="w")
                row += 1
        self.var_learn = tk.BooleanVar()
        self.chk_learn = ttk.Checkbutton(w, variable=self.var_learn, command=self._on_learn)
        self.chk_learn.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.lbl_hint = ttk.Label(w, foreground="#777", font=("Segoe UI", 8), wraplength=400, justify="left")
        self.lbl_hint.grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def retext(self):
        """Called on start-up and whenever the window language changes."""
        t = self.panel.t
        self.box_ptt.configure(text=t("set_ptt_box"))
        self.box_audio.configure(text=t("set_audio_box"))
        self.box_warn.configure(text=t("set_warn_box"))
        self.lbl_mic.configure(text=t("set_mic"))
        self.lbl_spk.configure(text=t("set_speaker"))
        self.lbl_vox.configure(text=t("set_voice"))
        self.btn_preview.configure(text=t("set_preview"))
        self.lbl_edit.configure(text=t("set_edit_class"))
        self.lbl_use.configure(text=t("set_use_class"))
        self.chk_bsi.configure(text=t("set_bsi_on"))
        for key, text_key, _fmt in self.SLIDERS:
            self.sliders[key][3].configure(text=t(text_key))
        self.chk_learn.configure(text=t("set_fuel_learn"))
        self.lbl_hint.configure(text=t("set_hint"))
        self.btn_save.configure(text=t("btn_save"))
        self.btn_reset.configure(text=t("btn_reset"))
        self._refresh_combos()
        self._update_psi_hint()

    def _refresh_combos(self):
        t = self.panel.t
        default = t("set_default_dev")
        for cmb, names, key in ((self.cmb_mic, self.mic_names, "mic"), (self.cmb_spk, self.spk_names, "speaker")):
            cmb.configure(values=[default] + names)
            cmb.set(self.data[key] if self.data[key] in names else default)
        self.cmb_voice.configure(values=[label for _id, label in user_settings.VOICES])
        voice = self.data.get("voice") or getattr(config, "KOKORO_VOICE", "bm_george")
        self.cmb_voice.set(user_settings.voice_label(voice))
        labels = [user_settings.CLASS_LABELS[k] for k in user_settings.CLASS_ORDER]
        self.cmb_edit.configure(values=labels)
        self.cmb_edit.set(user_settings.CLASS_LABELS[self.klass])
        self.cmb_use.configure(values=[t("set_auto_class")] + labels)
        forced = self.data["forced_class"]
        self.cmb_use.set(user_settings.CLASS_LABELS[forced] if forced in user_settings.CLASS_LABELS
                         else t("set_auto_class"))

    def _update_psi_hint(self):
        """'Warns at 35 PSI, calls you to box at 40 PSI' for the target now on the slider."""
        target = float(self.data["classes"][self.klass]["tire_target_psi"])
        warn, box = user_settings.psi_limits(target, getattr(config, "TYRE_PSI_WARN_OFFSET", 8.5),
                                             getattr(config, "TYRE_PSI_BOX_OFFSET", 13.5))
        self.lbl_psi.configure(text=self.panel.t("set_psi_hint").format(warn=warn, box=box))

    def _fill_class(self):
        """Show the saved values of the class being edited."""
        self.loading = True
        prof = self.data["classes"][self.klass]
        self.var_bsi.set(bool(prof["spotter_on"]))
        for key, (var, _step, fmt, _lbl, val) in self.sliders.items():
            var.set(float(prof[key]))
            val.configure(text=fmt.format(float(prof[key])))
        self.var_learn.set(bool(self.data["fuel_learning"]))
        self.loading = False
        self._update_psi_hint()

    # ------------------------------------------------------------------ edits (they change self.data only; Save writes it)
    def _dirty(self):
        self.lbl_state.configure(text=self.panel.t("set_unsaved"))

    def _moved(self, key):
        if self.loading:
            return
        var, step, fmt, _lbl, val = self.sliders[key]
        v = round(round(var.get() / step) * step, 4)      # snap to the step (0.1 laps, 0.5 PSI, whole percent ...)
        if abs(v - var.get()) > 1e-9:
            var.set(v)
        val.configure(text=fmt.format(v))
        self.data["classes"][self.klass][key] = int(v) if step >= 1 else v
        if key == "tire_target_psi":
            self._update_psi_hint()
        self._dirty()

    def _on_bsi(self):
        if not self.loading:
            self.data["classes"][self.klass]["spotter_on"] = bool(self.var_bsi.get())
            self._dirty()

    def _on_learn(self):
        if not self.loading:
            self.data["fuel_learning"] = bool(self.var_learn.get())
            self._dirty()

    def _on_device(self, key, cmb, names):
        """A microphone / speaker was picked: apply it to the running program and save it at once (no need to press Save)."""
        i = cmb.current()                    # 0 = "System default"
        name = names[i - 1] if i > 0 else None
        self.data[key] = name
        t = self.panel.t
        result = self.panel._call("set_audio_device", None, key, name)
        if result == "failed":               # Windows would not open it: show the default again
            self.data[key] = None
            cmb.set(t("set_default_dev"))
            self.lbl_state.configure(text=t("set_dev_failed"))
        elif result == "ok":
            self.lbl_state.configure(text=t("set_dev_saved"))
        else:                                # backend without live audio (the demo): Save will write it
            self._dirty()

    def _on_voice(self, _e=None):
        i = self.cmb_voice.current()
        if i >= 0:
            self.data["voice"] = user_settings.VOICES[i][0]
            self._dirty()

    def _on_edit_class(self, _e=None):
        i = self.cmb_edit.current()
        if i >= 0:
            self.klass = user_settings.CLASS_ORDER[i]
            self._fill_class()

    def _on_use_class(self, _e=None):
        i = self.cmb_use.current()
        self.data["forced_class"] = "auto" if i <= 0 else user_settings.CLASS_ORDER[i - 1]
        self._dirty()

    # ------------------------------------------------------------------ buttons
    def _preview(self):
        """Switches to the chosen voice right now and speaks a test line (Save keeps it for next time)."""
        i = self.cmb_voice.current()
        if i >= 0:
            self.panel._call("preview_voice", None, user_settings.VOICES[i][0])

    def _save(self):
        t = self.panel.t
        if not user_settings.save(self.data):
            self.lbl_state.configure(text=t("set_save_failed"))
            return
        back = user_settings.load()          # read it back: prove that what we wrote is what the next start will read
        wrong = [k for k in ("mic", "speaker", "voice") if back[k] != self.data[k]]
        if wrong:
            print("%s Saved file does not match the window for: %s" % (TAG, ", ".join(wrong)))
            self.lbl_state.configure(text=t("set_save_failed"))
            return
        self.panel._call("apply_settings")   # Engineer.apply_settings: live, no restart
        self.lbl_state.configure(text=t("set_saved"))

    def _reset(self):
        t = self.panel.t
        if not messagebox.askyesno(t("set_reset"), t("set_reset_ask"), parent=self):
            return
        self.data["classes"] = copy.deepcopy(user_settings.DEFAULT_CLASSES)
        self.data["forced_class"] = "auto"
        self.data["fuel_learning"] = True
        self._refresh_combos()
        self._fill_class()
        self.lbl_state.configure(text=t("set_reset_done"))


# =====================================================================================
#  The window
# =====================================================================================
class ControlPanel:
    POLL_MS = 100

    def __init__(self, backend, settings=None, version=None, installer_cmd=None, hub=LOG):
        self.backend = backend
        self.settings = settings or Settings()
        self.hub = hub
        self.version = version or getattr(config, "APP_VERSION", None) or DEFAULT_VERSION
        self.installer_cmd = installer_cmd
        lang = self.settings.get("language") or system_language()
        self.lang = lang if lang in LANGUAGES else "en"

        self.ui_q = queue.Queue()
        self.busy = self.quitting = self.rebinding = self._closed = self._hinted = False
        self.ptt_dialog = None
        self._kind = "off"
        self._shown_kind = None
        self._last_details = 0.0
        self.log_win = None
        self.tray = None
        self.tray_possible = Tray.available()

        self.root = root = tk.Tk()
        root.title(APP_TITLE)
        root.geometry(self._centered(520, 780))
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_icon()
        try:
            style = ttk.Style()
            style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        except tk.TclError:
            pass
        self._build()
        self._apply_language()
        if self.tray_possible:
            self.tray = Tray(self)
            if not self.tray.start():
                self.tray = None
        self.tray_ok = self.tray is not None

    # ------------------------------------------------------------------ small helpers
    def t(self, key):
        return tr(self.lang, key)

    def post(self, *msg):
        """Thread-safe: tray / PTT / worker threads talk to the window through here."""
        self.ui_q.put(msg)

    def _call(self, name, default=None, *args):
        fn = getattr(self.backend, name, None)
        if fn is None:
            return default
        try:
            return fn(*args)
        except Exception:
            print("%s backend.%s() failed:\n%s" % (TAG, name, traceback.format_exc()))
            return default

    def _running(self):
        return bool(self._call("is_running", False))

    def is_on(self):
        return self._running()

    def _centered(self, w, h):
        k = self.root.winfo_fpixels("1i") / 96.0                 # 1.0 at 100% Windows scaling, 1.5 at 150% ...
        w, h = int(w * k), min(int(h * k), self.root.winfo_screenheight() - 90)
        return "%dx%d+%d+%d" % (w, h, (self.root.winfo_screenwidth() - w) // 2, (self.root.winfo_screenheight() - h) // 3)

    def _bg(self):
        return ttk.Style().lookup("TFrame", "background") or self.root.cget("bg")

    def _set_icon(self):
        for d in resource_dirs():
            path = os.path.join(d, "cesium.ico")
            if os.path.isfile(path):
                try:
                    self.root.iconbitmap(path)
                except tk.TclError:
                    pass
                return

    # ------------------------------------------------------------------ layout
    def _build(self):
        root, bg = self.root, None
        head = tk.Frame(root, bg="white")
        head.pack(fill="x")
        tk.Label(head, text=APP_TITLE, bg="white", font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=20, pady=(10, 0))
        tk.Label(head, text=self.version, bg="white", fg="#555", font=("Segoe UI", 9)).pack(anchor="w", padx=20, pady=(0, 14))
        ttk.Separator(root).pack(fill="x", pady=(0, 14))

        bar = ttk.Frame(root, padding=(14, 10))                      # packed first so it is never squeezed out
        bar.pack(side="bottom", fill="x")
        ttk.Separator(root).pack(side="bottom", fill="x")
        self.btn_quit = ttk.Button(bar, command=lambda: self._quit(ask=True))
        self.btn_quit.pack(side="right")
        self.btn_bug = ttk.Button(bar, command=lambda: bug_reporter.open_bug_report(self.root))
        self.btn_bug.pack(side="right", padx=6)                      # right next to Quit, on its left
        self.btn_log = ttk.Button(bar, command=self._show_log)
        self.btn_log.pack(side="left")
        self.btn_inst = ttk.Button(bar, command=self._open_installer)
        self.btn_inst.pack(side="left", padx=6)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 0))
        body = ttk.Frame(self.nb, padding=(20, 14))
        self.nb.add(body, text="Radio")
        body.columnconfigure(0, weight=1)
        bg = self._bg()

        row = ttk.Frame(body)
        row.grid(row=0, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)
        left = ttk.Frame(row)
        left.grid(row=0, column=0, sticky="w")
        self.lbl_radio = ttk.Label(left, font=("Segoe UI", 13, "bold"))
        self.lbl_radio.pack(anchor="w")
        status = ttk.Frame(left)
        status.pack(anchor="w", pady=(4, 0))
        self.dot = tk.Canvas(status, width=14, height=14, bg=bg, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 6))
        self.dot_item = self.dot.create_oval(1, 1, 13, 13, fill=COLORS["off"], outline="")
        self.lbl_status = ttk.Label(status, wraplength=280, justify="left")
        self.lbl_status.pack(side="left")
        self.switch = ToggleSwitch(row, command=self._on_switch, bg=bg)
        self.switch.grid(row=0, column=1, sticky="e")

        ttk.Separator(body).grid(row=1, column=0, sticky="ew", pady=12)

        info = ttk.Frame(body)
        info.grid(row=2, column=0, sticky="ew")
        info.columnconfigure(1, weight=1)
        self.lbl_ai, self.val_ai = ttk.Label(info), ttk.Label(info, foreground="#555", wraplength=250)
        self.lbl_voice, self.val_voice = ttk.Label(info), ttk.Label(info, foreground="#555", wraplength=250)
        self.lbl_lang = ttk.Label(info)
        self.cmb_lang = ttk.Combobox(info, state="readonly", width=16, values=[v[0] for v in LANGUAGES.values()])
        self.cmb_lang.bind("<<ComboboxSelected>>", self._on_language)
        for r, (a, b) in enumerate(((self.lbl_ai, self.val_ai), (self.lbl_voice, self.val_voice))):
            a.grid(row=r, column=0, sticky="w", pady=3, padx=(0, 14))
            b.grid(row=r, column=1, sticky="w", pady=3)
        self.lbl_lang.grid(row=3, column=0, sticky="w", pady=(10, 3), padx=(0, 14))
        self.cmb_lang.grid(row=3, column=1, sticky="w", pady=(10, 3))
        self.lbl_note = ttk.Label(body, foreground="#777", font=("Segoe UI", 8), wraplength=400, justify="left")
        self.lbl_note.grid(row=3, column=0, sticky="w", pady=(4, 0))

        ttk.Separator(body).grid(row=4, column=0, sticky="ew", pady=12)
        self.var_auto = tk.BooleanVar(value=bool(self.settings.get("auto_start")))
        self.chk_auto = ttk.Checkbutton(body, variable=self.var_auto, command=self._save_options)
        self.chk_auto.grid(row=5, column=0, sticky="w")
        self.var_tray = tk.BooleanVar(value=bool(self.settings.get("close_to_tray")))
        self.chk_tray = ttk.Checkbutton(body, variable=self.var_tray, command=self._save_options)
        if self.tray_possible:
            self.chk_tray.grid(row=6, column=0, sticky="w", pady=(4, 0))

        self.settings_tab = SettingsTab(self.nb, self)       # the Settings tab (also owns the push-to-talk widgets)
        self.nb.add(self.settings_tab, text="Settings")

    def _apply_language(self):
        t, rtl = self.t, LANGUAGES[self.lang][1]
        side, just = ("e", "right") if rtl else ("w", "left")
        self.lbl_radio.configure(text=t("radio"))
        self.lbl_status.configure(justify=just)
        self.lbl_ai.configure(text=t("ai"))
        self.lbl_voice.configure(text=t("voice"))
        self.lbl_ptt.configure(text=t("ptt"))
        self.btn_ptt.configure(text=t("ptt_change"))
        self.lbl_lang.configure(text=t("language"))
        self.lbl_note.configure(text=t("lang_note"), anchor=side, justify=just)
        self.chk_auto.configure(text=t("opt_auto"))
        self.chk_tray.configure(text=t("opt_tray"))
        self.btn_log.configure(text=t("btn_log"))
        self.btn_inst.configure(text=t("btn_installer"))
        self.btn_quit.configure(text=t("btn_quit"))
        self.btn_bug.configure(text=t("btn_bug"))
        self.nb.tab(0, text=t("tab_radio"))
        self.nb.tab(1, text=t("tab_settings"))
        self.settings_tab.retext()
        self.cmb_lang.set(LANGUAGES[self.lang][0])
        self._set_status(self._kind, force=True)
        self._last_details = 0.0

    # ------------------------------------------------------------------ status
    def _set_status(self, kind, force=False):
        self._kind = kind
        if kind == self._shown_kind and not force:
            return
        self._shown_kind = kind
        self.lbl_status.configure(text=self.t("st_" + kind))
        self.dot.itemconfigure(self.dot_item, fill=COLORS[kind])
        if self.tray is not None:
            self.tray.update(kind)

    def _sync(self):
        if self._call("quit_requested", False):
            self._quit()
            return
        if not self.busy:
            running = self._running()
            self.switch.set(running)
            if running:
                self._set_status("live" if self._call("status", "waiting") == "live" else "waiting")
            else:
                self._set_status("off")
        now = time.monotonic()
        if now - self._last_details >= 1.0:
            self._last_details = now
            self._refresh_details()

    def _refresh_details(self):
        d = self._call("details", {}) or {}
        ai, voice = d.get("ai", "idle"), d.get("voice", "off")
        self.val_ai.configure(text=self.t({"ready": "svc_ready", "loading": "svc_loading", "failed": "svc_failed"}.get(ai, "svc_off")))
        self.val_voice.configure(text=self.t({"ready": "voice_natural", "loading": "voice_loading"}.get(voice, "voice_windows")))
        if not self.rebinding:
            label = self._call("ptt_label", None)
            if label:
                self.val_ptt.configure(text=label)

    # ------------------------------------------------------------------ the main loop of the window
    def run(self):
        self.root.after(self.POLL_MS, self._poll)
        if self.settings.get("auto_start"):
            self.root.after(400, lambda: self._begin_toggle(True))
        try:
            self.root.mainloop()
        finally:
            # a tray app with no console must never linger invisibly: if cleanup hangs, end the process
            timer = threading.Timer(6.0, os._exit, args=(0,))
            timer.daemon = True
            timer.start()
        return 0

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                self._handle(self.ui_q.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            print("%s error in the window loop:\n%s" % (TAG, traceback.format_exc()))
        if not self.quitting and not self._closed:
            try:
                self._sync()
            except Exception:
                print("%s status refresh failed:\n%s" % (TAG, traceback.format_exc()))
        if not self._closed:
            self.root.after(self.POLL_MS, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "show":
            self._show_window()
        elif kind == "toggle":
            self._begin_toggle(not self._running())
        elif kind == "quit":
            self._quit()
        elif kind == "toggled":
            _, want_on, err = msg
            self.busy = False
            self.switch.set_busy(False)
            if err:
                print("%s Could not %s the radio:\n%s" % (TAG, "start" if want_on else "stop", err))
                messagebox.showerror(self.t("error_title"), err.strip().splitlines()[-1], parent=self.root)
            if not self.quitting:
                self._sync()
        elif kind == "ptt_done":
            self.rebinding = False
            if self.ptt_dialog is not None:
                self.ptt_dialog.close()
                self.ptt_dialog = None
            self.btn_ptt.state(["!disabled"])
            self._last_details = 0.0          # the next refresh shows the new button, e.g. "Mouse: Side button 2"
        elif kind == "exit":
            self._closed = True
            if self.tray is not None:
                self.tray.stop()
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ on / off
    def _on_switch(self, want_on):
        self._begin_toggle(want_on)

    def _begin_toggle(self, want_on):
        if self.quitting:
            return
        if self.busy or want_on == self._running():
            self.switch.set(self._running())
            return
        self.busy = True
        self.switch.set_busy(True)
        self._set_status("starting" if want_on else "stopping")
        threading.Thread(target=self._toggle_worker, args=(want_on,), name="RadioToggle", daemon=True).start()

    def _toggle_worker(self, want_on):
        err = None
        try:
            (self.backend.start if want_on else self.backend.stop)()
        except Exception:
            err = traceback.format_exc()
        self.post("toggled", want_on, err)

    # ------------------------------------------------------------------ language, options, push-to-talk
    def _on_language(self, _event=None):
        code = next((c for c, v in LANGUAGES.items() if v[0] == self.cmb_lang.get()), "en")
        if code == self.lang:
            return
        self.lang = code
        self.settings.set("language", code)
        self._apply_language()
        self._call("set_language", None, code)
        if self.tray is not None:
            self.tray.refresh_menu()

    def _save_options(self):
        self.settings.set("auto_start", bool(self.var_auto.get()))
        self.settings.set("close_to_tray", bool(self.var_tray.get()))

    def _rebind(self):
        if self.rebinding:
            return
        self.rebinding = True
        self.btn_ptt.state(["disabled"])
        self.val_ptt.configure(text=self.t("ptt_press"))
        info = self._call("ptt_sources", None)
        try:
            session = self.backend.rebind_ptt(lambda label: self.post("ptt_done", label))
        except Exception:
            print("%s rebind failed:\n%s" % (TAG, traceback.format_exc()))
            self.post("ptt_done", None)
            return
        if session is None:                  # push-to-talk is not running: say so instead of doing nothing
            messagebox.showwarning(self.t("error_title"), self.t("ptt_unavailable"), parent=self.root)
            return
        self.ptt_dialog = PttDialog(self, session, info)      # modal; closed by the "ptt_done" message

    # ------------------------------------------------------------------ window, close, quit
    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def _on_close(self):
        if self.tray_ok and self.var_tray.get():
            self.root.withdraw()
            if not self._hinted:
                self._hinted = True
                self.tray.notify(self.t("tray_hint"))
            return
        self._quit(ask=True)

    def _quit(self, ask=False):
        if self.quitting:
            return
        if ask and self._running() and not messagebox.askyesno(self.t("quit_title"), self.t("quit_ask"), parent=self.root):
            return
        self.quitting = True
        self.switch.set_busy(True)
        self._set_status("stopping")
        threading.Thread(target=self._quit_worker, name="Shutdown", daemon=True).start()

    def _quit_worker(self):
        try:
            if self._running():
                self.backend.stop()                       # says goodbye on the radio, up to a few seconds
        except Exception:
            print("%s error while stopping:\n%s" % (TAG, traceback.format_exc()))
        self._call("shutdown")
        self.post("exit")

    # ------------------------------------------------------------------ installer + log window
    def _open_installer(self):
        cmd = self.installer_cmd or default_installer_command()
        if not cmd:
            messagebox.showerror(self.t("error_title"), self.t("installer_missing"), parent=self.root)
            return
        try:
            subprocess.Popen(cmd, close_fds=True)
        except OSError as e:
            print("%s Could not start the setup wizard: %s" % (TAG, e))
            messagebox.showerror(self.t("error_title"), str(e), parent=self.root)

    def _show_log(self):
        if self.log_win is not None and self.log_win.winfo_exists():
            self.log_win.deiconify()
            self.log_win.lift()
            return
        win = self.log_win = tk.Toplevel(self.root)
        win.title("%s - %s" % (APP_TITLE, self.t("log_title")))
        win.geometry("780x440")
        bar = ttk.Frame(win, padding=6)
        bar.pack(side="bottom", fill="x")
        txt = tk.Text(win, wrap="word", font=("Consolas", 9), bg="#fafafa", state="disabled")
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)

        def copy_all():
            self.root.clipboard_clear()
            self.root.clipboard_append(txt.get("1.0", "end-1c"))

        def open_folder():
            if self.hub.path:
                try:
                    os.startfile(os.path.dirname(self.hub.path))
                except Exception:
                    pass

        ttk.Button(bar, text=self.t("log_copy"), command=copy_all).pack(side="right")
        ttk.Button(bar, text=self.t("log_folder"), command=open_folder).pack(side="right", padx=6)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        seen = {"n": 0}

        def pump():
            if not win.winfo_exists():
                return
            newest, new = self.hub.since(seen["n"])
            seen["n"] = newest
            if new:
                at_end = txt.yview()[1] >= 0.999
                txt.configure(state="normal")
                txt.insert("end", _bmp(new))
                lines = int(txt.index("end-1c").split(".")[0])
                if lines > 4000:
                    txt.delete("1.0", "%d.0" % (lines - 4000))
                txt.configure(state="disabled")
                if at_end:
                    txt.see("end")
            win.after(400, pump)

        pump()


# =====================================================================================
#  Entry point
# =====================================================================================
def run_gui(backend, settings=None, version=None, installer_cmd=None):
    """Show the window (and tray icon) and block until the user quits. Returns an exit code."""
    enable_dpi_awareness()
    return ControlPanel(backend, settings, version, installer_cmd).run()


# ---------------------------------------------------------------------------------------
#  Stand-alone demo:  python gui.py
# ---------------------------------------------------------------------------------------
class _DemoBackend:
    def __init__(self):
        self._on, self._t0 = False, 0.0

    def start(self):
        time.sleep(0.8)
        self._on, self._t0 = True, time.time()

    def stop(self):
        time.sleep(1.5)
        self._on = False

    def is_running(self):
        return self._on

    def status(self):
        return "live" if self._on and time.time() - self._t0 > 4 else "waiting"

    def details(self):
        return {"ai": "ready", "voice": "loading" if time.time() % 20 < 10 else "ready"}

    def ptt_label(self):
        return getattr(self, "_label", "Keyboard: F")

    def ptt_sources(self):
        return {"status": {"keyboard": "ok", "mouse": "ok", "joystick": "ok"}, "devices": ["Demo wheel"]}

    def rebind_ptt(self, on_done):
        def finish():
            self._label = "Mouse: Side button 2"
            on_done(self._label)
        timer = threading.Timer(4.0, finish)
        timer.start()

        class Session:                       # what ptt_manager.RebindSession offers the popup
            last_seen, deadline = "Mouse: Side button 2", time.monotonic() + 4.0

            def cancel(self):
                timer.cancel()
                on_done(None)
        return Session()

    def set_language(self, code):
        print("backend language ->", code)


if __name__ == "__main__":
    setup_runtime()
    print("gui.py demo: switch the radio on and off, change the language, open the log.")
    sys.exit(run_gui(_DemoBackend()))
