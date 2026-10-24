"""
installer_gui.py - Cesium AI Race Engineer: first-run setup wizard (v0.1.0-beta).

One window, one "Install" button. It runs these steps on a worker thread and shows live progress:
  1. Checks the Python libraries the app needs and pip-installs any that are missing   (source runs only, see note)
  2. Creates a Desktop shortcut to CesiumAIRaceEngineer.exe
  3. Creates a Start Menu entry (per-user, no admin needed) so the app shows up in Start search and recent items
  4. Installs the in-game PitwallSpotter app into Assetto Corsa (optional; offers a UAC relaunch if Windows refuses)

NOTES
  * A packaged (Nuitka / PyInstaller) build already contains its libraries, and pip cannot run from inside an .exe.
    So step 1 is skipped automatically in a packaged build and only does real work when run from source.
  * Windows does not let programs pin themselves to the taskbar. The Start Menu entry makes the app searchable;
    the user can then right-click it > "Pin to taskbar" once. The final message says so.
  * Shortcuts are created with PowerShell (WScript.Shell) and every path is passed through environment variables,
    never through the command line, so Arabic / accented user names and folders work.
Standard library only.
"""
import argparse
import ctypes
import importlib
import importlib.util
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import uuid
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Cesium AI Race Engineer"
VERSION = "0.1.0-beta"
APP_EXE_NAME = "CesiumAIRaceEngineer.exe"
START_MENU_FOLDER = "Cesium AI Race Engineer"
DEFAULT_AC_PATH = r"C:\Program Files (x86)\Steam\steamapps\common\assettocorsa"
APP_FOLDER = "PitwallSpotter"
PATH_MARKER = "spotter_path.txt"     # a current PitwallSpotter.py mentions this; an outdated one does not

# (pip name, import name). The import name is only CHECKED with find_spec, never imported.
REQUIRED_PACKAGES = [
    ("numpy", "numpy"),
    ("sounddevice", "sounddevice"),
    ("keyboard", "keyboard"),
    ("mouse", "mouse"),
    ("pygame", "pygame"),
    ("pystray", "pystray"),
    ("pillow", "PIL"),
    ("pywin32", "win32com"),
    ("faster-whisper", "faster_whisper"),
    ("kokoro-onnx", "kokoro_onnx"),
    ("ddgs", "ddgs"),
]

CREATE_NO_WINDOW = 0x08000000


# =====================================================================================
#  Environment helpers
# =====================================================================================
def is_pyinstaller():
    return bool(getattr(sys, "frozen", False))


def is_compiled():
    return is_pyinstaller() or "__compiled__" in globals()          # "__compiled__" = Nuitka


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def relaunch_as_admin(extra_args):
    """Start this program again through the UAC prompt. Returns True if Windows accepted the request."""
    try:
        if is_pyinstaller():
            exe, args = sys.executable, list(extra_args)
        elif is_compiled():                                          # Nuitka (onefile: argv[0] is the real exe)
            exe, args = os.path.abspath(sys.argv[0]), list(extra_args)
        else:
            exe, args = sys.executable, [os.path.abspath(__file__)] + list(extra_args)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(args), None, 1)
        return rc > 32
    except Exception:
        return False


def resource_dirs():
    """Places where the app exe / ac_app folder may live (source tree, PyInstaller, Nuitka, next to the exe)."""
    dirs = []
    for d in (getattr(sys, "_MEIPASS", None),
              os.path.dirname(os.path.abspath(__file__)),
              os.path.dirname(os.path.abspath(sys.argv[0] or "")),
              os.path.dirname(os.path.abspath(sys.executable)),
              os.getcwd()):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def find_app_exe():
    """CesiumAIRaceEngineer.exe next to this program (or the running exe itself when launched with --installer)."""
    for cand in (sys.executable, os.path.abspath(sys.argv[0] or "")):
        if cand and os.path.basename(cand).lower() == APP_EXE_NAME.lower() and os.path.isfile(cand):
            return os.path.normpath(cand)
    for d in resource_dirs():
        p = os.path.join(d, APP_EXE_NAME)
        if os.path.isfile(p):
            return os.path.normpath(p)
    return None


def known_folder(guid, fallback):
    """A Windows special folder by its KNOWNFOLDERID (follows OneDrive redirection and non-English names)."""
    if os.name != "nt":
        return fallback
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
        u = uuid.UUID(guid)
        g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_void_p()
        fn = ctypes.windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        fn.restype = ctypes.c_long
        if fn(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0 or not out.value:
            return fallback
        path = ctypes.wstring_at(out.value)
        ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.ole32.CoTaskMemFree(out)
        return path or fallback
    except Exception:
        return fallback


def desktop_dir():
    return known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
                        os.path.join(os.path.expanduser("~"), "Desktop"))


def start_menu_programs_dir():
    fallback = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                            "Microsoft", "Windows", "Start Menu", "Programs")
    return known_folder("A77F5D77-2E2B-44C3-A6A2-ABA601054A51", fallback)


def notify_shell_changed():
    """Tell Explorer / Start that files changed, so the new entries show up without a restart."""
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)   # SHCNE_ASSOCCHANGED
    except Exception:
        pass


# =====================================================================================
#  Errors
# =====================================================================================
class NeedsAdmin(Exception):
    """Windows refused to write into the game folder."""


class InstallError(Exception):
    """Something went wrong; the message is meant for the user."""


# =====================================================================================
#  Step 1: dependencies
# =====================================================================================
def missing_packages():
    out = []
    for pip_name, module in REQUIRED_PACKAGES:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append((pip_name, module))
    return out


def run_pip(pip_name, log):
    """pip install one package, streaming its output into the log. Returns True on success."""
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", pip_name]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as e:
        log(f"   Could not start pip: {e}")
        return False
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log("   " + line[:200])
    return proc.wait() == 0


def ensure_dependencies(log):
    """Returns a list of pip names that could not be installed (empty = all good)."""
    if is_compiled():
        log("Packaged build: all libraries are already bundled. Nothing to install.")
        return []
    todo = missing_packages()
    if not todo:
        log("All required libraries are already installed.")
        return []
    log("Missing libraries: " + ", ".join(p for p, _ in todo))
    try:
        probe = subprocess.run([sys.executable, "-m", "pip", "--version"], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=30,
                               creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        if probe.returncode != 0:
            raise OSError("pip is not available")
    except (OSError, subprocess.SubprocessError) as e:
        log(f"pip is not usable ({e}). Install the libraries by hand:  pip install -r requirements.txt")
        return [p for p, _ in todo]
    failed = []
    for n, (pip_name, _module) in enumerate(todo, 1):
        log(f"[{n}/{len(todo)}] Installing {pip_name} ...")
        if run_pip(pip_name, log):
            log(f"   {pip_name} installed.")
        else:
            log(f"   FAILED: {pip_name}. (No internet, or no wheel for this Python version?)")
            failed.append(pip_name)
    importlib.invalidate_caches()
    still = {p for p, _ in missing_packages()}
    return [p for p in failed if p in still]


# =====================================================================================
#  Steps 2 and 3: shortcuts
# =====================================================================================
_PS_SCRIPT = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CES_LNK);"
              "$s.TargetPath=$env:CES_TARGET;$s.WorkingDirectory=$env:CES_WD;"
              "$s.IconLocation=$env:CES_ICON;$s.Description=$env:CES_DESC;$s.Save()")


def _make_lnk_powershell(lnk, target, workdir, icon, desc):
    env = dict(os.environ, CES_LNK=lnk, CES_TARGET=target, CES_WD=workdir, CES_ICON=icon, CES_DESC=desc)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", _PS_SCRIPT],
                       env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout).decode("utf-8", "replace").strip()[:300] or "PowerShell failed")


def _make_lnk_pywin32(lnk, target, workdir, icon, desc):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        s = win32com.client.Dispatch("WScript.Shell").CreateShortCut(lnk)
        s.TargetPath, s.WorkingDirectory, s.IconLocation, s.Description = target, workdir, icon, desc
        s.save()
    finally:
        pythoncom.CoUninitialize()


def create_shortcut(lnk, target, log):
    """Creates (or replaces) a .lnk pointing at target. Raises InstallError with a clear message on failure."""
    if os.name != "nt":
        raise InstallError("Shortcuts can only be created on Windows.")
    if not os.path.isfile(target):
        raise InstallError(f"The program file was not found:\n   {target}")
    workdir = os.path.dirname(target)
    ico = os.path.join(workdir, "cesium.ico")
    icon = (ico if os.path.isfile(ico) else target) + ",0"
    desc = f"{APP_TITLE} - your sim-racing engineer"
    try:
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
    except PermissionError as e:
        raise InstallError(f"Windows would not let me create the folder:\n   {os.path.dirname(lnk)}") from e
    errors = []
    for maker in (_make_lnk_powershell, _make_lnk_pywin32):
        try:
            maker(lnk, target, workdir, icon, desc)
            if os.path.isfile(lnk):
                return lnk
            errors.append(f"{maker.__name__}: file was not created")
        except Exception as e:
            errors.append(f"{maker.__name__}: {type(e).__name__}: {e}")
    for e in errors:
        log("   " + e)
    raise InstallError(f"Could not create the shortcut:\n   {lnk}")


def remove_if_exists(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


# =====================================================================================
#  Assetto Corsa detection
# =====================================================================================
def steam_libraries():
    roots = []
    try:
        import winreg
        for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(os.path.normpath(winreg.QueryValueEx(k, value)[0]))
            except OSError:
                pass
    except ImportError:
        pass
    libs = list(roots)
    for root in roots:
        try:
            with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="ignore") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    libs.append(os.path.normpath(m.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
    return list(dict.fromkeys(libs))


def check_ac_folder(path):
    """(ok, message) for a folder the user picked."""
    if not path or not os.path.isdir(path):
        return False, "That folder does not exist."
    if os.path.isfile(os.path.join(path, "acs.exe")) or os.path.isdir(os.path.join(path, "apps", "python")):
        return True, "Assetto Corsa found."
    return False, "This doesn't look like the Assetto Corsa folder (acs.exe not found)."


def find_ac():
    candidates = [DEFAULT_AC_PATH] + [os.path.join(lib, "steamapps", "common", "assettocorsa")
                                      for lib in steam_libraries()]
    for p in candidates:
        if check_ac_folder(p)[0]:
            return os.path.normpath(p)
    return None


# =====================================================================================
#  Step 4: the in-game spotter app
# =====================================================================================
def find_source_dir():
    for d in resource_dirs():
        p = os.path.join(d, "ac_app", APP_FOLDER)
        if os.path.isfile(os.path.join(p, APP_FOLDER + ".py")):
            return p
    return None


def load_embedded():
    """The copy of the app that lives inside install_spotter.py (used if ac_app is missing or outdated)."""
    try:
        import install_spotter
        return {APP_FOLDER + ".py": install_spotter.APP_SOURCE, "manifest.ini": install_spotter.MANIFEST}
    except Exception:
        return None


def default_spotter_file():
    try:
        import config
        return config.SPOTTER_FILE
    except Exception:
        folder = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
        return os.path.join(folder, "pitwall_cars.txt")


def _is_current(py_file):
    try:
        with open(py_file, encoding="utf-8", errors="ignore") as f:
            return PATH_MARKER in f.read()
    except OSError:
        return False


def _encode_for_game(text):
    """The game's embedded Python 3.3 reads the file with the Windows ANSI code page, so write it that way
    (identical to UTF-8 for plain ASCII paths, and it keeps accented user names working)."""
    if os.name == "nt":
        try:
            return text.encode("mbcs"), "mbcs"
        except UnicodeEncodeError:
            pass
    return text.encode("utf-8"), "utf-8"


def install_spotter_app(ac_path, spotter_file, log):
    dest = os.path.join(ac_path, "apps", "python", APP_FOLDER)
    src, embedded = find_source_dir(), load_embedded()
    try:
        log(f"Target: {dest}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        if src:
            log(f"Copying the app from {src}")
            shutil.copytree(src, dest, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "spotter_path.txt"))
        elif embedded:
            log("ac_app folder not found, rebuilding the app from the built-in copy")
            os.makedirs(dest, exist_ok=True)
            for name, text in embedded.items():
                with open(os.path.join(dest, name), "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
        else:
            raise InstallError("The PitwallSpotter files are missing from this installer "
                               "(ac_app/PitwallSpotter was not bundled).")

        py = os.path.join(dest, APP_FOLDER + ".py")
        if not _is_current(py):
            if not embedded:
                raise InstallError("The bundled PitwallSpotter.py is outdated (it ignores spotter_path.txt) "
                                   "and no newer copy is available.")
            log("The bundled app was an older version, replacing it with the current one")
            with open(py, "w", encoding="utf-8", newline="\n") as f:
                f.write(embedded[APP_FOLDER + ".py"])

        data, enc = _encode_for_game(spotter_file)
        if enc == "utf-8" and not spotter_file.isascii():
            log("Warning: the link path has characters the game may not read correctly.")
        cfg = os.path.join(dest, PATH_MARKER)
        with open(cfg, "wb") as f:
            f.write(data)
        with open(cfg, "rb") as f:                                   # verify what the game will read
            if f.read().decode(enc).strip() != spotter_file:
                raise InstallError("spotter_path.txt could not be verified after writing.")
        log(f"Linked the app to: {spotter_file}")
    except PermissionError as e:
        raise NeedsAdmin(str(e)) from e
    return dest


# =====================================================================================
#  The wizard
# =====================================================================================
class Wizard(tk.Tk):
    OK_COLOR, BAD_COLOR, WARN_COLOR = "#1a7f37", "#c62828", "#b26a00"

    def __init__(self, args):
        super().__init__()
        self.title(f"{APP_TITLE} Setup" + ("  (Administrator)" if is_admin() else ""))
        self.geometry(self._centered(700, 680))
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        try:
            ttk.Style().theme_use("vista" if "vista" in ttk.Style().theme_names() else "clam")
        except tk.TclError:
            pass

        self.spotter_file = args.spotter_file or default_spotter_file()
        self.msgs = queue.Queue()
        self.running = self.done = False
        self.spotter_only = bool(args.spotter_only)

        self.exe_var = tk.StringVar(value=args.app_exe or find_app_exe() or "")
        self.ac_var = tk.StringVar(value=args.ac_path or find_ac() or DEFAULT_AC_PATH)
        self.var_deps = tk.BooleanVar(value=not self.spotter_only)
        self.var_desktop = tk.BooleanVar(value=not self.spotter_only)
        self.var_start = tk.BooleanVar(value=not self.spotter_only)
        self.var_spotter = tk.BooleanVar(value=True)

        self._build()
        self.exe_var.trace_add("write", lambda *_: self._validate())
        self.ac_var.trace_add("write", lambda *_: self._validate())
        self._validate()
        self.after(50, self._poll)
        if args.auto:                                                # resumed after the UAC relaunch
            self.after(300, self._start)

    # ------------------------------------------------------------------ layout
    def _centered(self, w, h):
        return f"{w}x{h}+{(self.winfo_screenwidth() - w) // 2}+{max(0, (self.winfo_screenheight() - h) // 3)}"

    def _build(self):
        head = tk.Frame(self, bg="white", height=64)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text=APP_TITLE, bg="white", font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=20, pady=(10, 0))
        tk.Label(head, text=f"Setup  -  v{VERSION}", bg="white", fg="#555", font=("Segoe UI", 9)).pack(anchor="w", padx=20)
        ttk.Separator(self).pack(fill="x")

        bar = ttk.Frame(self, padding=(14, 10))                      # packed before the body so it is never squeezed out
        bar.pack(side="bottom", fill="x")
        ttk.Separator(self).pack(side="bottom", fill="x")
        self.btn_close = ttk.Button(bar, text="Close", command=self._close)
        self.btn_close.pack(side="right")
        self.btn_install = ttk.Button(bar, text="Install", command=self._start, width=14)
        self.btn_install.pack(side="right", padx=6)

        body = ttk.Frame(self, padding=(20, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        ttk.Label(body, text="Click Install and the wizard does the rest.\nClose Assetto Corsa first.",
                  justify="left").grid(row=0, column=0, sticky="w")

        # ---- where things are ----
        box = ttk.LabelFrame(body, text="Locations", padding=8)
        box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text=APP_EXE_NAME).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(box, textvariable=self.exe_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(box, text="Browse...", command=self._browse_exe).grid(row=0, column=2, padx=(6, 0))
        self.lbl_exe = ttk.Label(box, text="", wraplength=560)
        self.lbl_exe.grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Label(box, text="Assetto Corsa").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        ttk.Entry(box, textvariable=self.ac_var).grid(row=2, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(box, text="Browse...", command=self._browse_ac).grid(row=2, column=2, padx=(6, 0), pady=(6, 0))
        self.lbl_ac = ttk.Label(box, text="", wraplength=560)
        self.lbl_ac.grid(row=3, column=1, columnspan=2, sticky="w")

        # ---- what to do ----
        opts = ttk.LabelFrame(body, text="What to install", padding=8)
        opts.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(opts, text="Check / install required Python libraries (source runs only)",
                        variable=self.var_deps).pack(anchor="w")
        ttk.Checkbutton(opts, text="Desktop shortcut", variable=self.var_desktop).pack(anchor="w")
        ttk.Checkbutton(opts, text="Start Menu entry (searchable, pin to taskbar from there)",
                        variable=self.var_start).pack(anchor="w")
        ttk.Checkbutton(opts, text="In-game spotter app for Assetto Corsa", variable=self.var_spotter).pack(anchor="w")

        # ---- progress ----
        self.headline = ttk.Label(body, text="Ready to install", font=("Segoe UI", 12, "bold"))
        self.headline.grid(row=3, column=0, sticky="w", pady=(12, 0))
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=1, value=0)
        self.progress.grid(row=4, column=0, sticky="ew", pady=6)
        logbox = ttk.Frame(body)
        logbox.grid(row=5, column=0, sticky="nsew")
        body.rowconfigure(5, weight=1)
        logbox.rowconfigure(0, weight=1)
        logbox.columnconfigure(0, weight=1)
        self.log_box = tk.Text(logbox, height=10, state="disabled", wrap="word", font=("Consolas", 9), bg="#fafafa")
        self.log_box.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logbox, command=self.log_box.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.tag_configure("err", foreground=self.BAD_COLOR)
        self.log_box.tag_configure("ok", foreground=self.OK_COLOR)
        self.after_text = ttk.Label(body, wraplength=640, justify="left")
        self.after_text.grid(row=6, column=0, sticky="w", pady=(8, 0))

    # ------------------------------------------------------------------ inputs
    def _browse_exe(self):
        start = os.path.dirname(self.exe_var.get()) if os.path.isdir(os.path.dirname(self.exe_var.get() or "")) else "C:\\"
        chosen = filedialog.askopenfilename(title=f"Select {APP_EXE_NAME}", initialdir=start,
                                            filetypes=[("Program", "*.exe"), ("All files", "*.*")])
        if chosen:
            self.exe_var.set(os.path.normpath(chosen))

    def _browse_ac(self):
        start = self.ac_var.get() if os.path.isdir(self.ac_var.get()) else "C:\\"
        chosen = filedialog.askdirectory(title="Select the Assetto Corsa folder", initialdir=start, mustexist=True)
        if chosen:
            self.ac_var.set(os.path.normpath(chosen))

    def _validate(self):
        exe = self.exe_var.get().strip()
        if os.path.isfile(exe):
            self.lbl_exe.configure(text="Program found.", foreground=self.OK_COLOR)
        else:
            self.lbl_exe.configure(text=f"{APP_EXE_NAME} not found: shortcuts need it. Use Browse.",
                                   foreground=self.WARN_COLOR)
        ok, msg = check_ac_folder(self.ac_var.get().strip())
        self.lbl_ac.configure(text=msg, foreground=self.OK_COLOR if ok else self.WARN_COLOR)

    # ------------------------------------------------------------------ install flow
    def _set_inputs_state(self, state):
        for w in self._all_widgets(self):
            if isinstance(w, (ttk.Entry, ttk.Checkbutton)) or (isinstance(w, ttk.Button) and w is not self.btn_close):
                try:
                    w.state(["disabled"] if state == "disabled" else ["!disabled"])
                except tk.TclError:
                    pass

    def _all_widgets(self, w):
        for c in w.winfo_children():
            yield c
            yield from self._all_widgets(c)

    def _start(self):
        if self.running:
            return
        plan = {"deps": self.var_deps.get(), "desktop": self.var_desktop.get(), "start": self.var_start.get(),
                "spotter": self.var_spotter.get(), "exe": self.exe_var.get().strip(), "ac": self.ac_var.get().strip()}
        if not any(plan[k] for k in ("deps", "desktop", "start", "spotter")):
            messagebox.showinfo(APP_TITLE, "Nothing is selected.", parent=self)
            return
        self.running, self.done = True, False
        self.headline.configure(text="Installing...")
        self.after_text.configure(text="")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._set_inputs_state("disabled")
        self.btn_install.state(["disabled"])
        threading.Thread(target=self._worker, args=(plan,), name="Installer", daemon=True).start()

    def _say(self, text, tag=None):
        self.msgs.put(("log", text, tag))

    def _worker(self, plan):
        steps = []
        if plan["deps"]:
            steps.append(("Checking libraries", self._step_deps))
        if plan["desktop"]:
            steps.append(("Desktop shortcut", self._step_desktop))
        if plan["start"]:
            steps.append(("Start Menu entry", self._step_start))
        if plan["spotter"]:
            steps.append(("In-game spotter app", self._step_spotter))
        self.msgs.put(("max", len(steps)))
        warnings, needs_admin = [], None
        for n, (name, fn) in enumerate(steps, 1):
            self._say(f"\n== {name} ==")
            try:
                note = fn(plan)
                if note:
                    warnings.append(note)
                self._say(f"{name}: done.", "ok")
            except NeedsAdmin as e:
                needs_admin = str(e)
                self._say(f"{name}: Windows refused to write to the game folder.", "err")
            except InstallError as e:
                warnings.append(f"{name}: {e}")
                self._say(f"{name}: {e}", "err")
            except PermissionError as e:
                warnings.append(f"{name}: permission denied ({e})")
                self._say(f"{name}: permission denied ({e})", "err")
            except Exception as e:                               # a bug in one step must not stop the others
                warnings.append(f"{name}: {type(e).__name__}: {e}")
                self._say(f"{name}: {type(e).__name__}: {e}", "err")
            self.msgs.put(("step", n))
        self.msgs.put(("finished", warnings, needs_admin))

    # ---- the steps (all run on the worker thread) ----
    def _step_deps(self, plan):
        failed = ensure_dependencies(self._say)
        if failed:
            return "Could not install: " + ", ".join(failed) + ". Run  pip install -r requirements.txt  by hand."
        return None

    def _require_exe(self, plan):
        exe = plan["exe"]
        if not os.path.isfile(exe):
            raise InstallError(f"{APP_EXE_NAME} was not found. Use Browse to select it, then install again.")
        return exe

    def _step_desktop(self, plan):
        exe = self._require_exe(plan)
        lnk = os.path.join(desktop_dir(), APP_TITLE + ".lnk")
        remove_if_exists(lnk)
        create_shortcut(lnk, exe, self._say)
        notify_shell_changed()
        self._say(f"Created {lnk}")
        return None

    def _step_start(self, plan):
        exe = self._require_exe(plan)
        folder = os.path.join(start_menu_programs_dir(), START_MENU_FOLDER)
        lnk = os.path.join(folder, APP_TITLE + ".lnk")
        remove_if_exists(lnk)
        create_shortcut(lnk, exe, self._say)
        notify_shell_changed()
        self._say(f"Created {lnk}")
        return None

    def _step_spotter(self, plan):
        ok, msg = check_ac_folder(plan["ac"])
        if not ok:
            raise InstallError(f"{msg} Pick the folder that contains acs.exe, then install again.")
        install_spotter_app(plan["ac"], self.spotter_file, self._say)
        return None

    # ------------------------------------------------------------------ UI updates (main thread)
    def _poll(self):
        try:
            while True:
                msg = self.msgs.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1], msg[2])
                elif kind == "max":
                    self.progress.configure(maximum=max(1, msg[1]), value=0)
                elif kind == "step":
                    self.progress.configure(value=msg[1])
                elif kind == "finished":
                    self._finish(msg[1], msg[2])
        except queue.Empty:
            pass
        self.after(50, self._poll)

    def _log(self, text, tag=None):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n", tag or ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _finish(self, warnings, needs_admin):
        self.running, self.done = False, True
        self.btn_install.configure(text="Install again")
        self.btn_install.state(["!disabled"])
        self._set_inputs_state("enabled")
        if needs_admin:
            self.headline.configure(text="Administrator rights needed")
            if self._ask_admin():
                return
            warnings.append("Spotter app: administrator permission was not granted. Run the installer as administrator.")
        if warnings:
            self.headline.configure(text="Finished with warnings", foreground=self.WARN_COLOR)
            self.after_text.configure(text="\n".join(warnings), foreground=self.BAD_COLOR)
            return
        self.headline.configure(text="Installation complete", foreground=self.OK_COLOR)
        tips = []
        if self.var_start.get() and not self.spotter_only:
            tips.append("To keep it on the taskbar: press Start, type Cesium, right-click the result > Pin to taskbar.")
        if self.var_spotter.get():
            tips.append("In Assetto Corsa: open the app bar (right edge of the screen) and switch on PitwallSpotter.")
        self.after_text.configure(text="\n".join(tips), foreground="")

    def _ask_admin(self):
        """Offer a UAC relaunch for the spotter step. True if the relaunch started (this window closes)."""
        if is_admin():
            self._log("Access was denied even as administrator. Check that the folder is not read-only or locked "
                      "by antivirus.", "err")
            return False
        if not messagebox.askyesno("Administrator rights needed",
                                   "Windows needs administrator permission to write into the game folder.\n\n"
                                   "Restart the installer as administrator and install the spotter app?",
                                   parent=self):
            return False
        args = ["--ac-path", self.ac_var.get().strip(), "--spotter-file", self.spotter_file,
                "--app-exe", self.exe_var.get().strip(), "--spotter-only", "--auto"]
        if relaunch_as_admin(args):
            self.destroy()
            return True
        self._log("The permission request was cancelled.", "err")
        return False

    def _close(self):
        if self.running:
            return
        self.destroy()


# =====================================================================================
def main():
    parser = argparse.ArgumentParser(description=f"{APP_TITLE} installer")
    parser.add_argument("--ac-path", default=None, help="Assetto Corsa folder (used by the UAC relaunch)")
    parser.add_argument("--spotter-file", default=None, help="shared car-position file (used by the UAC relaunch)")
    parser.add_argument("--app-exe", default=None, help=f"full path of {APP_EXE_NAME} (used by the UAC relaunch)")
    parser.add_argument("--spotter-only", action="store_true", help="only install the in-game app (UAC relaunch)")
    parser.add_argument("--auto", action="store_true", help="start installing immediately (used by the UAC relaunch)")
    args, _ = parser.parse_known_args()
    enable_dpi_awareness()
    Wizard(args).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------------------
# PACKAGING (run from the project folder, which contains ac_app/ and install_spotter.py)
#
#   Nuitka:
#     python -m nuitka --onefile --enable-plugin=tk-inter --windows-console-mode=disable ^
#            --include-data-dir=ac_app=ac_app --output-filename=CesiumSetup.exe installer_gui.py
#
#   PyInstaller:
#     pyinstaller --noconsole --onefile --name CesiumSetup --add-data "ac_app;ac_app" installer_gui.py
#
# Ship CesiumSetup.exe in the same folder as CesiumAIRaceEngineer.exe (it finds it automatically; Browse otherwise).
# Do NOT use --uac-admin: the wizard asks for admin only when the game folder needs it.
# ---------------------------------------------------------------------------------------