"""Settings, saved as JSON next to the program (in the `data` folder)."""

from __future__ import annotations

import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("QIDI_BRIDGE_DATA") or os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    # The QIDI printer on your network
    "printer_host": "",
    "printer_port": 7125,
    "printer_api_key": "",
    # Slicer used to prepare models for the printer (OrcaSlicer or QIDI Studio)
    "slicer_path": "",
    "machine_preset": "Qidi Q2 0.4 nozzle",
    "process_preset": "0.20mm Standard @Qidi Q2",
    "filament_preset": "QIDI PLA Rapido @Qidi Q2 0.4 nozzle",
    # Use the QIDI Box when the printer has one: read the slot colours, pick the
    # matching filament settings and print multi-colour models in colour.
    "use_box": True,
    # The bed plate on the printer (decides the bed temperature) and the extra
    # first-layer care that helps prints stick: slower first layer, fan off for
    # the first 3 layers, slightly warmer first-layer bed for PLA/PETG.
    "bed_type": "Textured PEI Plate",
    "extra_grip": True,
    # True once the first-run setup on the web page has been completed.
    "setup_done": False,
    # Web page
    "web_port": 8765,
    "pin": "",
    "start_print": True,
    # Fake Bambu printer for Bambu Studio / OrcaSlicer (step 2)
    "virtual_printer_enabled": False,
    "virtual_printer_name": "QIDI Q2 (bridge)",
    "virtual_printer_access_code": "",
    "virtual_printer_serial": "",
    "virtual_printer_bind_ip": "",
}

_lock = threading.Lock()


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(updates: dict) -> dict:
    with _lock:
        cfg = load()
        for k, v in updates.items():
            if k not in DEFAULTS:
                continue
            default = DEFAULTS[k]
            if isinstance(default, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            else:
                v = "" if v is None else str(v).strip()
            cfg[k] = v
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return cfg
