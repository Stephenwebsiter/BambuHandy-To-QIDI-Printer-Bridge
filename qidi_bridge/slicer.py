"""Run OrcaSlicer / QIDI Studio from the command line to slice for the Q2."""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

from . import profile_fetch, profiles, threemf


class SliceError(Exception):
    pass


_LIB_CACHE: dict[str, profiles.PresetLibrary] = {}


def library(slicer_exe: str, refresh: bool = False, log=None) -> profiles.PresetLibrary:
    """Presets for the Q2: from the slicer's own folders, or downloaded if the slicer keeps them packed."""
    key = os.path.abspath(slicer_exe) if slicer_exe else ""
    if refresh or key not in _LIB_CACHE:
        roots = profiles.profile_roots_for(slicer_exe)
        if profile_fetch.have_cache():
            roots.append((profile_fetch.CACHE_ROOT, False))
        lib = profiles.PresetLibrary(roots)
        if not any("q2" in n.lower() for n in lib.names("machine")):
            try:
                profile_fetch.fetch_q2_profiles(log or print)
            except OSError as e:
                raise SliceError(
                    "This slicer's QIDI Q2 settings can't be read, and downloading them failed "
                    f"({e}). Check the PC's internet connection and try again."
                ) from e
            roots = profiles.profile_roots_for(slicer_exe) + [(profile_fetch.CACHE_ROOT, False)]
            lib = profiles.PresetLibrary(roots)
        _LIB_CACHE[key] = lib
    return _LIB_CACHE[key]


def resolve_slicer(cfg: dict) -> str:
    exe = cfg.get("slicer_path") or ""
    if exe and os.path.isdir(exe):
        exe = profiles.find_slicer_executable(exe) or ""
    if not exe:
        exe = profiles.detect_slicer() or ""
    if not exe or not os.path.isfile(exe):
        raise SliceError(
            "No slicer found. Install OrcaSlicer (free, orcaslicer.com) or QIDI Studio on this PC, "
            "or set its location in Settings."
        )
    return exe


def project_filament_count(path: str) -> int:
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            cfg = json.loads(z.read("Metadata/project_settings.config").decode("utf-8", "replace"))
        ids = cfg.get("filament_settings_id") or []
        return max(1, len(ids))
    except Exception:
        return 1


def _read_result(out_dir: str) -> str:
    """Bambu-family slicers write result.json with an error message when they fail."""
    for name in ("result.json", "slice_info.json"):
        p = os.path.join(out_dir, name)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
                msg = data.get("error_string") or data.get("message") or ""
                code = data.get("return_code")
                if msg or code:
                    return f"{msg} (code {code})" if code else msg
            except (OSError, ValueError):
                pass
    return ""


def run_cli(exe: str, inputs: list[str], settings: list[str], filaments: list[str], out_dir: str,
            plate: int, arrange: bool, log, timeout: int = 900) -> str:
    os.makedirs(out_dir, exist_ok=True)
    out_3mf = os.path.join(out_dir, "result.gcode.3mf")
    cmd = [exe,
           "--slice", str(plate),
           "--load-settings", ";".join(settings),
           "--load-filaments", ";".join(filaments),
           "--arrange", "1" if arrange else "0",
           "--orient", "0",
           "--allow-newer-file",
           "--outputdir", out_dir,
           # Relative on purpose: the slicer puts it inside --outputdir itself.
           "--export-3mf", os.path.basename(out_3mf),
           *inputs]
    log("Running slicer: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                              cwd=out_dir, **kwargs)
    except subprocess.TimeoutExpired as e:
        raise SliceError("Slicing took too long and was stopped.") from e
    except OSError as e:
        raise SliceError(f"Could not start the slicer: {e}") from e
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-25:])
    if tail:
        log(tail)
    gcodes = sorted(glob.glob(os.path.join(out_dir, "*.gcode")))
    if proc.returncode == 0 or gcodes or os.path.isfile(out_3mf):
        if gcodes:
            return gcodes[0]
        if os.path.isfile(out_3mf):
            info = threemf.inspect(out_3mf)
            if info["kind"] == "sliced_3mf":
                wanted = plate if plate in info["plates"] else info["plates"][0]
                return threemf.extract_gcode(out_3mf, wanted, os.path.join(out_dir, f"plate_{wanted}.gcode"))
    reason = _read_result(out_dir) or f"exit code {proc.returncode}"
    raise SliceError(f"The slicer could not slice this model: {reason}")


def slice_for_q2(cfg: dict, src: str, plate: int, work_dir: str, log,
                 process: str | None = None, filament: str | None = None) -> str:
    """Slice `src` with QIDI Q2 settings and return the path of the resulting G-code."""
    exe = resolve_slicer(cfg)
    lib = library(exe, log=log)
    machine = cfg.get("machine_preset") or "Qidi Q2 0.4 nozzle"
    process = process or cfg.get("process_preset")
    filament = filament or cfg.get("filament_preset")
    log(f"Slicer: {exe}")
    log(f"Settings: printer '{machine}', quality '{process}', filament '{filament}'")
    preset_dir = os.path.join(work_dir, "presets")
    try:
        m_file = lib.write_resolved("machine", machine, preset_dir)
        p_file = lib.write_resolved("process", process, preset_dir)
        f_file = lib.write_resolved("filament", filament, preset_dir)
    except KeyError as e:
        raise SliceError(str(e.args[0]) + " Pick another one in Settings.") from e

    lower = src.lower()
    if lower.endswith(".3mf"):
        colours = project_filament_count(src)
        if colours > 1 and not cfg.get("qidi_box"):
            log(f"This project uses {colours} colours. Without a QIDI Box it will be printed in one colour.")
            meshes = threemf.export_plate_meshes(src, plate, os.path.join(work_dir, "meshes"))
            return run_cli(exe, meshes, [m_file, p_file], [f_file], os.path.join(work_dir, "out"), 0, True, log)
        try:
            return run_cli(exe, [src], [m_file, p_file], [f_file] * colours,
                           os.path.join(work_dir, "out"), plate, False, log)
        except SliceError as first:
            log(f"Slicing the project file directly did not work ({first}). Trying again with just the shapes.")
            meshes = threemf.export_plate_meshes(src, plate, os.path.join(work_dir, "meshes"))
            return run_cli(exe, meshes, [m_file, p_file], [f_file], os.path.join(work_dir, "out2"), 0, True, log)
    return run_cli(exe, [src], [m_file, p_file], [f_file], os.path.join(work_dir, "out"), 0, True, log)
