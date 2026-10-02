import json
import pathlib

from klippy.extras.box_k2rfid_catalog import K2RfidMaterialCatalog


def write_db(path, rows):
    path.write_text(json.dumps({
        "code": 0,
        "result": {"list": rows},
    }), encoding="utf-8")


def full_row(material_id, name, color, pressure_advance="0.04"):
    return {
        "base": {
            "id": material_id,
            "brand": "Example",
            "name": name,
            "meterialType": "PLA",
            "colors": [color],
            "density": 1.24,
            "diameter": "1.75",
        },
        "kvParam": {
            "default_filament_colour": color,
            "filament_notes": name,
            "filament_flow_ratio": "0.98",
            "filament_max_volumetric_speed": "20",
            "nozzle_temperature": "220",
            "pressure_advance": pressure_advance,
        },
    }
def test_full_database_color_variants_share_one_profile(tmp_path):
    database = tmp_path / "material_database.json"
    write_db(database, [
        full_row("12345", "Example PLA Black", "#000000"),
        full_row("67890", "Example PLA Red", "#FF0000"),
    ])

    catalog = K2RfidMaterialCatalog(str(database))

    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry["id"] == "12345"
    assert "67890" in entry["aliases"]
    assert catalog.lookup("112345")["id"] == "12345"
    assert catalog.lookup("167890")["id"] == "12345"


def test_different_tuning_stays_a_distinct_profile(tmp_path):
    database = tmp_path / "material_database.json"
    write_db(database, [
        full_row("12345", "Example PLA", "#000000", "0.04"),
        full_row("67890", "Example PLA Fast", "#FF0000", "0.06"),
    ])

    catalog = K2RfidMaterialCatalog(str(database))

    assert len(catalog.entries) == 2
    assert catalog.lookup("112345")["id"] == "12345"
    assert catalog.lookup("167890")["id"] == "67890"
def test_compact_database_keeps_explicit_aliases_and_tag_codes(tmp_path):
    database = tmp_path / "custom.json"
    database.write_text(json.dumps({
        "materials": [{
            "id": "42175",
            "brand": "Bambulab",
            "name": "Bambulab ABS",
            "material": "ABS",
            "target_temp": 260,
            "aliases": ["24934"],
            "rfid_codes": ["42175", "24934"],
        }]
    }), encoding="utf-8")

    catalog = K2RfidMaterialCatalog(str(database))

    assert catalog.lookup("142175")["id"] == "42175"
    assert catalog.lookup("124934")["id"] == "42175"


def test_shipped_system_catalog_contains_creality_and_generic_profiles():
    root = pathlib.Path(__file__).resolve().parents[1]
    catalog = K2RfidMaterialCatalog(
        str(root / "config" / "k2" / "cfs_system_filaments.json")
    )

    entries = catalog.entries
    assert len(entries) == 61
    assert sum(1 for item in entries if item["brand"] == "Creality") == 30
    assert sum(1 for item in entries if item["brand"] == "Generic") == 31
    tpu = catalog.lookup("100005")
    assert tpu["name"] == "Generic TPU"
    assert tpu["min_temp"] == 210
    assert tpu["max_temp"] == 240
    assert tpu["pressure_advance"] is not None
    assert all(item["pressure_advance"] is not None for item in entries)
    assert tpu["system"] is True