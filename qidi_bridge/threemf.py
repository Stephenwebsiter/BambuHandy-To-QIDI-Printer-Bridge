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


def _model_key(name: str) -> str:
    """'Qidi Q2 0.4 nozzle' / 'QIDI Q2' / 'Qidi X-Plus 4' -> 'q2' / 'q2' / 'xplus4'."""
    n = (name or "").lower()
    n = re.sub(r"\b\d\.\d+\s*(mm\s*)?nozzle\b", "", n)
    n = n.replace("qidi", "")
    return re.sub(r"[^a-z0-9]", "", n)


def same_printer(model: str | None, machine_preset: str) -> bool:
    """Was a file made for the printer the bridge is set up for (machine preset name)?"""
    if not model:
        return False
    if "qidi" not in model.lower():
        return False
    return _model_key(model) == _model_key(machine_preset or "Qidi Q2")


def is_qidi_q2(model: str | None) -> bool:
    return same_printer(model, "Qidi Q2")


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


def _bounds(points) -> list[float] | None:
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    seen = False
    for p in points:
        seen = True
        for i in range(3):
            if p[i] < lo[i]:
                lo[i] = p[i]
            if p[i] > hi[i]:
                hi[i] = p[i]
    if not seen:
        return None
    return [round(hi[i] - lo[i], 2) for i in range(3)]


def _stl_points(path: str):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(84)
        count = struct.unpack("<I", head[80:84])[0] if len(head) >= 84 else 0
        if len(head) >= 84 and size == 84 + count * 50:
            for _ in range(count):
                rec = fh.read(50)
                if len(rec) < 50:
                    return
                v = struct.unpack("<12f", rec[:48])
                yield v[3:6]
                yield v[6:9]
                yield v[9:12]
            return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                yield float(parts[1]), float(parts[2]), float(parts[3])


def _obj_points(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("v "):
                parts = line.split()
                if len(parts) >= 4:
                    yield float(parts[1]), float(parts[2]), float(parts[3])


def model_size(path: str, plate: int = 1) -> list[float] | None:
    """Width, depth and height of the model in mm (all objects of a plate together), if it can be read."""
    lower = path.lower()
    try:
        if lower.endswith(".stl"):
            return _bounds(_stl_points(path))
        if lower.endswith(".obj"):
            return _bounds(_obj_points(path))
        if lower.endswith(".3mf"):
            with zipfile.ZipFile(path) as z:
                reader = _ModelReader(z)
                main = "3D/3dmodel.model"
                build = reader.doc(main).find(f"{{{CORE_NS}}}build")
                if build is None:
                    return None
                wanted = plate_objects(z).get(plate)

                def pts():
                    for item in build.findall(f"{{{CORE_NS}}}item"):
                        oid = item.get("objectid")
                        if wanted is not None and oid not in wanted:
                            continue
                        part = item.get(f"{{{PROD_NS}}}path") or main
                        for tri in reader.triangles(part, oid, _parse_transform(item.get("transform"))):
                            yield from tri
                return _bounds(pts())
    except (OSError, ValueError, KeyError, struct.error, zipfile.BadZipFile, ET.ParseError):
        return None
    return None


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


# ---------------------------------------------------------------------------
# Colours / filaments a model uses, and its preview picture
# ---------------------------------------------------------------------------

def _listify(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        return [x for x in re.split(r"[;,]", v) if x != ""]
    return []


def model_filaments(path: str, plate: int = 1) -> list[dict]:
    """The filaments (colours) a model is set up with: [{"index": 0, "color": "#RRGGBB", "type": "PLA", ...}].

    Plain models (STL/OBJ/STEP) and single-colour projects give one entry.
    """
    import json
    default = [{"index": 0, "color": "#FFFFFF", "type": "PLA", "name": "", "used": True}]
    if not path.lower().endswith(".3mf"):
        if path.lower().endswith(".gcode"):
            return _gcode_filaments(path) or default
        return default
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            cfg = {}
            if "Metadata/project_settings.config" in names:
                try:
                    cfg = json.loads(z.read("Metadata/project_settings.config").decode("utf-8", "replace"))
                except ValueError:
                    cfg = {}
            used = _used_filaments(z, names, plate)
    except zipfile.BadZipFile:
        return default
    colours = _listify(cfg.get("filament_colour"))
    types = _listify(cfg.get("filament_type"))
    ids = _listify(cfg.get("filament_settings_id"))
    count = max(len(colours), len(types), len(ids), 1)
    out = []
    for i in range(count):
        col = colours[i] if i < len(colours) else "#FFFFFF"
        out.append({
            "index": i,
            "color": _norm_hex(col),
            "type": (types[i] if i < len(types) else (types[0] if types else "PLA")) or "PLA",
            "name": (ids[i] if i < len(ids) else "").strip('"'),
            "used": (used is None) or (i + 1 in used),
        })
    if not any(f["used"] for f in out):
        for f in out:
            f["used"] = True
    return out


def _norm_hex(v: str) -> str:
    v = str(v).strip().strip('"').lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{8}", v):
        v = v[:6]
    return "#" + v.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", v) else "#FFFFFF"


def _used_filaments(z: zipfile.ZipFile, names: set, plate: int):
    """1-based filament numbers used on a plate, or None when it can't be told."""
    if "Metadata/slice_info.config" in names:
        try:
            root = ET.fromstring(z.read("Metadata/slice_info.config"))
            for p in root.iter("plate"):
                idx = None
                for md in p.findall("metadata"):
                    if md.get("key") == "index":
                        idx = int(md.get("value", "0") or 0)
                if idx == plate:
                    ids = {int(f.get("id")) for f in p.iter("filament") if (f.get("id") or "").isdigit()}
                    if ids:
                        return ids
        except (ET.ParseError, ValueError):
            pass
    # Painted models can use any colour, so don't guess.
    for n in names:
        if n.startswith("3D/") and n.endswith(".model"):
            with z.open(n) as fh:
                head = fh.read(4_000_000)
            if b"mmu_segmentation" in head or b"paint_color" in head:
                return None
    try:
        root = ET.fromstring(z.read("Metadata/model_settings.config"))
    except (KeyError, ET.ParseError):
        return None
    wanted = set(plate_objects(z).get(plate) or [])
    used = set()
    for obj in root.iter("object"):
        if wanted and obj.get("id") not in wanted:
            continue
        for md in obj.iter("metadata"):
            if md.get("key") == "extruder":
                try:
                    used.add(int(md.get("value")))
                except (TypeError, ValueError):
                    pass
    return used or None


def _gcode_filaments(path: str) -> list[dict]:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 400_000))
        tail = fh.read().decode("utf-8", "replace")
    cols = _config_value(tail, "filament_colour")
    types = _config_value(tail, "filament_type")
    if not cols:
        return []
    cols = _listify(cols)
    types = _listify(types or "")
    return [{"index": i, "color": _norm_hex(c), "type": types[i] if i < len(types) else "PLA", "name": "", "used": True}
            for i, c in enumerate(cols)]


def thumbnail(path: str, plate: int = 1) -> bytes | None:
    """Preview picture stored inside a 3MF (sliced or project), if any."""
    if not path.lower().endswith(".3mf"):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            for n in (f"Metadata/plate_{plate}.png", "Metadata/plate_1.png", "Metadata/thumbnail.png",
                      "Auxiliaries/.thumbnails/thumbnail_middle.png", "Auxiliaries/.thumbnails/thumbnail_3mf.png"):
                if n in names:
                    return z.read(n)
            pngs = [n for n in names if n.lower().endswith(".png") and "thumb" in n.lower()]
            if pngs:
                return z.read(pngs[0])
    except zipfile.BadZipFile:
        pass
    return None
