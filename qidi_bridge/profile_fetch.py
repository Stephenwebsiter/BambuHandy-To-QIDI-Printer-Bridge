"""Download the official QIDI printer presets when the installed slicer keeps its own packed away.

Newer OrcaSlicer builds store bundled presets in a packed format the bridge
can't read. In that case we download the same presets (plain JSON) from the
OrcaSlicer project once and keep them in data/profiles. Only the files the
Q2 needs are fetched: its printer, quality and filament presets plus the
presets they build on.
"""

from __future__ import annotations

import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config

BASE_URL = os.environ.get(
    "QIDI_BRIDGE_PROFILE_URL",
    "https://raw.githubusercontent.com/SoftFever/OrcaSlicer/main/resources/profiles",
)
VENDOR = "Qidi"
CACHE_ROOT = os.path.join(config.DATA_DIR, "profiles")
MARKER = "q2"


def cache_dir() -> str:
    return os.path.join(CACHE_ROOT, VENDOR)


def have_cache() -> bool:
    return os.path.isfile(os.path.join(cache_dir(), ".complete"))


def _get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "qidi-q2-bridge"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _quote(path: str) -> str:
    return urllib.request.quote(path)


def fetch_q2_profiles(log=print, marker: str = MARKER) -> str:
    """Download Q2 presets (and their parents) into the cache. Returns the cache folder."""
    index = json.loads(_get(f"{BASE_URL}/{_quote(VENDOR)}.json"))
    by_type = {}
    for key, ptype in (("machine_list", "machine"), ("process_list", "process"), ("filament_list", "filament"),
                       ("machine_model_list", "model")):
        by_type[ptype] = {e["name"]: e["sub_path"] for e in index.get(key, []) if e.get("name") and e.get("sub_path")}
    out = cache_dir()
    os.makedirs(out, exist_ok=True)

    def download(sub_path: str) -> dict | None:
        dest = os.path.join(out, *sub_path.split("/"))
        if os.path.isfile(dest):
            with open(dest, "rb") as fh:
                raw = fh.read()
        else:
            raw = _get(f"{BASE_URL}/{_quote(VENDOR)}/{_quote(sub_path)}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".tmp", "wb") as fh:
                fh.write(raw)
            os.replace(dest + ".tmp", dest)
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except ValueError:
            return None

    todo = {(t, n) for t in ("machine", "process", "filament") for n in by_type[t] if marker in n.lower()}
    done: set[tuple[str, str]] = set()
    log(f"Downloading {len(todo)} QIDI Q2 presets (one time only)…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        while todo:
            batch = sorted(todo - done)
            todo = set()
            results = list(pool.map(lambda tn: (tn, download(by_type[tn[0]][tn[1]])), batch))
            for (ptype, name), data in results:
                done.add((ptype, name))
                parent = (data or {}).get("inherits")
                if parent and parent in by_type[ptype] and (ptype, parent) not in done:
                    todo.add((ptype, parent))
    for model, sub in by_type["model"].items():
        if marker in model.lower():
            try:
                download(sub)
            except OSError:
                pass
    with open(os.path.join(out, ".complete"), "w") as fh:
        fh.write(str(index.get("version", "")))
    log(f"Saved {len(done)} presets to {out}")
    return out
