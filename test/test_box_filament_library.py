"""CFS filament library file: custom profiles kept apart from runtime state."""
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import Box, BoxError, BoxStore  # noqa: E402


class FakeCatalog:
    def __init__(self, entries):
        self._entries = entries

    @property
    def entries(self):
        return [dict(item) for item in self._entries]


class FakeChangeEngine:
    default_temp = 220


class FakeGcmd:
    def __init__(self, params):
        self.params = dict(params)

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name, default)
        return None if value is None else int(value)

    def get_float(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name, default)
        return None if value is None else float(value)

    def get_command_parameters(self):
        return dict(self.params)

    def error(self, message):
        return BoxError(message)


SYSTEM = [
    {"id": "01001", "material": "PLA", "brand": "Creality", "name": "Hyper PLA",
     "target_temp": 220, "min_temp": 190, "max_temp": 240, "system": True},
]
CUSTOM = {"material": "PETG", "brand": "SUNLU", "name": "Sunlu PETG",
          "target_temp": 245, "pressure_advance": 0.04, "spoolman_id": 7}


def paths(tmp_path):
    return str(tmp_path / "filament_box.json"), str(tmp_path / "config" / "cfs_filaments.json")


def read(path):
    with open(path) as stream:
        return json.load(stream)


def make_box(tmp_path, imported=()):
    state, library = paths(tmp_path)
    box = Box.__new__(Box)
    box.store = BoxStore(state, library)
    box.change_engine = FakeChangeEngine()
    box.system_material_catalog = FakeCatalog(SYSTEM)
    box.material_catalog = FakeCatalog(list(imported))
    box.material_database_path = str(tmp_path / "config" / "k2_rfid_custom_materials.json")
    box.unknown_rfid = {}
    box._info = lambda *args: None
    box._seed_material_catalog()
    return box


def test_custom_profiles_are_saved_in_the_library_file_only(tmp_path):
    state, library = paths(tmp_path)
    store = BoxStore(state, library)
    store.set_system(SYSTEM)
    store.set_filament("90001", CUSTOM)
    store.set_setting("runout_swap_enabled", True)

    lib = read(library)
    assert [item["id"] for item in lib["materials"]] == ["90001"]
    assert lib["materials"][0]["spoolman_id"] == 7
    assert lib["materials"][0]["source"] == "user"
    assert "system" not in lib["materials"][0]
    assert "filaments" not in read(state)
    merged = BoxStore(state, library)
    merged.set_system(SYSTEM)
    assert set(merged.filaments) == {"01001", "90001"}
    assert merged.filament("01001")["source"] == "system"


def test_legacy_state_profiles_migrate_once_with_a_backup(tmp_path):
    state, library = paths(tmp_path)
    with open(state, "w") as stream:
        json.dump({"filaments": {
            "90001": dict(CUSTOM, id="90001"),
            "01001": dict(SYSTEM[0], target_temp=210),
        }, "slots": {"1": {"material": "PETG", "filament_id": "90001",
                           "source": "library"}}}, stream)

    store = BoxStore(state, library)

    assert [item["id"] for item in read(library)["materials"]] == ["90001"]
    assert "filaments" not in read(state)
    assert read(state)["slots"]["1"]["filament_id"] == "90001"
    assert "filaments" in read(state + ".pre-library")
    assert store.filament("90001")["brand"] == "SUNLU"


def test_system_catalog_updates_apply_after_restart(tmp_path):
    state, library = paths(tmp_path)
    store = BoxStore(state, library)
    store.set_system(SYSTEM)
    restarted = BoxStore(state, library)
    restarted.set_system([dict(SYSTEM[0], target_temp=225)])
    assert restarted.filament("01001")["target_temp"] == 225


def test_deleted_import_stays_deleted_until_the_import_changes(tmp_path):
    imported = [{"id": "83161", "material": "PLA", "brand": "SUNLU",
                 "name": "Sunlu PLA Plus HS 2.0", "target_temp": 220}]
    box = make_box(tmp_path, imported)
    assert box.store.filament("83161")["source"] == "import"

    assert box.store.delete_filament("83161") is True
    box._seed_material_catalog()
    assert box.store.filament("83161") is None

    box.material_catalog = FakeCatalog(imported + [
        {"id": "06691", "material": "PA6-CF", "brand": "SUNLU",
         "name": "Sunlu PA6-CF", "target_temp": 300}])
    assert box._seed_material_catalog() == 2
    assert box.store.filament("06691") is not None


def test_import_keeps_user_temperatures(tmp_path):
    imported = [{"id": "83161", "material": "PLA", "brand": "SUNLU",
                 "name": "Sunlu PLA Plus HS 2.0", "target_temp": 220}]
    box = make_box(tmp_path, imported)
    box.store.set_filament("83161", dict(box.store.filament("83161"), target_temp=230))
    box.material_catalog = FakeCatalog([dict(imported[0], min_temp=200)])
    box._seed_material_catalog()
    assert box.store.filament("83161")["target_temp"] == 230


def test_library_file_replaced_on_disk_is_reloaded_and_refreshes_slots(tmp_path):
    state, library = paths(tmp_path)
    store = BoxStore(state, library)
    store.set_filament("90001", CUSTOM)
    store.set_profile(1, {"material": "PETG", "color": "#112233", "brand": "SUNLU",
                          "name": "Sunlu PETG", "target_temp": 245,
                          "filament_id": "90001", "source": "library"})
    store.set_profile(2, {"material": "PETG", "filament_id": "90002",
                          "source": "library"})

    # A companion app replaces the file through the Moonraker file API.
    lib = read(library)
    lib["materials"][0]["target_temp"] = 250
    with open(library, "w") as stream:
        json.dump(lib, stream)
    os.utime(library, (1, 1))

    assert store.refresh_library() is True
    assert store.filament("90001")["target_temp"] == 250
    assert store.profile(1)["target_temp"] == 250
    assert store.profile(1)["color"] == "#112233"
    assert BoxStore(state, library).profile(1)["target_temp"] == 250


def test_profile_removed_from_the_file_detaches_its_slots(tmp_path):
    state, library = paths(tmp_path)
    store = BoxStore(state, library)
    store.set_filament("90001", CUSTOM)
    store.set_profile(1, {"material": "PETG", "filament_id": "90001", "source": "library"})
    with open(library, "w") as stream:
        json.dump({"materials": []}, stream)
    os.utime(library, (1, 1))
    assert store.refresh_library() is True
    assert store.profile(1)["filament_id"] == ""
    assert store.profile(1)["source"] == "manual"
    assert store.profile(1)["material"] == "PETG"


def test_damaged_library_file_is_never_overwritten(tmp_path):
    state, library = paths(tmp_path)
    os.makedirs(os.path.dirname(library))
    with open(library, "w") as stream:
        stream.write("{not json")

    store = BoxStore(state, library)

    assert "not valid JSON" in store.library_status["error"]
    with pytest.raises(BoxError):
        store.set_filament("90001", CUSTOM)
    with open(library) as stream:
        assert stream.read() == "{not json"

    with open(library, "w") as stream:
        json.dump({"materials": [dict(CUSTOM, id="90001")]}, stream)
    os.utime(library, (1, 1))
    assert store.refresh_library() is True
    assert store.library_status["error"] == ""
    assert store.filament("90001")["name"] == "Sunlu PETG"


def test_library_accepts_a_plain_list_and_rfid_lookup_prefers_custom(tmp_path):
    state, library = paths(tmp_path)
    os.makedirs(os.path.dirname(library))
    with open(library, "w") as stream:
        json.dump([{"id": "01001", "material": "PLA"},
                   {"id": "90001", "material": "PETG", "rfid_codes": ["01001"]}], stream)
    store = BoxStore(state, library)
    store.set_system(SYSTEM)
    # A custom profile cannot shadow a system ID, but it can claim its tag code.
    assert store.filament("01001")["system"] is True
    assert store.filament_for_rfid("101001")["id"] == "90001"


def test_filament_set_without_target_keeps_the_saved_temperature(tmp_path):
    box = make_box(tmp_path)
    box._param = lambda gcmd, name: gcmd.params.get(name)
    box._normal_color = lambda value: value
    box._apply_new_filament = lambda filament: None
    box.store.set_filament("90001", CUSTOM)

    box.cmd_filament_set(FakeGcmd({"ID": "90001", "MATERIAL": "PETG", "BRAND": "Other"}))

    saved = box.store.filament("90001")
    assert saved["brand"] == "Other"
    assert saved["target_temp"] == 245
    assert saved["pressure_advance"] == 0.04
    assert saved["spoolman_id"] == 7


def test_library_status_reports_the_file(tmp_path):
    box = make_box(tmp_path)
    box.store.set_filament("90001", CUSTOM)
    status = box.store.library_status
    assert status["path"].endswith("cfs_filaments.json")
    assert status["separate_file"] is True
    assert status["custom_count"] == 1 and status["system_count"] == 1
