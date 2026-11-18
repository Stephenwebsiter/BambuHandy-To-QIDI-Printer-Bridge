"""Print jobs: receive a file, prepare it for the Q2, send it to the printer."""

from __future__ import annotations

import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid

from . import config, slicer, threemf
from .moonraker import Moonraker, MoonrakerError

JOBS_DIR = os.path.join(config.DATA_DIR, "jobs")
KEEP_JOBS = 30


class Job:
    def __init__(self, filename: str, source: str = "phone", plate: int = 1, start: bool = True,
                 trust_gcode: bool = False, process: str | None = None, filament: str | None = None):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.filename = filename
        self.source = source
        self.plate = plate
        self.start = start
        self.trust_gcode = trust_gcode
        self.process = process or None
        self.filament = filament or None
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


def process_job(job: Job):
    cfg = config.load()
    job.set("checking", "Looking at the file")
    info = threemf.inspect(job.path)
    model = info.get("printer_model")
    job.log(f"File type: {info['kind']}, made for: {model or 'unknown printer'}, plates: {info['plates']}")
    plate = job.plate if job.plate in info["plates"] else info["plates"][0]
    gcode_path = os.path.join(job.dir, "ready.gcode")

    if info["kind"] == "gcode":
        if model and not threemf.is_qidi_q2(model):
            raise RuntimeError(f"This G-code was made for a different printer ({model}). "
                               "Send the .3mf or .stl instead and the bridge will prepare it for the Q2.")
        if not model and not job.trust_gcode:
            raise RuntimeError("Can't tell which printer this G-code is for. Tick 'This G-code is for my Q2' "
                               "if you're sure, or send the .3mf/.stl instead.")
        gcode_path = job.path
    elif info["kind"] == "sliced_3mf" and threemf.is_qidi_q2(model):
        job.set("checking", f"Already sliced for the Q2, using plate {plate}")
        threemf.extract_gcode(job.path, plate, gcode_path)
    else:
        if info["kind"] == "sliced_3mf":
            job.log(f"This file was sliced for {model}, so it will be sliced again for the Q2.")
        job.set("slicing", "Preparing the model for the Q2 (this can take a minute or two)")
        gcode_path = slicer.slice_for_q2(cfg, job.path, plate, os.path.join(job.dir, "slice"), job.log,
                                         process=job.process, filament=job.filament)

    job.remote_name = gcode_name_for(job.filename)
    action = "Sending to the Q2 and starting the print" if job.start else "Sending to the Q2"
    job.set("uploading", action)
    try:
        mr = Moonraker(cfg["printer_host"], cfg["printer_port"], cfg.get("printer_api_key", ""))
        if job.start:
            st = mr.status().get("print_stats", {}).get("state")
            if st in ("printing", "paused"):
                job.log("The printer is busy, so the file is only uploaded. Start it from the printer when ready.")
                job.start = False
        mr.upload(gcode_path, job.remote_name, start_print=job.start)
    except MoonrakerError as e:
        raise RuntimeError(str(e)) from e
    if job.start:
        job.set("done", f"Printing {job.remote_name} on the Q2")
    else:
        job.set("done", f"Uploaded {job.remote_name}. Start it from the printer screen.")
