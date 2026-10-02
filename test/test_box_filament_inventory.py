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

def test_store_persists_schema_and_manual_slot_assignment(tmp_path):
    path = tmp_path / "filament_box.json"
    store = BoxStore(str(path))
    store.set_filament("00014", filament())
    store.set_profile(2, {
        "material": "PETG-CF",
        "color": "#6C4E43",
        "brand": "Generic",
        "name": "Generic PETG-CF",
        "target_temp": 250,
        "pressure_advance": 0.045,
        "spoolman_id": None,
        "filament_id": "00014",
        "source": "library",
        "rfid_code": "",
    })

    reloaded = BoxStore(str(path))
    assert reloaded.data["schema_version"] == FILAMENT_INVENTORY_VERSION
    assert reloaded.profile(2)["filament_id"] == "00014"
    assert reloaded.profile(2)["color"] == "#6C4E43"

    reloaded.clear_profile(2)
    assert BoxStore(str(path)).profile(2)["material"] == ""


def test_mark_slot_depleted_zeroes_estimate_and_clears_assignment(tmp_path):
    store = BoxStore(str(tmp_path / "filament_box.json"))
    store.set_profile(1, {
        "material": "PLA",
        "color": "#B1BEC6",
        "brand": "Bambulab",
        "name": "Bambulab PLA Basic",
        "target_temp": 215,
        "pressure_advance": None,
        "spoolman_id": None,
        "filament_id": "05628",
        "source": "rfid",
        "rfid_code": "105628",
    })
    store.set_setting("rfid_slot_keys", {"1": "tag:example"})
    store.set_setting("rfid_estimates", {
        "tag:example": {"total_mm": 330000.0, "remaining_mm": 33000.0}
    })

    box = Box.__new__(Box)
    box.store = store
    box.drivers = {1: object()}
    box.rfid_spools = {
        1: {
            "key": "tag:example",
            "fingerprint": "tag:example",
            "total_mm": 330000.0,
            "remaining_mm": 33000.0,
        }
    }
    box.rfid_percent = {1: 10.0}
    box.rfid_reported_percent = {1: 12}
    box.rfid_live_slots = {1}
    box.unknown_rfid = {}
    box.rfid_estimate_dirty = False

    box.mark_slot_depleted(1)

    reloaded = BoxStore(store.path)
    assert reloaded.profile(1)["material"] == ""
    assert reloaded.setting("rfid_estimates")["tag:example"]["remaining_mm"] == 0.0
    assert "1" not in reloaded.setting("rfid_slot_keys", {})
    assert box.rfid_percent[1] == 0.0
    assert 1 not in box.rfid_live_slots