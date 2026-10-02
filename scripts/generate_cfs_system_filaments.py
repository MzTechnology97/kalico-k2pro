#!/usr/bin/env python3
"""Regenerate K2-OpenHost's shipped Creality/Generic CFS catalog.

Source data is DnG-Crafts/K2-RFID db/k2.json. The generated catalog is
color-neutral: spool colors remain slot/RFID metadata instead of creating a
separate material profile for every color.
"""

import argparse
import json
import pathlib
import sys
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box_k2rfid_catalog import K2RfidMaterialCatalog  # noqa: E402

DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/DnG-Crafts/K2-RFID/main/db/k2.json"
)
DEFAULT_OUTPUT = ROOT / "config" / "k2" / "cfs_system_filaments.json"


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
    args = parser.parse_args()

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