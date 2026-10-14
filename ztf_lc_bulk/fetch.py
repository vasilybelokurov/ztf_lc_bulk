"""Obtain IRSA ZTF parquet files: local mirror if present, else HTTPS download."""

from __future__ import annotations

import time
from pathlib import Path

import requests

from . import IRSA_BASE_URL

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
CHUNK = 4 * 1024 * 1024


class FetchError(RuntimeError):
    pass


def _sleep_for(attempt: int, resp: requests.Response | None) -> float:
    if resp is not None and "Retry-After" in resp.headers:
        try:
            return min(120.0, float(resp.headers["Retry-After"]))
        except ValueError:
            pass
    return min(60.0, 2.0 * 2 ** attempt)


def download(rel: str, dest_root: Path, base_url: str = IRSA_BASE_URL,
             session: requests.Session | None = None, retries: int = 5,
             timeout: float = 120.0) -> tuple[Path, int]:
    """Download ``base_url + rel`` to ``dest_root / rel``.

    Writes to ``*.part`` and renames only after the byte count matches
    Content-Length, so a file present under its final name is complete.

    Returns
    -------
    (path, n_bytes_downloaded); n_bytes is 0 if the file was already present.
    """
    dest = dest_root / rel
    if dest.exists():
        return dest, 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    sess = session or requests.Session()
    url = base_url + rel
    last = None
    for attempt in range(retries):
        resp = None
        try:
            resp = sess.get(url, stream=True, timeout=timeout)
            if resp.status_code in RETRY_STATUS:
                raise FetchError(f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                # 404 etc.: not retryable
                raise FetchError(f"HTTP {resp.status_code} (fatal) for {url}")
            expected = int(resp.headers.get("Content-Length", -1))
            n = 0
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(CHUNK):
                    fh.write(chunk)
                    n += len(chunk)
            if expected >= 0 and n != expected:
                raise FetchError(f"short read {n}/{expected}")
            part.rename(dest)
            return dest, n
        except (requests.RequestException, FetchError) as e:
            last = e
            if "(fatal)" in str(e):
                break
            time.sleep(_sleep_for(attempt, resp))
        finally:
            if resp is not None:
                resp.close()
    part.unlink(missing_ok=True)
    raise FetchError(f"failed after {retries} attempts: {url}: {last}")


def resolve(rel: str, raw_dir: Path, mirror: Path | None = None,
            **kw) -> tuple[Path, int, bool]:
    """Return (local path, bytes downloaded, is_temporary)."""
    if mirror is not None:
        p = Path(mirror) / rel
        if p.exists():
            return p, 0, False
    path, n = download(rel, raw_dir, **kw)
    return path, n, True
