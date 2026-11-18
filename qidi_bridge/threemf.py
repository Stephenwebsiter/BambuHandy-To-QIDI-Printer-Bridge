"""Look inside model files people send to the bridge.

Handles:
  * .gcode            - already sliced, sent as-is
  * .gcode.3mf / .3mf - Bambu Studio / QIDI Studio / OrcaSlicer projects, which
                        may already contain sliced G-code for one or more plates
  * .stl / .obj / .step / .stp - plain models that need slicing
"""

from __future__ import annotations

import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile

MESH_EXTS = (".stl", ".obj", ".step", ".stp", ".amf")
CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_PLATE_GCODE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")


class ModelFileError(Exception):
    pass


def _config_value(text: str, key: str) -> str | None:
    """Read `; key = value` (G-code) or `"key": "value"` (JSON) from slicer config text."""
    m = re.search(r"^;\s*" + re.escape(key) + r"\s*=\s*(.*)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"]*)"', text)
    if m:
        return m.group(1).strip()
    return None


def is_qidi_q2(model: str | None) -> bool:
    if not model:
        return False
    m = model.lower()
    return "qidi" in m and "q2" in m


def inspect(path: str) -> dict:
    lower = path.lower()
    if lower.endswith(".gcode"):
        return {"kind": "gcode", "printer_model": gcode_printer_model(path), "plates": [1]}
    if lower.endswith(".3mf"):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                plates = sorted(int(m.group(1)) for n in names if (m := _PLATE_GCODE.match(n)))
                printer_model = None
                if "Metadata/project_settings.config" in names:
                    cfg = z.read("Metadata/project_settings.config").decode("utf-8", "replace")
                    printer_model = _config_value(cfg, "printer_model") or _config_value(cfg, "printer_settings_id")
                if not printer_model and plates:
                    head = z.read(f"Metadata/plate_{plates[0]}.gcode")[-200_000:].decode("utf-8", "replace")
                    printer_model = _config_value(head, "printer_model")
                model_plates = list_model_plates(z)
        except zipfile.BadZipFile as e:
            raise ModelFileError("This .3mf file looks damaged (it is not a valid 3MF/zip file).") from e
        return {
            "kind": "sliced_3mf" if plates else "project_3mf",
            "printer_model": printer_model,
            "plates": plates or model_plates or [1],
        }
    if lower.endswith(MESH_EXTS):
        return {"kind": "mesh", "printer_model": None, "plates": [1]}
    raise ModelFileError("Unsupported file type. Send a .3mf, .stl, .obj, .step or .gcode file.")


def gcode_printer_model(path: str) -> str | None:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(64_000).decode("utf-8", "replace")
        fh.seek(max(0, size - 400_000))
        tail = fh.read().decode("utf-8", "replace")
    for text in (head, tail):
        v = _config_value(text, "printer_model")
        if v:
            return v
    for text in (head, tail):
        v = _config_value(text, "printer_settings_id")
        if v:
            return v
    return None


def extract_gcode(path: str, plate: int, out_path: str) -> str:
    with zipfile.ZipFile(path) as z:
        name = f"Metadata/plate_{plate}.gcode"
        if name not in z.namelist():
            raise ModelFileError(f"Plate {plate} has no sliced G-code in this file.")
        with z.open(name) as src, open(out_path, "wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
    return out_path


# ---------------------------------------------------------------------------
# Plates and geometry (used when the slicer cannot re-use a project file as-is)
# ---------------------------------------------------------------------------

def list_model_plates(z: zipfile.ZipFile) -> list[int]:
    return sorted(plate_objects(z).keys())


def plate_objects(z: zipfile.ZipFile) -> dict[int, list[str]]:
    """Map plate number -> list of build object ids, from Bambu-style model_settings.config."""
    try:
        root = ET.fromstring(z.read("Metadata/model_settings.config"))
    except (KeyError, ET.ParseError):
        return {}
    out: dict[int, list[str]] = {}
    for plate in root.iter("plate"):
        pid = None
        objs = []
        for md in plate.findall("metadata"):
            if md.get("key") == "plater_id":
                try:
                    pid = int(md.get("value", ""))
                except ValueError:
                    pid = None
        for inst in plate.iter("model_instance"):
            for md in inst.findall("metadata"):
                if md.get("key") == "object_id":
                    objs.append(md.get("value"))
        if pid is not None:
            out[pid] = objs
    return out


def _parse_transform(s: str | None):
    if not s:
        return None
    v = [float(x) for x in s.split()]
    if len(v) != 12:
        return None
    return v


def _compose(a, b):
    """Return transform applying a first, then b (3MF row-vector 3x4 matrices)."""
    if a is None:
        return b
    if b is None:
        return a
    A = [[a[0], a[1], a[2]], [a[3], a[4], a[5]], [a[6], a[7], a[8]], [a[9], a[10], a[11]]]
    B = [[b[0], b[1], b[2]], [b[3], b[4], b[5]], [b[6], b[7], b[8]], [b[9], b[10], b[11]]]
    R = []
    for i in range(4):
        row = []
        for j in range(3):
            val = sum(A[i][k] * B[k][j] for k in range(3))
            if i == 3:
                val += B[3][j]
            row.append(val)
        R.append(row)
    return [x for row in R for x in row]


def _apply(t, x, y, z):
    if t is None:
        return x, y, z
    return (x * t[0] + y * t[3] + z * t[6] + t[9],
            x * t[1] + y * t[4] + z * t[7] + t[10],
            x * t[2] + y * t[5] + z * t[8] + t[11])


class _ModelReader:
    def __init__(self, z: zipfile.ZipFile):
        self.z = z
        self.docs: dict[str, ET.Element] = {}

    def doc(self, part: str) -> ET.Element:
        part = part.lstrip("/")
        if part not in self.docs:
            self.docs[part] = ET.fromstring(self.z.read(part))
        return self.docs[part]

    def objects(self, part: str) -> dict[str, ET.Element]:
        root = self.doc(part)
        return {o.get("id"): o for o in root.iter(f"{{{CORE_NS}}}object")}

    def triangles(self, part: str, object_id: str, transform, depth: int = 0):
        if depth > 16:
            return
        obj = self.objects(part).get(object_id)
        if obj is None:
            return
        mesh = obj.find(f"{{{CORE_NS}}}mesh")
        if mesh is not None:
            verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
                     for v in mesh.iter(f"{{{CORE_NS}}}vertex")]
            verts = [_apply(transform, *v) for v in verts]
            for t in mesh.iter(f"{{{CORE_NS}}}triangle"):
                yield verts[int(t.get("v1"))], verts[int(t.get("v2"))], verts[int(t.get("v3"))]
        comps = obj.find(f"{{{CORE_NS}}}components")
        if comps is not None:
            for c in comps.findall(f"{{{CORE_NS}}}component"):
                sub_part = c.get(f"{{{PROD_NS}}}path") or part
                t = _compose(_parse_transform(c.get("transform")), transform)
                yield from self.triangles(sub_part, c.get("objectid"), t, depth + 1)


def _write_binary_stl(path: str, tris) -> int:
    count = 0
    with open(path, "wb") as fh:
        fh.write(b"QIDI bridge export".ljust(80, b" "))
        fh.write(struct.pack("<I", 0))
        for a, b, c in tris:
            fh.write(struct.pack("<12fH", 0.0, 0.0, 0.0, *a, *b, *c, 0))
            count += 1
        fh.seek(80)
        fh.write(struct.pack("<I", count))
    return count


def export_plate_meshes(path: str, plate: int, out_dir: str) -> list[str]:
    """Write each object on a plate of a 3MF as its own STL file (shape only, no settings)."""
    os.makedirs(out_dir, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        reader = _ModelReader(z)
        main = "3D/3dmodel.model"
        root = reader.doc(main)
        build = root.find(f"{{{CORE_NS}}}build")
        if build is None:
            raise ModelFileError("This 3MF has no objects in it.")
        wanted = plate_objects(z).get(plate)
        files = []
        for i, item in enumerate(build.findall(f"{{{CORE_NS}}}item")):
            oid = item.get("objectid")
            if wanted is not None and oid not in wanted:
                continue
            part = item.get(f"{{{PROD_NS}}}path") or main
            t = _parse_transform(item.get("transform"))
            out = os.path.join(out_dir, f"object_{i + 1}.stl")
            if _write_binary_stl(out, reader.triangles(part, oid, t)) > 0:
                files.append(out)
            else:
                os.remove(out)
    if not files:
        raise ModelFileError(f"Could not find any printable objects on plate {plate}.")
    return files
