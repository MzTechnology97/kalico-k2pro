import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import Box, BoxStore
from extras.box_lane_data import lanes_from_slots
from extras.box_orca import OrcaPresetTable, clean_orca_id

SHIPPED = ROOT / "config" / "k2" / "orca_k2pro_filament_ids.json"
CATALOG = ROOT / "config" / "k2" / "cfs_system_filaments.json"


def table():
    return OrcaPresetTable(str(SHIPPED))


def system_entries():
    with open(CATALOG, encoding="utf-8") as stream:
        return json.load(stream)["materials"]


def make_store(tmp_path, library=True):
    store = BoxStore(
        str(tmp_path / "filament_box.json"),
        str(tmp_path / "cfs_filaments.json") if library else None)
    store.set_system(system_entries())
    store.set_orca_table(table())
    return store


def test_shipped_table_covers_the_catalog_and_presets():
    orca = table()
    assert orca.source.startswith("OrcaSlicer/OrcaSlicer ")
    # Creality and Generic catalog filaments with an OrcaSlicer K2 Pro preset.
    assert orca.default_for({"id": "01001", "name": "Hyper PLA"}) == "OFCZsqXg"
    assert orca.preset_name("OFCZsqXg") == "Hyper PLA @K2 Pro-all"
    assert orca.default_for({"id": "00001", "name": "Generic PLA"}) == "OFDSrzZ8"
    assert orca.material_default("pla") == "OFDSrzZ8"
    assert len(orca.presets_status) > 200
    ids = [preset["id"] for preset in orca.presets_status]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("filament, expected", [
    # Bambu tags: the product in the OrcaSlicer filament library.
    ({"id": "BAMBU-BAMBULAB-PETG-HF", "brand": "Bambulab",
      "name": "Bambulab PETG HF", "material": "PETG"}, "Bambu PETG HF @System"),
    ({"id": "35512", "brand": "Bambulab", "name": "Bambulab PC",
      "material": "PC"}, "Bambu PC @System"),
    ({"id": "X1", "brand": "Bambu Lab", "name": "PLA Basic",
      "material": "PLA"}, "Bambu PLA Basic @System"),
    # A custom profile named like a K2 Pro preset.
    ({"id": "X2", "brand": "Creality", "name": "Hyper PETG",
      "material": "PETG"}, "Hyper PETG @K2 Pro-all"),
    # Brand + name of a library vendor.
    ({"id": "X3", "brand": "eSUN", "name": "PLA+", "material": "PLA"},
     "eSUN PLA+ @System"),
])
def test_default_for_custom_and_third_party_profiles(filament, expected):
    orca = table()
    assert orca.preset_name(orca.default_for(filament)) == expected


def test_unknown_profile_has_no_default():
    orca = table()
    assert orca.default_for({"id": "X9", "brand": "Nobody",
                             "name": "Mystery PLA", "material": "PLA"}) == ""
    assert orca.default_for(None) == ""
    assert orca.material_default("UNOBTAINIUM") == ""


@pytest.mark.parametrize("value, expected", [
    ("OFCZsqXg", "OFCZsqXg"), (" P637bdcb ", "P637bdcb"), ("GFL99", "GFL99"),
    ("", ""), (None, "")])
def test_clean_orca_id(value, expected):
    assert clean_orca_id(value) == expected


@pytest.mark.parametrize("value", ["has space", "a;b", "x" * 41, 'q"'])
def test_clean_orca_id_rejects(value):
    with pytest.raises(ValueError):
        clean_orca_id(value)


def test_missing_table_is_harmless(tmp_path):
    orca = OrcaPresetTable(str(tmp_path / "missing.json"))
    assert orca.error == ""
    assert orca.default_for({"id": "01001"}) == ""
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert "broken.json" in OrcaPresetTable(str(broken)).error


def test_status_fields_and_override_on_a_system_profile(tmp_path):
    store = make_store(tmp_path)
    hyper = store.filaments_status["01001"]
    assert hyper["orca_filament_id"] == "OFCZsqXg"
    assert hyper["orca_filament_id_default"] == "OFCZsqXg"
    assert hyper["orca_filament_id_custom"] is False
    assert hyper["orca_preset"] == "Hyper PLA @K2 Pro-all"

    saved = store.set_orca_override("01001", "P637bdcb")
    assert saved["orca_filament_id"] == "P637bdcb"
    assert saved["orca_filament_id_custom"] is True
    assert saved["orca_preset"] == ""
    # The system profile itself is untouched and nothing is in its entry.
    assert store.filament("01001").get("orca_filament_id") is None
    library = json.loads((tmp_path / "cfs_filaments.json").read_text())
    assert library["orca_filament_ids"] == {"01001": "P637bdcb"}
    assert all("orca_filament_id" not in entry for entry in library["materials"])

    # Back to the default: the override is dropped.
    assert store.set_orca_override("01001", "")["orca_filament_id"] == "OFCZsqXg"
    library = json.loads((tmp_path / "cfs_filaments.json").read_text())
    assert library["orca_filament_ids"] == {}


def test_override_equal_to_default_is_not_stored(tmp_path):
    store = make_store(tmp_path)
    store.set_orca_override("01001", "OFCZsqXg")
    assert store.orca_overrides == {}


def test_overrides_survive_a_restart_and_follow_the_file(tmp_path):
    store = make_store(tmp_path)
    store.set_filament("BAMBU-BAMBULAB-PETG-HF", {
        "material": "PETG", "brand": "Bambulab", "name": "Bambulab PETG HF",
        "target_temp": 250})
    assert store.filaments_status["BAMBU-BAMBULAB-PETG-HF"][
        "orca_preset"] == "Bambu PETG HF @System"
    store.set_orca_override("BAMBU-BAMBULAB-PETG-HF", "P1234567")

    again = make_store(tmp_path)
    assert again.filaments_status["BAMBU-BAMBULAB-PETG-HF"][
        "orca_filament_id"] == "P1234567"

    # Deleting the profile drops its override.
    assert again.delete_filament("BAMBU-BAMBULAB-PETG-HF")
    assert again.orca_overrides == {}


def test_overrides_without_a_library_file(tmp_path):
    store = make_store(tmp_path, library=False)
    store.set_orca_override("00003", "GFG99")
    again = make_store(tmp_path, library=False)
    assert again.filaments_status["00003"]["orca_filament_id"] == "GFG99"


def test_invalid_overrides_in_the_file_are_ignored(tmp_path):
    (tmp_path / "cfs_filaments.json").write_text(json.dumps({
        "materials": [],
        "orca_filament_ids": {"01001": "bad id", "00001": "GFL99"},
    }), encoding="utf-8")
    store = make_store(tmp_path)
    assert store.orca_overrides == {"00001": "GFL99"}
    assert store.filaments_status["01001"]["orca_filament_id"] == "OFCZsqXg"


def test_unknown_filament_and_bad_id_are_refused(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(Exception, match="Unknown filament"):
        store.set_orca_override("NOPE", "GFL99")
    with pytest.raises(ValueError):
        store.set_orca_override("01001", "bad id")


def test_slot_id_from_profile_or_material(tmp_path):
    store = make_store(tmp_path)
    assert store.orca_filament_id("01001", "PLA") == "OFCZsqXg"
    # No profile, or a profile without a preset: the material's generic one.
    assert store.orca_filament_id("", "PETG") == store.orca.material_default("PETG")
    assert store.orca_filament_id("NOPE", "PLA") == "OFDSrzZ8"
    assert store.orca_filament_id("", "") == ""


def test_lane_data_carries_the_orca_id():
    lanes = lanes_from_slots([{
        "index": 0, "present": True, "external": False, "material": "PLA",
        "color": "#FF0000", "filament_id": "01001",
        "orca_filament_id": "OFCZsqXg"}])
    lane = lanes["lane1"]
    # filament_id keeps its meaning (the CFS filament); the preset is apart.
    assert lane["filament_id"] == "01001"
    assert lane["orca_filament_id"] == "OFCZsqXg"


class FakeGcmd:
    def __init__(self, **params):
        self.params = {key: str(value) for key, value in params.items()}
        self.messages = []

    def get_command_parameters(self):
        return self.params

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params[name]) if name in self.params else default

    def error(self, message):
        return RuntimeError(message)


def make_box(tmp_path):
    box = Box.__new__(Box)
    box.store = make_store(tmp_path)
    box.messages = []
    box._info = lambda gcmd, message: box.messages.append(message)
    return box


def test_command_reports_sets_and_resets(tmp_path):
    box = make_box(tmp_path)
    box.cmd_filament_orca_id(FakeGcmd(ID="01001"))
    assert "OFCZsqXg [Hyper PLA @K2 Pro-all]" in box.messages[-1]

    box.cmd_filament_orca_id(FakeGcmd(ID="01001", ORCA_ID="P637bdcb"))
    assert box.store.filaments_status["01001"]["orca_filament_id"] == "P637bdcb"

    box.cmd_filament_orca_id(FakeGcmd(ID="01001", RESET=1))
    assert box.store.filaments_status["01001"]["orca_filament_id"] == "OFCZsqXg"

    with pytest.raises(RuntimeError, match="does not exist"):
        box.cmd_filament_orca_id(FakeGcmd(ID="NOPE"))
    with pytest.raises(RuntimeError, match="invalid OrcaSlicer"):
        box.cmd_filament_orca_id(FakeGcmd(ID="01001", ORCA_ID="bad id"))
