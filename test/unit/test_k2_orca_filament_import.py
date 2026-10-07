"""OrcaSlicer preset import: exact names only (scripts/k2_orca_filament_import.py)."""

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "k2_orca_filament_import", ROOT / "scripts" / "k2_orca_filament_import.py"
)
orca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orca)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def orca_dir(tmp_path):
    system = tmp_path / "system" / "Creality" / "filament"
    write(
        system / "base.json",
        {
            "name": "fdm_filament_pla",
            "filament_type": ["PLA"],
            "filament_max_volumetric_speed": ["12"],
        },
    )
    write(
        system / "pla.json",
        {"name": "Bambu PLA Basic @base", "inherits": "fdm_filament_pla"},
    )
    user = tmp_path / "user" / "1" / "filament"
    write(
        user / "k2.json",
        {
            "name": "Bambu PLA Basic @K2",
            "inherits": "Bambu PLA Basic @base",
            "filament_max_volumetric_speed": ["21"],
            "compatible_printers": ["Creality K2 Pro 0.4 nozzle"],
        },
    )
    write(
        user / "k1.json",
        {
            "name": "Bambu PLA Basic @K1",
            "inherits": "Bambu PLA Basic @base",
            "filament_max_volumetric_speed": ["30"],
            "compatible_printers": ["Creality K1 0.4 nozzle"],
        },
    )
    write(
        user / "generic.json", {"name": "PLA", "inherits": "fdm_filament_pla"}
    )
    return orca.load_presets(str(tmp_path))


def test_same_product_for_this_printer_wins(tmp_path):
    presets = orca_dir(tmp_path)
    profile = {
        "brand": "Bambulab",
        "name": "Bambulab PLA Basic",
        "material": "PLA",
    }
    name = orca.match(profile, presets, "K2")
    assert name == "Bambu PLA Basic @K2"
    assert orca.resolve(presets, name, "filament_max_volumetric_speed") == "21"
    # inherited when the preset has no value of its own
    assert (
        orca.resolve(
            presets, "Bambu PLA Basic @base", "filament_max_volumetric_speed"
        )
        == "12"
    )


def test_no_near_matches_or_other_materials(tmp_path):
    presets = orca_dir(tmp_path)
    plus = {"brand": "Sunlu", "name": "Sunlu PLA Plus HS", "material": "PLA"}
    assert orca.match(plus, presets, "K2") is None  # not the generic "PLA"
    petg = {
        "brand": "Bambulab",
        "name": "Bambulab PLA Basic",
        "material": "PETG",
    }
    assert orca.match(petg, presets, "K2") is None


def test_normalize_drops_the_printer_and_aliases_vendors():
    assert orca.normalize("Bambu Lab PLA Basic @K2 Pro") == "bambu pla basic"
    assert orca.normalize("Bambulab PETG-CF") == "bambu petg cf"
