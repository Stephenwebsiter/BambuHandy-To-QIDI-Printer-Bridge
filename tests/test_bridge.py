import json
import os
import struct
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp(prefix="qidi-bridge-test-")
os.environ["QIDI_BRIDGE_DATA"] = os.path.join(TMP, "data")

from tests import helpers  # noqa: E402
from qidi_bridge import config, profiles, slicer, threemf  # noqa: E402
from qidi_bridge.moonraker import Moonraker  # noqa: E402
from qidi_bridge import webserver  # noqa: E402


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = helpers.make_fake_slicer(os.path.join(TMP, "slicer"))

    def test_inheritance_is_flattened(self):
        lib = profiles.PresetLibrary(profiles.profile_roots_for(self.exe))
        m = lib.resolve("machine", "Qidi Q2 0.4 nozzle")
        self.assertEqual(m["printable_area"][2], "270x270")
        self.assertEqual(m["printer_model"], "Qidi Q2")
        self.assertNotIn("inherits", m)
        f = lib.resolve("filament", "QIDI PLA Rapido @Qidi Q2 0.4 nozzle")
        self.assertEqual(f["nozzle_temperature"], ["210"])
        self.assertNotIn("compatible_printers", f)

    def test_lists_only_matching_presets(self):
        lib = profiles.PresetLibrary(profiles.profile_roots_for(self.exe))
        self.assertEqual(lib.names("process", "Qidi Q2 0.4 nozzle"), ["0.20mm Standard @Qidi Q2"])
        self.assertNotIn("fdm_q_common", lib.names("machine"))


class ThreeMFTests(unittest.TestCase):
    def test_project_plate_meshes(self):
        p = helpers.make_bambu_project(os.path.join(TMP, "proj.3mf"), plates=2)
        info = threemf.inspect(p)
        self.assertEqual(info["kind"], "project_3mf")
        self.assertEqual(info["plates"], [1, 2])
        files = threemf.export_plate_meshes(p, 2, os.path.join(TMP, "meshes"))
        self.assertEqual(len(files), 1)
        with open(files[0], "rb") as fh:
            fh.seek(80)
            (count,) = struct.unpack("<I", fh.read(4))
            self.assertEqual(count, 12)
            first = struct.unpack("<12f", fh.read(48))
        # Plate 2 object sits at x offset 400 (100 + 300) from its build transform.
        self.assertAlmostEqual(first[3], 400.0)

    def test_sliced_project(self):
        p = helpers.make_bambu_project(os.path.join(TMP, "sliced.3mf"), plates=1, sliced_for="Qidi Q2")
        info = threemf.inspect(p)
        self.assertEqual(info["kind"], "sliced_3mf")
        self.assertTrue(threemf.is_qidi_q2(info["printer_model"]))


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = helpers.make_fake_slicer(os.path.join(TMP, "slicer"))
        cls.printer = helpers.FakeMoonraker()
        cls.slicer_log = os.path.join(TMP, "slicer-calls.log")
        os.environ["FAKE_SLICER_LOG"] = cls.slicer_log
        config.save({"printer_host": "127.0.0.1", "printer_port": cls.printer.port, "slicer_path": cls.exe})
        cls.httpd = webserver.serve(port=0, host="127.0.0.1")
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        import threading
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.printer.close()

    def setUp(self):
        self.printer.uploads.clear()
        os.environ.pop("FAKE_SLICER_FAIL_3MF", None)

    def send(self, path, name=None, **headers):
        with open(path, "rb") as fh:
            data = fh.read()
        req = urllib.request.Request(self.base + "/api/jobs", data=data, method="POST")
        req.add_header("X-Filename", name or os.path.basename(path))
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req) as r:
            job = json.loads(r.read())["job"]
        for _ in range(100):
            with urllib.request.urlopen(self.base + "/api/jobs") as r:
                jobs = {j["id"]: j for j in json.loads(r.read())["jobs"]}
            j = jobs[job["id"]]
            if j["state"] in ("done", "error"):
                return j
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_stl_is_sliced_and_printed(self):
        stl = helpers.write_ascii_stl(os.path.join(TMP, "cube.stl"))
        j = self.send(stl)
        self.assertEqual(j["state"], "done", j["log"])
        self.assertEqual(len(self.printer.uploads), 1)
        body = self.printer.uploads[0]["body"]
        self.assertIn(b'name="print"\r\n\r\ntrue', body)
        self.assertIn(b"printer_model = Qidi Q2", body)
        self.assertIn(b'filename="cube_', body)

    def test_bambu_project_resliced(self):
        p = helpers.make_bambu_project(os.path.join(TMP, "bambu.3mf"), plates=2, sliced_for="Bambu Lab X1 Carbon")
        j = self.send(p, **{"X-Plate": "2"})
        self.assertEqual(j["state"], "done", j["log"])
        self.assertIn(b"printer_model = Qidi Q2", self.printer.uploads[0]["body"])

    def test_project_falls_back_to_shapes(self):
        os.environ["FAKE_SLICER_FAIL_3MF"] = "1"
        p = helpers.make_bambu_project(os.path.join(TMP, "bambu2.3mf"), plates=1)
        j = self.send(p)
        self.assertEqual(j["state"], "done", j["log"])
        self.assertIn(b"inputs = object_1.stl", self.printer.uploads[0]["body"])

    def test_multicolour_without_box_uses_shapes(self):
        p = helpers.make_bambu_project(os.path.join(TMP, "multi.3mf"), plates=1, filaments=4)
        j = self.send(p)
        self.assertEqual(j["state"], "done", j["log"])
        self.assertIn(b"inputs = object_1.stl", self.printer.uploads[0]["body"])
        self.assertTrue(any("one colour" in line for line in j["log"]))

    def test_q2_sliced_project_sent_as_is(self):
        p = helpers.make_bambu_project(os.path.join(TMP, "q2.3mf"), plates=1, sliced_for="Qidi Q2")
        before = os.path.getsize(self.slicer_log) if os.path.exists(self.slicer_log) else 0
        j = self.send(p)
        self.assertEqual(j["state"], "done", j["log"])
        after = os.path.getsize(self.slicer_log) if os.path.exists(self.slicer_log) else 0
        self.assertEqual(before, after, "slicer should not run")
        self.assertIn(b"G28 ; plate 1", self.printer.uploads[0]["body"])

    def test_foreign_gcode_refused(self):
        g = os.path.join(TMP, "x1c.gcode")
        with open(g, "w") as fh:
            fh.write("; printer_model = Bambu Lab X1 Carbon\nG28\n")
        j = self.send(g)
        self.assertEqual(j["state"], "error")
        self.assertEqual(self.printer.uploads, [])

    def test_unknown_gcode_needs_confirmation(self):
        g = os.path.join(TMP, "plain.gcode")
        with open(g, "w") as fh:
            fh.write("G28\nG1 X10\n")
        self.assertEqual(self.send(g)["state"], "error")
        self.assertEqual(self.send(g, **{"X-Trust-Gcode": "1"})["state"], "done")

    def test_busy_printer_upload_only(self):
        self.printer.state = "printing"
        try:
            stl = helpers.write_ascii_stl(os.path.join(TMP, "cube2.stl"))
            j = self.send(stl)
            self.assertEqual(j["state"], "done")
            self.assertIn(b'name="print"\r\n\r\nfalse', self.printer.uploads[0]["body"])
        finally:
            self.printer.state = "standby"

    def test_pin(self):
        config.save({"pin": "1234"})
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(self.base + "/api/state")
            self.assertEqual(cm.exception.code, 401)
            req = urllib.request.Request(self.base + "/api/state", headers={"X-Bridge-Pin": "1234"})
            with urllib.request.urlopen(req) as r:
                self.assertTrue(json.loads(r.read())["ok"])
        finally:
            config.save({"pin": ""})

    def test_page_and_presets(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"Q2 Print Bridge", r.read())
        with urllib.request.urlopen(self.base + "/api/presets") as r:
            p = json.loads(r.read())
        self.assertEqual(p["machine"], ["Qidi Q2 0.4 nozzle"])
        self.assertIn("QIDI PLA Rapido @Qidi Q2 0.4 nozzle", p["filament"])


class MoonrakerAddressTests(unittest.TestCase):
    def test_address_forms(self):
        self.assertEqual(Moonraker("192.168.1.5").base, "http://192.168.1.5:7125")
        self.assertEqual(Moonraker("192.168.1.5:80").base, "http://192.168.1.5:80")
        self.assertEqual(Moonraker("http://q2.local:7125/").base, "http://q2.local:7125")


if __name__ == "__main__":
    unittest.main()


class ProfileDownloadTests(unittest.TestCase):
    """OrcaSlicer builds that pack their presets (.opc) make the bridge download them instead."""

    def test_downloads_q2_presets_when_slicer_has_none(self):
        import functools
        import shutil
        import threading
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from qidi_bridge import profile_fetch

        # A pretend copy of OrcaSlicer's online profile folder, served over HTTP.
        site = os.path.join(TMP, "site")
        src = helpers.make_fake_slicer(os.path.join(TMP, "site-src"))
        shutil.copytree(os.path.join(os.path.dirname(src), "resources", "profiles", "Qidi"),
                        os.path.join(site, "Qidi"), dirs_exist_ok=True)
        index = {"version": "1", "machine_model_list": [], "machine_list": [], "process_list": [], "filament_list": []}
        for dirpath, _d, files in os.walk(os.path.join(site, "Qidi")):
            for fn in files:
                rel = os.path.relpath(os.path.join(dirpath, fn), os.path.join(site, "Qidi")).replace(os.sep, "/")
                data = json.load(open(os.path.join(dirpath, fn)))
                index[data["type"] + "_list"].append({"name": data["name"], "sub_path": rel})
        with open(os.path.join(site, "Qidi.json"), "w") as fh:
            json.dump(index, fh)
        handler = functools.partial(SimpleHTTPRequestHandler, directory=site)
        handler.log_message = lambda *a: None
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        # A slicer install whose Qidi folder has no readable presets.
        bare = os.path.join(TMP, "bare-slicer")
        exe = helpers.make_fake_slicer(bare)
        shutil.rmtree(os.path.join(bare, "resources", "profiles", "Qidi"))
        os.makedirs(os.path.join(bare, "resources", "profiles", "Qidi"))

        old = profile_fetch.BASE_URL
        profile_fetch.BASE_URL = f"http://127.0.0.1:{httpd.server_address[1]}"
        shutil.rmtree(profile_fetch.CACHE_ROOT, ignore_errors=True)
        try:
            lib = slicer.library(exe, refresh=True, log=lambda *_: None)
            self.assertIn("Qidi Q2 0.4 nozzle", lib.names("machine"))
            self.assertEqual(lib.resolve("machine", "Qidi Q2 0.4 nozzle")["printable_area"][2], "270x270")
            self.assertIn("QIDI PLA Rapido @Qidi Q2 0.4 nozzle", lib.names("filament", "Qidi Q2 0.4 nozzle"))
            self.assertTrue(profile_fetch.have_cache())
        finally:
            profile_fetch.BASE_URL = old
            httpd.shutdown()
            shutil.rmtree(profile_fetch.CACHE_ROOT, ignore_errors=True)
            slicer._LIB_CACHE.clear()
