#!/usr/bin/env python3
"""Regenerate K2-OpenHost's shipped Creality/Generic CFS catalog.

Source data is DnG-Crafts/K2-RFID db/k2.json. The generated catalog is
color-neutral: spool colors remain slot/RFID metadata instead of creating a
separate material profile for every color.

With --orca-dir, each profile also gets max_flow (mm3/s) from OrcaSlicer's
preset of the same name for the K2 Pro ("<name> @K2 Pro-all", then the K2 and
K2 Plus variants), else the generic value of its material (DEFAULT_MAX_FLOW
in box.py). --fill-max-flow only adds it to the existing output file.
"""

import argparse
import importlib.util
import json
import pathlib
import sys
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import DEFAULT_MAX_FLOW  # noqa: E402
from extras.box_k2rfid_catalog import K2RfidMaterialCatalog  # noqa: E402

DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/DnG-Crafts/K2-RFID/main/db/k2.json"
)
DEFAULT_OUTPUT = ROOT / "config" / "k2" / "cfs_system_filaments.json"
ORCA_SUFFIXES = (" @K2 Pro-all", " @K2-all", " @K2 Plus-all")


def _orca_module():
    spec = importlib.util.spec_from_file_location(
        "k2_orca_filament_import",
        ROOT / "scripts" / "k2_orca_filament_import.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fill_max_flow(materials, orca_dir):
    """Set max_flow on each profile; returns (from Orca, from material)."""
    orca = _orca_module()
    presets = orca.load_presets(orca_dir)
    if not presets:
        raise SystemExit("No OrcaSlicer filament presets in %s" % orca_dir)
    counts = [0, 0]
    for item in materials:
        value = None
        for suffix in ORCA_SUFFIXES:
            name = item.get("name", "") + suffix
            if name in presets:
                value = orca.resolve(
                    presets, name, "filament_max_volumetric_speed")
                if value is not None:
                    break
        try:
            value = round(float(value), 2)
            counts[0] += 1
        except (TypeError, ValueError):
            value = DEFAULT_MAX_FLOW.get(str(item.get("material", "")).upper())
            counts[1] += value is not None
        item["max_flow"] = value
    return counts


def material_source(value):
    path = pathlib.Path(value).expanduser()
    if path.exists():
        return path.read_bytes()
    with urllib.request.urlopen(value, timeout=30) as response:
        return response.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--orca-dir", default=None,
        help="OrcaSlicer configuration folder, for max_flow")
    parser.add_argument(
        "--fill-max-flow", action="store_true",
        help="only add max_flow to the existing output (needs --orca-dir)")
    args = parser.parse_args()

    if args.fill_max_flow:
        if not args.orca_dir:
            raise SystemExit("--fill-max-flow needs --orca-dir")
        output = pathlib.Path(args.output).expanduser()
        result = json.loads(output.read_text(encoding="utf-8"))
        orca_count, material_count = fill_max_flow(
            result["materials"], args.orca_dir)
        output.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print("max_flow: %d from OrcaSlicer K2 presets, %d from the material, "
              "%d profiles" % (orca_count, material_count,
                               len(result["materials"])))
        return

    payload = material_source(args.source)
    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        tmp.write(payload)
        tmp.flush()
        catalog = K2RfidMaterialCatalog(tmp.name)
        materials = []
        for entry in catalog.entries:
            if entry.get("brand") not in ("Creality", "Generic"):
                continue
            clean = {
                key: value for key, value in entry.items()
                if not key.startswith("_")
            }
            clean["system"] = True
            materials.append(clean)

    if args.orca_dir:
        fill_max_flow(materials, args.orca_dir)
    materials.sort(key=lambda item: (
        0 if item.get("brand") == "Creality" else 1,
        item.get("material", ""),
        item.get("name", ""),
        item.get("id", ""),
    ))
    result = {
        "schema_version": 1,
        "source": "DnG-Crafts/K2-RFID db/k2.json",
        "source_url": (
            "https://github.com/DnG-Crafts/K2-RFID/blob/main/db/k2.json"
        ),
        "description": (
            "Complete Creality and Generic K2 filament profile catalog imported "
            "from DnG-Crafts/K2-RFID for K2-OpenHost. Color is spool metadata "
            "and is intentionally not part of profile identity."
        ),
        "materials": materials,
    }
    output = pathlib.Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        "wrote %d profiles (%d Creality, %d Generic) to %s"
        % (
            len(materials),
            sum(item["brand"] == "Creality" for item in materials),
            sum(item["brand"] == "Generic" for item in materials),
            output,
        )
    )


if __name__ == "__main__":
    main()