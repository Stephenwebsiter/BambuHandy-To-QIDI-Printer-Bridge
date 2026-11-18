"""Tiny Moonraker client (the API that Klipper printers such as the QIDI Q2 expose).

Only the Python standard library is used so the bridge runs on a fresh
Windows install without extra packages.
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid


class MoonrakerError(Exception):
    pass


class Moonraker:
    def __init__(self, host: str, port: int = 7125, api_key: str = "", timeout: float = 15.0):
        host = (host or "").strip()
        if not host:
            raise MoonrakerError("The printer address is not set yet. Open Settings and enter the Q2's IP address.")
        # Accept "192.168.1.20", "192.168.1.20:7125" or "http://192.168.1.20".
        if "://" in host:
            parsed = urllib.parse.urlparse(host)
            host = parsed.hostname or host
            if parsed.port:
                port = parsed.port
        elif host.count(":") == 1:
            host, _, p = host.partition(":")
            if p.isdigit():
                port = int(p)
        self.base = f"http://{host}:{int(port)}"
        self.api_key = api_key.strip()
        self.timeout = timeout

    # -- helpers ---------------------------------------------------------
    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None, timeout: float | None = None) -> dict:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if self.api_key:
            req.add_header("X-Api-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8", "replace")).get("error", {}).get("message", "")
            except Exception:
                pass
            raise MoonrakerError(f"The printer answered with error {e.code}{': ' + detail if detail else ''}") from e
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise MoonrakerError(
                f"Could not reach the printer at {self.base} ({reason}). "
                "Check that the Q2 is switched on, on the same Wi-Fi, and that the IP address in Settings is right."
            ) from e
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"raw": raw.decode("utf-8", "replace")}

    # -- API -------------------------------------------------------------
    def info(self) -> dict:
        return self._request("GET", "/printer/info").get("result", {})

    def status(self) -> dict:
        q = "/printer/objects/query?print_stats&display_status&virtual_sdcard&extruder&heater_bed"
        return self._request("GET", q).get("result", {}).get("status", {})

    def upload(self, file_path: str, remote_name: str | None = None, start_print: bool = True) -> dict:
        """Upload a G-code file to the printer's gcodes folder and optionally start it."""
        remote_name = remote_name or os.path.basename(file_path)
        boundary = "----qidibridge" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
        with open(file_path, "rb") as fh:
            data = fh.read()
        parts = []

        def field(name: str, value: str):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )

        field("root", "gcodes")
        field("print", "true" if start_print else "false")
        safe_name = remote_name.replace('"', "_")
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n".encode()
        )
        parts.append(data)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        return self._request(
            "POST", "/server/files/upload", body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
            timeout=max(60.0, len(body) / 200_000),
        )

    def start_print(self, remote_name: str) -> dict:
        q = urllib.parse.urlencode({"filename": remote_name})
        return self._request("POST", f"/printer/print/start?{q}")

    def control(self, action: str) -> dict:
        """Pause, resume or cancel the current print."""
        if action not in ("pause", "resume", "cancel"):
            raise MoonrakerError(f"Unknown printer action: {action}")
        return self._request("POST", f"/printer/print/{action}")
