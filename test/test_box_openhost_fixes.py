import importlib
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box as box_module
from extras import box_print_mapping as mapping_module
from extras.box import Box, BoxError, BoxSnapshot, BoxStore, TrackingOwner
from extras.box_addr import AutoAddressManager
from extras.box_change import BoxChangeEngine
from extras.box_protocol import AutoAddressReply
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
    box.rfid_fallback_tried = set()
    box.rfid_cleared_on_insert = set()
    box.rfid_insert_time = {}
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


def test_system_catalog_is_kept_in_memory_and_never_written(tmp_path, monkeypatch):
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
    assert saves == []
    assert box.store.filament("10001")["system"] is True
    assert "10001" not in json.loads(
        (tmp_path / "filament_box.json").read_text()).get("filaments", {})


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

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))

    def error(self, message):
        return BoxError(message)


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
    # It is a package-relative import, so load it as klippy.extras.z_align.
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.setitem(
        sys.modules, "klippy.mcu", types.ModuleType("klippy.mcu"))
    ZAlign = importlib.import_module("klippy.extras.z_align").ZAlign
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


class FakeMcuSerial:
    class msgparser:
        @staticmethod
        def get_constant_float(name):
            assert name == "CLOCK_FREQ"
            return 120000000.0


def test_z_align_mcu_units_match_stock_creality(monkeypatch):
    z_align = make_z_align(monkeypatch, 1)
    z_align._main_mcu = type("M", (), {"_serial": FakeMcuSerial()})()
    # K2 Pro stock profile: rotation_distance 8, 16 microsteps, gear ratio
    # ignored by the stock host code -> identical MCU arguments.
    z_align._mcu_step_distance = 8.0 / (200 * 16)
    assert z_align._calc_speed_ticks(10.0) == 15000
    assert z_align._calc_distance_steps(10.0) == 8000
    assert z_align._calc_distance_steps(40.0) == 32000
    # 64 microsteps on the reference CM5: same physical motion, clamped safe.
    z_align._mcu_step_distance = 8.0 / (200 * 64)
    assert z_align._calc_distance_steps(10.0) == 32000
    assert z_align._calc_distance_steps(40.0) == 0xFFFF


# ---------------------------------------------------------------------------
# Upstream 071c813 integration (native print mapping, loader-mode start)
# ---------------------------------------------------------------------------

class FakeAddressClient:
    def __init__(self, uid):
        self.uid = uid
        self.mode = 1
        self.started = 0

    def query(self, address):
        if address != 1:
            return None
        return AutoAddressReply(self.uid, self.mode)

    def start_app(self):
        self.started += 1
        self.mode = 0

    def discover(self):
        return None


def test_loader_mode_box_is_started_and_verified():
    uid = bytes.fromhex("5f983b4d1454b01247343534")
    client = FakeAddressClient(uid)
    pauses = []
    result = AutoAddressManager(1, {1: uid}).enumerate(client, pause=pauses.append)
    assert client.started == 1 and pauses == [2.0]
    assert result.online == {1: uid} and result.errors == ()


class FakeMappingBox:
    external_slot = 16

    def __init__(self, helix=None):
        self.helix = helix or {}
        self.registered = []

    def _mapped_slot(self, tool):
        return self.helix.get(tool, tool)

    def is_valid_slot(self, slot):
        return 0 <= slot < 4 or slot == self.external_slot

    def _register_tools(self, tools):
        self.registered.extend(tools)


def make_engine(box):
    engine = BoxChangeEngine.__new__(BoxChangeEngine)
    engine.box = box
    engine.reset_print_mapping()
    engine.calls = []
    engine.change = lambda gcmd, target, flush=True, logical_tool=None: (
        engine.calls.append((target, flush, logical_tool)) or True)
    return engine


class FlushGcmd:
    def get_int(self, name, default=None, minval=None, maxval=None):
        return default


def test_tool_without_print_map_keeps_helix_assignment():
    engine = make_engine(FakeMappingBox(helix={0: 2}))
    engine.select_tool(FlushGcmd(), 0)
    engine.select_tool(FlushGcmd(), 1)
    assert engine.calls == [(2, True, None), (1, True, None)]


def test_tool_with_print_map_uses_native_map():
    engine = make_engine(FakeMappingBox(helix={0: 2}))
    engine.tool_map, engine.mapping_filename = {0: 3}, "part.gcode"
    engine.select_tool(FlushGcmd(), 0)
    assert engine.calls == [(3, True, 0)]


def test_runout_swap_moves_every_tool_of_the_empty_slot():
    engine = make_engine(FakeMappingBox())
    engine.tool_map, engine.mapping_filename = {0: 0, 1: 1, 2: 0}, "part.gcode"
    engine._remap_print_slot(0, 3)
    assert engine.tool_map == {0: 3, 1: 1, 2: 3}
    unmapped = make_engine(FakeMappingBox())
    unmapped._remap_print_slot(0, 3)
    assert unmapped.tool_map == {}


def test_print_mapping_survives_power_loss_roundtrip():
    box = FakeMappingBox()
    engine = make_engine(box)
    engine.tool_map, engine.mapping_filename = {0: 2, 1: 16}, "part.gcode"
    engine.active_tool, engine.active_slot = 1, 16
    saved = json.loads(json.dumps(engine.mapping_status()))
    restored = make_engine(box)
    restored.restore_print_mapping(saved)
    assert restored.mapping_status() == engine.mapping_status()
    assert sorted(box.registered) == [0, 1]
    with pytest.raises(ValueError):
        restored.restore_print_mapping(dict(saved, map={"0": 9}))


class FakePrinterObjects:
    def __init__(self, objects):
        self.objects = objects

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


def make_mapping(objects):
    mapping = mapping_module.BoxPrintMapping.__new__(mapping_module.BoxPrintMapping)
    mapping.printer = FakePrinterObjects(objects)
    mapping.box = types.SimpleNamespace(observation_mode=False, print_info=None)
    mapping.change_engine = make_engine(FakeMappingBox())
    mapping.auto_map_prints = True
    mapping.auto_map_block_unresolved = True
    mapping._explicit_start_in_progress = False
    return mapping


def test_auto_mapping_does_not_run_during_power_loss_recovery():
    plr = types.SimpleNamespace(recovering=True)
    mapping = make_mapping({"power_loss_recovery": plr})
    mapping._handle_file_loaded()
    assert mapping.auto_mapping["state"] == "idle"
    assert mapping.change_engine.mapping_filename is None


def test_auto_mapping_does_not_run_during_explicit_start():
    mapping = make_mapping({})
    mapping._explicit_start_in_progress = True
    mapping._handle_file_loaded()
    assert mapping.change_engine.mapping_filename is None


def test_k2_macros_only_call_registered_box_commands():
    import re
    macros = "".join(
        (ROOT / "config" / "k2" / "macros" / name).read_text()
        for name in ("print.cfg", "fans.cfg", "maintenance.cfg")
    )
    box_source = (ROOT / "klippy" / "extras" / "box.py").read_text()
    registered = set(re.findall(r'\("(_?[A-Z][A-Z0-9_]+)",', box_source))
    defined = set(re.findall(r"^\[(?:gcode_macro|delayed_gcode) (\S+)\]", macros, re.M))
    used = set(re.findall(r"^\s*(_?BOX_[A-Z0-9_]+)", macros, re.M))
    assert {"_BOX_PAUSE_CAPTURE", "_BOX_RESUME_PREPARE", "_BOX_RESUME_COMMIT"} <= used
    assert used - defined <= registered, used - defined - registered


def test_box_unit_statuses_report_each_cfs():
    box = Box.__new__(Box)
    box.drivers = {1: object(), 3: object()}
    reply = types.SimpleNamespace(status=0, box_state=2, temp_c=30.5, humidity_pct=34)
    box.box_replies = {1: reply}
    units = box._box_unit_statuses()
    assert [unit["address"] for unit in units] == [1, 3]
    assert units[0]["online"] and units[0]["temp_c"] == 30.5
    assert units[0]["slots"] == [0, 1, 2, 3]
    assert not units[1]["online"] and units[1]["temp_c"] is None
    assert units[1]["slots"] == [8, 9, 10, 11]


def test_operation_status_reports_live_stage_and_sensor():
    box = Box.__new__(Box)
    box.operation_depth = 0
    box.operation_progress = None
    box.snapshot = BoxSnapshot(loaded_slot=-1)
    box.change_engine = types.SimpleNamespace(
        pending=types.SimpleNamespace(last_step="load", target=2))
    sensor = {"filament_detected": False}
    box.reactor = types.SimpleNamespace(monotonic=lambda: 0.0)
    box._filament_sensor = lambda: types.SimpleNamespace(
        get_status=lambda eventtime: dict(sensor))
    with box._operation():
        box._set_operation_progress("load", 2, "feeding_to_printhead")
        status = box._operation_status()
        assert status == {"active": True, "kind": "load", "slot": 2,
                          "stage": "feeding_to_printhead",
                          "change_step": "load", "change_target": 2}
        sensor["filament_detected"] = True
        assert box._poll(10.0) == 10.25
        assert box.snapshot.filament_detected is True
    assert box.operation_progress is None
    assert box._operation_status()["active"] is False


def white_pla_box(tmp_path):
    box = make_box(tmp_path)
    for slot in (0, 1, 2):
        box.set_profile(slot, {"material": "PLA", "color": "#FFFFFF", "source": "manual"})
    return box


def test_runout_order_follows_the_manual_sequence(tmp_path):
    box = white_pla_box(tmp_path)
    assert runout_chain(box, 0b0111) == [1, 2]
    box.store.set_setting("runout_order", [2, 1, 0])
    assert runout_chain(box, 0b0111) == [2, 1]


def test_manual_order_wins_over_rfid_remaining(tmp_path):
    box = white_pla_box(tmp_path)
    box.rfid_percent = {1: 10.0, 2: 80.0}
    assert runout_chain(box, 0b0111) == [1, 2]
    box.store.set_setting("runout_order", [2])
    assert runout_chain(box, 0b0111) == [2, 1]


def test_runout_groups_report_the_manual_order(tmp_path):
    box = white_pla_box(tmp_path)
    box.store.set_setting("runout_order", [2, 0, 1])
    snap = BoxSnapshot(slot_mask=0b0111, loaded_slot=-1)
    groups = box._runout_groups(box._slot_statuses(snap))
    pla = [group for group in groups if group["material"] == "PLA"][0]
    assert pla["slots"] == [2, 0, 1]
    assert pla["strategy"] == "manual_order"
    statuses = {item["index"]: item for item in box._slot_statuses(snap)}
    assert statuses[2]["runout_rank"] == 0 and statuses[1]["runout_rank"] == 2


def test_runout_order_command(tmp_path):
    box = white_pla_box(tmp_path)
    box._info = lambda *args: None
    box.cmd_runout_order(FakeGcmd({"ORDER": "2, 1,0,2"}))
    assert box.runout_order == [2, 1, 0]
    with pytest.raises(BoxError):
        box.cmd_runout_order(FakeGcmd({"ORDER": "9"}))
    with pytest.raises(BoxError):
        box.cmd_runout_order(FakeGcmd({"ORDER": "two"}))
    box.cmd_runout_order(FakeGcmd({"ORDER": "AUTO"}))
    assert box.runout_order == []


# --- K2-OpenHost: CFS rediscovery when RS-485 was down at startup ----------


class FakeReactor:
    NEVER = 9.9e99

    def __init__(self):
        self.now = 100.0
        self.timers = {}

    def monotonic(self):
        return self.now

    def update_timer(self, timer, when):
        self.timers[timer] = when

    def pause(self, when):
        self.now = max(self.now, when)


class FakePrinter:
    def __init__(self, state="standby", shutdown=False):
        self.print_stats = types.SimpleNamespace(state=state)
        self.shutdown = shutdown
        self.events = []

    def lookup_object(self, name, default=None):
        if name == "print_stats":
            return self.print_stats
        if name == "serial_485 serial485":
            return object()
        return default

    def is_shutdown(self):
        return self.shutdown

    def send_event(self, name, *args):
        self.events.append(name)


class FakeAddressManager:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def enumerate(self, client, pause=None):
        self.calls += 1
        online = self.results.pop(0) if self.results else {}
        return types.SimpleNamespace(online=online, known=dict(online), errors=())


class FakeGcode:
    def __init__(self):
        self.messages = []

    def respond_info(self, msg):
        self.messages.append(msg)


def make_discovery_box(results, state="standby"):
    box = Box.__new__(Box)
    box.reactor = FakeReactor()
    box.printer = FakePrinter(state)
    box.gcode = FakeGcode()
    box.observation_mode = False
    box.address_manager = FakeAddressManager(results)
    box.store = types.SimpleNamespace(set_known_addresses=lambda known: None)
    box.drivers, box.drivers_ready, box.klippy_ready = {}, False, True
    box.operation_depth = 0
    box.poll_timer, box.rediscovery_timer = "poll", "rediscovery"
    box.rediscovery_delay = box_module.REDISCOVERY_MIN
    box.rediscovery_attempts = 0
    box.tracking_epoch, box.tracking_owner, box.path_owner = 0, None, None
    box.fault_episodes = {}
    box._clear_runout_state = lambda: None
    box._initialize_rfid = lambda: None
    box._register_t_commands = lambda: None
    return box


UID = bytes.fromhex("5f983b4d1454b01247343534")


def test_startup_without_cfs_schedules_rediscovery():
    box = make_discovery_box([{}])
    box._enumerate(box.reactor.now)
    assert box.drivers == {} and box.drivers_ready
    assert box.reactor.timers["rediscovery"] == 100.0 + box_module.REDISCOVERY_MIN


def test_startup_with_cfs_does_not_schedule_rediscovery():
    box = make_discovery_box([{1: UID}])
    box._enumerate(box.reactor.now)
    assert list(box.drivers) == [1]
    assert "rediscovery" not in box.reactor.timers


def test_rediscovery_finds_the_cfs_when_the_link_is_back():
    box = make_discovery_box([{}, {}, {1: UID}])
    box._enumerate(box.reactor.now)
    first = box._rediscover(box.reactor.now)  # still nothing: backs off
    assert first == box.reactor.now + 2 * box_module.REDISCOVERY_MIN
    assert box._rediscover(box.reactor.now) == box.reactor.NEVER
    assert list(box.drivers) == [1] and box.address_manager.calls == 3
    assert box.rediscovery_delay == box_module.REDISCOVERY_MIN
    assert any("CFS found" in m for m in box.gcode.messages)
    assert box.printer.events.count("box:ready") == 3  # one per enumeration


def test_rediscovery_backoff_is_capped():
    box = make_discovery_box([])
    for _ in range(10):
        box._rediscover(box.reactor.now)
    assert box.rediscovery_delay == box_module.REDISCOVERY_MAX


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_rediscovery_never_runs_during_a_print(state):
    box = make_discovery_box([{1: UID}], state=state)
    when = box._rediscover(box.reactor.now)
    assert box.address_manager.calls == 0 and box.drivers == {}
    assert when == box.reactor.now + box_module.REDISCOVERY_MIN


def test_rediscovery_waits_for_running_operations_and_shutdown():
    box = make_discovery_box([{1: UID}])
    box.operation_depth = 1
    box._rediscover(box.reactor.now)
    box.operation_depth = 0
    box.printer.shutdown = True
    box._rediscover(box.reactor.now)
    assert box.address_manager.calls == 0


def test_link_restored_triggers_an_immediate_retry_only_without_cfs():
    box = make_discovery_box([])
    box.drivers_ready = True
    box.rediscovery_delay = box_module.REDISCOVERY_MAX
    box._link_restored({})
    assert box.reactor.timers["rediscovery"] == box.reactor.now + 1.0
    assert box.rediscovery_delay == box_module.REDISCOVERY_MIN
    box.reactor.timers.clear()
    box.drivers = {1: object()}
    box._link_restored({})
    assert "rediscovery" not in box.reactor.timers
