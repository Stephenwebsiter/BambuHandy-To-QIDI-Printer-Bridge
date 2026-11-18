"""Run OrcaSlicer / QIDI Studio from the command line to slice for the Q2."""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

from . import profile_fetch, profiles, threemf


DEFAULT_MACHINE = "Qidi Q2 0.4 nozzle"


# Bed plates OrcaSlicer knows, with the filament setting that holds each plate's bed temperature.
PLATES = {
    "Textured PEI Plate": "textured_plate_temp",
    "High Temp Plate": "hot_plate_temp",
    "Engineering Plate": "eng_plate_temp",
    "Cool Plate": "cool_plate_temp",
    "Textured Cool Plate": "textured_cool_plate_temp",
    "Supertack Plate": "supertack_plate_temp",
}
PLATE_LABELS = {
    "Textured PEI Plate": "Textured PEI (gold/black, rough)",
    "High Temp Plate": "Smooth PEI / High Temp plate",
    "Engineering Plate": "Engineering plate",
    "Cool Plate": "Smooth Cool plate",
    "Textured Cool Plate": "Textured Cool plate",
    "Supertack Plate": "SuperTack plate",
}
DEFAULT_PLATE = "Textured PEI Plate"


def _num(v, default=0.0) -> float:
    if isinstance(v, list):
        v = v[0] if v else default
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return default


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def process_overrides(data: dict, plate: str, grip: bool) -> dict:
    """Quality settings changed for the bed plate and, with `grip`, for a first layer that sticks well."""
    over = {"curr_bed_type": plate if plate in PLATES else DEFAULT_PLATE}
    if grip:
        # A slower first layer presses the plastic onto the bed instead of dragging it.
        for key, limit in (("initial_layer_speed", 35), ("initial_layer_infill_speed", 60)):
            cur = _num(data.get(key), limit)
            if cur <= 0 or cur > limit:
                over[key] = _fmt(limit)
        if str(data.get("brim_type", "auto_brim")) == "no_brim":
            over["brim_type"] = "auto_brim"
    return over


def filament_overrides(data: dict, plate: str, grip: bool) -> dict:
    over = {}
    if grip:
        # Keep the part-cooling fan off for the first 3 layers so they stay warm and stick.
        if _num(data.get("close_fan_the_first_x_layers"), 1) < 3:
            over["close_fan_the_first_x_layers"] = ["3"]
    key = PLATES.get(plate, PLATES[DEFAULT_PLATE])
    first = _num(data.get(key + "_initial_layer"), 0)
    later = _num(data.get(key), 0)
    if grip and first > 0 and later > 0 and first < later + 5 and normalize_family(data) in ("PLA", "PETG"):
        # A few degrees warmer for the first layer only helps PLA/PETG grip, without warping.
        over[key + "_initial_layer"] = [_fmt(later + 5)]
    return over


# What the phone page may change, and the values it may use (anything else is ignored).
TWEAKS = {
    "infill": ("sparse_infill_density", {"10": "10%", "15": "15%", "25": "25%", "40": "40%", "60": "60%", "100": "100%"}),
    "walls": ("wall_loops", {"2": "2", "3": "3", "4": "4", "6": "6"}),
    "top": ("top_shell_layers", {"3": "3", "5": "5", "7": "7"}),
}


def tweak_overrides(tweaks: dict | None) -> dict:
    """Turn the phone page's simple choices into slicer settings."""
    out = {}
    t = tweaks or {}
    for key, (setting, allowed) in TWEAKS.items():
        v = str(t.get(key, "")).strip()
        if v in allowed:
            out[setting] = allowed[v]
    sup = str(t.get("supports", ""))
    if sup == "off":
        out["enable_support"] = "0"
    elif sup in ("tree", "normal"):
        out["enable_support"] = "1"
        out["support_type"] = f"{sup}(auto)"
        if t.get("support_bed_only"):
            out["support_on_build_plate_only"] = "1"
    brim = str(t.get("brim", ""))
    if brim == "off":
        out["brim_type"] = "no_brim"
    elif brim == "auto":
        out["brim_type"] = "auto_brim"
    elif brim == "on":
        out["brim_type"] = "outer_only"
        out["brim_width"] = "5"
    return out


def normalize_family(data: dict) -> str:
    t = data.get("filament_type")
    if isinstance(t, list):
        t = t[0] if t else ""
    return str(t or "").upper().split("-")[0]


def bed_temp(data: dict, plate: str) -> tuple[int, int]:
    key = PLATES.get(plate, PLATES[DEFAULT_PLATE])
    return int(_num(data.get(key + "_initial_layer"), 0)), int(_num(data.get(key), 0))


class SliceError(Exception):
    pass


_LIB_CACHE: dict[tuple, profiles.PresetLibrary] = {}


def library(slicer_exe: str, refresh: bool = False, log=None, machine: str | None = None) -> profiles.PresetLibrary:
    """Presets for the chosen QIDI printer: from the slicer's own folders, or downloaded if it keeps them packed."""
    machine = machine or DEFAULT_MACHINE
    key = (os.path.abspath(slicer_exe) if slicer_exe else "", machine)
    if refresh or key not in _LIB_CACHE:
        roots = profiles.profile_roots_for(slicer_exe)
        if profile_fetch.have_cache():
            roots.append((profile_fetch.CACHE_ROOT, False))
        lib = profiles.PresetLibrary(roots)
        if machine not in lib.names("machine"):
            marker = profile_fetch.marker_for(machine)
            try:
                profile_fetch.fetch_profiles(log or print, marker)
            except OSError as e:
                raise SliceError(
                    f"The settings for the {profiles.model_of(machine)} can't be read from this slicer, and "
                    f"downloading them failed ({e}). Check the PC's internet connection and try again."
                ) from e
            roots = profiles.profile_roots_for(slicer_exe) + [(profile_fetch.CACHE_ROOT, False)]
            lib = profiles.PresetLibrary(roots)
        _LIB_CACHE[key] = lib
    return _LIB_CACHE[key]


def default_process(names: list[str]) -> str:
    """The everyday quality preset: '0.20mm Standard' when there is one."""
    for want in ("0.20mm standard", "standard", "0.20mm"):
        hit = [n for n in names if want in n.lower()]
        if hit:
            return min(hit, key=len)
    return names[0] if names else ""


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
            plate: int, arrange: bool, log, timeout: int = 900, scale: float = 1.0, copies: int = 1) -> str:
    os.makedirs(out_dir, exist_ok=True)
    scaled = abs((scale or 1.0) - 1.0) > 1e-6
    copies = max(1, min(50, int(copies or 1)))
    repeat = []
    if copies > 1:
        arrange = True
        if plate > 0:
            # A project plate: the slicer copies the whole plate and spreads the copies out.
            repeat = ["--repetitions", str(copies)]
        else:
            # Plain shapes: hand the slicer the same files several times.
            inputs = list(inputs) * copies
    if scaled:
        # A resized model may no longer fit where it was, so let the slicer place it again.
        arrange = True
    out_3mf = os.path.join(out_dir, "result.gcode.3mf")
    cmd = [exe,
           "--slice", str(plate),
           "--load-settings", ";".join(settings),
           "--load-filaments", ";".join(filaments),
           "--arrange", "1" if arrange else "0",
           "--orient", "0",
           # --scale grows the model around its middle; --ensure-on-bed puts it back on the plate.
           *(["--scale", f"{scale:g}", "--ensure-on-bed"] if scaled else []),
           *repeat,
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


class Plan:
    """What to slice with: printer, quality, and one filament (preset + colour) per tool."""

    def __init__(self, machine: str, process: str, filaments: list[str], colours: list[str] | None = None,
                 plate: str = DEFAULT_PLATE, grip: bool = True):
        self.machine = machine
        self.process = process
        self.filaments = filaments
        self.colours = colours or []
        self.plate = plate if plate in PLATES else DEFAULT_PLATE
        self.grip = grip
        self.scale = 1.0
        self.copies = 1
        self.extra: dict = {}


def make_plan(cfg: dict, process: str | None = None, filament: str | None = None,
              filaments: list[str] | None = None, colours: list[str] | None = None) -> Plan:
    fils = [f for f in (filaments or []) if f] or [filament or cfg.get("filament_preset")]
    return Plan(cfg.get("machine_preset") or DEFAULT_MACHINE, process or cfg.get("process_preset"),
                fils, colours, cfg.get("bed_type") or DEFAULT_PLATE, bool(cfg.get("extra_grip", True)))


def _write_presets(lib, plan: Plan, work_dir: str, log, count: int):
    preset_dir = os.path.join(work_dir, "presets")
    proc = lib.find("process", plan.process or "")
    if proc is None or not proc.compatible_with(plan.machine):
        better = default_process(lib.names("process", plan.machine))
        if better:
            log(f"The quality '{plan.process}' doesn't fit {plan.machine}; using '{better}'.")
            plan.process = better
    for i, name in enumerate(plan.filaments):
        fp = lib.find("filament", name or "")
        if fp is None or not fp.compatible_with(plan.machine):
            fam = profiles.preset_filament_type(lib, name) if fp else ""
            better = profiles.match_filament_preset(lib, plan.machine, "", "", fam or "PLA", "")
            if better and better != name:
                log(f"The filament '{name}' doesn't fit {plan.machine}; using '{better}'.")
                plan.filaments[i] = better
    log(f"Settings: printer '{plan.machine}', quality '{plan.process}', plate '{plan.plate}'"
        + (", extra bed grip" if plan.grip else ""))
    try:
        m_file = lib.write_resolved("machine", plan.machine, preset_dir)
        p_over = process_overrides(lib.resolve("process", plan.process), plan.plate, plan.grip)
        if plan.extra:
            p_over.update(plan.extra)
            log("Your changes: " + ", ".join(f"{k}={v}" for k, v in plan.extra.items()))
        p_file = lib.write_resolved("process", plan.process, preset_dir, p_over)
        f_files = []
        for i in range(count):
            name = plan.filaments[i] if i < len(plan.filaments) else plan.filaments[-1]
            data = lib.resolve("filament", name)
            over = filament_overrides(data, plan.plate, plan.grip)
            colour = plan.colours[i] if i < len(plan.colours) and plan.colours[i] else ""
            if colour:
                over["filament_colour"] = [colour]
            first_bed, bed = bed_temp(dict(data, **over), plan.plate)
            if bed <= 0:
                log(f"Warning: '{name}' is not meant for the {plan.plate}.")
            f_files.append(lib.write_resolved("filament", name, preset_dir, over, tag=f"{i + 1}"))
            log(f"Filament {i + 1}: '{name}'" + (f" colour {colour}" if colour else "")
                + f", bed {first_bed}°C first layer then {bed}°C")
    except KeyError as e:
        raise SliceError(str(e.args[0]) + " Pick another one in Settings.") from e
    return m_file, p_file, f_files


def slice_files_for_q2(cfg: dict, files: list[str], work_dir: str, log,
                       process: str | None = None, filament: str | None = None, plan: Plan | None = None) -> str:
    """Slice several plain model files together on one plate."""
    plan = plan or make_plan(cfg, process, filament)
    exe = resolve_slicer(cfg)
    lib = library(exe, log=log, machine=plan.machine)
    log(f"Slicer: {exe}")
    m_file, p_file, f_files = _write_presets(lib, plan, work_dir, log, 1)
    return run_cli(exe, files, [m_file, p_file], f_files, os.path.join(work_dir, "out"), 0, True, log, scale=plan.scale, copies=plan.copies)


def slice_for_q2(cfg: dict, src: str, plate: int, work_dir: str, log,
                 process: str | None = None, filament: str | None = None, plan: Plan | None = None,
                 multicolour: bool | None = None) -> str:
    """Slice `src` for the configured QIDI printer and return the path of the resulting G-code.

    With a QIDI Box (`multicolour` true) every colour of a project keeps its own filament;
    otherwise the whole model is printed with the first filament.
    """
    plan = plan or make_plan(cfg, process, filament)
    exe = resolve_slicer(cfg)
    lib = library(exe, log=log, machine=plan.machine)
    log(f"Slicer: {exe}")
    if multicolour is None:
        multicolour = False

    lower = src.lower()
    if lower.endswith(".3mf"):
        colours = project_filament_count(src)
        if colours > 1 and not multicolour:
            log(f"This project uses {colours} colours. Without a QIDI Box it will be printed in one colour.")
            m_file, p_file, f_files = _write_presets(lib, plan, work_dir, log, 1)
            meshes = threemf.export_plate_meshes(src, plate, os.path.join(work_dir, "meshes"))
            return run_cli(exe, meshes, [m_file, p_file], f_files, os.path.join(work_dir, "out"), 0, True, log, scale=plan.scale, copies=plan.copies)
        m_file, p_file, f_files = _write_presets(lib, plan, work_dir, log, colours)
        try:
            return run_cli(exe, [src], [m_file, p_file], f_files, os.path.join(work_dir, "out"), plate, False, log, scale=plan.scale, copies=plan.copies)
        except SliceError as first:
            if colours > 1:
                raise SliceError(f"{first} Multi-colour projects can't fall back to a one-colour slice "
                                 "automatically. Pick the same Box slot for every colour to print it in one colour.")
            log(f"Slicing the project file directly did not work ({first}). Trying again with just the shapes.")
            meshes = threemf.export_plate_meshes(src, plate, os.path.join(work_dir, "meshes"))
            return run_cli(exe, meshes, [m_file, p_file], f_files[:1], os.path.join(work_dir, "out2"), 0, True, log, scale=plan.scale, copies=plan.copies)
    m_file, p_file, f_files = _write_presets(lib, plan, work_dir, log, 1)
    return run_cli(exe, [src], [m_file, p_file], f_files, os.path.join(work_dir, "out"), 0, True, log, scale=plan.scale, copies=plan.copies)
