"""
fetch.py - one safe, resumable file downloader (no extra packages, no account).
Used for the built-in AI model and the natural voice. A broken or cut-off download never leaves a half file behind
that looks finished: data goes to <name>.part and is renamed only when complete.
"""
import os
import time


def download(url, dest, min_bytes=1, label="a file", log=print, retries=3):
    """Download url -> dest, resuming a partial download if one exists. Returns dest, or None on failure."""
    import urllib.error                      # loaded only when something is really downloaded
    import urllib.request
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    for attempt in range(1, retries + 1):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pitwall-engineer"})
            if have:
                req.add_header("Range", f"bytes={have}-")
            try:
                resp = urllib.request.urlopen(req, timeout=30)
            except urllib.error.HTTPError as e:
                if e.code == 416 and have:           # we already have every byte
                    resp = None
                else:
                    raise
            if resp is not None:
                with resp:
                    if have and getattr(resp, "status", 200) != 206:
                        have = 0                      # server ignored the resume request: start over
                    total = int(resp.headers.get("Content-Length") or 0) + have
                    done, shown = have, -1
                    with open(part, "ab" if have else "wb") as f:
                        while True:
                            chunk = resp.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            if total and done * 10 // total != shown:
                                shown = done * 10 // total
                                log(f"downloading {label}... {done * 100 // total}%")
                    if total and done < total:
                        raise IOError("download ended early")
            if os.path.getsize(part) < min_bytes:
                raise IOError("downloaded file is too small")
            os.replace(part, dest)
            return dest
        except Exception as e:
            log(f"download problem with {label} ({type(e).__name__}: {e}), attempt {attempt} of {retries}")
            if "too small" in str(e):
                try:
                    os.remove(part)
                except OSError:
                    pass
            if attempt < retries:
                time.sleep(2 * attempt)
    return None
