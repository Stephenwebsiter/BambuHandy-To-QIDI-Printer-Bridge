"""Download the official QIDI printer presets when the installed slicer keeps its own packed away.

Newer OrcaSlicer builds store bundled presets in a packed format the bridge
can't read. In that case we download the same presets (plain JSON) from the
OrcaSlicer project once and keep them in data/profiles. Only the files the
chosen printer model needs are fetched: its printer, quality and filament
presets plus the presets they build on.
"""

from __future__ import annotations

import json
import os
import re
import time
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


def marker_for(machine_preset: str) -> str:
    """'Qidi X-Plus 4 0.4 nozzle' -> 'xplus4', used to pick that model's presets by name."""
    from .profiles import model_of
    return re.sub(r"[^a-z0-9]", "", model_of(machine_preset).lower()) or MARKER


def _flat(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def fetched_markers() -> set[str]:
    try:
        with open(os.path.join(cache_dir(), ".complete"), encoding="utf-8") as fh:
            lines = [x.strip() for x in fh.read().splitlines() if x.strip()]
    except OSError:
        return set()
    # Older bridges wrote only a version number here, after fetching the Q2 presets.
    return {x[len("marker:"):] for x in lines if x.startswith("marker:")} or {MARKER}


def _index() -> dict:
    """The list of every QIDI preset in OrcaSlicer (cached for a day)."""
    path = os.path.join(CACHE_ROOT, f"{VENDOR}.json")
    try:
        if time.time() - os.path.getmtime(path) < 86400:
            with open(path, "rb") as fh:
                return json.loads(fh.read())
    except (OSError, ValueError):
        pass
    try:
        raw = _get(f"{BASE_URL}/{_quote(VENDOR)}.json")
    except OSError:
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return json.loads(fh.read())
        raise
    os.makedirs(CACHE_ROOT, exist_ok=True)
    with open(path + ".tmp", "wb") as fh:
        fh.write(raw)
    os.replace(path + ".tmp", path)
    return json.loads(raw)


def available_machines() -> list[str]:
    """Every QIDI printer + nozzle that OrcaSlicer knows, e.g. 'Qidi Plus4 0.4 nozzle'."""
    try:
        idx = _index()
    except (OSError, ValueError):
        return []
    return sorted({e["name"] for e in idx.get("machine_list", []) if "nozzle" in e.get("name", "")}, key=str.lower)


def _get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "qidi-q2-bridge"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _quote(path: str) -> str:
    return urllib.request.quote(path)


def fetch_q2_profiles(log=print, marker: str = MARKER) -> str:
    return fetch_profiles(log, marker)


def fetch_profiles(log=print, marker: str = MARKER) -> str:
    """Download one printer model's presets (and their parents) into the cache. Returns the cache folder."""
    marker = _flat(marker)
    index = _index()
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

    todo = {(t, n) for t in ("machine", "process", "filament") for n in by_type[t] if marker in _flat(n)}
    done: set[tuple[str, str]] = set()
    if not todo:
        raise OSError(f"OrcaSlicer has no presets for '{marker}'")
    log(f"Downloading {len(todo)} QIDI presets for this printer (one time only)…")
    with ThreadPoolExecutor(max_workers=12) as pool:
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
        if marker in _flat(model):
            try:
                download(sub)
            except OSError:
                pass
    markers = (fetched_markers() if have_cache() else set()) | {marker}
    with open(os.path.join(out, ".complete"), "w") as fh:
        fh.write(f"version:{index.get('version', '')}\n" + "".join(f"marker:{m}\n" for m in sorted(markers)))
    log(f"Saved {len(done)} presets to {out}")
    return out
