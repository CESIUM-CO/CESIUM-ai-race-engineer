"""
boot_guard.py - import this FIRST in main_pitwall.py.

  * Logs uncaught exceptions (main thread + worker threads) to %APPDATA%\\CesiumAIRaceEngineer\\logs\\boot.log,
    even when the build has no console (sys.stderr is None).
  * Turns on faulthandler, so hard native crashes leave a stack trace in that file.
  * Shows a message box for a fatal startup error instead of dying silently.
  * `CesiumAIRaceEngineer.exe --selftest` checks that every library, DLL, data file and the AI worker are bundled.
Standard library only.
"""
import faulthandler
import os
import sys
import threading
import time
import traceback

APP_DIR = "CesiumAIRaceEngineer"
IS_WORKER = "--llm-worker" in sys.argv[1:]
_log = None


def _log_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_DIR, "logs")
    os.makedirs(path, exist_ok=True)
    return path


def note(text):
    line = "%s [pid %d%s] %s\n" % (time.strftime("%H:%M:%S"), os.getpid(), " worker" if IS_WORKER else "", text)
    for stream in (_log, sys.stderr):
        if stream is not None:
            try:
                stream.write(line)
                stream.flush()
            except Exception:
                pass


def _popup(title, text):
    if IS_WORKER or os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text[:1500], title, 0x10)
    except Exception:
        pass


def _fmt(etype, value, tb):
    return "".join(traceback.format_exception(etype, value, tb))


def _main_hook(etype, value, tb):
    if issubclass(etype, KeyboardInterrupt):
        return
    text = _fmt(etype, value, tb)
    note("UNCAUGHT EXCEPTION\n" + text)
    _popup("Cesium AI Race Engineer - startup error",
           text[-1100:] + "\n\nFull log:\n" + os.path.join(_log_dir(), "boot.log"))


def _thread_hook(args):
    name = args.thread.name if args.thread else "?"
    note("EXCEPTION IN THREAD %s\n%s" % (name, _fmt(args.exc_type, args.exc_value, args.exc_traceback)))


def _espeak_dirs():
    """Folders where the packaged build keeps espeak-ng.dll (Nuitka puts it in <dist>\\espeakng_loader)."""
    here = os.path.dirname(os.path.abspath(sys.executable))
    dirs = [os.path.join(here, "espeakng_loader"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "espeakng_loader")]
    return [d for i, d in enumerate(dirs) if d not in dirs[:i] and os.path.isfile(os.path.join(d, "espeak-ng.dll"))]


def _add_espeak_dll_dir():
    """Lets Windows resolve espeak-ng.dll AND the DLLs it depends on from its own folder (the 'or one of its
    dependencies' part of the error). Harmless when the folder does not exist (source runs)."""
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    for d in _espeak_dirs():
        try:
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            pass


def _install():
    global _log
    try:
        _log = open(os.path.join(_log_dir(), "boot.log"), "a", encoding="utf-8", errors="replace", buffering=1)
        faulthandler.enable(file=_log, all_threads=True)       # native crashes (access violation, DLL init) land here
    except Exception:
        _log = None
    sys.excepthook = _main_hook
    threading.excepthook = _thread_hook
    _add_espeak_dll_dir()
    comp = globals().get("__compiled__")
    note("=== start argv=%s frozen=%s compiled=%s exe=%s" % (
        sys.argv[1:], bool(getattr(sys, "frozen", False)), comp is not None, sys.executable))


# ----------------------------------------------------------------------------------------- self-test
MODULES = [
    # GUI
    "tkinter", "_tkinter", "pystray", "pystray._win32", "PIL.Image",
    # audio / input
    "numpy", "sounddevice", "keyboard", "mouse", "pygame", "win32com.client", "pythoncom",
    # speech + AI
    "faster_whisper", "ctranslate2", "onnxruntime", "kokoro_onnx", "llama_cpp", "ddgs",
    # the app's own modules (lazy imports are the usual victims)
    "config", "user_settings", "paths", "fetch", "credentials", "account_manager", "cloud_coach",
    "local_llm", "llm_worker", "ptt_manager", "updater", "installer_gui", "gui", "voice_radio", "listener", "brain",
    "speech_repair", "intent_router", "ollama_client", "persona",
]


def _selftest():
    import glob
    import importlib
    import subprocess
    lines, bad = [], 0

    def out(s=""):
        lines.append(s)
        print(s, flush=True)

    out("== Python / build ==")
    out("executable : %s" % sys.executable)
    out("compiled   : %s" % ("__compiled__" in globals()))
    out("")
    out("== imports ==")
    for name in MODULES:
        try:
            importlib.import_module(name)
            out("OK    %s" % name)
        except BaseException as e:
            bad += 1
            out("FAIL  %-18s %s: %s" % (name, type(e).__name__, e))

    out("")
    out("== tkinter can open a window ==")
    try:
        import tkinter
        tkinter.Tk().destroy()
        out("OK")
    except BaseException as e:
        bad += 1
        out("FAIL  %s: %s" % (type(e).__name__, e))

    out("")
    out("== files next to the exe ==")
    base = os.path.dirname(os.path.abspath(sys.executable))
    for rel in ("cesium.ico", os.path.join("ac_app", "PitwallSpotter", "PitwallSpotter.py")):
        ok = os.path.isfile(os.path.join(base, rel))
        bad += not ok
        out("%s  %s" % ("OK   " if ok else "MISSING", rel))
    try:
        import ctypes
        import espeakng_loader
        dll, data = espeakng_loader.get_library_path(), espeakng_loader.get_data_path()
        ok = os.path.isfile(dll) and os.path.isdir(data)
        bad += not ok
        out("%s  espeak-ng files: %s | %s" % ("OK   " if ok else "MISSING", dll, data))
        if ok:
            ctypes.CDLL(dll)                                   # loads the DLL AND its dependencies, like Kokoro will
            out("OK     espeak-ng.dll loads")
    except BaseException as e:
        bad += 1
        out("FAIL  espeak-ng: %s: %s" % (type(e).__name__, e))
    try:
        import llama_cpp
        lib = os.path.join(os.path.dirname(llama_cpp.__file__), "lib")
        dlls = [os.path.basename(p) for p in glob.glob(os.path.join(lib, "*.dll"))]
        bad += not dlls
        out("%s  llama_cpp\\lib DLLs: %s" % ("OK   " if dlls else "MISSING", dlls or lib))
    except BaseException as e:
        out("skip  llama_cpp lib check (%s)" % e)

    out("")
    out("== AI worker process (bad model path on purpose) ==")
    try:
        import paths
        cmd = paths.worker_command() + ["__no_such_model__.gguf", "512", "1"]
        out("command: %s" % cmd)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        out("stdout: %s" % (r.stdout.strip() or "(empty)"))
        out("stderr: %s" % (r.stderr.strip()[-600:] or "(empty)"))
        if "model failed to load" in r.stdout:
            out("=> GOOD: llama_cpp imports fine inside the worker.")
        else:
            bad += 1
            out("=> PROBLEM: the worker could not even import llama_cpp (see the message above).")
    except BaseException as e:
        bad += 1
        out("FAIL  %s: %s" % (type(e).__name__, e))

    out("")
    out("RESULT: %s" % ("everything looks bundled" if not bad else "%d problem(s) found" % bad))
    path = os.path.join(_log_dir(), "selftest.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except OSError:
        pass
    _popup("Cesium self-test", "%s\n\nReport saved to:\n%s" % (lines[-1], path))


_install()
if "--selftest" in sys.argv[1:]:
    _selftest()
    sys.exit(0)
