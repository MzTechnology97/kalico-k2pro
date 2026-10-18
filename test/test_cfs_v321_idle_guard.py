"""Offline regression for the v3.21 CFS host write guard.

No live printer, socket, serial port, firmware or filesystem write is used.
"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock


class FakeBox:
    def __init__(self):
        self.data = {
            "state": "IDLE",
            "state_code": 0,
            "loaded_slot": -1,
            "operation": {"active": False},
        }
        self.rfid_read_owner = None
        self.claim_count = 0
        self.release_count = 0
        self._cfs_runtime_write_owner = None
        self.operation_depth = 0
        self.operation_progress = None
        self.change_engine = type("ChangeEngine", (), {"pending": None})()

    def acquire_cfs_runtime_write(self, owner):
        if (
            self._cfs_runtime_write_owner is not None
            or self.operation_depth != 0
            or self.operation_progress is not None
            or self.change_engine.pending is not None
        ):
            raise RuntimeError("Runtime writer cannot overlap movement")
        self._cfs_runtime_write_owner = owner

    def release_cfs_runtime_write(self, owner):
        assert self._cfs_runtime_write_owner == owner
        self._cfs_runtime_write_owner = None

    def get_status(self, eventtime):
        return self.data

    def acquire_rfid_read(self, owner):
        if self.rfid_read_owner is not None:
            raise RuntimeError("RFID read busy")
        self.rfid_read_owner = owner
        self.claim_count += 1

    def release_rfid_read(self):
        self.rfid_read_owner = None
        self.release_count += 1


class FakeObject:
    def __init__(self, **status):
        self.data = status

    def get_status(self, eventtime):
        return self.data


class FakePrinter:
    def __init__(self, box, sensor, print_stats):
        self.objects = {
            "box": box,
            "filament_switch_sensor filament_sensor": sensor,
            "print_stats": print_stats,
        }

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class FakeReactor:
    def monotonic(self):
        return 100.0


def _load_runtime_module():
    extras = types.ModuleType("extras")
    extras.__path__ = []
    protocol = types.ModuleType("extras.box_protocol")
    protocol.ProtocolError = type("ProtocolError", (Exception,), {})
    serial = types.ModuleType("extras.serial_485")
    serial.build_485_body = lambda *args, **kwargs: b""
    replacements = {
        "extras": extras,
        "extras.box_protocol": protocol,
        "extras.serial_485": serial,
    }
    source = (
        Path(__file__).resolve().parents[1]
        / "klippy"
        / "extras"
        / "box_cfs_runtime.py"
    )
    spec = importlib.util.spec_from_file_location("cfs_v321_mock", source)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, replacements):
        spec.loader.exec_module(module)
    return module


def test_v321_write_guard():
    runtime = _load_runtime_module()
    box = FakeBox()
    sensor = FakeObject(filament_detected=False)
    stats = FakeObject(state="standby")
    instance = runtime.BoxCfsRuntime.__new__(runtime.BoxCfsRuntime)
    instance.printer = FakePrinter(box, sensor, stats)
    instance.reactor = FakeReactor()

    def accepted():
        with instance._v321_claim_write({"features": 0x97}):
            assert box.rfid_read_owner == "BOX_CFS_RUNTIME_WRITE"
            assert box._cfs_runtime_write_owner == "BOX_CFS_RUNTIME_WRITE"
        assert box.rfid_read_owner is None
        assert box._cfs_runtime_write_owner is None

    def rejected():
        try:
            with instance._v321_claim_write({"features": 0x97}):
                raise AssertionError("Write should have been rejected")
        except runtime.CfsRuntimeBusy:
            pass
        else:
            raise AssertionError("Busy guard did not reject write")

    accepted()
    # Retained RFID slot 1 is not evidence that a motor is moving.
    box.active_slot_raw = 1
    accepted()

    cases = [
        (box.data, "state", "PRINT"),
        (box.data, "state_code", 2),
        (box.data, "loaded_slot", 1),
        (box.data["operation"], "active", True),
        (sensor.data, "filament_detected", True),
        (sensor.data, "filament_detected", None),
        (stats.data, "state", "printing"),
        (box.data, "state", "NO_RESPONSE"),
        (box.data, "state_code", None),
    ]
    for target, key, value in cases:
        original = target.get(key)
        try:
            target[key] = value
            rejected()
        finally:
            target[key] = original

    box.rfid_read_owner = "EXTERNAL_RFID_TASK"
    rejected()
    assert box.rfid_read_owner == "EXTERNAL_RFID_TASK"
    box.rfid_read_owner = None

    try:
        with instance._v321_claim_write({"features": 0x97}):
            raise ValueError("simulated SET failure")
    except ValueError:
        pass
    assert box.rfid_read_owner is None
    assert box._cfs_runtime_write_owner is None
    assert box.claim_count == box.release_count

    # A physical operation already in progress excludes SET/RESET.
    box.operation_depth = 1
    try:
        with instance._v321_claim_write({"features": 0x97}):
            raise AssertionError("Must not write during motion")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Motion did not exclude runtime write")
    assert box._cfs_runtime_write_owner is None
    assert box.rfid_read_owner is None
    box.operation_depth = 0


if __name__ == "__main__":
    test_v321_write_guard()
    print("PASS: v3.21 write preflight and RFID claim regression")
