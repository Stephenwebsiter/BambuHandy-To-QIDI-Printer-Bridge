"""Print jobs: receive a file, prepare it for the QIDI printer, send it to the printer."""

from __future__ import annotations

import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid

from . import box as qbox
from . import config, profiles, slicer, threemf
from .moonraker import Moonraker, MoonrakerError

JOBS_DIR = os.path.join(config.DATA_DIR, "jobs")
KEEP_JOBS = 30


class Job:
    def __init__(self, filename: str, source: str = "phone", plate: int = 1, start: bool = True,
                 trust_gcode: bool = False, process: str | None = None, filament: str | None = None,
                 slots: list | None = None, filaments: list | None = None, use_box: bool | None = None,
                 scale: float = 1.0, copies: int = 1, tweaks: dict | None = None):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.filename = filename
        self.source = source
        self.plate = plate
        self.start = start
        self.trust_gcode = trust_gcode
        self.process = process or None
        self.filament = filament or None
        # Per model colour (filament 1, 2, ...): which Box slot (0-based) and which filament preset.
        self.slots = [int(x) if str(x).lstrip("-").isdigit() else -1 for x in slots] if slots else None
        self.filaments = [str(x or "") for x in filaments] if filaments else None
        self.use_box = use_box
        try:
            self.copies = max(1, min(50, int(copies or 1)))
        except (TypeError, ValueError):
            self.copies = 1
        self.tweaks = dict(tweaks) if isinstance(tweaks, dict) else {}
        try:
            self.scale = min(10.0, max(0.05, float(scale or 1.0)))
        except (TypeError, ValueError):
            self.scale = 1.0
        self.colours_used: list[dict] = []
        self.mapping: list[int] | None = None
        self.state = "queued"
        self.message = "Waiting its turn"
        self.created = time.time()
        self.finished = None
        self.remote_name = None
        self.log_lines: list[str] = []
        self.dir = os.path.join(JOBS_DIR, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, safe_filename(filename))

    def log(self, line: str):
        stamp = time.strftime("%H:%M:%S")
        for part in str(line).splitlines() or [""]:
            self.log_lines.append(f"{stamp} {part}")
        del self.log_lines[:-300]

    def set(self, state: str, message: str):
        self.state = state
        self.message = message
        self.log(f"[{state}] {message}")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "filename": self.filename, "source": self.source, "state": self.state,
            "message": self.message, "created": self.created, "finished": self.finished,
            "plate": self.plate, "remote_name": self.remote_name, "log": self.log_lines[-60:],
            "colours": self.colours_used,
        }


def safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")) or "model"
    name = re.sub(r"[^A-Za-z0-9 ._()\-+]", "_", name).strip(" .") or "model"
    return name[:120]


def gcode_name_for(filename: str) -> str:
    base = safe_filename(filename)
    for ext in (".gcode.3mf", ".3mf", ".gcode", ".stl", ".obj", ".step", ".stp", ".amf"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    base = base.replace(" ", "_") or "model"
    return f"{base}_{time.strftime('%m%d-%H%M')}.gcode"


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.q: queue.Queue[Job] = queue.Queue()
        self.lock = threading.Lock()
        self.worker = threading.Thread(target=self._run, daemon=True, name="job-worker")
        self.worker.start()

    def submit(self, job: Job) -> Job:
        with self.lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            while len(self.order) > KEEP_JOBS:
                old = self.jobs.pop(self.order.pop(0), None)
                if old and old.state in ("done", "error"):
                    shutil.rmtree(old.dir, ignore_errors=True)
        job.log(f"Received {job.filename} from {job.source}")
        self.q.put(job)
        return job

    def list(self) -> list[dict]:
        with self.lock:
            return [self.jobs[i].to_dict() for i in reversed(self.order) if i in self.jobs]

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def _run(self):
        while True:
            job = self.q.get()
            try:
                process_job(job)
            except Exception as e:  # keep the worker alive whatever happens
                job.log(traceback.format_exc())
                job.set("error", str(e) or e.__class__.__name__)
            finally:
                job.finished = time.time()


def unpack_zip(job: Job) -> str:
    """MakerWorld and others often hand out a .zip. Use the 3MF inside, or all the STL/OBJ/STEP files."""
    import zipfile
    out = os.path.join(job.dir, "unzipped")
    os.makedirs(out, exist_ok=True)
    try:
        z = zipfile.ZipFile(job.path)
    except zipfile.BadZipFile as e:
        raise RuntimeError("This .zip file looks damaged.") from e
    with z:
        names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
        pick = [n for n in names if n.lower().endswith(".3mf")][:1]
        if not pick:
            pick = [n for n in names if n.lower().endswith((".stl", ".obj", ".step", ".stp"))]
        if not pick:
            pick = [n for n in names if n.lower().endswith(".gcode")][:1]
        if not pick:
            raise RuntimeError("The .zip has no 3MF, STL, OBJ, STEP or G-code file inside.")
        paths = []
        for i, n in enumerate(pick):
            dest = os.path.join(out, f"{i + 1}_{safe_filename(n)}")
            with z.open(n) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            paths.append(dest)
    job.log("Inside the zip: " + ", ".join(os.path.basename(n) for n in pick))
    if len(paths) == 1:
        return paths[0]
    # Several separate models: slice them together on one plate.
    return paths


def _printer(cfg: dict) -> Moonraker:
    return Moonraker(cfg["printer_host"], cfg["printer_port"], cfg.get("printer_api_key", ""))


def printer_name(cfg: dict) -> str:
    return profiles.model_of(cfg.get("machine_preset") or "") or "printer"


def _read_box(job: Job, cfg: dict) -> dict | None:
    use = job.use_box if job.use_box is not None else cfg.get("use_box", True)
    if not use:
        return None
    try:
        b = qbox.read_box(_printer(cfg))
    except (MoonrakerError, OSError, ValueError) as e:
        job.log(f"Could not read the QIDI Box ({e}); printing without it.")
        return None
    if not b.get("present"):
        return None
    if not any(s.get("loaded") for s in b["slots"]):
        job.log("The QIDI Box has no filament loaded, so it is not used.")
        return None
    return b


def plan_colours(job: Job, cfg: dict, path: str, plate: int, box: dict | None) -> tuple[list[dict], list[int] | None]:
    """Work out the model's colours and which Box slot prints each one."""
    fils = threemf.model_filaments(path, plate)
    if box is None:
        return fils, None
    if job.slots and len(job.slots) >= len(fils):
        mapping = job.slots[:len(fils)]
    elif len(fils) == 1 and not path.lower().endswith((".3mf", ".gcode")):
        # A plain shape has no colour of its own: use the filament that is in the print head now.
        active = [s["index"] for s in box["slots"] if s.get("active") and s.get("loaded")]
        mapping = active[:1] or qbox.suggest_mapping(fils, box["slots"])
    else:
        mapping = qbox.suggest_mapping(fils, box["slots"])
        if job.slots:
            mapping[:len(job.slots)] = job.slots
    by_index = {s["index"]: s for s in box["slots"]}
    loaded = [s["index"] for s in box["slots"] if s.get("loaded")]
    mapping = [m if m in by_index and by_index[m].get("loaded") else (loaded[0] if loaded else -1) for m in mapping]
    return fils, mapping


def describe(fils: list[dict], mapping: list[int] | None, box: dict | None) -> list[dict]:
    by_index = {s["index"]: s for s in (box or {}).get("slots", [])}
    out = []
    for i, f in enumerate(fils):
        if not f.get("used", True):
            continue
        entry = {"index": i, "model_color": f.get("color"), "type": f.get("type")}
        if mapping and i < len(mapping) and mapping[i] in by_index:
            s = by_index[mapping[i]]
            entry.update({"slot": s["index"], "slot_label": s["label"], "color": s["color"], "name": s["name"]})
        out.append(entry)
    return out


def _slice_plan(job: Job, cfg: dict, fils: list[dict], mapping: list[int] | None, box: dict | None,
                lib) -> slicer.Plan:
    machine = cfg.get("machine_preset") or slicer.DEFAULT_MACHINE
    default_fil = job.filament or cfg.get("filament_preset")
    by_index = {s["index"]: s for s in (box or {}).get("slots", [])}
    presets, colours = [], []
    for i, f in enumerate(fils):
        chosen = job.filaments[i] if job.filaments and i < len(job.filaments) and job.filaments[i] else ""
        slot = by_index.get(mapping[i]) if mapping and i < len(mapping) else None
        if not chosen and slot is not None and lib is not None:
            chosen = profiles.match_filament_preset(lib, machine, slot["name"], slot.get("vendor", ""),
                                                    slot.get("type", ""), default_fil)
        if not chosen and lib is not None and not job.filament and f.get("type") and len(fils) > 1:
            chosen = profiles.match_filament_preset(lib, machine, "", "", f["type"], default_fil)
        presets.append(chosen or default_fil)
        colours.append(slot["color"] if slot is not None else f.get("color", ""))
    plan = slicer.make_plan(cfg, job.process, None, presets, colours)
    plan.scale = job.scale
    plan.copies = job.copies
    plan.extra = slicer.tweak_overrides(job.tweaks)
    if job.copies > 1:
        job.log(f"Copies: {job.copies}")
    if abs(job.scale - 1) > 1e-6:
        job.log(f"Size: {job.scale * 100:g}% of the original")
    return plan


def process_job(job: Job):
    cfg = config.load()
    pname = printer_name(cfg)
    job.set("checking", "Looking at the file")
    box = _read_box(job, cfg)
    if box:
        job.log("QIDI Box: " + ", ".join(
            f"slot {s['label']} {s['name'] or 'empty'} {s['color']}" for s in box["slots"] if s.get("loaded")))
    if job.path.lower().endswith(".zip"):
        unpacked = unpack_zip(job)
        if isinstance(unpacked, list):
            fils, mapping = plan_colours(job, cfg, unpacked[0], 1, box)
            fils, mapping = fils[:1], (mapping[:1] if mapping else None)
            job.colours_used = describe(fils, mapping, box)
            job.set("slicing", f"Preparing {len(unpacked)} models for the {pname} (this can take a minute or two)")
            lib = slicer.library(slicer.resolve_slicer(cfg), log=job.log, machine=cfg.get("machine_preset"))
            plan = _slice_plan(job, cfg, fils, mapping, box, lib)
            gcode_path = slicer.slice_files_for_q2(cfg, unpacked, os.path.join(job.dir, "slice"), job.log, plan=plan)
            return send_to_printer(job, cfg, gcode_path, mapping)
        job.path = unpacked
    info = threemf.inspect(job.path)
    model = info.get("printer_model")
    job.log(f"File type: {info['kind']}, made for: {model or 'unknown printer'}, plates: {info['plates']}")
    plate = job.plate if job.plate in info["plates"] else info["plates"][0]
    gcode_path = os.path.join(job.dir, "ready.gcode")
    target = cfg.get("machine_preset") or ""

    if info["kind"] == "gcode":
        if model and not threemf.same_printer(model, target):
            raise RuntimeError(f"This G-code was made for a different printer ({model}). "
                               f"Send the .3mf or .stl instead and the bridge will prepare it for the {pname}.")
        if not model and not job.trust_gcode:
            raise RuntimeError(f"Can't tell which printer this G-code is for. Tick 'This G-code is for my {pname}' "
                               "if you're sure, or send the .3mf/.stl instead.")
        fils, mapping = plan_colours(job, cfg, job.path, plate, box)
        gcode_path = job.path
    elif info["kind"] == "sliced_3mf" and threemf.same_printer(model, target):
        job.set("checking", f"Already sliced for the {pname}, using plate {plate}")
        threemf.extract_gcode(job.path, plate, gcode_path)
        fils, mapping = plan_colours(job, cfg, gcode_path, plate, box)
    else:
        if info["kind"] == "sliced_3mf":
            job.log(f"This file was sliced for {model}, so it will be sliced again for the {pname}.")
        fils, mapping = plan_colours(job, cfg, job.path, plate, box)
        used = [i for i, f in enumerate(fils) if f.get("used", True)] or [0]
        multicolour = box is not None and len(fils) > 1 and len({mapping[i] for i in used}) > 1
        if len(fils) > 1 and not multicolour:
            # One colour only: print everything with the slot (or filament) of the first used colour.
            first = used[0]
            fils = [dict(fils[first], index=0, used=True)]
            mapping = [mapping[first]] if mapping else None
            if job.filaments and first < len(job.filaments):
                job.filaments = [job.filaments[first]]
            if box is None:
                job.log("No QIDI Box in use, so this multi-colour model is printed in one colour.")
            else:
                job.log("All colours use the same Box slot, so it is printed in one colour.")
        job.set("slicing", f"Preparing the model for the {pname} (this can take a minute or two)")
        lib = slicer.library(slicer.resolve_slicer(cfg), log=job.log, machine=cfg.get("machine_preset"))
        plan = _slice_plan(job, cfg, fils, mapping, box, lib)
        job.colours_used = describe(fils, mapping, box)
        gcode_path = slicer.slice_for_q2(cfg, job.path, plate, os.path.join(job.dir, "slice"), job.log,
                                         plan=plan, multicolour=multicolour)
        return send_to_printer(job, cfg, gcode_path, mapping)

    job.colours_used = describe(fils, mapping, box)
    send_to_printer(job, cfg, gcode_path, mapping)


def send_to_printer(job: Job, cfg: dict, gcode_path: str, mapping: list[int] | None = None):
    pname = printer_name(cfg)
    job.remote_name = gcode_name_for(job.filename)
    action = f"Sending to the {pname} and starting the print" if job.start else f"Sending to the {pname}"
    job.set("uploading", action)
    try:
        mr = _printer(cfg)
        if job.start:
            st = mr.status().get("print_stats", {}).get("state")
            if st in ("printing", "paused"):
                job.log("The printer is busy, so the file is only uploaded. Start it from the printer when ready.")
                job.start = False
        if job.start and mapping and any(m >= 0 for m in mapping):
            script = qbox.tool_slot_script(mapping)
            if script:
                job.log("Box slots: " + ", ".join(f"T{t} -> slot {m + 1}" for t, m in enumerate(mapping) if m >= 0))
                # The printer's screen switches the Box on for each print it starts; do the same,
                # otherwise the print runs with whatever the last print left (often "off").
                mr.gcode(qbox.ENABLE_BOX + "\n" + script)
        mr.upload(gcode_path, job.remote_name, start_print=job.start)
    except MoonrakerError as e:
        raise RuntimeError(str(e)) from e
    if job.start:
        job.set("done", f"Printing {job.remote_name} on the {pname}")
    else:
        job.set("done", f"Uploaded {job.remote_name}. Start it from the printer screen.")
