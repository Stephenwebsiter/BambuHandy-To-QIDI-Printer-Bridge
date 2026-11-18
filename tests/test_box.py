"""QIDI Box: reading the slots, matching colours, and multi-colour prints."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

if "QIDI_BRIDGE_DATA" not in os.environ:
    os.environ["QIDI_BRIDGE_DATA"] = os.path.join(tempfile.mkdtemp(prefix="qidi-box-test-"), "data")
os.environ.setdefault("QIDI_BRIDGE_PROFILE_URL", "http://127.0.0.1:9")

from tests import helpers  # noqa: E402
from qidi_bridge import box, config, profiles, slicer, threemf, webserver  # noqa: E402
from qidi_bridge.moonraker import Moonraker  # noqa: E402

TMP = tempfile.mkdtemp(prefix="qidi-box-")


class BoxReadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.printer = helpers.FakeMoonraker(box="q2_box.json")

    @classmethod
    def tearDownClass(cls):
        cls.printer.close()

    def test_reads_all_four_slots(self):
        b = box.read_box(Moonraker("127.0.0.1", self.printer.port))
        self.assertTrue(b["present"])
        self.assertEqual(len(b["slots"]), 4)
        self.assertTrue(all(s["loaded"] for s in b["slots"]))
        self.assertEqual([s["color"] for s in b["slots"]], ["#FAFAFA", "#FF362D", "#DFD628", "#1353AB"])
        self.assertEqual(b["slots"][0]["type"], "ABS")
        self.assertEqual(b["slots"][1]["name"], "PLA Basic")
        self.assertEqual([s["active"] for s in b["slots"]], [False, False, False, True])
        self.assertEqual(b["slots"][3]["tools"], [0])
        self.assertEqual(b["climate"][0]["humidity"], 59)

    def test_suggest_mapping_by_colour_and_material(self):
        b = box.read_box(Moonraker("127.0.0.1", self.printer.port))
        fils = [{"color": "#FF0000", "type": "PLA"}, {"color": "#0000FF", "type": "PLA"},
                {"color": "#FFFFFF", "type": "ABS"}, {"color": "#FFFF00", "type": "PLA"}]
        self.assertEqual(box.suggest_mapping(fils, b["slots"]), [1, 3, 0, 2])

    def test_tool_slot_script(self):
        self.assertEqual(box.tool_slot_script([3, 1]),
                         "SAVE_VARIABLE VARIABLE=value_t0 VALUE=\"'slot3'\"\n"
                         "SAVE_VARIABLE VARIABLE=value_t1 VALUE=\"'slot1'\"")

    def test_filament_preset_matching(self):
        exe = helpers.make_fake_slicer(os.path.join(TMP, "slicer-m"))
        lib = profiles.PresetLibrary(profiles.profile_roots_for(exe))
        m = "Qidi Q2 0.4 nozzle"
        self.assertEqual(profiles.match_filament_preset(lib, m, "PLA Basic", "QIDI", "PLA"),
                         "QIDI PLA Basic @Qidi Q2 0.4 nozzle")
        self.assertEqual(profiles.match_filament_preset(lib, m, "ABS", "Generic", "ABS"),
                         "Generic ABS @Qidi Q2 0.4 nozzle")

    def test_printer_names(self):
        self.assertEqual(profiles.model_of("Qidi X-Plus 4 0.4 nozzle"), "X-Plus 4")
        self.assertTrue(threemf.same_printer("Qidi X-Plus 4", "Qidi X-Plus 4 0.4 nozzle"))
        self.assertFalse(threemf.same_printer("Qidi Q2", "Qidi X-Plus 4 0.4 nozzle"))
        self.assertTrue(threemf.same_printer("Qidi Q2", "Qidi Q2 0.4 nozzle"))

    def test_model_colours(self):
        p = helpers.make_bambu_project(os.path.join(TMP, "c.3mf"), plates=1, filaments=2,
                                       colours=["#FF0000", "#0000FFFF"])
        fils = threemf.model_filaments(p, 1)
        self.assertEqual([f["color"] for f in fils], ["#FF0000", "#0000FF"])


class AdhesionTests(unittest.TestCase):
    def test_first_layer_settings(self):
        proc = {"initial_layer_speed": "50", "initial_layer_infill_speed": "105", "brim_type": "auto_brim"}
        over = slicer.process_overrides(proc, "Textured PEI Plate", True)
        self.assertEqual(over, {"curr_bed_type": "Textured PEI Plate", "initial_layer_speed": "35",
                                "initial_layer_infill_speed": "60"})
        self.assertEqual(slicer.process_overrides(proc, "Cool Plate", False), {"curr_bed_type": "Cool Plate"})
        fil = {"filament_type": ["PLA"], "close_fan_the_first_x_layers": ["1"],
               "textured_plate_temp": ["60"], "textured_plate_temp_initial_layer": ["60"]}
        self.assertEqual(slicer.filament_overrides(fil, "Textured PEI Plate", True),
                         {"close_fan_the_first_x_layers": ["3"], "textured_plate_temp_initial_layer": ["65"]})
        self.assertEqual(slicer.filament_overrides(fil, "Textured PEI Plate", False), {})
        self.assertEqual(slicer.bed_temp(fil, "Textured PEI Plate"), (60, 60))


class BoxPrintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = helpers.make_fake_slicer(os.path.join(TMP, "slicer"))
        cls.printer = helpers.FakeMoonraker(box="q2_box.json")
        cls.old_cfg = config.load()
        config.save({"printer_host": "127.0.0.1", "printer_port": cls.printer.port, "slicer_path": cls.exe,
                     "use_box": True})
        slicer._LIB_CACHE.clear()
        cls.httpd = webserver.serve(port=0, host="127.0.0.1")
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.printer.close()
        config.save({k: cls.old_cfg[k] for k in ("printer_host", "printer_port", "slicer_path", "use_box")})
        slicer._LIB_CACHE.clear()

    def setUp(self):
        self.printer.uploads.clear()
        self.printer.gcode.clear()

    def post(self, path, data, headers=None):
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def wait(self, job_id):
        for _ in range(100):
            with urllib.request.urlopen(self.base + "/api/jobs") as r:
                j = {x["id"]: x for x in json.loads(r.read())["jobs"]}[job_id]
            if j["state"] in ("done", "error"):
                return j
            time.sleep(0.1)
        self.fail("job did not finish")

    def upload(self, path):
        with open(path, "rb") as fh:
            return self.post("/api/files", fh.read(), {"X-Filename": os.path.basename(path)})

    def test_box_api(self):
        with urllib.request.urlopen(self.base + "/api/box") as r:
            b = json.loads(r.read())
        self.assertTrue(b["present"])
        self.assertEqual(b["slots"][1]["preset"], "QIDI PLA Basic @Qidi Q2 0.4 nozzle")

    def test_multicolour_project_uses_box_slots(self):
        p = helpers.make_bambu_project(os.path.join(TMP, "two.3mf"), plates=1, filaments=2,
                                       colours=["#FF0000", "#0000FF"])
        info = self.upload(p)
        self.assertEqual(len(info["filaments"]), 2)
        res = self.post("/api/jobs", json.dumps({"file_id": info["file_id"], "plate": 1}).encode(),
                        {"Content-Type": "application/json"})
        j = self.wait(res["job"]["id"])
        self.assertEqual(j["state"], "done", j["log"])
        body = self.printer.uploads[0]["body"]
        self.assertIn(b"colours = #FF362D|#1353AB", body)
        self.assertIn(b"inputs = two.3mf", body)
        self.assertEqual(self.printer.gcode[-1], "SAVE_VARIABLE VARIABLE=value_t0 VALUE=\"'slot1'\"\n"
                                                 "SAVE_VARIABLE VARIABLE=value_t1 VALUE=\"'slot3'\"")
        self.assertEqual([c["slot"] for c in j["colours"]], [1, 3])

    def test_filament_library(self):
        with urllib.request.urlopen(self.base + "/api/filaments") as r:
            d = json.loads(r.read())
        self.assertTrue(d["ok"])
        names = {f["name"]: f for f in d["filaments"]}
        self.assertEqual(names["Generic ABS @Qidi Q2 0.4 nozzle"]["family"], "ABS")

    def test_chosen_slots_are_used(self):
        p = helpers.make_bambu_project(os.path.join(TMP, "two-b.3mf"), plates=1, filaments=2,
                                       colours=["#FF0000", "#0000FF"])
        info = self.upload(p)
        res = self.post("/api/jobs", json.dumps({"file_id": info["file_id"], "slots": [2, 0]}).encode(),
                        {"Content-Type": "application/json"})
        j = self.wait(res["job"]["id"])
        self.assertEqual(j["state"], "done", j["log"])
        body = self.printer.uploads[0]["body"]
        self.assertIn(b"colours = #DFD628|#FAFAFA", body)
        self.assertIn(b"Generic ABS @Qidi Q2 0.4 nozzle", body)

    def test_same_slot_for_all_colours_prints_one_colour(self):
        p = helpers.make_bambu_project(os.path.join(TMP, "two-c.3mf"), plates=1, filaments=2,
                                       colours=["#FF0000", "#0000FF"])
        info = self.upload(p)
        res = self.post("/api/jobs", json.dumps({"file_id": info["file_id"], "slots": [1, 1]}).encode(),
                        {"Content-Type": "application/json"})
        j = self.wait(res["job"]["id"])
        self.assertEqual(j["state"], "done", j["log"])
        self.assertIn(b"inputs = object_1.stl", self.printer.uploads[0]["body"])
        self.assertEqual(self.printer.gcode[-1], "SAVE_VARIABLE VARIABLE=value_t0 VALUE=\"'slot1'\"")

    def test_plain_stl_uses_filament_in_print_head(self):
        stl = helpers.write_ascii_stl(os.path.join(TMP, "cube.stl"))
        info = self.upload(stl)
        res = self.post("/api/jobs", json.dumps({"file_id": info["file_id"]}).encode(),
                        {"Content-Type": "application/json"})
        j = self.wait(res["job"]["id"])
        self.assertEqual(j["state"], "done", j["log"])
        self.assertIn(b"colours = #1353AB", self.printer.uploads[0]["body"])
        self.assertEqual(self.printer.gcode[-1], "SAVE_VARIABLE VARIABLE=value_t0 VALUE=\"'slot3'\"")

    def test_no_slot_change_when_only_uploading(self):
        self.printer.state = "printing"
        try:
            stl = helpers.write_ascii_stl(os.path.join(TMP, "cube3.stl"))
            info = self.upload(stl)
            res = self.post("/api/jobs", json.dumps({"file_id": info["file_id"]}).encode(),
                            {"Content-Type": "application/json"})
            j = self.wait(res["job"]["id"])
            self.assertEqual(j["state"], "done", j["log"])
            self.assertEqual(self.printer.gcode, [])
        finally:
            self.printer.state = "standby"


if __name__ == "__main__":
    unittest.main()
