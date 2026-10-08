import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import (
    Box, BoxStore, FALLBACK_SPOOL_LENGTH_M, default_spool_length_m)


@pytest.mark.parametrize("material, length", [
    ("PLA", 335.0),
    ("pla", 335.0),
    ("PLA-SILK", 335.0),
    ("PLA-CF", 320.0),
    ("PETG", 327.0),
    ("PETG-CF", 320.0),
    ("PETG-GF", 327.0),
    ("ABS", 400.0),
    ("ABS-GF", 400.0),
    ("ASA", 390.0),
    ("ASA-CF", 390.0),
    ("PC", 345.0),
    ("TPU", 345.0),
    ("PA", 365.0),
    ("PA6", 365.0),
    ("PA12", 365.0),
    ("PA-CF", 355.0),
    ("PA6-CF", 355.0),
    ("PA612-CF", 355.0),
    ("PAHT-CF", 355.0),
    ("PA-GF", 365.0),
    ("PP", 460.0),
    ("PPS", FALLBACK_SPOOL_LENGTH_M),
    ("PET-CF", FALLBACK_SPOOL_LENGTH_M),
    ("BVOH", FALLBACK_SPOOL_LENGTH_M),
    ("", FALLBACK_SPOOL_LENGTH_M),
    (None, FALLBACK_SPOOL_LENGTH_M),
])
def test_reference_length_by_material(material, length):
    assert default_spool_length_m(material) == length


def make_box(tmp_path):
    box = Box.__new__(Box)
    box.store = BoxStore(str(tmp_path / "filament_box.json"))
    box.rfid_spools = {}
    box.rfid_percent = {}
    box.rfid_estimate_dirty = False
    box.persisted = 0

    def persist(force=False):
        box.persisted += 1

    box._persist_rfid_estimates = persist
    return box


def test_length_priority_tag_profile_material(tmp_path):
    box = make_box(tmp_path)

    assert box._third_party_length_m(
        {"filament_length_m": 250}, {"nominal_length_m": 400,
                                     "material": "PLA"}) == (250.0, "tag")
    assert box._third_party_length_m(
        {}, {"nominal_length_m": 400, "material": "PLA"}) == (
            400.0, "profile")
    assert box._third_party_length_m(
        {}, {"material": "ABS"}) == (400.0, "material")
    # No profile material: the decoded tag material.
    assert box._third_party_length_m(
        {"material": "PETG"}, {}) == (327.0, "material")


def test_defaults_are_published_for_known_materials(tmp_path):
    box = make_box(tmp_path)
    box.store.set_material("PAHT-CF", 280)

    defaults = box.spool_length_defaults()

    assert defaults["PLA"] == 335.0
    assert defaults["ABS"] == 400.0
    # A material Box knows gets its family length.
    assert defaults["PAHT-CF"] == 355.0
    # Unknown to Box (no profile, no material entry): the fallback key.
    assert "PEBA" not in defaults
    assert defaults["*"] == FALLBACK_SPOOL_LENGTH_M


def make_refresh_box(tmp_path, filament):
    box = make_box(tmp_path)
    box.store.filament = lambda key: (
        dict(filament) if key == filament["id"] else None)
    box.slot_profiles = {}
    box.profile = lambda slot: box.slot_profiles.get(slot, {})
    return box


def test_profile_length_edit_updates_its_loaded_spools(tmp_path):
    box = make_refresh_box(tmp_path, {
        "id": "BAMBU-BAMBULAB-ABS", "material": "ABS",
        "nominal_length_m": 350.0})
    box.slot_profiles = {
        0: {"filament_id": "BAMBU-BAMBULAB-ABS"},
        1: {"filament_id": "BAMBU-BAMBULAB-ABS"},
        2: {"filament_id": "OTHER"},
    }
    box.rfid_spools = {
        0: {"total_mm": 400000.0, "remaining_mm": 100000.0,
            "length_source": "material"},
        1: {"total_mm": 250000.0, "remaining_mm": 50000.0,
            "length_source": "tag"},
        2: {"total_mm": 400000.0, "remaining_mm": 100000.0,
            "length_source": "material"},
    }

    assert box._refresh_spool_lengths("bambu-bambulab-abs") == 1

    spool = box.rfid_spools[0]
    assert spool["total_mm"] == 350000.0
    assert spool["remaining_mm"] == pytest.approx(87500.0)
    assert box.rfid_percent[0] == pytest.approx(25.0)
    assert spool["length_source"] == "profile"
    # A tag length and another profile's spool are left alone.
    assert box.rfid_spools[1]["total_mm"] == 250000.0
    assert box.rfid_spools[2]["total_mm"] == 400000.0
    assert box.persisted == 1


def test_emptied_profile_length_goes_back_to_the_material(tmp_path):
    box = make_refresh_box(tmp_path, {
        "id": "90002", "material": "PETG", "nominal_length_m": None})
    box.slot_profiles = {3: {"filament_id": "90002"}}
    box.rfid_spools = {3: {"total_mm": 300000.0, "remaining_mm": 150000.0,
                           "length_source": "profile"}}

    box._refresh_spool_lengths("90002")

    assert box.rfid_spools[3]["total_mm"] == 327000.0
    assert box.rfid_percent[3] == pytest.approx(50.0)
    assert box.rfid_spools[3]["length_source"] == "material"


def test_unchanged_length_saves_nothing(tmp_path):
    box = make_refresh_box(tmp_path, {
        "id": "90002", "material": "PETG", "nominal_length_m": None})
    box.slot_profiles = {3: {"filament_id": "90002"}}
    box.rfid_spools = {3: {"total_mm": 327000.0, "remaining_mm": 100.0,
                           "length_source": "material"}}

    assert box._refresh_spool_lengths("90002") == 0
    assert box.persisted == 0


def make_estimate_box(tmp_path, cfs_percent):
    box = make_box(tmp_path)
    box.rfid_spools = {2: {"key": "tag:BAMBU:PC:8BD9CFFC",
                           "total_mm": 345000.0, "remaining_mm": None}}

    def read_remaining(slot):
        if cfs_percent is not None:
            box._apply_reported_remaining(slot, cfs_percent)

    box.rfid_reported_percent = {}
    box._read_rfid_remaining = read_remaining
    return box


def test_new_third_party_spool_takes_the_cfs_percentage(tmp_path):
    box = make_estimate_box(tmp_path, cfs_percent=67)

    box._start_third_party_estimate(2, "material")

    spool = box.rfid_spools[2]
    assert spool["remaining_mm"] == pytest.approx(231150.0)
    assert box.rfid_percent[2] == pytest.approx(67.0)
    assert spool["length_source"] == "material"


def test_new_third_party_spool_without_cfs_value_starts_full(tmp_path):
    box = make_estimate_box(tmp_path, cfs_percent=None)

    box._start_third_party_estimate(2, "profile")

    assert box.rfid_spools[2]["remaining_mm"] == 345000.0
    assert box.rfid_percent[2] == 100.0
    assert box.persisted == 1


def test_saved_estimate_is_kept_and_capped_by_the_cfs(tmp_path):
    box = make_estimate_box(tmp_path, cfs_percent=50)
    box.rfid_spools[2]["remaining_mm"] = 100000.0

    box._start_third_party_estimate(2, "material")

    # min(saved 100 m, CFS 50 % of 345 m): the saved one is lower.
    assert box.rfid_spools[2]["remaining_mm"] == 100000.0


def test_length_source_is_saved_with_the_estimate(tmp_path):
    box = make_box(tmp_path)
    box.rfid_spools = {0: {"key": "tag:BAMBU:PETG HF:763EA0C6",
                           "total_mm": 327000.0, "remaining_mm": 36300.0,
                           "length_source": "material"}}
    box.rfid_estimate_dirty = True

    Box._persist_rfid_estimates(box, force=True)

    saved = box.store.setting("rfid_estimates")["tag:BAMBU:PETG HF:763EA0C6"]
    assert saved == {"total_mm": 327000.0, "remaining_mm": 36300.0,
                     "length_source": "material"}
