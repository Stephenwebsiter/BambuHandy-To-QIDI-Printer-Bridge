"""A pretend Bambu Lab printer, so Bambu Studio (and OrcaSlicer) can "print" to the Q2.

Bambu Studio talks to printers on the local network (LAN mode) like this:

  * discovery  - the printer announces itself on UDP port 2021 (SSDP style)
  * bind       - a short "who are you" handshake on TCP 3000 / TLS 3002
  * MQTT       - status and commands over TLS on port 8883 (user "bblp",
                 password = the printer's 8-character access code)
  * FTPS       - the sliced .3mf is uploaded over implicit TLS on port 990

This module answers all four. When a file arrives it is handed to the same
job queue the phone page uses, so it is re-sliced with QIDI Q2 settings and
sent to the Q2 through Moonraker.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import secrets
import socket
import ssl
import struct
import threading
import time

from . import certs, config
from .jobs import Job

SSDP_PORT = 2021
BIND_PORTS = (3000, 3002)
MQTT_PORT = 8883
FTPS_PORT = 990
PASSIVE_PORTS = (50000, 50100)
SEARCH_TARGET = "urn:bambulab-com:device:3dprinter:1"
# We pretend to be an X1 Carbon: it has the most permissive LAN-mode flow in Bambu Studio.
MODEL_CODE = "BL-P001"
SERIAL_PREFIX = "00M00A"
FIRMWARE = "01.07.00.00"
UPLOAD_DIR = os.path.join(config.DATA_DIR, "studio_uploads")
PRINT_COMMAND_WAIT = 20  # seconds to wait for "print" after an upload before treating it as "send only"


def ensure_identity(cfg: dict) -> dict:
    """Make sure the pretend printer has a serial number and access code, saved in settings."""
    updates = {}
    if not re.fullmatch(r"[0-9A-Z]{15}", cfg.get("virtual_printer_serial") or ""):
        updates["virtual_printer_serial"] = SERIAL_PREFIX + "".join(random.choice("0123456789") for _ in range(9))
    if not re.fullmatch(r"[0-9A-Za-z]{8}", cfg.get("virtual_printer_access_code") or ""):
        updates["virtual_printer_access_code"] = "".join(random.choice("0123456789") for _ in range(8))
    if updates:
        cfg = config.save(updates)
    return cfg


def pick_ip(cfg: dict) -> str:
    if cfg.get("virtual_printer_bind_ip"):
        return cfg["virtual_printer_bind_ip"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Small MQTT helpers (MQTT 3.1.1, just what Bambu Studio uses)
# ---------------------------------------------------------------------------

def _encode_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def mqtt_publish_packet(topic: str, payload: bytes) -> bytes:
    t = topic.encode()
    body = struct.pack("!H", len(t)) + t + payload
    return bytes([0x30]) + _encode_len(len(body)) + body


async def _read_packet(reader: asyncio.StreamReader):
    first = await reader.readexactly(1)
    mult, length = 1, 0
    for _ in range(4):
        b = (await reader.readexactly(1))[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    body = await reader.readexactly(length) if length else b""
    return first[0], body


def _read_str(buf: bytes, i: int) -> tuple[str, int]:
    n = struct.unpack_from("!H", buf, i)[0]
    return buf[i + 2:i + 2 + n].decode("utf-8", "replace"), i + 2 + n


def _read_bytes(buf: bytes, i: int) -> tuple[bytes, int]:
    n = struct.unpack_from("!H", buf, i)[0]
    return buf[i + 2:i + 2 + n], i + 2 + n


class VirtualPrinter:
    def __init__(self, jobs, cfg: dict | None = None, ports: dict | None = None):
        cfg = ensure_identity(cfg or config.load())
        self.jobs = jobs
        self.serial = cfg["virtual_printer_serial"]
        self.access_code = cfg["virtual_printer_access_code"]
        self.name = cfg.get("virtual_printer_name") or "QIDI Q2 (bridge)"
        self.ip = pick_ip(cfg)
        self.ports = {"ssdp": SSDP_PORT, "mqtt": MQTT_PORT, "ftps": FTPS_PORT,
                      "bind_plain": BIND_PORTS[0], "bind_tls": BIND_PORTS[1]}
        self.ports.update(ports or {})
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.StreamWriter] = set()
        self.gcode_state = "IDLE"
        self.current_file = ""
        self.prepare_percent = 0
        self.seq = 0
        self.pending: dict[str, dict] = {}  # uploaded file name -> {"path", "time", "job"}
        self.print_requests: dict[str, dict] = {}  # file name -> print command data
        self.ssl_ctx: ssl.SSLContext | None = None
        self.started = threading.Event()
        self._jobs_by_file: dict[str, Job] = {}
        self.errors: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    def start_in_thread(self):
        threading.Thread(target=self._thread_main, daemon=True, name="virtual-printer").start()
        self.started.wait(15)
        return self

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start())
        except Exception as e:  # pragma: no cover - reported to the console
            self.errors.append(str(e))
            self.started.set()
            return
        self.started.set()
        self.loop.run_forever()

    async def _start(self):
        cert, key = certs.ensure_certificates(self.serial, [self.ip])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.set_ciphers("DEFAULT:AES256-GCM-SHA384:AES128-GCM-SHA256")
        except ssl.SSLError:
            pass
        self.ssl_ctx = ctx
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        await self._listen(self._mqtt_client, "mqtt", ctx, "MQTT")
        await self._listen(self._ftp_client, "ftps", ctx, "file upload (FTPS)")
        await self._listen(self._bind_client, "bind_plain", None, "printer handshake", required=False)
        await self._listen(self._bind_client, "bind_tls", ctx, "printer handshake (TLS)", required=False)
        self._start_ssdp()
        self.loop.create_task(self._status_loop())
        self.loop.create_task(self._pending_loop())

    async def _listen(self, handler, key, ctx, label, required=True):
        port = self.ports[key]
        try:
            srv = await asyncio.start_server(handler, "0.0.0.0", port, ssl=ctx)
        except OSError as e:
            msg = f"Could not open port {port} for {label}: {e}. Is another program (Bambuddy, another bridge) using it?"
            self.errors.append(msg)
            if required:
                raise RuntimeError(msg) from e
            return None
        self.ports[key] = srv.sockets[0].getsockname()[1]
        return srv

    # -- discovery ---------------------------------------------------------
    def _ssdp_message(self, notify: bool) -> bytes:
        first = "NOTIFY * HTTP/1.1\r\nHost: 239.255.255.250:1990\r\n" if notify else "HTTP/1.1 200 OK\r\n"
        lines = [
            "Server: UPnP/1.0",
            f"Location: {self.ip}",
            (f"NT: {SEARCH_TARGET}\r\nNTS: ssdp:alive" if notify else f"ST: {SEARCH_TARGET}"),
            f"USN: {self.serial}",
            "Cache-Control: max-age=1800",
            f"DevModel.bambu.com: {MODEL_CODE}",
            f"DevName.bambu.com: {self.name}",
            "DevSignal.bambu.com: -40",
            "DevConnect.bambu.com: lan",
            "DevBind.bambu.com: free",
            "Devseclink.bambu.com: secure",
            "DevInf.bambu.com: wlan0",
            f"DevVersion.bambu.com: {FIRMWARE}",
            "DevCap.bambu.com: 1",
        ]
        return (first + "\r\n".join(lines) + "\r\n\r\n").encode()

    def _start_ssdp(self):
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listen.bind(("0.0.0.0", self.ports["ssdp"]))
            listen.settimeout(1.0)
        except OSError:
            # Bambu Studio on this same PC may already hold the port; announcing still works.
            listen.close()
            listen = None

        def announce():
            msg = self._ssdp_message(True)
            for target in (("255.255.255.255", self.ports["ssdp"]), ("127.0.0.1", self.ports["ssdp"]),
                           (self.ip, self.ports["ssdp"])):
                try:
                    send.sendto(msg, target)
                except OSError:
                    pass
            self.loop.call_later(5, announce)

        def answer_searches():
            # A plain thread instead of loop.add_reader(): Windows' default event loop has no add_reader.
            while True:
                try:
                    data, addr = listen.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if b"M-SEARCH" in data and b"bambulab" in data:
                    try:
                        send.sendto(self._ssdp_message(False), addr)
                    except OSError:
                        pass

        if listen is not None:
            threading.Thread(target=answer_searches, daemon=True, name="ssdp-search").start()
        announce()

    # -- bind handshake ----------------------------------------------------
    async def _bind_client(self, reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=10)
            if len(data) < 6 or data[:2] != b"\xa5\xa5":
                return
            req = json.loads(data[4:-2].decode("utf-8", "replace"))
            if (req.get("login") or {}).get("command") != "detect":
                return
            resp = {"login": {"bind": "free", "command": "detect", "connect": "lan", "dev_cap": 1,
                              "id": self.serial, "model": MODEL_CODE, "name": self.name,
                              "sequence_id": 3021, "version": FIRMWARE}}
            payload = json.dumps(resp, separators=(",", ":")).encode()
            writer.write(b"\xa5\xa5" + struct.pack("<H", len(payload) + 6) + payload + b"\xa7\xa7")
            await writer.drain()
        except (asyncio.TimeoutError, ValueError, OSError, ssl.SSLError):
            pass
        finally:
            writer.close()

    # -- MQTT ----------------------------------------------------------------
    def _status(self) -> dict:
        self.seq += 1
        return {"print": {
            "command": "push_status", "msg": 0, "sequence_id": str(self.seq),
            "gcode_state": self.gcode_state, "gcode_file": self.current_file,
            "gcode_file_prepare_percent": str(self.prepare_percent),
            "subtask_name": re.sub(r"(\.gcode)?\.3mf$", "", self.current_file),
            "mc_percent": 100 if self.gcode_state == "FINISH" else 0, "mc_remaining_time": 0,
            "mc_print_stage": "", "print_error": 0, "print_type": "local",
            "wifi_signal": "-40dBm", "bed_temper": 25.0, "bed_target_temper": 0.0,
            "nozzle_temper": 25.0, "nozzle_target_temper": 0.0, "chamber_temper": 25.0,
            "cooling_fan_speed": "0", "big_fan1_speed": "0", "big_fan2_speed": "0", "heatbreak_fan_speed": "0",
            "spd_lvl": 2, "spd_mag": 100, "stg": [], "stg_cur": 0, "layer_num": 0, "total_layer_num": 0,
            "home_flag": 256, "hw_switch_state": 0, "sdcard": True,
            "storage": {"free": 8_000_000_000, "total": 32_000_000_000},
            "online": {"ahb": False, "rfid": False, "version": 7}, "ams_status": 0,
            "lights_report": [{"node": "chamber_light", "mode": "on"}],
            "nozzle_diameter": "0.4", "nozzle_type": "hardened_steel",
            "upgrade_state": {"sequence_id": 0, "progress": "", "status": "", "consistency_request": False,
                              "dis_state": 0, "err_code": 0, "force_upgrade": False, "message": "",
                              "module": "", "new_version_state": 2, "new_ver_list": []},
            "ipcam": {"ipcam_dev": "0", "ipcam_record": "disable", "timelapse": "disable",
                      "resolution": "", "mode_bits": 0},
        }}

    def _version(self, seq) -> dict:
        mods = []
        for name, ver, hw in (("ota", FIRMWARE, "OTA"), ("rv1126", "00.00.27.38", "AP05"),
                              ("mc", "00.00.24.57", "MC07"), ("th", "00.00.06.75", "TH07")):
            mods.append({"name": name, "product_name": "X1 Carbon", "sw_ver": ver, "sw_new_ver": "",
                         "hw_ver": hw, "sn": self.serial, "flag": 0})
        return {"info": {"command": "get_version", "sequence_id": seq, "module": mods, "result": "success"}}

    def _publish(self, writer, data: dict):
        try:
            writer.write(mqtt_publish_packet(f"device/{self.serial}/report", json.dumps(data).encode()))
        except (OSError, RuntimeError):
            self.clients.discard(writer)

    def broadcast(self, data: dict):
        for w in list(self.clients):
            self._publish(w, data)

    async def _status_loop(self):
        while True:
            await asyncio.sleep(3)
            if self.clients:
                self.broadcast(self._status())

    async def _mqtt_client(self, reader, writer):
        authed = False
        try:
            while True:
                ptype, body = await asyncio.wait_for(_read_packet(reader), timeout=180)
                kind = ptype >> 4
                if kind == 1:  # CONNECT
                    i = 0
                    _proto, i = _read_str(body, i)
                    flags = body[i + 1]
                    i += 4
                    _client_id, i = _read_str(body, i)
                    if flags & 0x04:
                        _wt, i = _read_str(body, i)
                        _wm, i = _read_bytes(body, i)
                    user = pw = ""
                    if flags & 0x80:
                        user, i = _read_str(body, i)
                    if flags & 0x40:
                        pwb, i = _read_bytes(body, i)
                        pw = pwb.decode("utf-8", "replace")
                    authed = user == "bblp" and secrets.compare_digest(pw, self.access_code)
                    writer.write(bytes([0x20, 0x02, 0x00, 0x00 if authed else 0x05]))
                    await writer.drain()
                    if not authed:
                        return
                    self.clients.add(writer)
                elif not authed:
                    return
                elif kind == 8:  # SUBSCRIBE
                    pid = body[:2]
                    i, granted = 2, bytearray()
                    while i < len(body):
                        _topic, i = _read_str(body, i)
                        i += 1
                        granted.append(0)
                    writer.write(bytes([0x90]) + _encode_len(2 + len(granted)) + pid + bytes(granted))
                    self._publish(writer, self._status())
                elif kind == 3:  # PUBLISH
                    qos = (ptype >> 1) & 3
                    topic, i = _read_str(body, 0)
                    if qos:
                        pid = body[i:i + 2]
                        i += 2
                        writer.write(bytes([0x40, 0x02]) + pid)
                    if topic.endswith("/request"):
                        self._handle_request(writer, body[i:])
                elif kind == 12:  # PINGREQ
                    writer.write(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError, ssl.SSLError, struct.error, IndexError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()

    def _handle_request(self, writer, payload: bytes):
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        push = data.get("pushing") or {}
        if push.get("command") in ("pushall", "start"):
            self._publish(writer, self._status())
        info = data.get("info") or {}
        if info.get("command") == "get_version":
            self._publish(writer, self._version(info.get("sequence_id", "0")))
        pr = data.get("print") or {}
        if pr.get("command") in ("project_file", "gcode_file"):
            fname = os.path.basename(str(pr.get("file") or pr.get("url") or pr.get("subtask_name") or ""))
            fname = fname.split("?")[0]
            self.print_requests[fname] = pr
            self.gcode_state = "PREPARE"
            self.current_file = fname
            self._publish(writer, {"print": {"command": pr["command"], "sequence_id": pr.get("sequence_id", "0"),
                                             "param": pr.get("param", ""), "subtask_name": pr.get("subtask_name", ""),
                                             "result": "success", "gcode_state": "PREPARE"}})
        elif pr.get("command") in ("stop", "pause", "resume"):
            self.gcode_state = "IDLE" if pr["command"] == "stop" else self.gcode_state
            self._publish(writer, {"print": {"command": pr["command"], "sequence_id": pr.get("sequence_id", "0"),
                                             "result": "success"}})

    # -- FTPS ------------------------------------------------------------------
    async def _ftp_client(self, reader, writer):
        state = {"user": None, "authed": False, "data_srv": None, "data_conn": None}
        conn_ready = asyncio.Event()

        async def reply(code, text):
            writer.write(f"{code} {text}\r\n".encode())
            await writer.drain()

        async def open_passive():
            if state["data_srv"]:
                state["data_srv"].close()
            conn_ready.clear()
            state["data_conn"] = None

            async def on_data(r, w):
                state["data_conn"] = (r, w)
                conn_ready.set()

            for _ in range(20):
                port = random.randint(*PASSIVE_PORTS)
                try:
                    state["data_srv"] = await asyncio.start_server(on_data, "0.0.0.0", port, ssl=self.ssl_ctx)
                    return port
                except OSError:
                    continue
            return None

        await reply(220, "Bambu FTP ready")
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=300)
                if not line:
                    return
                cmd, _, arg = line.decode("utf-8", "replace").strip().partition(" ")
                cmd = cmd.upper()
                if cmd == "USER":
                    state["user"] = arg
                    await reply(331, "Password required")
                elif cmd == "PASS":
                    state["authed"] = state["user"] == "bblp" and secrets.compare_digest(arg, self.access_code)
                    await reply(230 if state["authed"] else 530, "Logged in" if state["authed"] else "Login incorrect")
                elif cmd in ("QUIT",):
                    await reply(221, "Bye")
                    return
                elif cmd in ("PBSZ",):
                    await reply(200, "PBSZ=0")
                elif cmd == "PROT":
                    await reply(200 if arg.upper() == "P" else 536, "OK" if arg.upper() == "P" else "Only P")
                elif cmd == "FEAT":
                    writer.write(b"211-Features:\r\n PASV\r\n EPSV\r\n UTF8\r\n SIZE\r\n")
                    await reply(211, "End")
                elif cmd == "OPTS":
                    await reply(200, "OK")
                elif cmd == "NOOP":
                    await reply(200, "OK")
                elif cmd == "SYST":
                    await reply(215, "UNIX Type: L8")
                elif not state["authed"]:
                    await reply(530, "Not logged in")
                elif cmd == "PWD":
                    await reply(257, '"/" is current directory')
                elif cmd in ("CWD", "MKD"):
                    await reply(250, "OK")
                elif cmd == "TYPE":
                    await reply(200, "Type set")
                elif cmd in ("PASV", "EPSV"):
                    port = await open_passive()
                    if port is None:
                        await reply(425, "Cannot open data connection")
                    elif cmd == "EPSV":
                        await reply(229, f"Entering Extended Passive Mode (|||{port}|)")
                    else:
                        ip = writer.get_extra_info("sockname")[0]
                        if ip.startswith("::ffff:"):
                            ip = ip[7:]
                        h = ip.replace(".", ",")
                        await reply(227, f"Entering Passive Mode ({h},{port >> 8},{port & 255})")
                elif cmd == "STOR":
                    await self._ftp_store(arg, state, conn_ready, reply)
                elif cmd in ("LIST", "NLST", "MLSD"):
                    await reply(150, "Here it comes")
                    try:
                        await asyncio.wait_for(conn_ready.wait(), timeout=5)
                        state["data_conn"][1].close()
                    except (asyncio.TimeoutError, TypeError):
                        pass
                    await reply(226, "Done")
                elif cmd == "SIZE":
                    await reply(550, "No such file")
                elif cmd == "DELE":
                    await reply(250, "Deleted")
                else:
                    await reply(502, "Not implemented")
        except (asyncio.TimeoutError, OSError, ssl.SSLError):
            pass
        finally:
            if state["data_srv"]:
                state["data_srv"].close()
            writer.close()

    async def _ftp_store(self, arg, state, conn_ready, reply):
        name = os.path.basename(arg.replace("\\", "/")) or "upload.3mf"
        name = re.sub(r"[^A-Za-z0-9 ._()\-+]", "_", name)
        await reply(150, f"Ready for {name}")
        try:
            await asyncio.wait_for(conn_ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            await reply(425, "No data connection")
            return
        r, w = state["data_conn"]
        path = os.path.join(UPLOAD_DIR, f"{int(time.time())}_{name}")
        total = 0
        try:
            with open(path, "wb") as fh:
                while True:
                    chunk = await asyncio.wait_for(r.read(1 << 16), timeout=60)
                    if not chunk:
                        break
                    total += len(chunk)
                    fh.write(chunk)
        except (asyncio.TimeoutError, OSError, ssl.SSLError):
            await reply(426, "Transfer failed")
            return
        finally:
            try:
                w.close()
            except OSError:
                pass
            if state["data_srv"]:
                state["data_srv"].close()
                state["data_srv"] = None
        await reply(226, "Transfer complete")
        if name.lower().endswith((".3mf", ".gcode")) and total:
            self.gcode_state = "PREPARE"
            self.current_file = name
            self.pending[name] = {"path": path, "time": time.time()}

    # -- hand files to the job queue -------------------------------------------
    async def _pending_loop(self):
        while True:
            await asyncio.sleep(1)
            now = time.time()
            for name, item in list(self.pending.items()):
                pr = self._take_print_request(name)
                if pr is None and now - item["time"] < PRINT_COMMAND_WAIT:
                    continue
                del self.pending[name]
                self._submit(name, item["path"], pr)
            # Reflect the job in the slicer's device page.
            for item_name, job in list(self._jobs_by_file.items()):
                if job.state == "done":
                    self.gcode_state, self.prepare_percent = "FINISH", 100
                    self._jobs_by_file.pop(item_name, None)
                elif job.state == "error":
                    self.gcode_state = "FAILED"
                    self._jobs_by_file.pop(item_name, None)

    @staticmethod
    def _stem(name: str) -> str:
        return re.sub(r"(\.gcode)?\.3mf$|\.gcode$", "", os.path.basename(name), flags=re.I).lower()

    def _take_print_request(self, name: str) -> dict | None:
        stem = self._stem(name)
        for key in list(self.print_requests):
            pr = self.print_requests[key]
            candidates = {self._stem(key), self._stem(str(pr.get("subtask_name", "")))}
            if stem in candidates:
                return self.print_requests.pop(key)
        if len(self.pending) == 1 and len(self.print_requests) == 1:
            # Only one upload and one print command: they belong together.
            return self.print_requests.pop(next(iter(self.print_requests)))
        return None

    def _submit(self, name: str, path: str, pr: dict | None):
        plate = 1
        if pr:
            m = re.search(r"plate_(\d+)", str(pr.get("param", "")))
            if m:
                plate = int(m.group(1))
        start = pr is not None
        job = Job(name, source="Bambu Studio", plate=plate, start=start)
        os.replace(path, job.path)
        if not start:
            job.log("Bambu Studio used 'Send' (not 'Print'), so the file is uploaded to the Q2 without starting.")
        self.jobs.submit(job)
        self._jobs_by_file[name] = job


_instance: VirtualPrinter | None = None


def start_virtual_printer(httpd) -> VirtualPrinter:
    """Called from __main__ when the Bambu Studio printer is switched on."""
    global _instance
    from .webserver import Handler
    vp = VirtualPrinter(Handler.jobs).start_in_thread()
    if vp.errors:
        raise RuntimeError("; ".join(vp.errors))
    _instance = vp
    print("Bambu Studio printer is on:")
    print(f"    Name: {vp.name}   IP: {vp.ip}")
    print(f"    Access code: {vp.access_code}   Serial: {vp.serial}")
    return vp


def current() -> VirtualPrinter | None:
    return _instance
