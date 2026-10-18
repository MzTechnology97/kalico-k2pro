"""Host guards for the experimental CFS firmware v3.20-v3.22.

Feature bytes, as box_cfs_runtime labels them: 0xF7 v3.19, 0xB7 v3.20,
0x97 v3.21, 0xD7 v3.22 (read-only diagnostic snapshot). All of them refuse
auto-apply, bulk APPLY and writes to the stock IDs 0..6; 0x97 and 0xD7 also
require an idle printer and hold the RFID and CFS-operation locks while
writing. Mocks only: nothing talks to a device.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box as box_module
from extras import box_cfs_runtime as runtime

EXPERIMENTAL = (0xF7, 0xB7, 0x97, 0xD7)
GUARDED = (0x97, 0xD7)


class MockDriver:
    def __init__(self, features):
        self.features = features
        self.calls = []
        self.values = {p.param_id: p.default for p in runtime.PARAMETERS}
        self.values[6] = 0
        for idx in range(28, 32):
            self.values[idx] = 0x1000 + idx

    def probe(self):
        return {"version": 2, "count": 28, "features": self.features,
                "override_mask": 0}

    def info_v2(self):
        return self.probe()

    def get_v2(self, param_id, timeout=None):
        self.calls.append(("get", param_id))
        return self.values[param_id]

    def set_v2(self, param_id, value):
        self.calls.append(("set", param_id, value))
        self.values[param_id] = value

    def reset_v2(self, param_id):
        self.calls.append(("reset", param_id))

    def describe_v2(self, param_id):
        raise AssertionError("experimental builds have no DESCRIBE")


class FakeBox:
    """The Box locks and status fields the runtime guards look at."""

    def __init__(self):
        self.status = {"state": "IDLE", "state_code": 0, "loaded_slot": -1,
                       "operation": {"active": False}}
        self.rfid_read_owner = None
        self.write_owner = None
        self.events = []

    def get_status(self, eventtime):
        return dict(self.status)

    def acquire_rfid_read(self, owner):
        if self.rfid_read_owner is not None:
            raise box_module.BoxError("RFID read busy")
        self.rfid_read_owner = owner
        self.events.append(("rfid", owner))

    def release_rfid_read(self):
        self.rfid_read_owner = None
        self.events.append(("rfid", None))

    def acquire_cfs_runtime_write(self, owner):
        self.write_owner = owner
        self.events.append(("write", owner))

    def release_cfs_runtime_write(self, owner):
        assert self.write_owner == owner
        self.write_owner = None
        self.events.append(("write", None))


class Stat:
    def __init__(self, **status):
        self.status = status

    def get_status(self, eventtime):
        return dict(self.status)


class FakePrinter:
    def __init__(self, box):
        self.objects = {
            "box": box,
            "filament_switch_sensor filament_sensor": Stat(filament_detected=False),
            "print_stats": Stat(state="standby"),
        }

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class Reactor:
    def monotonic(self):
        return 1.0


class Gcmd:
    def __init__(self, **params):
        self.params = params
        self.lines = []

    def get(self, key, default=None):
        return self.params.get(key, default)

    def get_int(self, key, default=None, minval=None, maxval=None):
        return int(self.params.get(key, default))

    def respond_info(self, line):
        self.lines.append(line)

    def error(self, message):
        return ValueError(message)


def make(features):
    driver = MockDriver(features)
    box = FakeBox()
    rt = runtime.BoxCfsRuntime.__new__(runtime.BoxCfsRuntime)
    rt._driver = lambda: driver
    rt._probe = lambda obj: obj.probe()
    rt._record_meta = lambda meta: None
    rt.printer = FakePrinter(box)
    rt.reactor = Reactor()
    rt.overrides = {2: 220, 7: 3300}
    rt.last_values = {}
    rt.last_error = None
    rt.override_mask = 0
    return rt, driver, box


@pytest.mark.parametrize("features", EXPERIMENTAL)
def test_experimental_builds_refuse_auto_and_bulk_apply(features):
    rt, driver, box = make(features)
    with pytest.raises(runtime.CfsRuntimeUnsupported):
        rt._apply_configured()
    with pytest.raises(ValueError):
        rt.cmd_apply(Gcmd())
    assert not any(call[0] == "set" for call in driver.calls)


@pytest.mark.parametrize("features", EXPERIMENTAL)
def test_experimental_builds_protect_stock_ids(features):
    rt, driver, box = make(features)
    with pytest.raises(ValueError):
        rt.cmd_set(Gcmd(PARAM="feeder_forward_speed", VALUE="200"))
    with pytest.raises(ValueError):
        rt.cmd_reset(Gcmd(PARAM="feeder_forward_speed"))
    assert not any(call[0] in ("set", "reset") for call in driver.calls)


@pytest.mark.parametrize("features", EXPERIMENTAL)
def test_advanced_set_and_reset_when_idle(features):
    rt, driver, box = make(features)
    if features == 0xF7:
        # v3.19 connects no ID to a stock callsite: SET is refused.
        with pytest.raises(ValueError):
            rt.cmd_set(Gcmd(PARAM="hub_transition_wait_ms", VALUE="3300"))
        assert not any(call[0] == "set" for call in driver.calls)
    else:
        rt.cmd_set(Gcmd(PARAM="hub_transition_wait_ms", VALUE="3300"))
        assert ("set", 7, 3300) in driver.calls
    rt.cmd_reset(Gcmd(PARAM="hub_transition_wait_ms"))
    assert ("reset", 7) in driver.calls
    # Locks are released afterwards in every case.
    assert box.rfid_read_owner is None and box.write_owner is None
    if features in GUARDED:
        assert ("write", "BOX_CFS_RUNTIME_WRITE") in box.events
    else:
        assert box.events == []


@pytest.mark.parametrize("features", GUARDED)
@pytest.mark.parametrize(
    "busy",
    [
        ("box", "state", "LOADING"),
        ("box", "loaded_slot", 2),
        ("box", "operation", {"active": True}),
        ("filament_switch_sensor filament_sensor", "filament_detected", True),
        ("print_stats", "state", "printing"),
    ],
)
def test_guarded_writes_need_an_idle_printer(features, busy):
    rt, driver, box = make(features)
    name, field, value = busy
    obj = rt.printer.objects[name]
    obj.status[field] = value
    with pytest.raises(ValueError):
        rt.cmd_set(Gcmd(PARAM="hub_transition_wait_ms", VALUE="3300"))
    with pytest.raises(ValueError):
        rt.cmd_reset(Gcmd(PARAM="hub_transition_wait_ms"))
    assert not any(call[0] in ("set", "reset") for call in driver.calls)
    assert box.rfid_read_owner is None and box.write_owner is None


@pytest.mark.parametrize("features", GUARDED)
def test_guarded_write_refused_during_rfid_read(features):
    rt, driver, box = make(features)
    box.rfid_read_owner = "BOX_RFID_SCAN"
    with pytest.raises(ValueError):
        rt.cmd_set(Gcmd(PARAM="hub_transition_wait_ms", VALUE="3300"))
    assert not any(call[0] == "set" for call in driver.calls)
    assert box.rfid_read_owner == "BOX_RFID_SCAN"


def test_snapshot_reads_only_ids_28_to_31_on_0xd7():
    rt, driver, box = make(0xD7)
    gcmd = Gcmd()
    rt.cmd_snapshot(gcmd)
    assert [c for c in driver.calls if c[0] == "get"] == [
        ("get", 28), ("get", 29), ("get", 30), ("get", 31)]
    assert "DIAG ONLY" in gcmd.lines[0]
    assert "101C:101D" in gcmd.lines[0] and "101E:101F" in gcmd.lines[0]
    assert box.rfid_read_owner is None


@pytest.mark.parametrize("features", (0xF7, 0xB7, 0x97, 0x0F))
def test_snapshot_refused_on_other_firmware(features):
    rt, driver, box = make(features)
    with pytest.raises(ValueError):
        rt.cmd_snapshot(Gcmd())
    assert driver.calls == []


class Engine:
    pending = None


def bare_box():
    b = box_module.Box.__new__(box_module.Box)
    b._cfs_runtime_write_owner = None
    b.operation_depth = 0
    b.operation_progress = None
    b.change_engine = Engine()
    b.clog_baseline = None
    return b


def test_box_runtime_write_lock_blocks_operations():
    b = bare_box()
    b.acquire_cfs_runtime_write("BOX_CFS_RUNTIME_WRITE")
    with pytest.raises(box_module.BoxError):
        with b._operation():
            pass
    assert b.operation_depth == 0
    with pytest.raises(box_module.BoxError):
        b.release_cfs_runtime_write("someone-else")
    b.release_cfs_runtime_write("BOX_CFS_RUNTIME_WRITE")
    with b._operation():
        assert b.operation_depth == 1
    assert b.operation_depth == 0


def test_box_runtime_write_refused_while_operating():
    b = bare_box()
    with b._operation():
        with pytest.raises(box_module.BoxError):
            b.acquire_cfs_runtime_write("BOX_CFS_RUNTIME_WRITE")
    b.change_engine.pending = object()
    with pytest.raises(box_module.BoxError):
        b.acquire_cfs_runtime_write("BOX_CFS_RUNTIME_WRITE")
    b.change_engine.pending = None
    with pytest.raises(box_module.BoxError):
        b.acquire_cfs_runtime_write("")
    b.acquire_cfs_runtime_write("BOX_CFS_RUNTIME_WRITE")
    with pytest.raises(box_module.BoxError):
        b.acquire_cfs_runtime_write("BOX_CFS_RUNTIME_WRITE")
