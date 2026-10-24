"""
credentials.py - API keys stored per Windows user, encrypted with DPAPI (CryptProtectData).
File: %APPDATA%\\CesiumAIRaceEngineer\\credentials.json   (keys are base64 DPAPI blobs, never plaintext on Windows)
Standard library only: safe for Nuitka / PyInstaller.
"""
import base64
import ctypes
import json
import os
import threading
from ctypes import wintypes

APP_DIR_NAME = "CesiumAIRaceEngineer"
TAG = "[CREDS]"
_lock = threading.Lock()


def credentials_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME, "credentials.json")


# ---------------------------------------------------------------- DPAPI
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    inp, _keep = _blob(data)            # _keep holds the buffer alive during the call
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _seal(text: str) -> str:
    if os.name != "nt":
        return "plain:" + text            # dev machines only; never ship this path
    return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def _unseal(token: str) -> str:
    kind, _, body = token.partition(":")
    if kind == "plain":
        return body
    return _dpapi(base64.b64decode(body), False).decode("utf-8")


# ---------------------------------------------------------------- store
def _read():
    try:
        with open(credentials_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"{TAG} could not read credentials ({type(e).__name__}: {e})")
        return {}


def _write(data):
    path = credentials_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def add_key(provider: str, label: str, key: str, priority: int = 100) -> bool:
    """Save or replace one credential. Lower priority number = tried first."""
    try:
        with _lock:
            data = _read()
            data.setdefault("version", 1)
            entries = [e for e in data.setdefault("keys", [])
                       if not (e["provider"] == provider and e["label"] == label)]
            entries.append({"provider": provider, "label": label, "priority": int(priority), "secret": _seal(key)})
            data["keys"] = entries
            _write(data)
        return True
    except Exception as e:
        print(f"{TAG} could not save key ({type(e).__name__}: {e})")
        return False


def remove_key(provider: str, label: str) -> bool:
    with _lock:
        data = _read()
        before = len(data.get("keys", []))
        data["keys"] = [e for e in data.get("keys", []) if not (e["provider"] == provider and e["label"] == label)]
        _write(data)
        return len(data["keys"]) < before


def load_keys(provider: str):
    """[(label, key, priority)] sorted by priority. Env var ANTHROPIC_API_KEY is appended as a last resort."""
    out = []
    for e in _read().get("keys", []):
        if e.get("provider") != provider:
            continue
        try:
            out.append((e["label"], _unseal(e["secret"]), int(e.get("priority", 100))))
        except Exception as ex:
            print(f"{TAG} skipping '{e.get('label')}' (cannot decrypt: {ex}). Keys are tied to this Windows user.")
    out.sort(key=lambda t: t[2])
    env = os.environ.get("ANTHROPIC_API_KEY", "") if provider == "anthropic" else ""
    if env and all(k != env for _, k, _ in out):
        out.append(("env", env, 1000))
    return out


def list_labels(provider: str):
    """For the GUI: labels only, never the secrets."""
    return [e["label"] for e in _read().get("keys", []) if e.get("provider") == provider]


if __name__ == "__main__":      # python credentials.py  -> quick self-test (uses a throwaway label)
    assert add_key("test", "selftest", "sk-test-123")
    print("round trip:", [k for _, k, _ in load_keys("test")] == ["sk-test-123"])
    remove_key("test", "selftest")
