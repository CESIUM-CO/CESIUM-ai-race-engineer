"""
updater.py - Cesium AI Race Engineer: self-updater (v0.2.0, with mandatory / forced updates). Standard library only.

WHAT IT DOES AT STARTUP
  1. Finishes or undoes an update that was interrupted last time (recover()).
  2. Asks GitHub for the newest release and compares its tag with the local version (a pre-release such as
     "v0.1.0-beta" is older than "v0.1.0", and "v0.1.0-beta.2" is newer than "v0.1.0-beta").
  3. If there is a newer one it shows a dialog: Update now / Remind me later / Skip this version.
  4. "Update now" runs in a background thread with a progress bar:
        download (resumable)  ->  verify  ->  unpack to a staging folder  ->  swap files in  ->  restart
  5. Nothing in the install folder is touched until the download is verified and fully unpacked. If anything fails
     before that point the app is exactly as it was.

MANDATORY (FORCED) UPDATES
  An update is mandatory when ANY of these is true (Release.mandatory / Release.mandatory_reason say which one):
    * config.FORCE_UPDATE = True            every newer release is forced. config.py is PRESERVED by updates, so this is
                                            mainly for your own testing; for real releases use one of the two below.
    * a marker in the release notes         [mandatory]   or   [force-update]   or   <!-- cesium:mandatory -->
    * a small release asset  update.json    {"mandatory": true}   or   {"min_version": "v0.2.0"}
                                            (min_version: every install OLDER than that version is forced)
  When an update is mandatory:
    * "Skip this version" and "Remind me later" do not exist, and an earlier "skip" for that version is ignored.
    * The Cancel button is hidden. Closing the window (X) or pressing "Exit Cesium" ends the program with sys.exit(0),
      so check_and_update() never returns while an outdated build is still running.
    * The program is blocked until the new version is downloaded, verified, unpacked and swapped in. It then restarts.
    * A failed install is rolled back at once through journal.json; a crash / kill mid-swap is rolled back by recover()
      on the next start (the old version comes back, then the forced update is offered again). The window offers Retry.
    * No window possible (no display / Tk error)? The same pipeline runs without a window and logs its progress.
    * Running from source (python main_pitwall.py) never forces anything: a source tree is never overwritten.
    * Offline: the check cannot see the release, so the program starts normally (fail-open). Nothing can force an update
      the program has never been told about.

HOW THE FILES ARE SWAPPED (and why it can be undone)
  Windows will not let a program overwrite or delete its own running .exe/.dll, but it DOES allow renaming them.
  So for every file in the new release: the old file is MOVED into _update/backup/, then the new one is moved into place.
  A journal (_update/journal.json) is written before the first move. Any error during the swap rolls everything back at
  once. If the PC crashes or the process is killed mid-swap, the next start reads the journal and rolls back. The backup
  is deleted only when the new version has started successfully (state "applied" -> committed in recover()).
  The main .exe is swapped last, so an early failure never leaves the app without one.

WHAT IS NEVER OVERWRITTEN (UPDATE_PRESERVE in config.py can change this list; a trailing / means a folder)
  data/  models/  logs/  voice_cache/  *.db  *.sqlite  *.log  config.py  ptt_binding.json  settings*.json  user_*.json
  A preserved file is skipped if it already exists, and installed if it is missing. Files that a newer release no longer
  contains are left in place.

WHAT THE RELEASE MUST CONTAIN
  * A .zip asset (if there are several, one with "win" in its name is preferred; UPDATE_ASSET_PATTERN can pick another).
    Its contents are the app folder (a single wrapping folder is stripped), and the main .exe must be at the top level.
  * A SHA-256, by either of these (new uploads get the first one from GitHub automatically):
        - the "digest" field GitHub puts on every asset, or
        - a sidecar asset:  <zipname>.sha256   or   SHA256SUMS / checksums.txt
    With neither, the update is refused (require_checksum=True). This proves the download is complete and untampered
    since GitHub computed the hash; it is not a code signature. Sign the .exe for real authenticity.

USAGE (top of run_pitwall(), before anything else starts; the llm-worker branch has already exited by then)
    from updater import check_and_update
    check_and_update()          # returns quickly if up to date / offline. May show the dialog and restart the app.

  Turned off by config.USE_UPDATER = False, by the environment variable CESIUM_NO_UPDATE=1, or by --no-update.
  Running from source (python main_pitwall.py) only prints a notice: it never overwrites a working tree.

  Command line:    python updater.py --check            (what would happen right now)
                   python updater.py --check --current v0.0.1 --dialog      (see the dialog)
"""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    import config
except Exception:                      # lets this file run on its own (tests, --check)
    config = None

__all__ = ["Updater", "UpdateDialog", "Release", "Asset", "UpdateError", "Cancelled", "NeedsAdmin",
           "check_and_update", "check_async", "parse_version", "is_newer"]

TAG = "[UPDATE]"
DEFAULT_VERSION = "v0.1.0-beta"
APP_DIR_NAME = "CesiumAIRaceEngineer"
LATEST_URL = "https://api.github.com/repos/CESIUM-CO/CESIUM-ai-race-engineer/releases/latest"
LIST_URL = "https://api.github.com/repos/CESIUM-CO/CESIUM-ai-race-engineer/releases?per_page=15"
RELEASES_PAGE = "https://github.com/CESIUM-CO/CESIUM-ai-race-engineer/releases"
TRUSTED_HOST_SUFFIXES = ("github.com", "githubusercontent.com")

DEFAULT_PRESERVE = ("data/", "models/", "logs/", "voice_cache/", "*.db", "*.sqlite", "*.log",
                    "config.py", "ptt_binding.json", "settings*.json", "user_*.json")
WORK_DIR = "_update"
MAX_UNPACKED_BYTES = int(getattr(config, "UPDATE_MAX_UNPACKED_MB", 4096)) * 1024 * 1024
SPACE_MARGIN = 100 * 1024 * 1024
CHUNK = 256 * 1024

# Mandatory-update signals that travel WITH a release (see the docstring).
MANDATORY_RE = re.compile(r"<!--\s*cesium:mandatory\s*-->|\[(?:mandatory|force[ -]?update)\]"
                          r"|\bmandatory[ -]update\s*:\s*(?:true|yes|1)\b", re.I)
META_ASSET_NAME = "update.json"


# =====================================================================================
#  Errors
# =====================================================================================
class UpdateError(Exception):
    """Something went wrong; the message is written for the user."""


class Cancelled(UpdateError):
    """The user cancelled."""


class NeedsAdmin(UpdateError):
    """The install folder is write-protected for this user."""


# =====================================================================================
#  Versions
# =====================================================================================
_PRE_RANK = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "preview": 3, "pre": 3, "rc": 4}


def parse_version(tag):
    """'v0.1.0-beta.2' -> a sortable key, or None if the tag is not a version at all."""
    t = (tag or "").strip().lstrip("vV").split("+", 1)[0]
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", t)
    if not m:
        return None
    core = [int(x) for x in m.group(1).split(".")]
    while len(core) < 3:
        core.append(0)
    while len(core) > 3 and core[-1] == 0:
        core.pop()
    pre = m.group(2).lstrip("-._ ")
    if not pre:
        return (tuple(core), 1, ())                       # a release is newer than any pre-release of the same number
    toks = [(0, int(x), "") if x.isdigit() else (1, _PRE_RANK.get(x.lower(), 9), x.lower())
            for x in re.findall(r"[A-Za-z]+|\d+", pre)]
    return (tuple(core), 0, tuple(toks))


def is_newer(remote, local):
    r, l = parse_version(remote), parse_version(local)
    return r is not None and l is not None and r > l


# =====================================================================================
#  Environment helpers
# =====================================================================================
def is_frozen():
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def app_executable():
    """The real path of the running program (for one-file builds too, where sys.executable can be a temp copy)."""
    comp = globals().get("__compiled__")
    if comp is not None:                                  # Nuitka
        for cand in (getattr(comp, "original_argv0", None), sys.argv[0] if sys.argv else None, sys.executable):
            if cand and os.path.isfile(os.path.abspath(cand)):
                return os.path.abspath(cand)
    return os.path.abspath(sys.executable)                # PyInstaller (or plain python)


def user_dir():
    if is_frozen():
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIR_NAME)
    return os.path.join(getattr(config, "BASE_DIR", None) or os.path.dirname(os.path.abspath(__file__)), "data")


def relaunch_elevated():
    """Start this program again through the UAC prompt. True if Windows accepted the request."""
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", app_executable(),
                                                 subprocess.list2cmdline(sys.argv[1:]), None, 1)
        return rc > 32
    except Exception:
        return False


def _safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "") or "x"


def _write_json_atomic(path, obj, durable=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
        if durable:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _retry(fn, tries=4, delay=0.25):
    """Antivirus and indexers briefly lock new files; retry a few times before giving up."""
    for n in range(tries):
        try:
            return fn()
        except PermissionError:
            if n == tries - 1:
                raise
            time.sleep(delay * (n + 1))


def _move(src, dst):
    try:
        os.replace(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copy2(src, dst)                             # different volume: copy, then drop the source
        os.remove(src)


def _pid_alive(pid):
    """Is a process with this id still running? Errs on the side of True (never steals a lock by mistake)."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5                   # access denied = it exists
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            return (not ok) or code.value == 259                      # 259 = STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _lock_owner_alive(path):
    """False only when the lock file names a process that is gone (the app was closed / killed mid-update)."""
    try:
        with open(path, "r") as f:
            text = f.read().strip()
        return _pid_alive(int(text)) if text.isdigit() else True
    except (OSError, ValueError):
        return True


class _FileLock:
    """One update at a time (two copies of the app starting together).
    A lock left behind by a process that no longer exists is removed at once, so a mandatory update that was closed
    half-way can be retried immediately instead of waiting an hour."""

    def __init__(self, path, stale_s=3600.0):
        self.path, self.stale_s, self.fd = path, stale_s, None

    def __enter__(self):
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale_s or not _lock_owner_alive(self.path):
                        os.remove(self.path)               # left behind by a crash or a closed window
                        continue
                except OSError:
                    pass
                break
        raise UpdateError("Another update is already running.")

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                os.close(self.fd)
            os.remove(self.path)
        except OSError:
            pass


# =====================================================================================
#  Release data
# =====================================================================================
@dataclass
class Asset:
    name: str
    url: str
    size: int = 0
    digest: str = ""            # sha256 hex from GitHub's "digest" field, if present


@dataclass
class Release:
    tag: str
    name: str = ""
    body: str = ""
    url: str = RELEASES_PAGE
    prerelease: bool = False
    asset: Optional[Asset] = None
    checksum_assets: list = field(default_factory=list)
    meta_asset: Optional[Asset] = None     # the optional update.json
    mandatory: bool = False                # filled in by Updater.check()
    mandatory_reason: str = ""


def _is_checksum_name(name):
    n = name.lower()
    return n.endswith((".sha256", ".sha256sum")) or n.startswith(("sha256sums", "checksums"))


def _parse_checksum_text(text, filename):
    """Finds the SHA-256 for `filename` in a sha256sum-style file, or a lone hash in a single-line .sha256 file."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for ln in lines:
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", ln)
        if m and os.path.basename(m.group(2).strip()) == filename:
            return m.group(1).lower()
    if len(lines) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", lines[0]):
        return lines[0].lower()
    return None


# =====================================================================================
#  The updater
# =====================================================================================
class Updater:
    def __init__(self, current_version=None, install_dir=None, exe_path=None, preserve=None,
                 include_prereleases=None, require_checksum=True, log=print, timeout=8.0,
                 asset_pattern=None, state_file=None):
        self.current = current_version or getattr(config, "APP_VERSION", None) or DEFAULT_VERSION
        self._log = log or (lambda *a, **k: None)
        self.timeout = timeout
        self.require_checksum = require_checksum
        self.asset_pattern = asset_pattern or getattr(config, "UPDATE_ASSET_PATTERN", None)
        self.preserve = tuple(preserve if preserve is not None else getattr(config, "UPDATE_PRESERVE", DEFAULT_PRESERVE))
        if include_prereleases is None:
            include_prereleases = getattr(config, "UPDATE_INCLUDE_PRERELEASES", None)
        if include_prereleases is None:                    # someone running a beta wants betas
            cur = parse_version(self.current)
            include_prereleases = bool(cur and cur[1] == 0)
        self.include_prereleases = include_prereleases

        self.can_self_update = is_frozen() or (install_dir is not None and exe_path is not None)
        self.exe_path = exe_path or (app_executable() if is_frozen() else None)
        self.install_dir = os.path.abspath(install_dir or (os.path.dirname(self.exe_path) if self.exe_path
                                                           else os.path.dirname(os.path.abspath(__file__))))
        self.exe_name = os.path.basename(self.exe_path) if self.exe_path else None
        self.work = os.path.join(self.install_dir, WORK_DIR)
        self.state_file = state_file or os.path.join(user_dir(), "update_state.json")
        self.api_latest, self.api_list = LATEST_URL, LIST_URL

    # ------------------------------------------------------------------ small helpers
    def _state(self):
        return _read_json(self.state_file)

    def _set_state(self, **kw):
        try:
            st = self._state()
            st.update(kw)
            _write_json_atomic(self.state_file, st)
        except OSError as e:
            self._log("%s Could not save update settings (%s)" % (TAG, e))

    def skip(self, tag):
        self._set_state(skipped=tag)

    def _target(self, rel):
        return os.path.join(self.install_dir, *rel.split("/"))

    def _preserved(self, rel):
        r = rel.lower()
        base = r.rsplit("/", 1)[-1]
        for p in self.preserve:
            p = p.lower()
            if p.endswith("/"):
                if r.startswith(p):
                    return True
            elif fnmatch.fnmatchcase(r, p) or (p.startswith("*") and fnmatch.fnmatchcase(base, p)):
                return True
        return False

    @staticmethod
    def _trusted_url(url):
        m = re.match(r"^https://([^/:@]+)(?::\d+)?/", url or "")
        host = m.group(1).lower() if m else ""
        return any(host == s or host.endswith("." + s) for s in TRUSTED_HOST_SUFFIXES)

    def _headers(self, accept="application/vnd.github+json", extra=None):
        h = {"User-Agent": "CesiumAIRaceEngineer-Updater", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        h.update(extra or {})
        return h

    # ------------------------------------------------------------------ 1. asking GitHub
    def _get_json(self, url):
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read(5_000_000).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                                  # no (non-pre-release) release yet
            if e.code in (403, 429):
                raise UpdateError("GitHub is rate-limiting this network right now")
            raise UpdateError("GitHub answered HTTP %d" % e.code)
        except (OSError, ValueError) as e:
            raise UpdateError("could not reach GitHub (%s)" % e)

    def _pick_asset(self, assets):
        zips = [a for a in assets if a.name.lower().endswith(".zip")]
        if self.asset_pattern:
            zips = [a for a in zips if fnmatch.fnmatch(a.name.lower(), self.asset_pattern.lower())]
        zips.sort(key=lambda a: "win" not in a.name.lower())   # stable: Windows packages first
        return zips[0] if zips else None

    def _parse_release(self, d):
        if not isinstance(d, dict) or d.get("draft") or not d.get("tag_name"):
            return None
        assets = []
        for a in d.get("assets") or []:
            if not isinstance(a, dict) or a.get("state", "uploaded") != "uploaded":
                continue
            url = str(a.get("browser_download_url") or "")
            digest = str(a.get("digest") or "")
            digest = digest[7:].lower() if digest.lower().startswith("sha256:") else ""
            if a.get("name") and self._trusted_url(url):
                assets.append(Asset(str(a["name"]), url, int(a.get("size") or 0),
                                    digest if re.fullmatch(r"[0-9a-f]{64}", digest) else ""))
        return Release(tag=str(d["tag_name"]), name=str(d.get("name") or ""), body=str(d.get("body") or ""),
                       url=str(d.get("html_url") or RELEASES_PAGE), prerelease=bool(d.get("prerelease")),
                       asset=self._pick_asset(assets), checksum_assets=[a for a in assets if _is_checksum_name(a.name)],
                       meta_asset=next((a for a in assets if a.name.lower() == META_ASSET_NAME), None))

    def fetch_latest(self):
        """The newest release, or None. Raises UpdateError if GitHub can't be reached.
        /releases/latest never returns pre-releases (and answers 404 if every release is one), so for a beta user,
        or when it 404s, the release list is read as well."""
        cands = []
        first = self._parse_release(self._get_json(self.api_latest))
        if first:
            cands.append(first)
        if first is None or self.include_prereleases:
            for d in self._get_json(self.api_list) or []:
                rel = self._parse_release(d)
                if rel and (self.include_prereleases or not rel.prerelease):
                    cands.append(rel)
        cands = [c for c in cands if parse_version(c.tag)]
        return max(cands, key=lambda c: parse_version(c.tag)) if cands else None

    def check(self):
        """A Release worth offering, or None (up to date, skipped by the user, already installed, or offline)."""
        try:
            rel = self.fetch_latest()
        except UpdateError as e:
            self._log("%s Could not check for updates: %s" % (TAG, e))
            return None
        if rel is None or not is_newer(rel.tag, self.current):
            self._log("%s Up to date (%s)." % (TAG, self.current))
            return None
        reason = self._resolve_mandatory(rel)
        rel.mandatory, rel.mandatory_reason = bool(reason), reason
        st = self._state()
        if st.get("skipped") == rel.tag and not rel.mandatory:     # a mandatory update can never be skipped
            self._log("%s %s is available but you chose to skip it." % (TAG, rel.tag))
            return None
        if st.get("installed") == rel.tag:                 # installed already, yet this build still reports an older version
            self._log("%s %s was already installed; the app's own version number needs bumping." % (TAG, rel.tag))
            return None
        return rel

    def _resolve_mandatory(self, rel):
        """'' when the update is optional, otherwise a short reason why it is forced (used for the log)."""
        if getattr(config, "FORCE_UPDATE", False):
            return "FORCE_UPDATE is switched on in config.py"
        if MANDATORY_RE.search(rel.body or ""):
            return "the release notes mark it as mandatory"
        if rel.meta_asset is not None:
            try:
                meta = json.loads(self._fetch_text(rel.meta_asset.url))
            except (UpdateError, ValueError) as e:
                self._log("%s Could not read %s (%s)." % (TAG, META_ASSET_NAME, e))
                return ""
            if isinstance(meta, dict):
                if meta.get("mandatory") is True:
                    return "%s says mandatory" % META_ASSET_NAME
                minimum = meta.get("min_version")
                if isinstance(minimum, str) and parse_version(minimum) and is_newer(minimum, self.current):
                    return "%s requires at least %s" % (META_ASSET_NAME, minimum)
        return ""

    # ------------------------------------------------------------------ 2. the whole pipeline
    def install(self, rel, progress=None, cancel=None):
        """download -> verify -> unpack -> swap. Raises UpdateError / Cancelled / NeedsAdmin. progress(phase, done, total, text)."""
        if not self.can_self_update:
            raise UpdateError("This copy of Cesium runs from source, so it can't update itself.")
        if rel.asset is None:
            raise UpdateError("This release has no .zip package to install.")
        j = _read_json(os.path.join(self.work, "journal.json"))
        if j.get("state") == "rollback_failed":
            raise UpdateError("An earlier update could not be undone completely. Your old files are in %s" %
                              os.path.join(self.work, "backup"))
        report = progress or (lambda *a: None)
        cancel = cancel or threading.Event()
        self._check_writable()
        with _FileLock(os.path.join(self.work, ".lock")):
            self._prepare(rel)
            zip_path = self._download(rel, report, cancel)
            self._verify(rel, zip_path, report, cancel)
            staged = self._extract(zip_path, report, cancel)
            try:
                self._validate_staged(staged)
                entries = self._plan(staged)
                if cancel.is_set():
                    raise Cancelled("cancelled")
                self._apply(rel, entries, staged, report)   # the point of no return; it undoes itself on failure
            except BaseException:
                shutil.rmtree(staged, ignore_errors=True)
                raise
            shutil.rmtree(os.path.join(self.work, "download"), ignore_errors=True)
        self._set_state(installed=rel.tag, skipped="")
        self._log("%s %s installed." % (TAG, rel.tag))

    def _check_writable(self):
        try:
            os.makedirs(self.work, exist_ok=True)
            probe = os.path.join(self.work, ".probe")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
        except PermissionError as e:
            raise NeedsAdmin("Windows doesn't let this program change its own folder:\n%s" % self.install_dir) from e
        except OSError as e:
            raise UpdateError("Can't write to %s (%s)" % (self.install_dir, e)) from e

    def _prepare(self, rel):
        for sub in ("staging", "backup"):
            shutil.rmtree(os.path.join(self.work, sub), ignore_errors=True)
        dl, keep = os.path.join(self.work, "download"), _safe_name(rel.tag)
        if os.path.isdir(dl):
            for n in os.listdir(dl):
                if n != keep:
                    shutil.rmtree(os.path.join(dl, n), ignore_errors=True)

    def _need_space(self, nbytes, what):
        free = shutil.disk_usage(self.work).free
        if free < nbytes + SPACE_MARGIN:
            raise UpdateError("Not enough free disk space to %s (about %d MB needed, %d MB free)." %
                              (what, (nbytes + SPACE_MARGIN) // 2**20, free // 2**20))

    # ------------------------------------------------------------------ 3. download (resumable, cancellable)
    def _download(self, rel, report, cancel):
        a = rel.asset
        folder = os.path.join(self.work, "download", _safe_name(rel.tag))
        os.makedirs(folder, exist_ok=True)
        final = os.path.join(folder, _safe_name(a.name))
        part = final + ".part"
        if os.path.isfile(final) and (not a.size or os.path.getsize(final) == a.size):
            report("download", os.path.getsize(final), os.path.getsize(final), a.name)
            return final
        self._need_space(max(a.size - (os.path.getsize(part) if os.path.exists(part) else 0), 0) * 2, "download the update")
        last_error = None
        for attempt in range(1, 5):
            if cancel.is_set():
                raise Cancelled("cancelled")
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if a.size and have > a.size:
                os.remove(part)
                have = 0
            extra = {"Range": "bytes=%d-" % have} if have else {}
            req = urllib.request.Request(a.url, headers=self._headers("application/octet-stream", extra))
            try:
                try:
                    resp = urllib.request.urlopen(req, timeout=20)
                except urllib.error.HTTPError as e:
                    if e.code == 416 and have:                 # we already hold every byte
                        break
                    raise
                with resp:
                    if have and getattr(resp, "status", 200) != 206:
                        have = 0                                # server ignored Range: start again
                    total = a.size or (int(resp.headers.get("Content-Length") or 0) + have)
                    done, tick = have, 0.0
                    with open(part, "ab" if have else "wb") as f:
                        while True:
                            if cancel.is_set():
                                raise Cancelled("cancelled")    # the .part stays, so a retry resumes
                            chunk = resp.read(CHUNK)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            if time.monotonic() - tick >= 0.1:
                                tick = time.monotonic()
                                report("download", done, total, a.name)
                    report("download", done, total or done, a.name)
                    if total and done < total:
                        raise IOError("the download ended early")
                break
            except Cancelled:
                raise
            except OSError as e:                                # URLError, timeouts, resets, disk errors
                last_error = e
                self._log("%s Download problem (%s), attempt %d of 4" % (TAG, e, attempt))
                if attempt < 4:
                    cancel.wait(min(2 * attempt, 6))
        else:
            raise UpdateError("The download failed (%s). Check your internet connection and try again." % last_error)
        size = os.path.getsize(part)
        if a.size and size != a.size:
            os.remove(part)
            raise UpdateError("The downloaded file has the wrong size (%d instead of %d bytes)." % (size, a.size))
        os.replace(part, final)
        return final

    # ------------------------------------------------------------------ 4. verify
    def _fetch_text(self, url):
        req = urllib.request.Request(url, headers=self._headers("application/octet-stream"))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read(1_000_000).decode("utf-8", "replace")
        except OSError as e:
            raise UpdateError("could not read the checksum file (%s)" % e)

    def _expected_hashes(self, rel):
        found = {}
        if rel.asset.digest:
            found["GitHub's asset digest"] = rel.asset.digest
        for ca in rel.checksum_assets:
            try:
                h = _parse_checksum_text(self._fetch_text(ca.url), rel.asset.name)
            except UpdateError as e:
                self._log("%s %s" % (TAG, e))
                continue
            if h:
                found["checksum file " + ca.name] = h
                break
        return found

    def _verify(self, rel, path, report, cancel):
        try:
            expected = self._expected_hashes(rel)
            if len(set(expected.values())) > 1:
                raise UpdateError("The release's checksums disagree with each other, so the update was refused.")
            if not expected and self.require_checksum:
                raise UpdateError("This release publishes no SHA-256 checksum, so the download can't be verified. "
                                  "The update was refused.")
            size, done = os.path.getsize(path), 0
            sha = hashlib.sha256()
            with open(path, "rb") as f:
                while True:
                    if cancel.is_set():
                        raise Cancelled("cancelled")
                    block = f.read(1 << 20)
                    if not block:
                        break
                    sha.update(block)
                    done += len(block)
                    report("verify", done, size, "")
            if expected and sha.hexdigest() != next(iter(expected.values())):
                raise UpdateError("The download is corrupt (checksum mismatch). It was deleted; please try again.")
            if not zipfile.is_zipfile(path):
                raise UpdateError("The downloaded file is not a valid zip archive.")
        except Cancelled:
            raise
        except UpdateError:
            try:
                os.remove(path)                                 # never keep a file that failed verification
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ 5. unpack into staging (never into the install)
    @staticmethod
    def _clean_name(name, prefix):
        name = name.replace("\\", "/")
        if prefix and name.startswith(prefix):
            name = name[len(prefix):]
        parts = name.split("/")
        if parts[0] == "__MACOSX" or not name:
            return None
        for p in parts:
            if p in ("", ".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', p) or p.endswith((".", " ")):
                raise UpdateError("The update package contains an unsafe path (%r) and was refused." % name)
        if parts[0].lower() == WORK_DIR:
            raise UpdateError("The update package contains a reserved folder and was refused.")
        return "/".join(parts)

    def _extract(self, zip_path, report, cancel):
        staging = os.path.join(self.work, "staging")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                if not infos:
                    raise UpdateError("The update package is empty.")
                total = sum(i.file_size for i in infos)
                if total > MAX_UNPACKED_BYTES:
                    raise UpdateError("The update package unpacks to an unreasonable size and was refused.")
                self._need_space(total, "unpack the update")
                names = [i.filename.replace("\\", "/") for i in infos]
                roots = {n.split("/", 1)[0] for n in names}
                prefix = (roots.pop() + "/") if len(roots) == 1 and all("/" in n for n in names) else ""
                done, tick = 0, 0.0
                for info in infos:
                    if cancel.is_set():
                        raise Cancelled("cancelled")
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise UpdateError("The update package contains a link and was refused.")
                    rel = self._clean_name(info.filename, prefix)
                    if rel is None:
                        continue
                    dest = os.path.join(staging, *rel.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(info) as src, open(dest, "wb") as out:
                        while True:
                            block = src.read(CHUNK)
                            if not block:
                                break
                            out.write(block)
                            done += len(block)
                            if time.monotonic() - tick >= 0.1:
                                tick = time.monotonic()
                                report("extract", done, total, rel)
                report("extract", total, total, "")
        except zipfile.BadZipFile as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise UpdateError("The update package is damaged (%s)." % e) from e
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return staging

    def _validate_staged(self, staged):
        names = {n.lower() for n in os.listdir(staged)}
        if self.exe_name.lower() not in names:
            raise UpdateError("The update package doesn't contain %s at its top level, so it was refused "
                              "(the installed version was not touched)." % self.exe_name)

    def _plan(self, staged):
        entries = []
        for dirpath, _dirs, files in os.walk(staged):
            for fn in files:
                rel = os.path.relpath(os.path.join(dirpath, fn), staged).replace(os.sep, "/")
                if self._preserved(rel) and os.path.lexists(self._target(rel)):
                    continue                                    # the user's file wins
                entries.append(rel)
        entries.sort(key=lambda r: (r.lower() == self.exe_name.lower(), r))   # the main .exe goes in last
        return entries

    # ------------------------------------------------------------------ 6. the swap (journal + rollback)
    def _journal_path(self):
        return os.path.join(self.work, "journal.json")

    def _apply(self, rel, entries, staged, report):
        backup = os.path.join(self.work, "backup")
        shutil.rmtree(backup, ignore_errors=True)
        existed = {r: os.path.lexists(self._target(r)) for r in entries}
        journal = {"version": 1, "state": "applying", "tag": rel.tag, "entries": [[r, existed[r]] for r in entries]}
        _write_json_atomic(self._journal_path(), journal, durable=True)    # BEFORE the first move
        try:
            for n, r in enumerate(entries, 1):
                dst = self._target(r)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if existed[r]:
                    bak = os.path.join(backup, *r.split("/"))
                    os.makedirs(os.path.dirname(bak), exist_ok=True)
                    _retry(lambda: os.replace(dst, bak))                   # old file out of the way (allowed even if running)
                _retry(lambda: _move(os.path.join(staged, *r.split("/")), dst))
                report("install", n, len(entries), r)
        except BaseException as e:
            self._log("%s Install failed (%s: %s). Rolling back..." % (TAG, type(e).__name__, e))
            ok = self._rollback(journal)
            if ok:
                try:
                    os.remove(self._journal_path())
                except OSError:
                    pass
                shutil.rmtree(backup, ignore_errors=True)
            else:
                journal["state"] = "rollback_failed"
                _write_json_atomic(self._journal_path(), journal, durable=True)
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            raise UpdateError("Installing the update failed (%s). %s" % (
                e, "Your previous version was restored." if ok else
                "Restoring the previous version was incomplete; the old files are in %s" % backup)) from e
        journal["state"] = "applied"                                       # committed by recover() once the new version runs
        _write_json_atomic(self._journal_path(), journal, durable=True)

    def _rollback(self, journal):
        backup = os.path.join(self.work, "backup")
        clean = True
        for rel, existed in reversed(journal.get("entries", [])):
            target, bak = self._target(rel), os.path.join(backup, *rel.split("/"))
            try:
                if os.path.lexists(bak):                                    # old file was moved out: put it back
                    if os.path.lexists(target):
                        os.remove(target)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    _retry(lambda: os.replace(bak, target))
                elif not existed and os.path.lexists(target):               # a brand-new file we added
                    os.remove(target)
            except OSError as e:
                clean = False
                self._log("%s Could not restore %s (%s)" % (TAG, rel, e))
        return clean

    def recover(self):
        """Call at every startup. Undoes an interrupted update, or commits one that worked. Returns a message to show, or None."""
        path = self._journal_path()
        j = _read_json(path)
        backup, staging = os.path.join(self.work, "backup"), os.path.join(self.work, "staging")
        if not j:
            if os.path.isdir(self.work):                                    # nothing in flight: leftovers are garbage
                shutil.rmtree(staging, ignore_errors=True)
                shutil.rmtree(backup, ignore_errors=True)
            return None
        state = j.get("state")
        if state == "applying":
            if self._rollback(j):
                for p in (backup, staging):
                    shutil.rmtree(p, ignore_errors=True)
                try:
                    os.remove(path)
                except OSError:
                    pass
                return "A previous update was interrupted. Your earlier version has been restored."
            j["state"] = "rollback_failed"
            _write_json_atomic(path, j, durable=True)
            return "A previous update was interrupted and could not be fully undone. Old files: %s" % backup
        if state == "applied":                                              # we are the new version and we started: commit
            shutil.rmtree(backup, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)
            if not os.path.exists(backup):
                try:
                    os.remove(path)
                except OSError:
                    pass
            return None                                                     # else: old exe still locked, retry next start
        if state == "rollback_failed":
            return "An earlier update could not be undone completely. Old files: %s" % backup
        return None

    # ------------------------------------------------------------------ 7. restart
    def restart(self, extra_args=None):
        """Start the (new) program in a fresh process. The caller then exits."""
        args = [self.exe_path] + list(sys.argv[1:] if extra_args is None else extra_args)
        env = dict(os.environ)
        for k in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL",
                  "_PYI_SPLASH_IPC", "NUITKA_ONEFILE_PARENT"):            # a one-file build must not reuse OUR temp folder
            env.pop(k, None)
        if getattr(sys, "frozen", False) and "__compiled__" not in globals():
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        flags = 0
        if os.name == "nt":
            import ctypes
            has_console = bool(ctypes.windll.kernel32.GetConsoleWindow())
            flags = (0x00000010 if has_console else 0x00000008) | 0x00000200   # new console or detached, new process group
        self._spawn(args, env, flags)

    @staticmethod
    def _spawn(args, env, flags):
        subprocess.Popen(args, env=env, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=None, stderr=None)


# =====================================================================================
#  The dialog (Tkinter, created on the calling thread; the work runs on a background thread)
# =====================================================================================
class UpdateDialog:
    """Offer -> progress -> result. run() returns: installed | later | skipped | cancelled | failed | elevate | page"""

    def __init__(self, updater, rel):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.u, self.rel = tk, ttk, updater, rel
        self.mandatory = bool(getattr(rel, "mandatory", False))
        self._worker_thread = None
        self.outcome, self.phase = ("exit" if self.mandatory else "later"), "offer"
        self.q, self.cancel, self.samples = queue.Queue(), threading.Event(), []

        self.root = root = tk.Tk()
        root.title("Cesium AI Race Engineer - " + ("Update required" if self.mandatory else "Update"))
        w, h = 580, 440
        root.geometry("%dx%d+%d+%d" % (w, h, (root.winfo_screenwidth() - w) // 2, (root.winfo_screenheight() - h) // 3))
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            ttk.Style().theme_use("vista" if "vista" in ttk.Style().theme_names() else "clam")
        except tk.TclError:
            pass
        head = tk.Frame(root, bg="white", height=60)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="Cesium AI Race Engineer", bg="white", font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=20, pady=(10, 0))
        tk.Label(head, text=("Required software update" if self.mandatory else "Software update"), bg="white", fg="#555", font=("Segoe UI", 9)).pack(anchor="w", padx=20)
        ttk.Separator(root).pack(fill="x")
        body = ttk.Frame(root, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.pages = {k: ttk.Frame(body) for k in ("offer", "work", "result")}
        for f in self.pages.values():
            f.grid(row=0, column=0, sticky="nsew")
        self._build_offer(self.pages["offer"])
        self._build_work(self.pages["work"])
        self._build_result(self.pages["result"])
        self._show("offer")
        root.after(50, self._poll)
        root.lift()
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))

    # ------------------------------------------------------------------ pages
    def _build_offer(self, f):
        ttk, tk, rel, u = self.ttk, self.tk, self.rel, self.u
        ttk.Label(f, text="Version %s is %s" % (rel.tag, "required" if self.mandatory else "available"),
                  font=("Segoe UI", 13, "bold")).pack(anchor="w")
        sub = "You have %s." % u.current + ("  This is a pre-release." if rel.prerelease else "")
        if not u.can_self_update:
            sub += "  (Running from source: download it from the release page.)"
        ttk.Label(f, text=sub, wraplength=520).pack(anchor="w", pady=(2, 8))
        if self.mandatory:
            ttk.Label(f, text="This update is required. Cesium can't start until it is installed.",
                      foreground="#b00020", wraplength=520).pack(anchor="w", pady=(0, 6))
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        txt = tk.Text(box, height=9, wrap="word", font=("Segoe UI", 9), bg="#fafafa", relief="solid", borderwidth=1)
        txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, command=txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        txt.configure(yscrollcommand=sb.set)
        notes = (rel.body or "No release notes.").replace("\r\n", "\n").strip()
        txt.insert("1.0", notes[:3000] + ("\n..." if len(notes) > 3000 else ""))
        txt.configure(state="disabled")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(10, 0))
        if u.can_self_update:
            ttk.Button(bar, text="Update now", command=self._start).pack(side="right")
        else:
            ttk.Button(bar, text="Open release page", command=self._open_page).pack(side="right")
        if self.mandatory:                                   # no "Remind me later", no "Skip this version"
            ttk.Button(bar, text="Exit Cesium", command=self._exit_app).pack(side="right", padx=6)
        else:
            ttk.Button(bar, text="Remind me later", command=lambda: self._finish("later")).pack(side="right", padx=6)
            ttk.Button(bar, text="Skip this version", command=self._skip).pack(side="left")

    def _build_work(self, f):
        ttk = self.ttk
        f.columnconfigure(0, weight=1)
        self.work_title = ttk.Label(f, text="Preparing...", font=("Segoe UI", 13, "bold"))
        self.work_title.grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(f, mode="indeterminate", maximum=100)
        self.bar.grid(row=1, column=0, sticky="ew", pady=(14, 6))
        self.work_detail = ttk.Label(f, text="", wraplength=520, justify="left")
        self.work_detail.grid(row=2, column=0, sticky="w")
        ttk.Label(f, wraplength=520, justify="left", foreground="#555",
                  text="\nYour settings, models and data are not touched. If anything goes wrong, "
                       "your current version is kept.").grid(row=3, column=0, sticky="w")
        self.btn_cancel = ttk.Button(f, text="Cancel", command=self._cancel)
        self.btn_cancel.grid(row=4, column=0, sticky="e", pady=(24, 0))
        if self.mandatory:
            self.btn_cancel.grid_remove()                    # a forced update cannot be cancelled

    def _build_result(self, f):
        ttk = self.ttk
        f.columnconfigure(0, weight=1)
        self.res_title = ttk.Label(f, text="", font=("Segoe UI", 13, "bold"), wraplength=520)
        self.res_title.grid(row=0, column=0, sticky="w")
        self.res_text = ttk.Label(f, text="", wraplength=520, justify="left")
        self.res_text.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.res_bar = ttk.Frame(f)
        self.res_bar.grid(row=2, column=0, sticky="e", pady=(24, 0))

    def _show(self, name):
        self.pages[name].tkraise()

    # ------------------------------------------------------------------ actions
    def _finish(self, outcome):
        self.outcome = outcome
        self.root.destroy()

    def _skip(self):
        self.u.skip(self.rel.tag)
        self._finish("skipped")

    def _open_page(self):
        try:
            import webbrowser
            webbrowser.open(self.rel.url)
        except Exception:
            pass
        self._finish("page")

    def _browse(self):
        """Open the release page WITHOUT closing this window (used by the mandatory error screen)."""
        try:
            import webbrowser
            webbrowser.open(self.rel.url)
        except Exception:
            pass

    def _exit_app(self):
        """Mandatory update closed or refused: stop any download and leave. check_and_update() then calls sys.exit(0)."""
        if self.phase == "install":
            return                                           # files are being swapped: let it finish, never kill it
        self.cancel.set()
        t = self._worker_thread
        if t is not None and t.is_alive():
            t.join(2.0)                                      # let the download stop and release its lock file
        self._finish("exit")

    def _on_close(self):
        if self.phase == "install":
            return                                           # past the point of no return: let it finish
        if self.mandatory:
            self._exit_app()                                 # X = exit the whole program
            return
        if self.phase == "work":
            self._cancel()
        else:
            self._finish("later" if self.phase == "offer" else self.outcome)

    def _cancel(self):
        self.cancel.set()
        self.btn_cancel.state(["disabled"])
        self.work_title.configure(text="Cancelling...")

    def _start(self):
        self.phase = "work"
        self.cancel.clear()                                  # a Retry after a failure starts clean
        self.samples = []
        self.work_title.configure(text="Preparing...")
        self.work_detail.configure(text="")
        self.btn_cancel.state(["!disabled"])
        self.bar.stop()
        self.bar.configure(mode="indeterminate", maximum=100, value=0)
        self._show("work")
        self.bar.start(12)
        self._worker_thread = threading.Thread(target=self._worker, name="UpdateWorker", daemon=True)
        self._worker_thread.start()

    def _worker(self):
        try:
            self.u.install(self.rel, progress=lambda *a: self.q.put(("p",) + a), cancel=self.cancel)
            self.q.put(("ok",))
        except Cancelled:
            self.q.put(("cancelled",))
        except NeedsAdmin as e:
            self.q.put(("admin", str(e)))
        except UpdateError as e:
            self.q.put(("err", str(e)))
        except Exception as e:
            self.q.put(("err", "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc(limit=3))))

    # ------------------------------------------------------------------ progress -> screen (main thread only)
    @staticmethod
    def _mb(n):
        return "%.1f MB" % (n / 1048576.0)

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "p":
                    self._progress(*msg[1:])
                elif kind == "ok":
                    self.bar.stop()
                    self._result("installed", "Update installed", "Restarting Cesium now...", [])
                    self.root.after(1200, lambda: self._finish("installed"))
                elif kind == "cancelled":
                    self._finish("exit" if self.mandatory else "cancelled")
                elif kind == "admin":
                    self._show_admin(msg[1])
                elif kind == "err":
                    self._show_error(msg[1])
        except queue.Empty:
            pass
        try:
            self.root.after(50, self._poll)
        except self.tk.TclError:
            pass

    def _show_admin(self, text):
        tail = ("\n\nRestart Cesium as administrator to install this update, or install the "
                "program in a folder your account can write to.")
        if self.mandatory:
            self._result("failed", "Administrator permission needed", text + tail,
                         [("Exit Cesium", lambda: self._finish("exit")),
                          ("Restart as administrator", lambda: self._finish("elevate"))])
        else:
            self._result("failed", "Administrator permission needed", text + tail,
                         [("Restart as administrator", lambda: self._finish("elevate")), ("Close", None)])

    def _show_error(self, text):
        if self.mandatory:
            self._result("failed", "The update didn't install",
                         text + "\n\nThis update is required, so Cesium can't start without it. Your previous "
                                "version was restored (or never touched). Press Retry, or exit and download the "
                                "update by hand.",
                         [("Open release page", self._browse), ("Exit Cesium", lambda: self._finish("exit")),
                          ("Retry", self._start)])
        else:
            self._result("failed", "The update didn't install", text +
                         "\n\nYour current version was not changed. You can keep using it, "
                         "or download the update by hand.",
                         [("Open release page", self._open_page), ("Close", None)])

    def _progress(self, phase, done, total, text):
        titles = {"download": "Downloading update...", "verify": "Verifying download...",
                  "extract": "Unpacking...", "install": "Installing..."}
        self.phase = "install" if phase == "install" else "work"
        if phase == "install":
            self.btn_cancel.state(["disabled"])
        if total and self.bar.cget("mode") != "determinate":
            self.bar.stop()
            self.bar.configure(mode="determinate")
        if total:
            self.bar.configure(maximum=total, value=done)
        detail = ""
        if phase == "download":
            now = time.monotonic()
            self.samples = [s for s in self.samples if now - s[0] <= 3.0] + [(now, done)]
            detail = "%s of %s" % (self._mb(done), self._mb(total) if total else "?")
            if len(self.samples) > 1 and self.samples[-1][0] > self.samples[0][0]:
                rate = (self.samples[-1][1] - self.samples[0][1]) / (self.samples[-1][0] - self.samples[0][0])
                if rate > 0:
                    detail += "   -   %s/s" % self._mb(rate)
                    if total and total > done:
                        detail += "   -   about %d s left" % ((total - done) / rate)
        elif phase == "install":
            detail = "%d of %d files" % (done, total)
        elif total:
            detail = "%d%%" % (done * 100 // total)
        self.work_title.configure(text=titles.get(phase, "Working..."))
        self.work_detail.configure(text=detail)

    def _result(self, outcome, title, text, buttons):
        self.phase, self.outcome = "result", outcome
        self.res_title.configure(text=title)
        self.res_text.configure(text=text)
        for w in self.res_bar.winfo_children():
            w.destroy()
        for label, cmd in reversed(buttons):
            self.ttk.Button(self.res_bar, text=label, command=cmd or (lambda: self._finish("failed"))).pack(side="right", padx=4)
        self._show("result")

    def run(self):
        self.root.mainloop()
        return self.outcome


# =====================================================================================
#  The two entry points the app uses
# =====================================================================================
def _enabled():
    return (getattr(config, "USE_UPDATER", True) and os.environ.get("CESIUM_NO_UPDATE") != "1"
            and "--no-update" not in sys.argv)


def _tell_user(text):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("Cesium AI Race Engineer", text, parent=root)
        root.destroy()
    except Exception:
        pass


def _exit_now(on_before_exit=None, code=0):
    """Leave the program at once (a mandatory update was not installed)."""
    if on_before_exit:
        try:
            on_before_exit()
        except Exception:
            pass
    sys.exit(code)


def _install_without_window(u, rel, log):
    """Mandatory update but no usable window: run the same pipeline and write its progress to the log.
    Returns installed | elevate | failed."""
    last = [0.0]

    def progress(phase, done, total, _text):
        now = time.monotonic()
        if now - last[0] >= 2.0 or (total and done >= total):
            last[0] = now
            log("%s %s: %s / %s" % (TAG, phase, done, total or "?"))
    try:
        u.install(rel, progress=progress)
        return "installed"
    except NeedsAdmin as e:
        log("%s %s" % (TAG, e))
        return "elevate"
    except Exception as e:                                    # UpdateError, Cancelled, anything unexpected
        log("%s The mandatory update failed (%s: %s)." % (TAG, type(e).__name__, e))
        return "failed"


def check_and_update(current_version=None, show_ui=True, log=print, on_before_exit=None,
                     exit_after_restart=True, updater=None, timeout=6.0):
    """Call once at startup. Returns one of:
        disabled | up-to-date | available | installed | restarting | later | skipped | cancelled | failed | error
    After a successful install it starts the new version and exits this process (unless exit_after_restart=False).

    MANDATORY updates (see the module docstring) never return "later" / "skipped" / "cancelled" / "failed": if the new
    version is not installed, this function ends the program with sys.exit(0). That is a SystemExit, so it passes through
    the `except Exception` around the call in main_pitwall.py, exactly as intended."""
    if not _enabled():
        return "disabled"
    try:
        u = updater or Updater(current_version=current_version, log=log, timeout=timeout)
        note = u.recover() if u.can_self_update else None     # undoes a half-finished (forced) update FIRST
        if note:
            log("%s %s" % (TAG, note))
            if show_ui:
                _tell_user(note)
        rel = u.check()
    except Exception as e:                                   # an update problem must never stop the engineer
        log("%s Update check skipped (%s: %s)" % (TAG, type(e).__name__, e))
        return "error"
    if rel is None:
        return "up-to-date"
    mandatory = bool(getattr(rel, "mandatory", False))
    log("%s Update available: %s (you have %s). %s%s" % (
        TAG, rel.tag, u.current, rel.url,
        ("  MANDATORY: %s." % rel.mandatory_reason) if mandatory else ""))
    if not u.can_self_update:
        return "available"                                   # from source: just a notice, never an overwrite
    if not show_ui and not mandatory:
        return "available"

    if show_ui:
        try:
            outcome = UpdateDialog(u, rel).run()
        except Exception as e:                               # no display / no Tk
            log("%s Can't show the update window (%s: %s)." % (TAG, type(e).__name__, e))
            if not mandatory:
                return "available"
            outcome = _install_without_window(u, rel, log)
    else:
        outcome = _install_without_window(u, rel, log)       # mandatory, windowless caller

    if outcome == "elevate":
        if relaunch_elevated():
            _exit_now(on_before_exit)
        if mandatory:
            _tell_user("Administrator permission was not granted. This update is required, so Cesium will close.")
            _exit_now(on_before_exit)
        return "failed"
    if outcome == "installed":
        try:
            u.restart()
        except Exception as e:
            log("%s Installed, but couldn't restart automatically (%s). Please start Cesium again." % (TAG, e))
            _tell_user("The update is installed. Please start Cesium again.")
            if mandatory:
                _exit_now(on_before_exit)                    # this process still runs the OLD code: never carry on
            return "installed"
        if exit_after_restart:
            _exit_now(on_before_exit)
        return "restarting"
    if mandatory:                                            # closed, exited, failed, cancelled: all mean "leave"
        log("%s This update is mandatory and was not installed. Closing Cesium." % TAG)
        _exit_now(on_before_exit)
    return outcome


def check_async(on_result, current_version=None, log=print):
    """Background check for apps that show their own UI: on_result(Release or None) is called from a worker thread."""
    def work():
        try:
            on_result(Updater(current_version=current_version, log=log).check())
        except Exception as e:
            log("%s Update check skipped (%s: %s)" % (TAG, type(e).__name__, e))
            on_result(None)
    t = threading.Thread(target=work, name="UpdateCheck", daemon=True)
    t.start()
    return t


# =====================================================================================
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Check for Cesium AI Race Engineer updates.")
    ap.add_argument("--check", action="store_true", help="ask GitHub and print what would happen (default)")
    ap.add_argument("--current", default=None, help="pretend this is the installed version, e.g. v0.0.1")
    ap.add_argument("--dialog", action="store_true", help="show the update window if an update exists")
    ap.add_argument("--force", action="store_true", help="pretend FORCE_UPDATE is on (see the mandatory window)")
    a = ap.parse_args(argv)
    if a.force and config is not None:
        config.FORCE_UPDATE = True
    u = Updater(current_version=a.current)
    print("%s Installed: %s   pre-releases: %s   can update itself: %s" %
          (TAG, u.current, "yes" if u.include_prereleases else "no", "yes" if u.can_self_update else "no (source run)"))
    rel = u.check()
    if rel is None:
        return 0
    print("%s Newer release: %s%s" % (TAG, rel.tag, " (pre-release)" if rel.prerelease else ""))
    print("   mandatory: %s" % (("YES - " + rel.mandatory_reason) if rel.mandatory else "no"))
    print("   page:      %s" % rel.url)
    print("   package:   %s" % (("%s, %.1f MB" % (rel.asset.name, rel.asset.size / 1048576.0)) if rel.asset else "NONE (no .zip asset)"))
    print("   checksum:  %s" % ("GitHub digest" if rel.asset and rel.asset.digest else
                                ("sidecar file" if rel.checksum_assets else "NONE (the update would be refused)")))
    if a.dialog:
        print("%s Closed with: %s" % (TAG, UpdateDialog(u, rel).run()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------------------
# PACKAGING
#   * Nothing extra to bundle: standard library only. tkinter is imported lazily inside the dialog; if you build with
#     Nuitka add  --enable-plugin=tk-inter  (the installer needs it anyway).
#   * The release .zip should be a ZIP of the build output folder (PyInstaller --onedir: the .exe + _internal/, or just
#     the .exe for --onefile), with the .exe name unchanged from release to release.
#   * Publish the zip's SHA-256 (GitHub adds a "digest" to new uploads automatically; or upload  <name>.zip.sha256).
#   * Do NOT put config.APP_VERSION in a file the updater preserves; the app's version must change with the build.
#   * Optional config.py settings:  APP_VERSION = "v0.1.0-beta"   USE_UPDATER = True   UPDATE_PRESERVE = (...)
#                                   UPDATE_ASSET_PATTERN = "*win*.zip"   UPDATE_INCLUDE_PRERELEASES = True
#                                   FORCE_UPDATE = False   (True = every newer release is mandatory)
#   * To force ONE release: put  [mandatory]  on its own line in the release notes, or upload update.json next to the zip.
# ---------------------------------------------------------------------------------------
