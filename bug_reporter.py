"""
bug_reporter.py - Cesium AI Race Engineer: in-app bug report window (v0.1.0).

Tkinter window: description + optional screenshot + optional cesium.log, sent to Cloudflare proxy.
The network request runs on a background thread, so the window never freezes.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import random
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
from dataclasses import dataclass
from tkinter import filedialog, ttk

try:
    import config
except Exception:
    config = None

APP_TITLE = "Cesium AI Race Engineer"

# Routed through your Cloudflare Worker proxy so server-side HWID bans are enforced.
WORKER_URL = "https://pitwall-ai.cesium-ai.workers.dev"

BAN_CHANNEL_ID = "1562494436783554611"
PINNED_MESSAGE_ID = "1562495016314736822"
BAN_CACHE_S = 600
_HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")

MAX_TEXT_CHARS = 1000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg"}
COOLDOWN_S = 60
CONNECT_TIMEOUT_S, READ_TIMEOUT_S = 6, 25
CREATE_NO_WINDOW = 0x08000000

_last_sent = 0.0
_send_lock = threading.Lock()

# ===================== hardware fingerprint (HWID) =====================
_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$o = [ordered]@{
  board    = (Get-CimInstance Win32_BaseBoard | Select-Object -First 1 -ExpandProperty SerialNumber)
  uuid     = (Get-CimInstance Win32_ComputerSystemProduct | Select-Object -First 1 -ExpandProperty UUID)
  cpu_id   = $cpu.ProcessorId
  cpu_name = $cpu.Name
  gpus     = @(Get-CimInstance Win32_VideoController | ForEach-Object { [ordered]@{ name = $_.Name; pnp = $_.PNPDeviceID } })
}
$o | ConvertTo-Json -Compress -Depth 4
"""

_JUNK = {"", "none", "default string", "to be filled by o.e.m.", "not specified", "system serial number",
         "n/a", "unknown", "123456789", "ffffffff-ffff-ffff-ffff-ffffffffffff", "base board serial number",
         "not applicable", "default", "o.e.m."}
_VIRTUAL_GPU = ("microsoft basic", "remote display", "virtual", "parsec", "indirect", "vmware", "hyper-v",
                "spacedesk", "mirage", "meta virtual")
_PCI_RE = re.compile(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", re.I)


def _clean_value(v):
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    if s.lower() in _JUNK or re.fullmatch(r"[0\-\s]+", s):
        return ""
    return s


def _mask(s):
    return ("*" * min(8, len(s) - 4) + s[-4:]) if len(s) > 4 else "****"


def _run_powershell(script, timeout=15):
    if os.name != "nt":
        return {}
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", script],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        text = r.stdout.decode("utf-8", "replace")
        start = text.find("{")
        return json.loads(text[start:]) if start >= 0 else {}
    except Exception:
        return {}


def _machine_guid():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return _clean_value(winreg.QueryValueEx(k, "MachineGuid")[0])
    except Exception:
        return ""


@dataclass(frozen=True)
class HardwareId:
    hash: str
    summary: str


_hwid_cache = None
_hwid_lock = threading.Lock()


def get_hwid():
    global _hwid_cache
    with _hwid_lock:
        if _hwid_cache is None:
            _hwid_cache = _build_hwid()
        return _hwid_cache


def _build_hwid():
    raw = _run_powershell(_PS_SCRIPT)
    board, uuid_ = _clean_value(raw.get("board")), _clean_value(raw.get("uuid")).lower()
    cpu_id, cpu_name = _clean_value(raw.get("cpu_id")).upper(), _clean_value(raw.get("cpu_name"))

    gpus_raw = raw.get("gpus") or []
    if isinstance(gpus_raw, dict):
        gpus_raw = [gpus_raw]
    gpu_ids, gpu_lines = set(), []
    for g in gpus_raw:
        if not isinstance(g, dict):
            continue
        name, pnp = _clean_value(g.get("name")), str(g.get("pnp") or "")
        m = _PCI_RE.search(pnp)
        if not m or any(v in name.lower() for v in _VIRTUAL_GPU):
            continue
        code = "%s:%s" % (m.group(1).upper(), m.group(2).upper())
        if code not in gpu_ids:
            gpu_ids.add(code)
            gpu_lines.append("%s [%s]" % (name or "GPU", code))

    parts = {"board": board, "uuid": uuid_, "cpu": cpu_id, "gpu": ",".join(sorted(gpu_ids))}
    if sum(1 for v in parts.values() if v) < 2:
        parts["machine_guid"] = _machine_guid().lower() or platform.node().lower()
    material = "cesium-hwid-v1|" + "|".join("%s=%s" % (k, parts[k]) for k in sorted(parts))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()

    lines = ["Board: " + (_mask(board) if board else "n/a"),
             "System UUID: " + (_mask(uuid_) if uuid_ else "n/a"),
             "CPU: %s (ID %s)" % (cpu_name or "n/a", _mask(cpu_id) if cpu_id else "n/a")]
    lines += ["GPU: " + g for g in gpu_lines] or ["GPU: n/a"]
    if "machine_guid" in parts:
        lines.append("Fallback id: " + _mask(parts["machine_guid"]))
    summary = "\n".join(lines).replace("`", "'")[:900]
    return HardwareId(digest, summary)


# ===================== validation and sanitizing (pure functions) =====================
class ReportError(Exception):
    pass


_MD_CHARS = re.compile(r"[*_~`|>#\\]")
_INVISIBLE = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u202a", "\u202b", "\u202c", "\u202d",
              "\u202e", "\u2066", "\u2067", "\u2068", "\u2069"}


def sanitize_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    kept = []
    for ch in text:
        if ch in _INVISIBLE:
            continue
        if ch in "\n\t" or not unicodedata.category(ch).startswith("C"):
            kept.append(ch)
    text = _MD_CHARS.sub("", "".join(kept))
    text = text.replace("@", "(at)")
    text = re.sub(r"<[@#:!&][^>]{0,40}>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_TEXT_CHARS]


def _read_limited(path, limit, what):
    try:
        size = os.path.getsize(path)
    except OSError:
        raise ReportError("%s: the file can't be read." % what)
    if size > limit:
        raise ReportError("%s is too large (%.1f MB). The limit is %d MB." % (what, size / 1048576.0, limit // 1048576))
    if size == 0:
        raise ReportError("%s is empty." % what)
    try:
        with open(path, "rb") as f:
            data = f.read(limit + 1)
    except OSError:
        raise ReportError("%s: the file can't be read." % what)
    if len(data) > limit:
        raise ReportError("%s is too large. The limit is %d MB." % (what, limit // 1048576))
    return data


def load_screenshot(path):
    ext = os.path.splitext(path or "")[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise ReportError("Only .png, .jpg and .jpeg screenshots are accepted.")
    data = _read_limited(path, MAX_IMAGE_BYTES, "The screenshot")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        kind = "jpg"
    else:
        raise ReportError("That file is not a real PNG or JPEG image.")
    if (kind == "png") != (ext == ".png"):
        raise ReportError("The file's contents don't match its extension.")
    return "screenshot." + kind, data, "image/png" if kind == "png" else "image/jpeg"


def log_path():
    if bool(getattr(sys, "frozen", False)) or "__compiled__" in globals():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "CesiumAIRaceEngineer", "logs", "cesium.log")
    root = getattr(config, "BASE_DIR", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "data", "logs", "cesium.log")


def load_log():
    path = log_path()
    if not os.path.isfile(path):
        raise ReportError("cesium.log was not found.")
    return "cesium.log", _read_limited(path, MAX_LOG_BYTES, "cesium.log"), "text/plain"


# ===================== sending (worker thread, touches no Tk) =====================
def send_report(description, screenshot=None, log=None):
    """Sends the bug report payload securely through the Cloudflare Worker proxy."""
    global _last_sent
    try:
        import requests
    except Exception:
        raise ReportError("Bug reporting needs the 'requests' package (pip install requests).")

    with _send_lock:
        wait = COOLDOWN_S - (time.monotonic() - _last_sent)
        if _last_sent and wait > 0:
            raise ReportError("Please wait %d s before sending another report." % (wait + 0.99))

        hw = get_hwid()
        attached = [n for n, k in (("screenshot", screenshot), ("log", log)) if k]

        discord_payload = {
            "username": "Cesium Bug Reporter",
            "allowed_mentions": {"parse": []},
            "embeds": [{
                "title": "Bug report",
                "description": description or "(no description)",
                "color": 0xD98E04,
                "fields": [
                    {"name": "App version", "value": str(getattr(config, "APP_VERSION", "v0.1.0"))[:32],
                     "inline": True},
                    {"name": "System", "value": ("%s %s" % (platform.system(), platform.release()))[:60],
                     "inline": True},
                    {"name": "Attachments", "value": ", ".join(attached) or "none", "inline": True},
                    {"name": "HWID hash (copy this to ban)", "value": "```%s```" % hw.hash, "inline": False},
                    {"name": "Hardware", "value": "```%s```" % hw.summary, "inline": False},
                ],
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }],
        }

        body_data = {
            "type": "bug_report",
            "hwid": hw.hash,
            "discord_payload": discord_payload
        }

        files = {}
        for n, item in enumerate(i for i in (screenshot, log) if i):
            name, data, mime = item
            files["files[%d]" % n] = (name, data, mime)

        try:
            if files:
                form_data = {"payload_json": json.dumps(body_data)}
                resp = requests.post(WORKER_URL, data=form_data, files=files,
                                     timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S), allow_redirects=False)
            else:
                resp = requests.post(WORKER_URL, json=body_data,
                                     timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S), allow_redirects=False)
        except requests.exceptions.Timeout:
            raise ReportError("The server took too long to answer. Check your connection and try again.")
        except requests.exceptions.SSLError:
            raise ReportError("A secure connection could not be made. Check your date/time and antivirus.")
        except requests.exceptions.ConnectionError:
            raise ReportError("No connection. Check your internet and try again.")
        except Exception:
            raise ReportError("The report could not be sent (network error).")

        code = resp.status_code
        if code in (200, 204):
            _last_sent = time.monotonic()
            return None
        if code == 403:
            time.sleep(random.uniform(0.8, 1.8))
            _last_sent = time.monotonic()
            return None
        if code == 429:
            try:
                retry = float(resp.json().get("retry_after", 30))
            except Exception:
                retry = 30
            raise ReportError("Too many reports right now. Try again in %d s." % (retry + 0.99))
        if code == 413:
            raise ReportError("The attachments are too large for the server.")
        raise ReportError("The server refused the report (HTTP %d). Try again later." % code)


# ===================== the window =====================
class BugReportWindow:
    POLL_MS = 100

    def __init__(self, parent=None):
        self.own_root = parent is None
        self.win = w = tk.Tk() if self.own_root else tk.Toplevel(parent)
        w.title("%s - Report a bug" % APP_TITLE)
        w.resizable(False, False)
        if not self.own_root:
            w.transient(parent)
        w.protocol("WM_DELETE_WINDOW", self._close)
        w.bind("<Escape>", lambda e: self._close())

        self.q = queue.Queue()
        self.sending = False
        self.shot_path = None
        self.var_log = tk.BooleanVar(value=False)

        body = ttk.Frame(w, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="What went wrong?", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="Describe what you did and what happened (max %d characters)." % MAX_TEXT_CHARS,
                  foreground="#555").grid(row=1, column=0, sticky="w", pady=(2, 6))

        box = ttk.Frame(body)
        box.grid(row=2, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        self.txt = tk.Text(box, width=62, height=9, wrap="word", font=("Segoe UI", 10), undo=True)
        sb = ttk.Scrollbar(box, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="ew")
        sb.grid(row=0, column=1, sticky="ns")
        self.txt.bind("<<Modified>>", self._on_edit)
        self.txt.bind("<<Paste>>", lambda e: self.win.after_idle(self._enforce_limit))
        self.lbl_count = ttk.Label(body, text="0 / %d" % MAX_TEXT_CHARS, foreground="#555")
        self.lbl_count.grid(row=3, column=0, sticky="e")

        shot = ttk.Frame(body)
        shot.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        shot.columnconfigure(1, weight=1)
        ttk.Button(shot, text="Attach screenshot...", command=self._pick_shot).grid(row=0, column=0, sticky="w")
        self.lbl_shot = ttk.Label(shot, text="None (PNG or JPG, max 5 MB)", foreground="#555")
        self.lbl_shot.grid(row=0, column=1, sticky="w", padx=8)
        self.btn_clear = ttk.Button(shot, text="Remove", command=self._clear_shot, state="disabled")
        self.btn_clear.grid(row=0, column=2, sticky="e")

        self.chk_log = ttk.Checkbutton(body, variable=self.var_log,
                                       text="Include cesium.log (max 2 MB). It may contain folder names.")
        self.chk_log.grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Label(body, wraplength=520, justify="left", foreground="#777", font=("Segoe UI", 8),
                  text="Every report includes an anonymous hardware ID (a one-way hash of your motherboard, CPU and "
                       "graphics card, plus a masked summary). It is used only to block spam. "
                       "Your name and files are never sent."
                  ).grid(row=6, column=0, sticky="w", pady=(6, 0))

        self.lbl_status = ttk.Label(body, text="", wraplength=520, justify="left")
        self.lbl_status.grid(row=7, column=0, sticky="w", pady=(10, 0))
        self.bar = ttk.Progressbar(body, mode="indeterminate")

        btns = ttk.Frame(body)
        btns.grid(row=9, column=0, sticky="e", pady=(12, 0))
        self.btn_send = ttk.Button(btns, text="Send report", command=self._send)
        self.btn_send.pack(side="right")
        self.btn_close = ttk.Button(btns, text="Close", command=self._close)
        self.btn_close.pack(side="right", padx=6)

        self.txt.focus_set()
        w.after(self.POLL_MS, self._poll)
        threading.Thread(target=get_hwid, name="HWID-warmup", daemon=True).start()

    def _enforce_limit(self):
        text = self.txt.get("1.0", "end-1c")
        if len(text) > MAX_TEXT_CHARS:
            self.txt.delete("1.0+%dc" % MAX_TEXT_CHARS, "end")
        self.lbl_count.configure(text="%d / %d" % (min(len(text), MAX_TEXT_CHARS), MAX_TEXT_CHARS))

    def _on_edit(self, _e=None):
        if self.txt.edit_modified():
            self.txt.edit_modified(False)
            self._enforce_limit()

    def _pick_shot(self):
        path = filedialog.askopenfilename(parent=self.win, title="Choose a screenshot",
                                          filetypes=[("Images (PNG, JPG)", "*.png *.jpg *.jpeg")])
        if not path:
            return
        try:
            load_screenshot(path)
        except ReportError as e:
            self._status(str(e), error=True)
            return
        self.shot_path = path
        self.lbl_shot.configure(text=os.path.basename(path)[:60])
        self.btn_clear.configure(state="normal")
        self._status("")

    def _clear_shot(self):
        self.shot_path = None
        self.lbl_shot.configure(text="None (PNG or JPG, max 5 MB)")
        self.btn_clear.configure(state="disabled")

    def _status(self, text, error=False, ok=False):
        self.lbl_status.configure(text=text, foreground="#c62828" if error else ("#1a7f37" if ok else "#333"))

    def _busy(self, on):
        self.sending = on
        for w in (self.btn_send, self.btn_close, self.chk_log):
            w.configure(state="disabled" if on else "normal")
        self.txt.configure(state="disabled" if on else "normal")
        if on:
            self.bar.grid(row=8, column=0, sticky="ew", pady=(6, 0))
            self.bar.start(12)
        else:
            self.bar.stop()
            self.bar.grid_remove()

    def _send(self):
        if self.sending:
            return
        description = sanitize_text(self.txt.get("1.0", "end-1c"))
        if len(description) < 10:
            self._status("Please describe the problem (at least 10 characters).", error=True)
            return
        try:
            screenshot = load_screenshot(self.shot_path) if self.shot_path else None
            logfile = load_log() if self.var_log.get() else None
        except ReportError as e:
            self._status(str(e), error=True)
            return
        self._busy(True)
        self._status("Sending...")
        threading.Thread(target=self._worker, args=(description, screenshot, logfile),
                         name="BugReport", daemon=True).start()

    def _worker(self, description, screenshot, logfile):
        try:
            send_report(description, screenshot, logfile)
            self.q.put(("ok", None))
        except ReportError as e:
            self.q.put(("err", str(e)))
        except Exception:
            self.q.put(("err", "Something unexpected went wrong. The report was not sent."))

    def _poll(self):
        try:
            while True:
                kind, msg = self.q.get_nowait()
                self._busy(False)
                if kind == "ok":
                    self.txt.delete("1.0", "end")
                    self._clear_shot()
                    self.var_log.set(False)
                    self._status("Thank you! Your report was sent.", ok=True)
                else:
                    self._status(msg, error=True)
        except queue.Empty:
            pass
        try:
            self.win.after(self.POLL_MS, self._poll)
        except tk.TclError:
            pass

    def _close(self):
        if self.sending:
            return
        self.win.destroy()


def open_bug_report(parent=None):
    ui = BugReportWindow(parent)
    if parent is None:
        ui.win.mainloop()
    return ui


if __name__ == "__main__":
    if "--hwid" in sys.argv:
        hw = get_hwid()
        print(hw.hash)
        print(hw.summary)
    else:
        open_bug_report()