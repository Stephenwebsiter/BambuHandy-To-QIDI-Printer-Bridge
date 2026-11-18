"""The web page you open on your phone, plus the small API behind it."""

from __future__ import annotations

import hmac
import json
import os
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, config, profiles, slicer
from .jobs import Job, JobManager
from .moonraker import Moonraker, MoonrakerError

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_UPLOAD = 600 * 1024 * 1024
ALLOWED_EXT = (".3mf", ".stl", ".obj", ".step", ".stp", ".amf", ".gcode", ".zip")


LOG_PATH = os.path.join(config.DATA_DIR, "bridge.log")


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
            if "slicer_path" in data:
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
        if path == "/api/jobs":
            return self._upload(cfg)
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

    def _upload(self, cfg: dict):
        try:
            return self._receive_upload()
        except Exception as e:  # never drop the connection without an answer
            self.log_error("upload failed: %r", e)
            self._drain()
            return self._error(f"The bridge could not take the file: {e}", 500)

    def _receive_upload(self):
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
        start = (self.headers.get("X-Start") or "1") == "1"
        job = Job(filename, source="phone", plate=plate, start=start,
                  trust_gcode=(self.headers.get("X-Trust-Gcode") == "1"),
                  process=urllib.parse.unquote(self.headers.get("X-Process") or ""),
                  filament=urllib.parse.unquote(self.headers.get("X-Filament") or ""))
        remaining = length
        with open(job.path, "wb") as fh:
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
        self.jobs.submit(job)
        return self._json({"ok": True, "job": job.to_dict()})

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
            "pin_set": bool(cfg.get("pin")), "addresses": [f"http://{ip}:{self.server.server_address[1]}" for ip in local_ips()],
        }

    def _presets(self, cfg: dict, machine: str, refresh: bool) -> dict:
        try:
            exe = slicer.resolve_slicer(cfg)
        except slicer.SliceError as e:
            return {"ok": False, "error": str(e), "machine": [], "process": [], "filament": []}
        lib = slicer.library(exe, refresh=refresh)
        machine = machine or cfg.get("machine_preset") or ""
        machines = [n for n in lib.names("machine") if "q2" in n.lower()] or lib.names("machine")
        return {"ok": True, "machine": machines, "process": lib.names("process", machine),
                "filament": lib.names("filament", machine)}


def serve(port: int | None = None, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    cfg = config.load()
    port = port or int(cfg.get("web_port") or 8765)
    Handler.jobs = JobManager()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
