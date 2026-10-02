import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import (  # noqa: E402
    API_VERSION,
    FILAMENT_INVENTORY_VERSION,
    Box,
    BoxStore,
)


def filament(name="Generic PETG-CF", color="#202020", temp=250):
    return {
        "material": "PETG-CF",
        "color": color,
        "brand": "Generic",
        "name": name,
        "target_temp": temp,
        "pressure_advance": 0.045,
        "rfid_code": "00014",
        "spoolman_id": None,
    }


def test_box_api_stays_orca_v1():
    assert API_VERSION == 1
    assert FILAMENT_INVENTORY_VERSION == 2

def test_k2_rfid_prefixed_id_resolves_custom_filament(tmp_path):
    store = BoxStore(str(tmp_path / "filament_box.json"))
    saved = store.set_filament("00014", filament())

    assert saved["id"] == "00014"
    assert store.filament_for_rfid("00014")["id"] == "00014"
    assert store.filament_for_rfid("100014")["id"] == "00014"


def test_library_assignment_is_refreshed_when_profile_changes(tmp_path):
    store = BoxStore(str(tmp_path / "filament_box.json"))
    store.set_filament("00014", filament())
    store.set_profile(1, {
        "material": "PETG-CF",
        "color": "#202020",
        "brand": "Generic",
        "name": "Generic PETG-CF",
        "target_temp": 250,
        "spoolman_id": None,
        "filament_id": "00014",
        "source": "library",
        "rfid_code": "00014",
    })

    store.set_filament(
        "00014",
        filament(name="My Orca PETG-CF", color="#303030", temp=255),
    )
    profile = store.profile(1)

    assert profile["name"] == "My Orca PETG-CF"
    assert profile["color"] == "#303030"
    assert profile["target_temp"] == 255
    assert profile["pressure_advance"] == 0.045


def test_library_refresh_preserves_slot_color_override(tmp_path):
    store = BoxStore(str(tmp_path / "filament_box.json"))
    store.set_filament("00014", filament())
    store.set_profile(2, {
        "material": "PETG-CF",
        "color": "#FF0000",
        "brand": "Generic",
        "name": "Generic PETG-CF",
        "target_temp": 250,
        "spoolman_id": None,
        "filament_id": "00014",
        "source": "library",
        "rfid_code": "00014",
    })
    store.set_filament(
        "00014",
        filament(name="My Orca PETG-CF", color="#303030", temp=255),
    )
    profile = store.profile(2)

    assert profile["color"] == "#FF0000"
    assert profile["name"] == "My Orca PETG-CF"
    assert profile["target_temp"] == 255
    assert profile["pressure_advance"] == 0.045

def test_system_filament_is_persistent_and_not_deletable(tmp_path):
    store = BoxStore(str(tmp_path / "filament_box.json"))
    value = filament(name="Generic TPU", temp=225)
    value.update({
        "material": "TPU",
        "min_temp": 210,
        "max_temp": 240,
        "system": True,
    })
    saved = store.set_filament("00005", value)

    assert saved["system"] is True
    assert saved["min_temp"] == 210
    assert saved["max_temp"] == 240
    assert store.delete_filament("00005") is False
    assert store.filament("00005") is not None


def rfid_fields(color="#6C4E43", serial="000001"):
    return {
        "supplier": "0276",
        "mat_id": "105628",
        "number": serial,
        "color": color,
        "len": "0330",
        "reserve": "000000",
    }


def test_k2rfid_generic_serial_uses_portable_fingerprint():
    box = Box.__new__(Box)
    box.rfid_spools = {}
    fields = rfid_fields()

    key_t1 = box._rfid_spool_key(fields, 1)
    key_t3 = box._rfid_spool_key(fields, 3)

    assert key_t1 == key_t3
    assert key_t1.startswith("fingerprint:")


def test_identical_active_k2rfid_tags_are_split_by_slot():
    box = Box.__new__(Box)
    fields = rfid_fields()
    fingerprint = box._rfid_spool_fingerprint(fields)
    box.rfid_spools = {
        1: {
            "key": fingerprint,
            "fingerprint": fingerprint,
            "total_mm": 330000.0,
            "remaining_mm": 100000.0,
        }
    }

    key = box._rfid_spool_key(fields, 3)

    assert key == "%s:slot:3" % fingerprint


def test_unique_k2rfid_serial_is_stable_across_slots():
    box = Box.__new__(Box)
    box.rfid_spools = {}
    fields = rfid_fields(serial="483921")

    assert box._rfid_spool_key(fields, 0) == box._rfid_spool_key(fields, 3)
    assert box._rfid_spool_key(fields, 0).endswith(":483921")