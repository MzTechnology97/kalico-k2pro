import importlib
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box as box_module
from extras.box import Box, BoxError, BoxSnapshot, BoxStore, TrackingOwner
from extras.power_loss_recovery import _TrustedZReference


class FakeCatalog:
    def __init__(self, entries):
        self._entries = entries

    @property
    def entries(self):
        return [dict(item) for item in self._entries]


class FakeChangeEngine:
    default_temp = 220


def make_box(tmp_path):
    box = Box.__new__(Box)
    box.store = BoxStore(str(tmp_path / "filament_box.json"))
    box.drivers = {1: object()}
    box.rfid_spools, box.rfid_percent, box.rfid_reported_percent = {}, {}, {}
    box.rfid_live_slots, box.unknown_rfid = set(), {}
    box.rfid_pending, box.rfid_snapshot, box.rfid_seen_invalid = set(), {}, set()
    box.rfid_presence, box.rfid_absent_confirm = {}, {}
    box.spoolman_tokens = {}
    box.rfid_estimate_dirty = False
    box.runout_active, box.runout_origin = False, None
    box.tracking_owner = None
    box.change_engine = FakeChangeEngine()
    for slot in (0, 2):
        box.set_profile(slot, {"material": "PETG-CF", "color": "#000000",
                               "source": "library"})
    return box


def runout_chain(box, slot_mask):
    snap = BoxSnapshot(slot_mask=slot_mask, loaded_slot=0)
    return box._runout_status(box._slot_statuses(snap), snap)["chain"]


def test_runout_source_profile_survives_cfs_slot_removal(tmp_path):
    box = make_box(tmp_path)
    box.runout_active, box.runout_origin = True, 0
    box._rfid_removed(0)
    assert box.profile(0)["material"] == "PETG-CF"
    assert runout_chain(box, 0b0100) == [2]


def test_tracked_source_survives_removal_before_runout_status(tmp_path):
    box = make_box(tmp_path)
    box.tracking_owner = TrackingOwner(1, 0, 1)
    box._rfid_removed(0)
    box.runout_active, box.runout_origin = True, 0
    assert runout_chain(box, 0b0100) == [2]


def test_tracked_source_survives_absent_confirmation(tmp_path):
    box = make_box(tmp_path)
    box.tracking_owner = TrackingOwner(1, 0, 1)
    box.rfid_presence[1] = 0b0100
    for _ in range(5):
        box._reconcile_presence(1, 0b0100)
    assert box.profile(0)["material"] == "PETG-CF"


def test_idle_removal_still_clears_slot(tmp_path):
    box = make_box(tmp_path)
    box._rfid_removed(2)
    assert box.profile(2)["material"] == ""


def test_seeding_writes_state_once_and_not_when_unchanged(tmp_path, monkeypatch):
    box = make_box(tmp_path)
    box.system_material_catalog = FakeCatalog([
        {"id": "10001", "material": "PLA", "brand": "Generic", "name": "Generic PLA",
         "target_temp": 220},
        {"id": "10002", "material": "PETG", "brand": "Generic", "name": "Generic PETG",
         "target_temp": 240},
    ])
    box.material_catalog = FakeCatalog([])
    saves = []
    original = BoxStore.save
    monkeypatch.setattr(BoxStore, "save", lambda self: (saves.append(1), original(self)))
    box._seed_material_catalog()
    assert len(saves) == 1
    saves.clear()
    box._seed_material_catalog()
    assert saves == []


def test_seeding_skips_invalid_catalog_entries(tmp_path):
    box = make_box(tmp_path)
    box.system_material_catalog = FakeCatalog([])
    box.material_catalog = FakeCatalog([
        {"id": "BAD01", "material": "PLA", "brand": "X", "name": "Broken",
         "min_temp": 900},
        {"id": "GOOD1", "material": "PLA", "brand": "X", "name": "Good"},
    ])
    box._seed_material_catalog()
    assert box.store.filament("GOOD1") is not None
    assert box.store.filament("BAD01") is None


def test_invalid_filament_in_state_file_does_not_block_loading(tmp_path):
    path = tmp_path / "filament_box.json"
    path.write_text(json.dumps({"filaments": {
        "OK1": {"id": "OK1", "material": "PLA"},
        "BROKEN": {"id": "BROKEN", "material": "PLA", "min_temp": 900},
    }}))
    store = BoxStore(str(path))
    assert "OK1" in store.data["filaments"]
    assert "BROKEN" not in store.data["filaments"]
    with pytest.raises(BoxError):
        store.set_filament("BROKEN", {"material": "PLA", "min_temp": 900})


def test_seeding_never_makes_custom_profile_system(tmp_path):
    box = make_box(tmp_path)
    box.store.set_filament("MYPLA", {"material": "PLA", "brand": "Generic",
                                     "name": "Generic PLA"})
    box.system_material_catalog = FakeCatalog([
        {"id": "10001", "material": "PLA", "brand": "Generic", "name": "Generic PLA",
         "target_temp": 220},
    ])
    box.material_catalog = FakeCatalog([])
    box._seed_material_catalog()
    assert box.store.filament("MYPLA")["system"] is False
    assert box.store.filament("10001")["system"] is True


class FakeGcmd:
    def __init__(self, params):
        self.params = params

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))


def test_runout_swap_without_enable_enables(tmp_path):
    box = make_box(tmp_path)
    box._info = lambda *args: None
    box.store.set_setting("runout_swap_enabled", False)
    box.cmd_runout_swap(FakeGcmd({}))
    assert box.runout_swap_enabled is True
    box.cmd_runout_swap(FakeGcmd({}))
    assert box.runout_swap_enabled is True


def test_observation_guard_allows_hardware_status():
    assert 0x15 in box_module._ReadOnlyCFSProxy.ALLOWED_FUNCTIONS
    assert 0x0D not in box_module._ReadOnlyCFSProxy.ALLOWED_FUNCTIONS


def test_public_unit_id_does_not_expose_uniid():
    uid = bytes.fromhex("5f983b4d1454b01247343534")
    public = Box._public_unit_id(uid)
    assert public != uid.hex() and uid.hex() not in public
    assert public == Box._public_unit_id(uid) and len(public) == 12
    assert Box._public_unit_id(None) == "-1"


class FakeRail:
    def get_range(self):
        return (-3.0, 303.0)


class FakeKinematics:
    rails = [FakeRail(), FakeRail(), FakeRail()]


class FakeToolhead:
    def __init__(self, z):
        self.position = [100.0, 120.0, z, 0.0]

    def get_kinematics(self):
        return FakeKinematics()

    def get_position(self):
        return list(self.position)


def test_trusted_z_reference_records_and_validates_kinematic_z():
    ref = _TrustedZReference(FakeToolhead(12.34))
    frame = ref.capture_reference_frame()
    assert frame["kind"] == "trusted_kinematic_z"
    assert frame["kinematic_z"] == 12.34 and ref.zmax == 303.0
    ref.validate_reference_frame(frame)
    with pytest.raises(ValueError):
        ref.validate_reference_frame({"kind": "z_align"})
    with pytest.raises(ValueError):
        ref.validate_reference_frame(dict(frame, kinematic_z=400.0))


class FakeZStepper:
    def get_step_dist(self):
        return 0.0025


class FakeZRail(FakeRail):
    def __init__(self, count):
        self.count = count

    def get_steppers(self):
        return [FakeZStepper() for _ in range(self.count)]


class FakeZPrinter:
    class command_error(Exception):
        pass


def make_z_align(monkeypatch, stepper_count):
    # z_align imports Kalico's mcu module at import time; only __init__ uses it.
    monkeypatch.setitem(sys.modules, "mcu", types.ModuleType("mcu"))
    ZAlign = importlib.import_module("extras.z_align").ZAlign
    z_align = ZAlign.__new__(ZAlign)
    z_align.printer = FakeZPrinter()
    kin = FakeKinematics()
    kin.rails = [FakeRail(), FakeRail(), FakeZRail(stepper_count)]
    z_align._toolhead = type("T", (), {"get_kinematics": lambda self: kin})()
    return z_align


def test_z_align_reference_frame_accepts_single_z_k2_pro(monkeypatch):
    assert make_z_align(monkeypatch, 1).validate_reference_frame([301.0]) == [301.0]
    assert make_z_align(monkeypatch, 2).validate_reference_frame(
        [300.0, 300.2]) == [300.0, 300.2]
    with pytest.raises(FakeZPrinter.command_error):
        make_z_align(monkeypatch, 1).validate_reference_frame([300.0, 300.2])
