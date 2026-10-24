"""
paths.py - one place that knows WHERE things are, whichever way the program was started:

    python main_pitwall.py          (source run, PyCharm)
    Nuitka --standalone             (CesiumAIRaceEngineer.dist\\CesiumAIRaceEngineer.exe)
    Nuitka --onefile                (the exe unpacks itself into a temp folder)
    PyInstaller                     (sys._MEIPASS)

WHY THE [WinError 2] HAPPENED
    local_llm.py decided "am I packaged?" with  getattr(sys, "frozen", False)  and then started the AI helper as
        [sys.executable, "<folder of local_llm.py>/llm_worker.py"]
    Inside a Nuitka build the .py files do not exist (they are compiled INTO the exe), so that path is missing and
    Windows answers "file not found". Nuitka marks compiled code with the module-level name __compiled__, so that is
    what is checked here (together with sys.frozen, for PyInstaller).

    The fix for the helper process: a compiled build starts ITSELF again with  --llm-worker  (main_pitwall.py
    already handles that flag at the very top). Only a source run starts a .py file.

Standard library only. Try it:   python paths.py
"""
import os
import sys


# ------------------------------------------------------------------ how are we running?
def is_pyinstaller():
    return bool(getattr(sys, "frozen", False)) and "__compiled__" not in globals()


def is_compiled():
    """True in a Nuitka build (standalone or onefile) and in a PyInstaller build."""
    return "__compiled__" in globals() or bool(getattr(sys, "frozen", False))


# ------------------------------------------------------------------ where is the program?
def _dir_of(path):
    return os.path.dirname(os.path.abspath(path)) if path else None


def app_executable():
    """Full path of the running program (the .exe in a build, python.exe in a source run)."""
    comp = globals().get("__compiled__")
    for cand in (sys.executable,
                 getattr(comp, "original_argv0", None),       # Nuitka onefile: the exe the user double-clicked
                 sys.argv[0] if sys.argv else None):
        if cand and os.path.isfile(os.path.abspath(cand)):
            return os.path.abspath(cand)
    return os.path.abspath(sys.executable)


def base_dirs():
    """Folders that can hold the program's files, best guess first, no duplicates."""
    comp = globals().get("__compiled__")
    dirs = []
    for d in (getattr(sys, "_MEIPASS", None),                  # PyInstaller (onefile: its temp folder)
              _dir_of(__file__),                               # next to this file (the .dist folder in Nuitka, the project in source)
              getattr(comp, "containing_dir", None),           # Nuitka: folder that holds the program
              _dir_of(sys.argv[0] if sys.argv and sys.argv[0] else None),
              _dir_of(sys.executable),
              os.getcwd()):
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


def get_resource_path(relative_path):
    """Absolute path of a bundled file or folder, e.g. get_resource_path("models") or ("ac_app/PitwallSpotter").

    Returns the first location where it really exists. If it exists nowhere, returns the first guess, so the
    error message you get names a sensible path instead of a bare file name."""
    rel = relative_path.replace("/", os.sep)
    dirs = base_dirs()
    for d in dirs:
        full = os.path.normpath(os.path.join(d, rel))
        if os.path.exists(full):
            return full
    return os.path.normpath(os.path.join(dirs[0] if dirs else os.getcwd(), rel))


def resource_candidates(relative_path):
    """Every place `relative_path` could be (for code that wants to look in all of them)."""
    rel = relative_path.replace("/", os.sep)
    return [os.path.normpath(os.path.join(d, rel)) for d in base_dirs()]


# ------------------------------------------------------------------ the AI helper process
def worker_command():
    """The command line that starts the AI helper process (without the model arguments).

    compiled build -> [CesiumAIRaceEngineer.exe, "--llm-worker"]   (main_pitwall.py runs llm_worker.main())
    source run     -> [python.exe, <project>/llm_worker.py]
    Raises FileNotFoundError, with the exact path, if the thing to start is not there."""
    if is_compiled():
        exe = app_executable()
        if not os.path.isfile(exe):
            raise FileNotFoundError(f"program file not found: {exe}")
        return [exe, "--llm-worker"]
    script = get_resource_path("llm_worker.py")
    if not os.path.isfile(script):
        raise FileNotFoundError(f"llm_worker.py not found (looked at: {script})")
    return [sys.executable, script]


if __name__ == "__main__":
    print("compiled:", is_compiled(), "| pyinstaller:", is_pyinstaller())
    print("executable:", app_executable())
    print("search folders:")
    for d in base_dirs():
        print("   ", d)
    print("llm_worker.py ->", get_resource_path("llm_worker.py"))
    print("models        ->", get_resource_path("models"))
    try:
        print("worker command:", worker_command())
    except FileNotFoundError as e:
        print("worker command: PROBLEM:", e)
