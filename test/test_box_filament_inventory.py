import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import (  # noqa: E402
    API_VERSION,
    FILAMENT_INVENTORY_VERSION,
    BoxStore,
)


def filament(name="Generic PETG-CF", color="#202020", temp=250):
    return {
        "material": "PETG-CF",
        "color": color,
        "brand": "Generic",
        "name": name,
        "target_temp": temp,
        "rfid_code": "00014",
        "spoolman_id": None,
    }


def test_box_api_stays_orca_v1():
    assert API_VERSION == 1
    assert FILAMENT_INVENTORY_VERSION == 1

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