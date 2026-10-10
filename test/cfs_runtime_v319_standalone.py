#!/usr/bin/env python3
"""Standalone host-only regression for CFS v3.19 feature guards.

This script deliberately replaces serial and protocol modules with mocks.
It never opens a device or makes a request to the printer.
"""

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
extras.box_protocol = proto
sys.modules.update(
    {
        "extras": extras,
        "extras.box_protocol": proto,
        "extras.serial_485": serial,
    }
)

spec = importlib.util.spec_from_file_location(
    "runtime_v319", ROOT / "klippy/extras/box_cfs_runtime.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MockDriver:
    def __init__(self):
        self.calls = []
        self.values = {p.param_id: p.default for p in module.PARAMETERS}
        self.values[6] = 0

    def probe(self):
        self.calls.append(("probe",))
        return {"version": 2, "count": 28, "features": 0xF7, "override_mask": 0}

    def info_v2(self):
        return self.probe()

    def get_v2(self, param_id):
        self.calls.append(("get", param_id))
        return self.values[param_id]

    def set_v2(self, param_id, value):
        assert param_id >= 7
        self.calls.append(("set", param_id, value))
        self.values[param_id] = value

    def reset_v2(self, param_id):
        self.calls.append(("reset", param_id))
        self.values[param_id] = module.PARAM_BY_ID[param_id].default

    def describe_v2(self, param_id):
        raise AssertionError("v3.19 has no DESCRIBE opcode")


class MockGcode:
    def __init__(self, params=None):
        self.params = params or {}
        self.lines = []

    def get(self, key, default=None):
        return self.params.get(key, default)

    def get_int(self, key, default=None, minval=None, maxval=None):
        return int(self.params.get(key, default))

    def respond_info(self, line):
        self.lines.append(line)

    def error(self, message):
        return ValueError(message)


def main():
    driver = MockDriver()
    runtime = module.BoxCfsRuntime.__new__(module.BoxCfsRuntime)
    runtime._driver = lambda: driver
    runtime._probe = lambda obj: obj.probe()
    runtime._record_meta = lambda meta: None
    runtime.overrides = {2: 220, 7: 3300}
    runtime.last_values = {}
    runtime.last_error = None
    runtime.override_mask = 0

    try:
        runtime._apply_configured()
        raise AssertionError("v3.19 auto-apply must be rejected")
    except module.CfsRuntimeUnsupported:
        pass
    assert not any(call[0] == "set" for call in driver.calls)

    try:
        runtime.cmd_apply(MockGcode())
        raise AssertionError("v3.19 bulk APPLY must be rejected")
    except ValueError:
        pass
    assert not any(call[0] == "set" for call in driver.calls)

    try:
        runtime.cmd_set(MockGcode({"PARAM": "feeder_forward_speed", "VALUE": "200"}))
        raise AssertionError("stock speed SET must be rejected")
    except ValueError:
        pass

    runtime.cmd_set(MockGcode({"PARAM": "hub_transition_wait_ms", "VALUE": "3300"}))
    assert driver.values[7] == 3300
    assert ("set", 7, 3300) in driver.calls

    runtime.cmd_reset(MockGcode({"PARAM": "hub_transition_wait_ms"}))
    assert driver.values[7] == 3200
    try:
        runtime.cmd_reset(MockGcode({"PARAM": "feeder_forward_speed"}))
        raise AssertionError("stock speed RESET must be rejected")
    except ValueError:
        pass

    lines = MockGcode({"ALL": 1})
    runtime.cmd_diag(lines)
    assert lines.lines and "features=0xF7" in lines.lines[0]
    assert not any(
        call[0] == "set" and call[1] < 7 for call in driver.calls
    )
    print("PASS: v3.19 manual advanced SET/RESET, blocked stock writes and auto-apply")


if __name__ == "__main__":
    main()
