"""Find slicer presets (printer / print quality / filament) and turn them into
stand-alone settings files the slicer's command line can load.

OrcaSlicer and QIDI Studio store presets as JSON files that "inherit" from
parent presets. Older slicer versions do not follow that chain when run from
the command line, so we follow it ourselves and write one complete file per
preset. That way any reasonably recent OrcaSlicer or QIDI Studio works.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from dataclasses import dataclass, field

PRESET_TYPES = ("machine", "process", "filament")

# Keys that only describe where a preset came from or which printers it fits.
# We drop them in the flattened copy so the slicer never refuses a combination
# that the person picked on purpose.
_STRIP_KEYS = (
    "inherits",
    "compatible_printers",
    "compatible_printers_condition",
    "compatible_prints",
    "compatible_prints_condition",
)


@dataclass
class Preset:
    name: str
    type: str
    path: str
    vendor_root: str
    user: bool
    data: dict = field(repr=False)

    @property
    def selectable(self) -> bool:
        if self.user:
            return True
        return str(self.data.get("instantiation", "true")).lower() != "false"

    def compatible_with(self, machine_name: str) -> bool:
        if self.type == "machine":
            return True
        printers = self.data.get("compatible_printers") or []
        if printers:
            return machine_name in printers
        cond = self.data.get("compatible_printers_condition") or ""
        if cond:
            # Rough check of the most common form: printer_notes=~/.*Q2.*/ or printer_model == "Qidi Q2"
            return "Q2" in cond or "q2" in cond
        # No restriction written down.
        return True


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def default_slicer_dirs() -> list[str]:
    """Likely install folders of OrcaSlicer / QIDI Studio on this computer."""
    out: list[str] = []
    if sys.platform.startswith("win"):
        for base in filter(None, {os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                                  os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")}):
            for pattern in ("OrcaSlicer*", "Orca Slicer*", "QIDIStudio*", "QIDI Studio*", "QIDISlicer*",
                            os.path.join("Programs", "OrcaSlicer*"), os.path.join("Programs", "QIDIStudio*")):
                out.extend(glob.glob(os.path.join(base, pattern)))
    elif sys.platform == "darwin":
        out.extend(glob.glob("/Applications/OrcaSlicer*.app/Contents"))
        out.extend(glob.glob("/Applications/QIDI*Studio*.app/Contents"))
    else:
        out.extend(glob.glob("/opt/OrcaSlicer*"))
        out.extend(glob.glob("/opt/QIDIStudio*"))
    return [d for d in out if os.path.isdir(d)]


def find_slicer_executable(folder: str) -> str | None:
    names = ("orca-slicer.exe", "OrcaSlicer.exe", "qidi-studio.exe", "QIDIStudio.exe", "QIDI Studio.exe",
             "orca-slicer", "OrcaSlicer", "qidi-studio", "QIDIStudio")
    for n in names:
        for candidate in (os.path.join(folder, n), os.path.join(folder, "MacOS", n), os.path.join(folder, "bin", n)):
            if os.path.isfile(candidate):
                return candidate
    return None


def detect_slicer() -> str | None:
    for d in default_slicer_dirs():
        exe = find_slicer_executable(d)
        if exe:
            return exe
    return None


def profile_roots_for(slicer_exe: str) -> list[tuple[str, bool]]:
    """Folders that hold presets for a given slicer: (folder, is_user_folder)."""
    roots: list[tuple[str, bool]] = []
    if not slicer_exe:
        return roots
    exe_dir = os.path.dirname(os.path.abspath(slicer_exe))
    for res in (os.path.join(exe_dir, "resources"), os.path.join(os.path.dirname(exe_dir), "Resources"),
                os.path.join(os.path.dirname(exe_dir), "resources")):
        prof = os.path.join(res, "profiles")
        if os.path.isdir(prof):
            roots.append((prof, False))
            break
    lower = os.path.basename(slicer_exe).lower()
    app_names = ["QIDIStudio", "QIDI Studio"] if "qidi" in lower else ["OrcaSlicer"]
    config_bases = []
    if sys.platform.startswith("win"):
        if os.environ.get("APPDATA"):
            config_bases.append(os.environ["APPDATA"])
    elif sys.platform == "darwin":
        config_bases.append(os.path.expanduser("~/Library/Application Support"))
    else:
        config_bases.append(os.path.expanduser("~/.config"))
    for base in config_bases:
        for app in app_names:
            app_dir = os.path.join(base, app)
            # Downloaded/updated system presets live here and win over the bundled ones.
            if os.path.isdir(os.path.join(app_dir, "system")):
                roots.insert(0, (os.path.join(app_dir, "system"), False))
            if os.path.isdir(os.path.join(app_dir, "user")):
                roots.append((os.path.join(app_dir, "user"), True))
    return roots


class PresetLibrary:
    def __init__(self, roots: list[tuple[str, bool]], vendor_filter: str | None = "qidi"):
        self.presets: list[Preset] = []
        self._by_name: dict[tuple[str, str], list[Preset]] = {}
        for root, is_user in roots:
            self._scan(root, is_user, vendor_filter)

    def _scan(self, root: str, is_user: bool, vendor_filter: str | None):
        if is_user:
            vendor_dirs = [root]
        else:
            vendor_dirs = [os.path.join(root, d) for d in sorted(os.listdir(root))
                           if os.path.isdir(os.path.join(root, d))]
            if vendor_filter:
                # Always keep the vendor we care about; other vendors are only needed for
                # inheritance, which stays inside a vendor folder.
                vendor_dirs = [d for d in vendor_dirs if vendor_filter in os.path.basename(d).lower()]
        for vdir in vendor_dirs:
            for dirpath, _dirs, files in os.walk(vdir):
                for fn in files:
                    if not fn.lower().endswith(".json"):
                        continue
                    path = os.path.join(dirpath, fn)
                    data = _read_json(path)
                    if not data:
                        continue
                    ptype = data.get("type")
                    name = data.get("name")
                    if ptype not in PRESET_TYPES or not name:
                        continue
                    p = Preset(name=name, type=ptype, path=path, vendor_root=vdir, user=is_user, data=data)
                    self.presets.append(p)
                    self._by_name.setdefault((ptype, name), []).append(p)

    def find(self, ptype: str, name: str, prefer_root: str | None = None) -> Preset | None:
        cands = self._by_name.get((ptype, name)) or []
        if not cands:
            return None
        if prefer_root:
            for c in cands:
                if c.vendor_root == prefer_root:
                    return c
        # User presets first, then the first system copy found (updated system folder is scanned first).
        for c in cands:
            if c.user:
                return c
        return cands[0]

    def names(self, ptype: str, machine: str | None = None) -> list[str]:
        seen = []
        for p in self.presets:
            if p.type != ptype or not p.selectable:
                continue
            if machine and not p.compatible_with(machine):
                continue
            if p.name not in seen:
                seen.append(p.name)
        return sorted(seen, key=str.lower)

    def resolve(self, ptype: str, name: str) -> dict:
        """Return the complete settings of a preset with its whole inheritance chain merged."""
        chain: list[Preset] = []
        p = self.find(ptype, name)
        if p is None:
            raise KeyError(f"Could not find the {ptype} preset '{name}'.")
        seen = set()
        while p is not None:
            if p.name in seen:
                break
            seen.add(p.name)
            chain.append(p)
            parent = p.data.get("inherits")
            if not parent:
                break
            nxt = self.find(ptype, parent, prefer_root=None if p.user else p.vendor_root)
            if nxt is None:
                raise KeyError(f"The {ptype} preset '{p.name}' builds on '{parent}', which could not be found.")
            p = nxt
        merged: dict = {}
        for item in reversed(chain):
            merged.update(item.data)
        for k in _STRIP_KEYS:
            merged.pop(k, None)
        merged["name"] = name
        merged["type"] = ptype
        merged["from"] = "User"
        merged["instantiation"] = "true"
        return merged

    def write_resolved(self, ptype: str, name: str, out_dir: str) -> str:
        data = self.resolve(ptype, name)
        os.makedirs(out_dir, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in " .-_@" else "_" for ch in name)
        path = os.path.join(out_dir, f"{ptype}-{safe}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        return path
