"""Read the QIDI Box: which filament and colour sits in each slot.

QIDI printers with a Box (Q2, Plus 4, Max 4) keep the slot contents in
Klipper's `save_variables`:

  box_count               number of Boxes (4 slots each)
  filament_slot<N>        row of [fila<row>] in officiall_filas_list.cfg (newer firmware: a text id)
  color_slot<N>           row of [colordict] in that file (newer firmware: a list of hex colours)
  vendor_slot<N>          0 = generic, 1 = QIDI
  value_t<T> = 'slot<N>'  tool T prints from slot N

and `box_stepper slot<N>` objects whose `runout_button` is 0 when filament
is present. Slots are numbered from 0 here; people see them as 1-4 per Box.
This follows the same rules OrcaSlicer's QIDI support uses.
"""

from __future__ import annotations

import re
import time

from .moonraker import Moonraker, MoonrakerError

FILAS_PATH = "/server/files/config/officiall_filas_list.cfg"
_CACHE: dict = {}


def parse_filas_list(text: str) -> dict:
    """Parse officiall_filas_list.cfg into colours, filaments and vendors."""
    colors: dict[int, str] = {}
    filaments: dict[int, dict] = {}
    vendors: dict[int, str] = {}
    section = None
    fila_idx = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            m = re.fullmatch(r"fila(\d+)", section)
            fila_idx = int(m.group(1)) if m else None
            if fila_idx is not None:
                filaments[fila_idx] = {}
            continue
        if "=" not in line and ":" not in line:
            continue
        sep = "=" if "=" in line else ":"
        key, _, value = line.partition(sep)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if section == "colordict":
            try:
                colors[int(key)] = normalize_color(value)
            except ValueError:
                pass
        elif section in ("vendor_list", "vendordict"):
            try:
                vendors[int(key)] = value
            except ValueError:
                pass
        elif fila_idx is not None:
            filaments[fila_idx][key.lower()] = value
    return {"colors": colors, "filaments": filaments, "vendors": vendors}


def normalize_color(value: str) -> str:
    v = str(value).strip().strip('"').strip("'").lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{8}", v):
        v = v[:6]  # RRGGBBAA
    if re.fullmatch(r"[0-9a-fA-F]{6}", v):
        return "#" + v.upper()
    if re.fullmatch(r"[0-9a-fA-F]{3}", v):
        return "#" + "".join(c * 2 for c in v).upper()
    return "#FFFFFF"


def normalize_type(name: str) -> str:
    """Main material family, e.g. 'PLA Rapido Matte' -> 'PLA'."""
    up = (name or "").upper()
    for fam in ("PLA", "PETG", "ABS", "ASA", "TPU", "PVA", "PC", "PET", "PPS", "PEBA"):
        if fam in up:
            return fam
    if "PA" in up or "NYLON" in up:
        return "PA"
    return up.strip() or "PLA"


def _as_int(v, default=0):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        if m:
            return int(m.group(0))
    return default


def filament_dict(mr: Moonraker, max_age: float = 600) -> dict:
    key = mr.base
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        d = parse_filas_list(mr.get_text(FILAS_PATH))
    except MoonrakerError:
        d = {"colors": {}, "filaments": {}, "vendors": {}}
    _CACHE[key] = (time.time(), d)
    return d


def read_box(mr: Moonraker) -> dict:
    """Return {"present": bool, "boxes": n, "slots": [...]} describing every Box slot."""
    try:
        objects = set(mr.object_list())
    except MoonrakerError as e:
        return {"present": False, "boxes": 0, "slots": [], "error": str(e)}
    stepper_names = sorted((o for o in objects if o.startswith("box_stepper slot")),
                           key=lambda o: _as_int(o.rsplit("slot", 1)[1]))
    query = {"save_variables": "variables"}
    if "box_extras" in objects:
        query["box_extras"] = None
    if "multi_color_controller" in objects:
        query["multi_color_controller"] = None
    for o in stepper_names:
        query[o] = None
    status = mr.query(query)
    variables = (status.get("save_variables") or {}).get("variables") or {}
    box_count = _as_int(variables.get("box_count"), 0)
    if not box_count and stepper_names:
        box_count = (len(stepper_names) + 3) // 4
    if box_count <= 0:
        return {"present": False, "boxes": 0, "slots": [], "enabled": False}
    # The Box can be switched off on the printer's screen; still show what is in it.
    enabled = str(variables.get("enable_box", "1")) not in ("0", "False", "false")
    extras = status.get("box_extras") or {}
    decoded = {}
    for b, boxinfo in enumerate(extras.get("boxes") or []):
        # The firmware always lists 4 Boxes; only the first box_count are real.
        if not isinstance(boxinfo, dict) or b >= box_count or not _as_int(boxinfo.get("is_online", 1), 1):
            continue
        for pos, fi in enumerate(boxinfo.get("filament_info") or []):
            if isinstance(fi, dict):
                decoded[b * 4 + pos] = fi
    d = filament_dict(mr) if not decoded else {"colors": {}, "filaments": {}, "vendors": {}}
    mcc = status.get("multi_color_controller") or {}
    tool_map = {}
    for k, v in variables.items():
        m = re.fullmatch(r"value_t(\d+)", k)
        if m:
            tool_map[int(m.group(1))] = _as_int(v, -1)
    active = _as_int(extras.get("loaded_slot") or variables.get("last_load_slot"), -1)

    slots = []
    for i in range(box_count * 4):
        stepper = status.get(f"box_stepper slot{i}") or {}
        rb = stepper.get("runout_button")
        state = _as_int(variables.get(f"slot{i}"), -1)
        if rb is not None:
            loaded = _as_int(rb, 1) == 0
        else:
            loaded = state in (1, 2, 3)
        fil_v = variables.get(f"filament_slot{i}", 0)
        col_v = variables.get(f"color_slot{i}", 0)
        ven_v = _as_int(variables.get(f"vendor_slot{i}"), 0)

        name = ""
        info = {}
        fi = decoded.get(i)
        if fi is not None:
            # box_extras already did the decoding for us (current QIDI firmware).
            name = str(fi.get("filament_str") or fi.get("filament_type") or "")
            ftype = str(fi.get("filament_type") or "")
            rgb = fi.get("color_rgb_str")
            color = normalize_color(rgb[0] if isinstance(rgb, list) and rgb else (rgb or "FFFFFF"))
            vendor = str(fi.get("vendor_str") or "Generic")
            sensed = (_as_int(rb, 1) == 0) if rb is not None else None
            has_info = bool(name) and sensed is not False
            slots.append({
                "index": i, "box": i // 4 + 1,
                "label": f"{i // 4 + 1}-{i % 4 + 1}" if box_count > 1 else str(i % 4 + 1),
                "loaded": has_info,
                "sensed": sensed,
                "active": i == active,
                "name": name, "type": normalize_type(ftype or name), "color": color, "vendor": vendor,
                "min_temp": fi.get("min_temp"), "max_temp": fi.get("max_temp"),
                "rfid": bool(_as_int(fi.get("info_from_rfid"), 0)),
                "tools": sorted(t for t, s_ in tool_map.items() if s_ == i),
            })
            continue
        if isinstance(fil_v, str) and not fil_v.strip().lstrip("-").isdigit():
            # Newer firmware: a text id, with the decoded type in multi_color_controller.
            box = (mcc.get("box") or {}) if isinstance(mcc.get("box"), dict) else mcc
            name = str(box.get(f"box{i // 4}_filament_info_{i % 4}_filament_type") or "")
        else:
            info = d["filaments"].get(_as_int(fil_v), {})
            name = info.get("filament", "")
        if isinstance(col_v, list) and col_v:
            color = normalize_color(col_v[0])
        elif isinstance(col_v, str) and re.fullmatch(r"#?[0-9a-fA-F]{6,8}", col_v.strip().strip("'[]")):
            color = normalize_color(col_v)
        else:
            color = d["colors"].get(_as_int(col_v), "")
        vendor = d["vendors"].get(ven_v) or ("QIDI" if ven_v == 1 else "Generic")
        if loaded and not name:
            name = "PLA"
        slots.append({
            "index": i,
            "box": i // 4 + 1,
            "label": f"{i // 4 + 1}-{i % 4 + 1}" if box_count > 1 else str(i % 4 + 1),
            "loaded": loaded,
            "active": i == active,
            "name": name if loaded else "",
            "type": normalize_type(name) if loaded else "",
            "color": (color or "#FFFFFF") if loaded else "",
            "vendor": vendor if loaded else "",
            "min_temp": info.get("min_temp"),
            "max_temp": info.get("max_temp"),
            "tools": sorted(t for t, s in tool_map.items() if s == i),
        })
    climate = []
    for b, boxinfo in enumerate((extras.get("boxes") or [])[:box_count]):
        if isinstance(boxinfo, dict):
            climate.append({"box": b + 1, "humidity": boxinfo.get("aht20_humidity"),
                            "temperature": boxinfo.get("aht20_temp"), "drying": bool(_as_int(boxinfo.get("dry_state"), 0))})
    return {"present": True, "boxes": box_count, "slots": slots, "enabled": enabled, "climate": climate}


def tool_slot_script(mapping: list[int]) -> str:
    """G-code telling the Box which slot each tool (filament) uses: T0 -> mapping[0], ..."""
    lines = []
    for tool, slot in enumerate(mapping[:16]):
        if isinstance(slot, int) and 0 <= slot < 16:
            lines.append(f"SAVE_VARIABLE VARIABLE=value_t{tool} VALUE=\"'slot{slot}'\"")
    return "\n".join(lines)


def color_distance(a: str, b: str) -> float:
    try:
        ra, ga, ba = (int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        rb, gb, bb = (int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, AttributeError):
        return 1e9
    # "redmean" weighting: cheap and close to how people see colour differences.
    rm = (ra + rb) / 2
    return ((2 + rm / 256) * (ra - rb) ** 2 + 4 * (ga - gb) ** 2 + (2 + (255 - rm) / 256) * (ba - bb) ** 2) ** 0.5


def suggest_mapping(model_filaments: list[dict], slots: list[dict]) -> list[int]:
    """Pick the closest loaded slot (same material first, then colour) for each model colour."""
    loaded = [s for s in slots if s.get("loaded")]
    out = []
    for f in model_filaments:
        if not loaded:
            out.append(-1)
            continue
        ftype = normalize_type(f.get("type", "PLA"))
        same = [s for s in loaded if s["type"] == ftype] or loaded
        best = min(same, key=lambda s: color_distance(f.get("color", "#FFFFFF"), s["color"]))
        out.append(best["index"])
    return out
