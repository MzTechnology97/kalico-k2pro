"""Offline-only regression for the experimental v3.22 diagnostic command."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock


class FakeDriver:
    def __init__(self):
        self.gets = []

    def get_v2(self, param_id, timeout=1.0):
        self.gets.append(param_id)
        return [0x1122, 0x3344, 0x5566, 0x7788][param_id - 28]

    def set_v2(self, *args, **kwargs):
        raise AssertionError("Snapshot must not write CFS parameters")

    def reset_v2(self, *args, **kwargs):
        raise AssertionError("Snapshot must not reset CFS parameters")


class FakeBox:
    def __init__(self):
        self.owner = None

    def acquire_rfid_read(self, owner):
        assert self.owner is None
        self.owner = owner

    def release_rfid_read(self):
        assert self.owner == "BOX_CFS_DIAG_SNAPSHOT"
        self.owner = None


class FakePrinter:
    def __init__(self, box):
        self.box = box

    def lookup_object(self, name, default=None):
        assert name == "box"
        return self.box


class FakeCommand:
    def __init__(self):
        self.lines = []

    def respond_info(self, line):
        self.lines.append(line)

    def error(self, message):
        return RuntimeError(message)


def _load_module():
    extras = types.ModuleType("extras")
    extras.__path__ = []
    protocol = types.ModuleType("extras.box_protocol")
    protocol.ProtocolError = type("ProtocolError", (Exception,), {})
    serial = types.ModuleType("extras.serial_485")
    serial.build_485_body = lambda *args, **kwargs: b""
    dependencies = {
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
    spec = importlib.util.spec_from_file_location("cfs_v322_probe_test", source)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, dependencies):
        spec.loader.exec_module(module)
    return module


def test_v322_fixed_readonly_snapshot_and_exception_cleanup():
    runtime = _load_module()
    box = FakeBox()
    driver = FakeDriver()
    extra = runtime.BoxCfsRuntime.__new__(runtime.BoxCfsRuntime)
    extra.printer = FakePrinter(box)
    extra._driver = lambda: driver
    extra._probe = lambda obj: {"version": 2, "count": 28, "features": 0xD7}

    response = FakeCommand()
    extra.cmd_snapshot(response)
    assert driver.gets == [28, 29, 30, 31]
    assert box.owner is None
    assert "1122:3344" in response.lines[0]
    assert "5566:7788" in response.lines[0]
    assert "not proof of idle" in response.lines[0]

    # v3.21 must NOT claim to have v3.22 memory probes.
    extra._probe = lambda obj: {"version": 2, "count": 28, "features": 0x97}
    try:
        extra.cmd_snapshot(FakeCommand())
    except RuntimeError as exc:
        assert "0xD7" in str(exc)
    else:
        raise AssertionError("Probe was accepted on non-diagnostic firmware")
    assert driver.gets == [28, 29, 30, 31]

    # An interrupted read must never leak the shared RFID lock.
    extra._probe = lambda obj: {"version": 2, "count": 28, "features": 0xD7}
    previous_get = driver.get_v2

    def failed_read(param_id, timeout=1.0):
        if param_id == 29:
            raise RuntimeError("Synthetic serial failure")
        return previous_get(param_id, timeout)

    driver.get_v2 = failed_read
    try:
        extra.cmd_snapshot(FakeCommand())
    except RuntimeError:
        pass
    else:
        raise AssertionError("Synthetic serial failure was not propagated")
    assert box.owner is None
