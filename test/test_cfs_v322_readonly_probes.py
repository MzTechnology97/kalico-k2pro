#!/usr/bin/env python3
"""v3.22 host debug command offline regression. No printer I/O."""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
extras = types.ModuleType("extras")
extras.__path__ = []
proto = types.ModuleType("extras.box_protocol")
proto.ProtocolError = type("ProtocolError", (Exception,), {})
serial = types.ModuleType("extras.serial_485")
serial.build_485_body = lambda *args, **kwargs: b""
sys.modules.update({
    "extras": extras, "extras.box_protocol": proto, "extras.serial_485": serial,
})
spec = importlib.util.spec_from_file_location(
    "cfs_v322", ROOT / "klippy" / "extras" / "box_cfs_runtime.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeDriver:
    def __init__(self):
        self.gets = []
    def get_v2(self, param_id, timeout=1.0):
        self.gets.append(param_id)
        return [0x1122, 0x3344, 0x5566, 0x7788][param_id-28]
    def set_v2(self, *args, **kwargs):
        raise AssertionError("Must never issue SET")
    def reset_v2(self, *args, **kwargs):
        raise AssertionError("Must never issue RESET")


class FakeBox:
    owner = None
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


class FakeCmd:
    def __init__(self):
        self.lines = []
    def respond_info(self, line):
        self.lines.append(line)
    def error(self, message):
        return RuntimeError(message)


box = FakeBox()
instance = module.BoxCfsRuntime.__new__(module.BoxCfsRuntime)
instance.printer = FakePrinter(box)
driver = FakeDriver()
instance._driver = lambda: driver
instance._probe = lambda obj: {"version": 2, "count": 28, "features": 0xD7}
cmd = FakeCmd()
instance.cmd_snapshot(cmd)
assert driver.gets == [28, 29, 30, 31], driver.gets
assert box.owner is None
assert "1122:3344" in cmd.lines[0] and "5566:7788" in cmd.lines[0]
assert "not proof of idle" in cmd.lines[0]

instance._probe = lambda obj: {"version": 2, "count": 28, "features": 0x97}
try:
    instance.cmd_snapshot(FakeCmd())
except RuntimeError as e:
    assert "0xD7" in str(e)
else:
    raise AssertionError("v3.21 shouldn't claim to support diagnostic fields")
assert len(driver.gets) == 4

instance._probe = lambda obj: {"version": 2, "count": 28, "features": 0xD7}
old_get = driver.get_v2
def failed_get(param_id,timeout=1.0):
    if param_id == 29:
        raise RuntimeError("Serial diagnostic failure")
    return old_get(param_id, timeout)
driver.get_v2 = failed_get
try:
    instance.cmd_snapshot(FakeCmd())
except RuntimeError:
    pass
else:
    raise AssertionError("Expected transport failure")
assert box.owner is None, "RFID ownership wasn't released on failure"

print("PASS v3.22 four fixed diagnostic GETs, feature gating, never SET, lock cleanup")
