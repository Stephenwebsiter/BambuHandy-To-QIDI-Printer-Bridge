"""The web page you open on your phone, plus the small API behind it."""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import socket
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, config, profile_fetch, profiles, slicer, threemf
from . import box as qbox
from .jobs import Job, JobManager, printer_name, safe_filename
from .moonraker import Moonraker, MoonrakerError

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_UPLOAD = 600 * 1024 * 1024
ALLOWED_EXT = (".3mf", ".stl", ".obj", ".step", ".stp", ".amf", ".gcode", ".zip")


LOG_PATH = os.path.join(config.DATA_DIR, "bridge.log")
UPLOADS_DIR = os.path.join(config.DATA_DIR, "uploads")
KEEP_UPLOADS = 12
_FIL_CACHE: dict = {}
_FILE_ID = re.compile(r"^[0-9a-f]{16}$")


def write_log(line: str):
    """Keep a copy of the bridge's log in data/bridge.log for troubleshooting."""
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except OSError:
        pass


def find_printers(timeout: float = 0.4) -> list[dict]:
    """Look for Klipper/Moonraker printers (port 7125) on the PC's home networks."""
    hosts = []
    for ip in local_ips():
        prefix = ip.rsplit(".", 1)[0]
        hosts += [f"{prefix}.{n}" for n in range(1, 255) if f"{prefix}.{n}" != ip]

    def probe(host):
        try:
            with socket.create_connection((host, 7125), timeout=timeout):
                pass
        except OSError:
            return None
        try:
            info = Moonraker(host, 7125, timeout=3).info()
        except MoonrakerError:
            return None
        return {"host": host, "name": info.get("hostname") or host, "state": info.get("state")}

    with ThreadPoolExecutor(max_workers=64) as pool:
        found = [r for r in pool.map(probe, hosts) if r]
    return found


def local_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


class Handler(BaseHTTPRequestHandler):
    server_version = f"QidiBridge/{__version__}"
    jobs: JobManager = None  # set in serve()

    def log_message(self, fmt, *args):
        line = "%s - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args)
        write_log(line)
        # Keep the console quiet for the page's regular status checks.
        if not (self.command == "GET" and (self.path.startswith("/api/jobs") or self.path.startswith("/api/printer"))):
            print(line, flush=True)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError) as e:
            write_log(f"{self.client_address[0]} - connection problem: {e!r}")
            raise

    # -- helpers ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data).encode("utf-8"), "application/json")

    def _error(self, msg: str, code: int = 400):
        self._json({"ok": False, "error": msg}, code)

    def _body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _authorized(self, cfg: dict) -> bool:
        pin = cfg.get("pin") or ""
        if not pin:
            return True
        given = self.headers.get("X-Bridge-Pin") or ""
        return hmac.compare_digest(given.encode(), pin.encode())

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if path == "/manifest.webmanifest":
            with open(os.path.join(WEB_DIR, "manifest.webmanifest"), "rb") as fh:
                return self._send(200, fh.read(), "application/manifest+json")
        if path in ("/guide", "/help"):
            from . import guide
            return self._send(200, guide.page(), "text/html; charset=utf-8")
        if path == "/icon.svg":
            with open(os.path.join(WEB_DIR, "icon.svg"), "rb") as fh:
                return self._send(200, fh.read(), "image/svg+xml")
        cfg = config.load()
        if path == "/api/hello":
            return self._json({"ok": True, "version": __version__, "pin_required": bool(cfg.get("pin"))})
        if not self._authorized(cfg):
            return self._error("Wrong or missing PIN.", 401)
        if path == "/api/state":
            return self._json(self._state(cfg))
        if path == "/api/presets":
            q = urllib.parse.parse_qs(url.query)
            return self._json(self._presets(cfg, q.get("machine", [""])[0], q.get("refresh", ["0"])[0] == "1"))
        if path == "/api/jobs":
            return self._json({"ok": True, "jobs": self.jobs.list()})
        if path == "/api/box":
            return self._json(self._box(cfg))
        if path == "/api/filaments":
            return self._json(self._filaments(cfg))
        if path == "/api/discover":
            return self._json({"ok": True, "printers": find_printers()})
        m = re.fullmatch(r"/api/files/([0-9a-f]+)(/thumb)?", path)
        if m and _FILE_ID.match(m.group(1)):
            q = urllib.parse.parse_qs(url.query)
            try:
                plate = max(1, int(q.get("plate", ["1"])[0]))
            except ValueError:
                plate = 1
            f = _uploaded(m.group(1))
            if not f:
                return self._error("That file is gone. Please pick it again.", 404)
            if m.group(2):
                png = threemf.thumbnail(f, plate)
                if not png:
                    return self._error("No preview", 404)
                return self._send(200, png, "image/png")
            return self._json(self._file_info(cfg, m.group(1), f, plate))
        if path == "/api/printer":
            try:
                mr = Moonraker(cfg["printer_host"], cfg["printer_port"], cfg.get("printer_api_key", ""), timeout=5)
                return self._json({"ok": True, "status": mr.status()})
            except MoonrakerError as e:
                return self._json({"ok": False, "error": str(e)})
        return self._error("Not found", 404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        cfg = config.load()
        if not self._authorized(cfg):
            self._drain()
            return self._error("Wrong or missing PIN.", 401)
        if path == "/api/settings":
            data = self._body_json()
            if "printer_api_key" in data and data["printer_api_key"] == "********":
                data.pop("printer_api_key")
            new = config.save(data)
            if "slicer_path" in data or "machine_preset" in data:
                slicer._LIB_CACHE.clear()
            return self._json({"ok": True, "state": self._state(new)})
        if path == "/api/test-printer":
            data = self._body_json()
            host = (data.get("printer_host") or "").strip() or cfg["printer_host"]
            port = data.get("printer_port") or cfg["printer_port"]
            try:
                info = Moonraker(host, int(port or 7125), cfg.get("printer_api_key", ""), timeout=5).info()
                return self._json({"ok": True, "info": info})
            except (MoonrakerError, ValueError) as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/files":
            return self._upload(cfg, store_only=True)
        if path == "/api/jobs":
            if (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._start_job(cfg, self._body_json())
            return self._upload(cfg)
        if path.startswith("/api/box/"):
            return self._box_action(cfg, path.rsplit("/", 1)[1], self._body_json())
        if path.startswith("/api/printer/"):
            action = path.rsplit("/", 1)[1]
            if action not in ("pause", "resume", "cancel"):
                return self._error("Not found", 404)
            try:
                Moonraker(cfg["printer_host"], cfg["printer_port"], cfg.get("printer_api_key", "")).control(action)
                return self._json({"ok": True})
            except MoonrakerError as e:
                return self._json({"ok": False, "error": str(e)})
        return self._error("Not found", 404)

    def _drain(self):
        """Read and throw away the request body.

        Answering before the phone has finished sending makes the connection reset,
        and the page then only sees "could not reach the bridge" instead of the reason.
        """
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            remaining = 0
        while remaining > 0:
            chunk = self.rfile.read(min(1 << 20, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _upload(self, cfg: dict, store_only: bool = False):
        try:
            return self._receive_upload(cfg, store_only)
        except Exception as e:  # never drop the connection without an answer
            self.log_error("upload failed: %r", e)
            self._drain()
            return self._error(f"The bridge could not take the file: {e}", 500)

    def _receive_upload(self, cfg: dict, store_only: bool = False):
        filename = urllib.parse.unquote(self.headers.get("X-Filename") or "")
        length = int(self.headers.get("Content-Length") or 0)
        self.log_message("upload %r, %d bytes, headers: %s", filename, length,
                         {k: v for k, v in self.headers.items() if k.lower() not in ("cookie",)})
        if not filename.lower().endswith(ALLOWED_EXT):
            self._drain()
            return self._error(f"'{filename}' isn't a file the bridge can print. Pick a .3mf, .stl, .obj, "
                               ".step, .gcode or .zip file.")
        if length <= 0:
            return self._error("The file was empty.")
        if length > MAX_UPLOAD:
            self._drain()
            return self._error("That file is too big (over 600 MB).", 413)
        try:
            plate = max(1, int(self.headers.get("X-Plate") or 1))
        except ValueError:
            plate = 1
        if store_only:
            file_id = uuid.uuid4().hex[:16]
            folder = os.path.join(UPLOADS_DIR, file_id)
            os.makedirs(folder, exist_ok=True)
            dest = os.path.join(folder, safe_filename(filename))
            _tidy_uploads()
        else:
            start = (self.headers.get("X-Start") or "1") == "1"
            job = Job(filename, source="phone", plate=plate, start=start,
                      trust_gcode=(self.headers.get("X-Trust-Gcode") == "1"),
                      process=urllib.parse.unquote(self.headers.get("X-Process") or ""),
                      filament=urllib.parse.unquote(self.headers.get("X-Filament") or ""))
            dest = job.path
        remaining = length
        with open(dest, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining:
            self.log_message("upload %r cut off: got %d of %d bytes", filename, length - remaining, length)
            return self._error("The upload was cut off. Please try again.")
        self.log_message("upload %r received in full", filename)
        if store_only:
            return self._json(self._file_info(cfg, file_id, dest, plate))
        self.jobs.submit(job)
        return self._json({"ok": True, "job": job.to_dict()})

    def _start_job(self, cfg: dict, data: dict):
        file_id = str(data.get("file_id") or "")
        src = _uploaded(file_id) if _FILE_ID.match(file_id) else None
        if not src:
            return self._error("That file is gone. Please pick it again.", 404)
        try:
            plate = max(1, int(data.get("plate") or 1))
        except (TypeError, ValueError):
            plate = 1
        slots = data.get("slots") if isinstance(data.get("slots"), list) else None
        fils = data.get("filaments") if isinstance(data.get("filaments"), list) else None
        job = Job(os.path.basename(src), source="phone", plate=plate, start=bool(data.get("start", True)),
                  trust_gcode=bool(data.get("trust_gcode")), process=str(data.get("process") or ""),
                  filament=str(data.get("filament") or ""), slots=slots, filaments=fils,
                  use_box=data.get("use_box") if isinstance(data.get("use_box"), bool) else None,
                  scale=data.get("scale") or 1.0, copies=data.get("copies") or 1,
                  tweaks=data.get("tweaks") if isinstance(data.get("tweaks"), dict) else None)
        shutil.copyfile(src, job.path)
        self.jobs.submit(job)
        return self._json({"ok": True, "job": job.to_dict()})

    def _file_info(self, cfg: dict, file_id: str, path: str, plate: int) -> dict:
        name = os.path.basename(path)
        out = {"ok": True, "file_id": file_id, "name": name, "plates": [1], "plate": 1, "kind": "zip",
               "made_for": None, "ready": False, "filaments": [], "thumb": False, "printer": printer_name(cfg)}
        if name.lower().endswith(".zip"):
            out["filaments"] = threemf.model_filaments("model.stl")
            return out
        try:
            info = threemf.inspect(path)
        except threemf.ModelFileError as e:
            return {"ok": False, "error": str(e), "file_id": file_id}
        plate = plate if plate in info["plates"] else info["plates"][0]
        out.update({"kind": info["kind"], "made_for": info.get("printer_model"), "plates": info["plates"],
                    "plate": plate})
        target = cfg.get("machine_preset") or ""
        out["ready"] = info["kind"] in ("gcode", "sliced_3mf") and threemf.same_printer(info.get("printer_model"), target)
        try:
            out["filaments"] = threemf.model_filaments(path, plate)
        except Exception:  # an odd file should never stop the page
            out["filaments"] = threemf.model_filaments("model.stl")
        out["thumb"] = threemf.thumbnail(path, plate) is not None
        out["size"] = None if out["ready"] or info["kind"] == "gcode" else threemf.model_size(path, plate)
        out["bed"] = _bed_size(cfg)
        return out

    def _filaments(self, cfg: dict) -> dict:
        """Every filament the printer has settings for, with the numbers people care about."""
        machine = cfg.get("machine_preset") or slicer.DEFAULT_MACHINE
        plate = cfg.get("bed_type") or slicer.DEFAULT_PLATE
        try:
            lib = slicer.library(slicer.resolve_slicer(cfg), machine=machine)
        except slicer.SliceError as e:
            return {"ok": False, "error": str(e), "filaments": []}
        key = (id(lib), machine, plate)
        if key not in _FIL_CACHE:
            out = []
            for name in lib.names("filament", machine):
                try:
                    d = lib.resolve("filament", name)
                except KeyError:
                    continue
                first_bed, bed = slicer.bed_temp(d, plate)
                ftype = d.get("filament_type")
                ftype = (ftype[0] if isinstance(ftype, list) and ftype else ftype) or "PLA"
                vendor = d.get("filament_vendor")
                vendor = (vendor[0] if isinstance(vendor, list) and vendor else vendor) or name.split(" ")[0]
                short = name.split("@")[0].strip()
                bv, bn = qbox.split_vendor(short)
                out.append({
                    "name": name, "short": short, "type": str(ftype), "box_vendor": bv, "box_name": bn,
                    "family": qbox.normalize_type(str(ftype)), "vendor": str(vendor),
                    "nozzle": int(slicer._num(d.get("nozzle_temperature"), 0)),
                    "nozzle_low": int(slicer._num(d.get("nozzle_temperature_range_low"), 0)),
                    "nozzle_high": int(slicer._num(d.get("nozzle_temperature_range_high"), 0)),
                    "bed": bed, "bed_first": first_bed,
                    "chamber": int(slicer._num(d.get("chamber_temperature"), 0)),
                    "fan": int(slicer._num(d.get("fan_max_speed"), 0)),
                    "flow": round(slicer._num(d.get("filament_max_volumetric_speed"), 0), 1),
                    "dry": int(slicer._num(d.get("filament_dev_ams_drying_temperature"), 0)),
                    "density": slicer._num(d.get("filament_density"), 0),
                })
            _FIL_CACHE.clear()
            _FIL_CACHE[key] = out
        return {"ok": True, "filaments": _FIL_CACHE[key], "plate": plate,
                "plate_label": slicer.PLATE_LABELS.get(plate, plate), "default": cfg.get("filament_preset"),
                "printer": printer_name(cfg)}

    def _box_action(self, cfg: dict, action: str, data: dict):
        """Change the Box from the phone, with the same commands QIDI Studio sends."""
        try:
            mr = Moonraker(cfg["printer_host"], cfg["printer_port"], cfg.get("printer_api_key", ""), timeout=10)
            if action == "set":
                script = qbox.set_slot_script(data.get("slot"), data.get("color"), data.get("vendor"), data.get("filament"))
            elif action in ("load", "unload", "rfid"):
                script = qbox.slot_action_script(action, data.get("slot"))
            else:
                return self._error("Not found", 404)
            if action in ("load", "unload", "rfid"):
                state = mr.status().get("print_stats", {}).get("state")
                if state in ("printing", "paused"):
                    return self._json({"ok": False, "error": "The printer is busy printing. Try again when it has finished."})
            write_log(f"box action {action}: {script}")
            mr.gcode(script)
        except qbox.BoxCommandError as e:
            return self._json({"ok": False, "error": str(e)})
        except MoonrakerError as e:
            return self._json({"ok": False, "error": str(e)})
        result = {"ok": True, "sent": script}
        if action == "set":
            # The printer quietly ignores a filament it doesn't know, so check that the slot changed.
            import time as _t
            for _ in range(6):
                _t.sleep(0.5)
                b = self._box(cfg)
                slot = next((s for s in b.get("slots", []) if s["index"] == int(data["slot"])), None)
                if slot and str(data.get("filament", "")).lower() == str(slot.get("name", "")).lower() and \
                        str(data.get("vendor", "")).lower() == str(slot.get("vendor", "")).lower():
                    result["box"] = b
                    return self._json(result)
            result.update(ok=False, box=b, error="The printer didn't accept that filament. Pick another one, "
                                                 "or set this slot on the printer's screen.")
        return self._json(result)

    def _box(self, cfg: dict) -> dict:
        try:
            mr = Moonraker(cfg["printer_host"], cfg["printer_port"], cfg.get("printer_api_key", ""), timeout=5)
            b = qbox.read_box(mr)
        except MoonrakerError as e:
            return {"ok": False, "error": str(e), "present": False, "slots": []}
        b["ok"] = True
        b["use_box"] = bool(cfg.get("use_box", True))
        try:
            fd = qbox.filament_dict(mr)
            b["palette"] = [c for _k, c in sorted(fd["colors"].items())]
            b["options"] = qbox.filament_options(fd, b.get("slots", []))
        except Exception:
            b["palette"], b["options"] = [], {}
        if b.get("present"):
            try:
                lib = slicer.library(slicer.resolve_slicer(cfg), machine=cfg.get("machine_preset"))
            except slicer.SliceError:
                lib = None
            for s in b["slots"]:
                if lib is not None and s.get("loaded"):
                    s["preset"] = profiles.match_filament_preset(
                        lib, cfg.get("machine_preset") or slicer.DEFAULT_MACHINE, s["name"], s.get("vendor", ""),
                        s.get("type", ""), cfg.get("filament_preset", ""))
        return b

    # -- data for the page -------------------------------------------------
    def _state(self, cfg: dict) -> dict:
        safe = dict(cfg)
        safe["printer_api_key"] = "********" if cfg.get("printer_api_key") else ""
        safe.pop("pin", None)
        try:
            exe = slicer.resolve_slicer(cfg)
        except slicer.SliceError as e:
            exe, slicer_error = "", str(e)
        else:
            slicer_error = ""
        from . import virtual_printer
        vp = virtual_printer.current()
        studio = {
            "enabled": bool(cfg.get("virtual_printer_enabled")),
            "running": vp is not None,
            "name": vp.name if vp else cfg.get("virtual_printer_name"),
            "ip": vp.ip if vp else "",
            "access_code": vp.access_code if vp else cfg.get("virtual_printer_access_code"),
            "serial": vp.serial if vp else cfg.get("virtual_printer_serial"),
        }
        return {
            "ok": True, "version": __version__, "config": safe, "slicer": exe, "slicer_error": slicer_error,
            "studio": studio,
            "printer_name": printer_name(cfg),
            "plates": [{"value": k, "label": v} for k, v in slicer.PLATE_LABELS.items()],
            "pin_set": bool(cfg.get("pin")), "addresses": [f"http://{ip}:{self.server.server_address[1]}" for ip in local_ips()],
        }

    def _presets(self, cfg: dict, machine: str, refresh: bool) -> dict:
        machine = machine or cfg.get("machine_preset") or slicer.DEFAULT_MACHINE
        known = profile_fetch.available_machines()
        try:
            exe = slicer.resolve_slicer(cfg)
        except slicer.SliceError as e:
            return {"ok": False, "error": str(e), "machine": known, "process": [], "filament": []}
        try:
            lib = slicer.library(exe, refresh=refresh, machine=machine)
        except slicer.SliceError as e:
            return {"ok": False, "error": str(e), "machine": known, "process": [], "filament": []}
        machines = sorted(set(lib.names("machine")) | set(known), key=str.lower)
        processes = lib.names("process", machine)
        filaments = lib.names("filament", machine)
        return {"ok": True, "machine": machines, "process": processes, "filament": filaments,
                "selected": machine, "model": profiles.model_of(machine),
                "default_process": slicer.default_process(processes),
                "default_filament": profiles.match_filament_preset(lib, machine, "PLA", "QIDI", "PLA", "")}


def _bed_size(cfg: dict) -> list[float] | None:
    """Printable width, depth and height of the configured printer, in mm."""
    try:
        lib = slicer.library(slicer.resolve_slicer(cfg), machine=cfg.get("machine_preset"))
        m = lib.resolve("machine", cfg.get("machine_preset") or slicer.DEFAULT_MACHINE)
    except (slicer.SliceError, KeyError):
        return None
    try:
        pts = [tuple(float(v) for v in p.split("x")) for p in m.get("printable_area") or []]
        h = float(m.get("printable_height") or 0)
    except (ValueError, AttributeError):
        return None
    if not pts:
        return None
    return [max(p[0] for p in pts) - min(p[0] for p in pts), max(p[1] for p in pts) - min(p[1] for p in pts), h]


def _uploaded(file_id: str) -> str | None:
    folder = os.path.join(UPLOADS_DIR, file_id)
    try:
        names = [n for n in os.listdir(folder) if not n.startswith(".")]
    except OSError:
        return None
    return os.path.join(folder, names[0]) if names else None


def _tidy_uploads():
    try:
        entries = sorted((os.path.getmtime(os.path.join(UPLOADS_DIR, d)), d) for d in os.listdir(UPLOADS_DIR))
    except OSError:
        return
    for _t, d in entries[:-KEEP_UPLOADS]:
        shutil.rmtree(os.path.join(UPLOADS_DIR, d), ignore_errors=True)


def serve(port: int | None = None, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    cfg = config.load()
    port = int(cfg.get("web_port") or 8765) if port is None else port
    Handler.jobs = JobManager()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
