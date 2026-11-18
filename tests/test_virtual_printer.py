"""Pretend to be Bambu Studio talking to the bridge's pretend Bambu printer."""

import ftplib
import json
import os
import socket
import ssl
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "QIDI_BRIDGE_DATA" not in os.environ:
    os.environ["QIDI_BRIDGE_DATA"] = os.path.join(tempfile.mkdtemp(prefix="qidi-vp-test-"), "data")

from tests import helpers  # noqa: E402
from qidi_bridge import certs, config, virtual_printer  # noqa: E402
from qidi_bridge.jobs import JobManager  # noqa: E402


def mqtt_str(s):
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def mqtt_packet(first, body):
    return bytes([first]) + virtual_printer._encode_len(len(body)) + body


def read_packet(sock):
    first = sock.recv(1)[0]
    mult, length = 1, 0
    while True:
        b = sock.recv(1)[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    body = b""
    while len(body) < length:
        body += sock.recv(length - len(body))
    return first, body


class ImplicitFTPS(ftplib.FTP_TLS):
    """Bambu printers use implicit FTPS: TLS from the first byte."""

    def connect(self, host, port, timeout=10):
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout)
        self.sock = self.context.wrap_socket(raw, server_hostname=self.serial)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        conn = self.context.wrap_socket(conn, server_hostname=self.serial, session=self.sock.session)
        return conn, size


class VirtualPrinterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = os.path.dirname(config.DATA_DIR)
        cls.exe = helpers.make_fake_slicer(os.path.join(tmp, "slicer-vp"))
        cls.printer = helpers.FakeMoonraker()
        config.save({"printer_host": "127.0.0.1", "printer_port": cls.printer.port, "slicer_path": cls.exe,
                     "virtual_printer_bind_ip": "127.0.0.1"})
        virtual_printer.PRINT_COMMAND_WAIT = 2
        cls.jobs = JobManager()
        cls.vp = virtual_printer.VirtualPrinter(cls.jobs, ports={"mqtt": 0, "ftps": 0, "bind_plain": 0,
                                                                   "bind_tls": 0, "ssdp": 0}).start_in_thread()
        assert not cls.vp.errors, cls.vp.errors
        cls.ctx = ssl.create_default_context(cafile=certs.CA_CERT)
        cls.ctx.check_hostname = True

    def tls(self, port):
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        return self.ctx.wrap_socket(raw, server_hostname=self.vp.serial)

    def test_identity_saved(self):
        cfg = config.load()
        self.assertRegex(cfg["virtual_printer_access_code"], r"^\d{8}$")
        self.assertTrue(cfg["virtual_printer_serial"].startswith("00M00A"))

    def test_bind_detect(self):
        s = socket.create_connection(("127.0.0.1", self.vp.ports["bind_plain"]), timeout=5)
        payload = json.dumps({"login": {"command": "detect", "sequence_id": "20000"}}).encode()
        s.sendall(b"\xa5\xa5" + struct.pack("<H", len(payload) + 6) + payload + b"\xa7\xa7")
        data = s.recv(4096)
        self.assertEqual(data[:2], b"\xa5\xa5")
        self.assertEqual(struct.unpack("<H", data[2:4])[0], len(data))
        resp = json.loads(data[4:-2])
        self.assertEqual(resp["login"]["id"], self.vp.serial)

    def test_ssdp_message(self):
        msg = self.vp._ssdp_message(True).decode()
        self.assertIn("NOTIFY * HTTP/1.1", msg)
        self.assertIn(f"USN: {self.vp.serial}", msg)
        self.assertIn("DevBind.bambu.com: free", msg)

    def mqtt_login(self, password):
        s = self.tls(self.vp.ports["mqtt"])
        flags = 0x80 | 0x40 | 0x02
        body = mqtt_str("MQTT") + bytes([4, flags]) + struct.pack("!H", 60) + mqtt_str("studio") + \
            mqtt_str("bblp") + mqtt_str(password)
        s.sendall(mqtt_packet(0x10, body))
        first, ack = read_packet(s)
        return s, first, ack

    def test_mqtt_wrong_code(self):
        _s, first, ack = self.mqtt_login("00000000" if self.vp.access_code != "00000000" else "11111111")
        self.assertEqual(first, 0x20)
        self.assertEqual(ack[1], 5)

    def test_studio_print_flow(self):
        s, first, ack = self.mqtt_login(self.vp.access_code)
        self.assertEqual((first, ack[1]), (0x20, 0))
        topic = f"device/{self.vp.serial}"
        s.sendall(mqtt_packet(0x82, b"\x00\x01" + mqtt_str(topic + "/report") + b"\x00"))
        first, body = read_packet(s)
        self.assertEqual(first, 0x90)
        first, body = read_packet(s)  # initial status
        self.assertEqual(first >> 4, 3)

        def request(obj):
            s.sendall(mqtt_packet(0x30, mqtt_str(topic + "/request") + json.dumps(obj).encode()))

        def wait_for(pred, timeout=10):
            end = time.time() + timeout
            while time.time() < end:
                f, b = read_packet(s)
                if f >> 4 != 3:
                    continue
                tl = struct.unpack("!H", b[:2])[0]
                msg = json.loads(b[2 + tl:])
                if pred(msg):
                    return msg
            self.fail("message not received")

        request({"info": {"command": "get_version", "sequence_id": "1"}})
        v = wait_for(lambda m: m.get("info", {}).get("command") == "get_version")
        self.assertEqual(v["info"]["module"][0]["sn"], self.vp.serial)

        # Upload a Bambu-sliced project over implicit FTPS, as Bambu Studio does.
        proj = helpers.make_bambu_project(os.path.join(config.DATA_DIR, "Cube.3mf"), plates=2,
                                          sliced_for="Bambu Lab X1 Carbon")
        ftp = ImplicitFTPS(context=self.ctx)
        ftp.serial = self.vp.serial
        ftp.connect("127.0.0.1", self.vp.ports["ftps"])
        ftp.login("bblp", self.vp.access_code)
        ftp.prot_p()
        with open(proj, "rb") as fh:
            ftp.storbinary("STOR Cube.3mf", fh)
        ftp.quit()

        request({"print": {"command": "project_file", "sequence_id": "2", "param": "Metadata/plate_2.gcode",
                           "subtask_name": "Cube", "url": "ftp://Cube.3mf", "use_ams": False}})
        ack = wait_for(lambda m: m.get("print", {}).get("command") == "project_file")
        self.assertEqual(ack["print"]["result"], "success")

        end = time.time() + 20
        while time.time() < end and not self.printer.uploads:
            time.sleep(0.2)
        self.assertEqual(len(self.printer.uploads), 1, self.jobs.list())
        body = self.printer.uploads[0]["body"]
        self.assertIn(b"printer_model = Qidi Q2", body)
        self.assertIn(b'name="print"\r\n\r\ntrue', body)
        job = self.jobs.list()[0]
        self.assertEqual(job["source"], "Bambu Studio")
        self.assertEqual(job["plate"], 2)
        wait_for(lambda m: m.get("print", {}).get("gcode_state") == "FINISH")


if __name__ == "__main__":
    unittest.main()
