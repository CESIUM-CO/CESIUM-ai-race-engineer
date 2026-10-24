"""
cesium_logger.py - Cesium AI Race Engineer: failure diagnostics for cesium.log.

WHY IT EXISTS
  "Telemetry isn't live yet" or a canned fallback line tells a developer WHAT the driver heard, not WHY the AI failed.
  This module writes ONE readable block per failure: a tag, the reason, the numbers around it (latency, CPU, RAM, VRAM,
  audio devices) and a plain-words hint, so a log from a weak PC can be read without asking the driver anything.

HOW IT REACHES cesium.log
  gui.setup_runtime() already copies everything print() writes into cesium.log (and the Log window). So this module just
  prints. Nothing here can raise into the app, block the telemetry loop, or spend more than a few ms on the happy path.
  A second machine-readable copy goes to  <logs>\\diagnostics.jsonl  (rotated at 2 MB) for scripts / bug reports.

WHAT IT LOGS
  boot_diagnostics()        OS, CPU, GPU, RAM, free disk in the work folder, audio drivers + devices, AI settings
  fail_stt(...)             [FAIL_STT]            low confidence / noise / too quiet / clipping
  fail_llm_timeout(...)     [FAIL_LLM_TIMEOUT]    inference slower than the cutoff (default 3.5 s)
  fail_ollama_offline(...)  [FAIL_OLLAMA_OFFLINE] why the fallback model was used
  fail_network(...)         [FAIL_NETWORK]        timeout / DNS / refused / TLS / HTTP status (URL is redacted)
  fail_memory_miss(...)     [FAIL_MEMORY_MISS]    casual or factual question with nothing retrieved
  timed("stt" | "llm" | "tts" | "net")   context manager: records latency, warns [PERF_SLOW] when over the limit
  llm_speed(...)            tokens / second and time-to-first-token bookkeeping

STANDARD LIBRARY ONLY. psutil is used when installed (more exact), otherwise ctypes / /proc fallbacks.
Works from source, PyInstaller and Nuitka (no data files, no dynamic imports except optional sounddevice / psutil).
Arabic / accented paths are safe: every file is opened as UTF-8 and every path stays a str.

Settings (all optional, config.py):
    LOG_LLM_CUTOFF_S = 3.5                         LLM answers slower than this are [FAIL_LLM_TIMEOUT]
    LOG_SLOW_S = {"stt": 2.5, "llm": 3.5, "tts": 1.5, "net": 6.0}
    LOG_STT_MIN_LOGPROB = -1.0                     Whisper avg_logprob below this = low confidence
    LOG_STT_MAX_NO_SPEECH = 0.6
    LOG_DIAGNOSTICS_JSONL = True

Try it:   python cesium_logger.py
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from collections import defaultdict, deque
from urllib.parse import urlparse

try:
    import psutil                                    # optional
except Exception:
    psutil = None

try:
    import config
except Exception:                                    # lets this file run on its own
    config = None

__all__ = ["TAG_STT", "TAG_LLM_TIMEOUT", "TAG_OLLAMA_OFFLINE", "TAG_NETWORK", "TAG_MEMORY_MISS",
           "boot_diagnostics", "boot_diagnostics_async", "snapshot", "format_snapshot", "fail", "fail_stt",
           "assess_stt", "fail_llm_timeout", "fail_ollama_offline", "fail_network", "fail_memory_miss",
           "timed", "record", "llm_speed", "set_sink", "prime"]

TAG_STT = "FAIL_STT"
TAG_LLM_TIMEOUT = "FAIL_LLM_TIMEOUT"
TAG_OLLAMA_OFFLINE = "FAIL_OLLAMA_OFFLINE"
TAG_NETWORK = "FAIL_NETWORK"
TAG_MEMORY_MISS = "FAIL_MEMORY_MISS"
TAG_PERF = "PERF_SLOW"

APP_DIR_NAME = "CesiumAIRaceEngineer"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
INDENT = " " * 9                                     # lines up under the text after "HH:MM:SS "
GB = 1024.0 ** 3


def _cfg(name, default):
    return getattr(config, name, default) if config is not None else default


# =====================================================================================
#  Output: print() (-> cesium.log through gui.LogHub) + diagnostics.jsonl
# =====================================================================================
_sink = None
_file_lock = threading.Lock()


def set_sink(fn):
    """Send lines somewhere else (tests, a GUI). fn(line: str). None = back to print()."""
    global _sink
    _sink = fn


def _emit(text):
    line = "%s %s" % (time.strftime("%H:%M:%S"), text)
    try:
        (_sink or print)(line)
    except Exception:
        pass


def _is_frozen():
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def log_dir():
    """Same folder gui.py writes cesium.log to."""
    if _is_frozen():
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIR_NAME)
    else:
        root = _cfg("BASE_DIR", None) or os.path.dirname(os.path.abspath(__file__))
        base = os.path.join(root, "data")
    return os.path.join(base, "logs")


def work_dir():
    return _cfg("USER_ROOT", None) or os.getcwd()


def _jsonl(record_):
    if not _cfg("LOG_DIAGNOSTICS_JSONL", True):
        return
    try:
        path = os.path.join(log_dir(), "diagnostics.jsonl")
        with _file_lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                if os.path.getsize(path) > 2 * 1024 * 1024:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(json.dumps(record_, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


# =====================================================================================
#  Low-level probes (each returns None instead of raising)
# =====================================================================================
class _CpuMeter:
    """System-wide CPU % without psutil: Windows GetSystemTimes, or /proc/stat. Samples over ~0.15 s if the last
    sample is old, so a failure that happens minutes after the previous one still gets a meaningful number."""

    def __init__(self):
        self.prev, self.prev_t = None, 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _read():
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                idle, kern, user = (wintypes.FILETIME() for _ in range(3))
                if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
                    return None
                to_int = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
                return to_int(idle), to_int(kern) + to_int(user)          # kernel time already includes idle
            with open("/proc/stat", "r") as f:
                parts = [int(x) for x in f.readline().split()[1:]]
            return parts[3] + (parts[4] if len(parts) > 4 else 0), sum(parts[:8])
        except Exception:
            return None

    def percent(self):
        with self._lock:
            cur, now = self._read(), time.monotonic()
            if cur is None:
                return None
            if self.prev is None or now - self.prev_t > 3.0:
                self.prev = cur
                time.sleep(0.15)
                cur, now = self._read(), time.monotonic()
                if cur is None:
                    return None
            d_idle, d_total = cur[0] - self.prev[0], cur[1] - self.prev[1]
            self.prev, self.prev_t = cur, now
            return None if d_total <= 0 else max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))


_cpu_meter = _CpuMeter()


def _cpu_percent():
    try:
        if psutil is not None:
            return float(psutil.cpu_percent(interval=0.15))
    except Exception:
        pass
    return _cpu_meter.percent()


def _ram():
    """(total_bytes, available_bytes) or (None, None)."""
    try:
        if psutil is not None:
            m = psutil.virtual_memory()
            return int(m.total), int(m.available)
        if os.name == "nt":
            import ctypes

            class MEMSTATUS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMSTATUS()
            st.dwLength = ctypes.sizeof(MEMSTATUS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys), int(st.ullAvailPhys)
            return None, None
        info = {}
        with open("/proc/meminfo", "r") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                info[k] = int(v.split()[0]) * 1024
        return info.get("MemTotal"), info.get("MemAvailable")
    except Exception:
        return None, None


def _process_memory():
    """(this process bytes, child processes bytes or None). Children matter: the AI model lives in a helper process."""
    try:
        if psutil is not None:
            me = psutil.Process()
            kids = 0
            for c in me.children(recursive=True):
                try:
                    kids += c.memory_info().rss
                except Exception:
                    pass
            return int(me.memory_info().rss), int(kids)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            k32, ps = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return int(pmc.WorkingSetSize), None
            return None, None
        with open("/proc/self/statm", "r") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE"), None
    except Exception:
        return None, None


def _run(cmd, timeout):
    """Run a helper program with no console window. Returns stdout text or None."""
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    except Exception:
        return None


def _nvidia_smi_path():
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for p in (r"C:\Windows\System32\nvidia-smi.exe",
              r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if os.path.isfile(p):
            return p
    return None


_gpu_cache = (0.0, None)
_gpu_lock = threading.Lock()


def _gpus(max_age=15.0):
    """[{name, vram_used_mb, vram_total_mb, util}] for NVIDIA cards (cached: nvidia-smi takes 100-300 ms). [] if unknown."""
    global _gpu_cache
    with _gpu_lock:
        at, cached = _gpu_cache
        if cached is not None and time.monotonic() - at < max_age:
            return cached
        out = []
        exe = _nvidia_smi_path()
        text = _run([exe, "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                     "--format=csv,noheader,nounits"], 2.5) if exe else None
        for ln in (text or "").splitlines():
            p = [x.strip() for x in ln.split(",")]
            if len(p) >= 4:
                try:
                    out.append({"name": p[0], "vram_used_mb": float(p[1]), "vram_total_mb": float(p[2]),
                                "util": float(p[3])})
                except ValueError:
                    pass
        _gpu_cache = (time.monotonic(), out)
        return out


def _gpu_names_windows():
    """Any GPU (AMD / Intel too) by name, via PowerShell. Slow (~1 s): boot only."""
    if os.name != "nt":
        return []
    text = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                 "(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + ' | driver ' + $_.DriverVersion }) -join \"`n\""],
                8)
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _sd():
    try:
        import sounddevice
        return sounddevice
    except Exception:
        return None


_audio_cache = (0.0, None)


def _audio_state(max_age=5.0):
    """{"input": (name, state), "output": (name, state)}. state = ok | ERR <type> | none. Probes the device the program
    will really use (Settings choice, else the Windows default) the same way sounddevice will open it."""
    global _audio_cache
    at, cached = _audio_cache
    if cached is not None and time.monotonic() - at < max_age:
        return cached
    out = {}
    sd = _sd()
    for kind, cfg_name, rate in (("input", "MIC_DEVICE", 16000), ("output", "SPEAKER_DEVICE", 24000)):
        if sd is None:
            out[kind] = ("?", "sounddevice missing")
            continue
        try:
            idx = _cfg(cfg_name, None)
            if idx is None:
                idx = sd.default.device[0 if kind == "input" else 1]
            if idx is None or idx < 0:
                out[kind] = ("-", "none")
                continue
            name = sd.query_devices(idx)["name"]
            try:
                (sd.check_input_settings if kind == "input" else sd.check_output_settings)(
                    device=idx, channels=1, samplerate=rate, dtype="float32")
                out[kind] = (name, "ok")
            except Exception as e:
                out[kind] = (name, "ERR " + type(e).__name__)
        except Exception as e:
            out[kind] = ("?", "ERR " + type(e).__name__)
    _audio_cache = (time.monotonic(), out)
    return out


# =====================================================================================
#  Snapshot
# =====================================================================================
_snap_cache = (0.0, None)
_snap_lock = threading.Lock()


def prime():
    """Call once at start-up (boot_diagnostics does): warms the CPU meter and the GPU cache so the first failure is fast."""
    try:
        _cpu_percent()
        _gpus()
    except Exception:
        pass


def snapshot(force=False, max_age=1.0):
    """A dict of the machine's state right now. Cached for `max_age` seconds so a burst of failures costs one probe."""
    global _snap_cache
    with _snap_lock:
        at, cached = _snap_cache
        if not force and cached is not None and time.monotonic() - at < max_age:
            return cached
        total, avail = _ram()
        app, kids = _process_memory()
        snap = {"cpu": _cpu_percent(), "ram_total": total, "ram_free": avail, "app_mem": app, "worker_mem": kids,
                "gpus": _gpus(), "audio": _audio_state(), "disk_free": None, "threads": threading.active_count()}
        try:
            snap["disk_free"] = shutil.disk_usage(work_dir()).free
        except Exception:
            pass
        _snap_cache = (time.monotonic(), snap)
        return snap


def _gb(n):
    return "?" if n is None else "%.1f" % (n / GB)


def format_snapshot(s=None):
    s = s or snapshot()
    parts = ["cpu=%s" % ("?" if s["cpu"] is None else "%.0f%%" % s["cpu"]),
             "ram_free=%s/%sGB" % (_gb(s["ram_free"]), _gb(s["ram_total"]))]
    if s["app_mem"] is not None:
        extra = "(+%sGB helpers)" % _gb(s["worker_mem"]) if s["worker_mem"] else ""
        parts.append("app=%sGB%s" % (_gb(s["app_mem"]), extra))
    if s["gpus"]:
        g = s["gpus"][0]
        parts.append("gpu=%s vram=%.1f/%.1fGB util=%.0f%%" % (g["name"], g["vram_used_mb"] / 1024.0,
                                                               g["vram_total_mb"] / 1024.0, g["util"]))
    else:
        parts.append("gpu=n/a")
    a = s["audio"]
    parts.append("mic=%s[%s] spk=%s[%s]" % (a["input"][0][:28], a["input"][1], a["output"][0][:28], a["output"][1]))
    if s["disk_free"] is not None:
        parts.append("disk_free=%sGB" % _gb(s["disk_free"]))
    return " ".join(parts)


# =====================================================================================
#  Latency bookkeeping
# =====================================================================================
_timings = defaultdict(lambda: deque(maxlen=50))
_timing_lock = threading.Lock()
_SLOW_DEFAULT = {"stt": 2.5, "llm": 3.5, "tts": 1.5, "net": 6.0}


def _slow_limit(name):
    return float((_cfg("LOG_SLOW_S", None) or {}).get(name, _SLOW_DEFAULT.get(name, 5.0)))


def stats(name):
    """(count, last, average, p95) of the recent timings for `name`, or None."""
    with _timing_lock:
        v = list(_timings[name])
    if not v:
        return None
    s = sorted(v)
    return len(v), v[-1], sum(v) / len(v), s[min(len(s) - 1, int(0.95 * len(s)))]


def _fmt_stats(name):
    st = stats(name)
    return "" if st is None else "avg=%.2fs p95=%.2fs n=%d" % (st[2], st[3], st[0])


def record(name, seconds, quiet=False, **ctx):
    """Store one latency. Logs [PERF_SLOW] (rate limited) when it exceeds the limit, unless quiet=True
    (the fail_* helpers use that: they print their own, more specific line)."""
    with _timing_lock:
        _timings[name].append(float(seconds))
    if quiet:
        return
    limit = _slow_limit(name)
    if seconds > limit:
        _report(TAG_PERF, "%s took %.2fs (limit %.1fs)" % (name, seconds, limit),
                ctx=dict(ctx, **{"recent": _fmt_stats(name)}), rate_key=(TAG_PERF, name), every=20.0)


class _Timer:
    elapsed = 0.0


@contextlib.contextmanager
def timed(name, **ctx):
    """with cesium_logger.timed("stt", words=n): ...   -> records the time, warns if slow. Exceptions pass through."""
    t = _Timer()
    t0 = time.perf_counter()
    try:
        yield t
    finally:
        t.elapsed = time.perf_counter() - t0
        try:
            record(name, t.elapsed, **ctx)
        except Exception:
            pass


def llm_speed(backend, elapsed, tokens=None, ttft=None):
    """Record one LLM answer. Returns tokens/second (or None). Pass ttft (seconds to the first token) when streaming."""
    record("llm", elapsed, backend=backend)
    tps = None
    if tokens and elapsed > 0:
        gen = elapsed - (ttft or 0.0)
        tps = tokens / gen if gen > 0 else None
    with _timing_lock:
        if tps:
            _timings["llm_tps"].append(tps)
        if ttft is not None:
            _timings["llm_ttft"].append(ttft)
    return tps


# =====================================================================================
#  The one reporting function
# =====================================================================================
_rate = {}
_rate_lock = threading.Lock()


def _short(v, n=90):
    if isinstance(v, float):
        return "%.2f" % v
    s = str(v).replace("\r", " ").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "..."


def _fmt_ctx(ctx):
    return " ".join("%s=%s" % (k, _short(v)) for k, v in ctx.items() if v not in (None, ""))


def _hints(tag, s):
    """Plain-words causes the numbers point to."""
    out = []
    cpu, free, total = s.get("cpu"), s.get("ram_free"), s.get("ram_total")
    if cpu is not None and cpu >= 85:
        out.append("CPU is saturated (%.0f%%): the game and the AI are competing. Use a smaller model, OLLAMA_NUM_GPU=0 "
                   "or lower LLM_THREADS." % cpu)
    if free is not None and (free < 1.5 * GB or (total and free / total < 0.08)):
        out.append("Very little free RAM (%sGB): the model is probably swapping to disk." % _gb(free))
    g = (s.get("gpus") or [None])[0]
    if g and g["vram_total_mb"] - g["vram_used_mb"] < 1024:
        out.append("GPU memory is nearly full (%.1f/%.1fGB): the game and Ollama are fighting over VRAM."
                   % (g["vram_used_mb"] / 1024.0, g["vram_total_mb"] / 1024.0))
    if g and g["util"] >= 95 and tag in (TAG_LLM_TIMEOUT, TAG_PERF):
        out.append("GPU is at %.0f%% load from the game: GPU inference will be slow." % g["util"])
    for kind in ("input", "output"):
        name, state = s["audio"][kind]
        if state not in ("ok", "none") and not state.startswith("sounddevice"):
            out.append("The %s device '%s' cannot be opened (%s): pick another one in Settings." % (kind, name, state))
    if s.get("disk_free") is not None and s["disk_free"] < 0.5 * GB:
        out.append("Less than 0.5GB free on the app drive: downloads, cache and logs may fail.")
    return out


def _report(tag, reason, ctx=None, hint=None, rate_key=None, every=3.0, snap=True):
    """Prints one block:   HH:MM:SS [TAG] reason | k=v k=v
                                       sys: cpu=... ram_free=...
                                       why: hint
    Repeats of the same (tag, reason) inside `every` seconds are counted and folded into the next block."""
    try:
        key = rate_key or (tag, reason[:48])
        now = time.monotonic()
        with _rate_lock:
            last, suppressed = _rate.get(key, (-1e9, 0))
            if now - last < every:
                _rate[key] = (last, suppressed + 1)
                return
            _rate[key] = (now, 0)
        ctx = dict(ctx or {})
        ctx_text = _fmt_ctx(ctx)
        lines = ["[%s] %s%s%s" % (tag, reason, " | " + ctx_text if ctx_text else "",
                                  "  (+%d similar suppressed)" % suppressed if suppressed else "")]
        s = None
        if snap:
            s = snapshot()
            lines.append("sys: " + format_snapshot(s))
        reasons = ([hint] if hint else []) + (_hints(tag, s) if s else [])
        lines += ["why: " + r for r in reasons]
        _emit(("\n" + INDENT).join(lines))
        _jsonl({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "tag": tag, "reason": reason, "ctx": ctx,
                "sys": {k: v for k, v in (s or {}).items()}, "why": reasons})
    except Exception:
        pass


def fail(tag, reason, hint=None, **ctx):
    """Generic failure line with a system snapshot. Prefer the specific helpers below."""
    _report(tag, reason, ctx, hint)


# =====================================================================================
#  Specific failures
# =====================================================================================
def assess_stt(segments=None, audio=None, text="", transcribe_s=None, sample_rate=16000):
    """Collect the numbers that say how trustworthy a transcription is.
    segments: the list of faster-whisper segments (convert the generator with list() first), audio: float32 array.
    Returns (metrics dict, reason string or "" when the transcript looks fine)."""
    m = {"transcribe_s": transcribe_s}
    segs = list(segments or [])
    if segs:
        lp = [getattr(s, "avg_logprob", None) for s in segs if getattr(s, "avg_logprob", None) is not None]
        ns = [getattr(s, "no_speech_prob", None) for s in segs if getattr(s, "no_speech_prob", None) is not None]
        m["avg_logprob"] = round(sum(lp) / len(lp), 2) if lp else None
        m["no_speech"] = round(max(ns), 2) if ns else None
    try:
        if audio is not None and len(audio):
            m["audio_s"] = round(len(audio) / float(sample_rate), 2)
            m["rms"] = round(float((audio.astype("float64") ** 2).mean() ** 0.5), 4)
            m["peak"] = round(float(abs(audio).max()), 3)
    except Exception:
        pass
    m["words"] = len(text.split()) if text else 0
    reasons = []
    if not m["words"]:
        reasons.append("nothing was recognised")
    if m.get("avg_logprob") is not None and m["avg_logprob"] < float(_cfg("LOG_STT_MIN_LOGPROB", -1.0)):
        reasons.append("low confidence (avg_logprob %.2f)" % m["avg_logprob"])
    if m.get("no_speech") is not None and m["no_speech"] > float(_cfg("LOG_STT_MAX_NO_SPEECH", 0.6)):
        reasons.append("mostly silence / noise (no_speech %.2f)" % m["no_speech"])
    if m.get("rms") is not None and m["rms"] < 0.004:
        reasons.append("microphone level is very low (rms %.4f)" % m["rms"])
    if m.get("peak") is not None and m["peak"] >= 0.99:
        reasons.append("audio is clipping (peak %.2f)" % m["peak"])
    if m.get("audio_s") is not None and m["audio_s"] < 0.6:
        reasons.append("button released too early (%.1fs of audio)" % m["audio_s"])
    return m, "; ".join(reasons)


def fail_stt(text, reason, metrics=None, **ctx):
    """[FAIL_STT] The speech-to-text result is not trustworthy. `text` is what Whisper heard (may be empty)."""
    m = dict(metrics or {})
    m.update(ctx)
    hint = None
    if "rms" in m and m["rms"] is not None and m["rms"] < 0.004:
        hint = "Raise the Windows microphone level or pick another microphone in Settings."
    elif "clipping" in reason:
        hint = "Lower the microphone gain: the signal is distorted."
    elif "no_speech" in reason or "low confidence" in reason:
        hint = "Background noise or engine / wheel sounds in the microphone. A headset mic or WHISPER_MODEL='small.en' helps."
    _report(TAG_STT, reason, dict(m, heard=text), hint, rate_key=(TAG_STT, reason[:30]), every=2.0)


def fail_llm_timeout(backend, elapsed, cutoff=None, **ctx):
    """[FAIL_LLM_TIMEOUT] An AI backend ("ollama", "built-in", "online") did not answer inside the cutoff (default 3.5 s)."""
    cutoff = float(cutoff if cutoff is not None else _cfg("LOG_LLM_CUTOFF_S", 3.5))
    record("llm", elapsed, quiet=True)
    _report(TAG_LLM_TIMEOUT, "%s answered after %.1fs, cutoff is %.1fs" % (backend, elapsed, cutoff),
            dict(ctx, recent=_fmt_stats("llm")), rate_key=(TAG_LLM_TIMEOUT, backend), every=5.0)


def fail_ollama_offline(reason, fallback="built-in model", state=None, url=None, **ctx):
    """[FAIL_OLLAMA_OFFLINE] Why the next AI in the chain was used instead of Ollama."""
    host = urlparse(url).netloc if url else _cfg("OLLAMA_URL", "")
    _report(TAG_OLLAMA_OFFLINE, "%s -> using %s" % (reason, fallback), dict(ctx, state=state, url=host),
            "Start the Ollama app, or set USE_OLLAMA = False to stop looking for it. The race calls never depend on it.",
            rate_key=(TAG_OLLAMA_OFFLINE, reason[:40]), every=30.0)


def _classify_network(exc):
    """(short cause, hint) from an exception. Never includes URLs or headers."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        cause = {401: "unauthorised (bad key)", 403: "forbidden (key rejected)", 404: "not found",
                 408: "request timeout", 413: "payload too large", 429: "rate limited",
                 500: "server error", 502: "bad gateway", 503: "service unavailable",
                 529: "overloaded"}.get(code, "HTTP error")
        hint = "Check the saved API key." if code in (401, 403) else (
            "The service is busy: it will be retried after the cooldown." if code in (429, 503, 529) else None)
        return "HTTP %d %s" % (code, cause), hint
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed", "No internet, or a DNS / firewall problem."
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
        return "timed out", "Slow or blocked connection. The app falls back to local logic."
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused", "Nothing is listening there (service down, or wrong address)."
    if isinstance(reason, ConnectionResetError):
        return "connection reset", "The server or a firewall / antivirus closed the connection."
    if "ssl" in type(reason).__name__.lower() or "certificate" in str(reason).lower():
        return "TLS / certificate error", "Check the PC date / time and antivirus HTTPS scanning."
    return "%s: %s" % (type(reason).__name__, _short(reason, 80)), None


def fail_network(where, exc, url=None, elapsed=None, **ctx):
    """[FAIL_NETWORK] A request failed. `where` = "anthropic" / "ollama" / "web search" / "update check" ...
    The URL is reduced to scheme://host so webhook tokens and API paths never reach the log."""
    cause, hint = _classify_network(exc)
    host = None
    if url:
        p = urlparse(url)
        host = "%s://%s" % (p.scheme, p.netloc) if p.netloc else None
    if elapsed is not None:
        record("net", elapsed, quiet=True)
    _report(TAG_NETWORK, "%s: %s" % (where, cause), dict(ctx, host=host, after="%.1fs" % elapsed if elapsed is not None else None),
            hint, rate_key=(TAG_NETWORK, where, cause), every=10.0)


def fail_memory_miss(query, route="", searched_web=None, best_score=None, **ctx):
    """[FAIL_MEMORY_MISS] A casual / factual question matched nothing in memory (and nothing was found online)."""
    _report(TAG_MEMORY_MISS, "no stored answer for the question", dict(
        ctx, query=_short(query, 80), route=route, web=searched_web,
        best_match=None if best_score is None else "%.2f (needs 0.60)" % best_score),
        "Teach it: say 'remember that' followed by the answer, or check USE_WEB / internet.",
        rate_key=(TAG_MEMORY_MISS, query[:40]), every=10.0, snap=False)


# =====================================================================================
#  Boot diagnostics
# =====================================================================================
def _os_text():
    try:
        text = platform.platform()
        if os.name == "nt":
            v = sys.getwindowsversion()
            name = "Windows 11" if v.build >= 22000 else "Windows 10" if v.major == 10 else "Windows %d" % v.major
            text = "%s build %d (%s)" % (name, v.build, platform.machine())
        return text
    except Exception:
        return platform.system() or "?"


def _cpu_name():
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return " ".join(str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).split())
        return platform.processor() or platform.machine()
    except Exception:
        return platform.processor() or "?"


def _audio_drivers_lines():
    sd = _sd()
    if sd is None:
        return ["audio: sounddevice is not available (push-to-talk and voice will not work)"]
    lines = []
    try:
        apis = sd.query_hostapis()
        devs = sd.query_devices()
        default_api = sd.default.hostapi
        lines.append("audio: PortAudio %s | %d devices | host APIs: %s" % (
            getattr(sd, "get_portaudio_version", lambda: ("?",))()[1] if hasattr(sd, "get_portaudio_version") else "?",
            len(devs), ", ".join(a["name"] + ("*" if i == default_api else "") for i, a in enumerate(apis))))
    except Exception as e:
        lines.append("audio: could not list devices (%s: %s)" % (type(e).__name__, e))
    a = _audio_state(max_age=0)
    lines.append("audio: mic '%s' [%s] | speaker '%s' [%s]" % (a["input"][0], a["input"][1], a["output"][0], a["output"][1]))
    return lines


def boot_diagnostics(work=None, extra=None):
    """Writes the 'machine and settings' header block to the log. Slow parts (GPU names) run here, so call it from a
    thread (boot_diagnostics_async) rather than on the GUI thread."""
    try:
        t0 = time.perf_counter()
        wd = work or work_dir()
        total, avail = _ram()
        lines = ["os:     %s" % _os_text(),
                 "cpu:    %s | %s logical%s | load now %s" % (
                     _cpu_name(), os.cpu_count() or "?",
                     (" / %d physical" % psutil.cpu_count(logical=False)) if psutil and psutil.cpu_count(logical=False) else "",
                     "?" if _cpu_percent() is None else "%.0f%%" % _cpu_percent()),
                 "ram:    %sGB total, %sGB free" % (_gb(total), _gb(avail))]
        gpus = _gpus(max_age=0)
        if gpus:
            lines += ["gpu:    %s | VRAM %.1f GB total, %.1f GB used | driver tool: nvidia-smi" % (
                g["name"], g["vram_total_mb"] / 1024.0, g["vram_used_mb"] / 1024.0) for g in gpus]
        else:
            names = _gpu_names_windows()
            lines += ["gpu:    " + n for n in names] or ["gpu:    not detected (no NVIDIA driver tools; VRAM unknown)"]
        try:
            du = shutil.disk_usage(wd)
            lines.append("disk:   %sGB free of %sGB in %s" % (_gb(du.free), _gb(du.total), wd))
        except Exception as e:
            lines.append("disk:   could not read %s (%s)" % (wd, type(e).__name__))
        lines += _audio_drivers_lines()
        lines.append("python: %s | %s | psutil=%s | exe=%s" % (
            platform.python_version(), "compiled/frozen" if _is_frozen() else "source",
            getattr(psutil, "__version__", "no"), sys.executable))
        lines.append("ai:     whisper=%s | ollama=%s (%s, gpu_layers=%s) | built-in=%s | cutoff=%.1fs" % (
            _cfg("WHISPER_MODEL", "?"), _cfg("USE_OLLAMA", "?"), _cfg("OLLAMA_MODEL", "?"), _cfg("OLLAMA_NUM_GPU", None),
            _cfg("LLM_FILE", "?") if _cfg("USE_LLM", False) else "off", float(_cfg("LOG_LLM_CUTOFF_S", 3.5))))
        lines.append("voice:  tts=%s %s/%s voice=%s | web=%s cloud=%s" % (
            _cfg("TTS_ENGINE", "?"), _cfg("KOKORO_QUALITY", "?"), _cfg("ENGINEER_ACCENT", "?"), _cfg("KOKORO_VOICE", "?"),
            _cfg("USE_WEB", "?"), _cfg("USE_CLOUD", "?")))
        for k, v in (extra or {}).items():
            lines.append("%-7s %s" % (k + ":", v))
        hints = _hints("BOOT", snapshot(force=True))
        _emit("[BOOT] system diagnostics (%s v%s)\n%s%s" % (
            APP_DIR_NAME, _cfg("APP_VERSION", "?"), INDENT, ("\n" + INDENT).join(lines)))
        for h in hints:
            _emit("[BOOT] warning: " + h)
        _jsonl({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "tag": "BOOT", "lines": lines, "took_s": round(time.perf_counter() - t0, 2)})
    except Exception as e:
        _emit("[BOOT] diagnostics failed (%s: %s)" % (type(e).__name__, e))


def boot_diagnostics_async(work=None, extra=None):
    """Same as boot_diagnostics(), on a daemon thread: never delays the window or the radio."""
    t = threading.Thread(target=boot_diagnostics, args=(work, extra), name="BootDiagnostics", daemon=True)
    t.start()
    return t


# =====================================================================================
#  Self-test:  python cesium_logger.py
# =====================================================================================
if __name__ == "__main__":
    prime()
    boot_diagnostics()
    fail_stt("what is my fool", "low confidence (avg_logprob -1.42)", {"avg_logprob": -1.42, "transcribe_s": 1.9})
    with timed("stt"):
        time.sleep(0.01)
    fail_llm_timeout("ollama", 8.1, tokens=80)
    fail_ollama_offline("Ollama is not running at http://localhost:11434", state="failed")
    fail_network("anthropic", urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
                 url="https://api.anthropic.com/v1/messages?key=SECRET", elapsed=6.1)
    fail_network("cloud coach", urllib.error.HTTPError("https://x", 529, "overloaded", {}, None))
    fail_memory_miss("what is the best line through eau rouge", route="knowledge", searched_web=True, best_score=0.31)
    print("\nselftest finished; JSONL copy in", os.path.join(log_dir(), "diagnostics.jsonl"))
