"""Ensure bench-only CFS runtime IDs are never advertised as effective.

Offline import via the existing CFS v3.22 mock fixture. No serial/device IO.
"""
from contextlib import nullcontext

from test_cfs_v322_readonly_probes import _load_module


class FakeGcmd:
    def __init__(self, param, value=3300):
        self.param = param
        self.value = value
        self.messages = []

    def get(self, name, default=""):
        return self.param if name == "PARAM" else default

    def get_int(self, name, minval=None, maxval=None):
        assert name == "VALUE"
        assert (minval is None or self.value >= minval)
        assert (maxval is None or self.value <= maxval)
        return self.value

    def respond_info(self, msg):
        self.messages.append(msg)

    def error(self, msg):
        return RuntimeError(msg)


class FakeDriver:
    def __init__(self):
        self.writes = []

    def set_v2(self, idx, value):
        self.writes.append((idx, value))

    def get_v2(self, idx):
        return {7: 3300, 8: 700}[idx]

    def info_v2(self):
        return {"version": 2, "count": 28, "features": 0xD7}


def test_bench_only_ids_never_become_motor_controls():
    runtime = _load_module()
    assert set(runtime.EXPERIMENTAL_EFFECTIVE_HOOKS) == {
        0xF7, 0xB7, 0x97, 0xD7}
    for feature, allowed in runtime.EXPERIMENTAL_EFFECTIVE_HOOKS.items():
        for param_id in range(28):
            try:
                runtime.BoxCfsRuntime._require_effective_hook(
                    {"version": 2, "features": feature}, param_id)
            except runtime.CfsRuntimeUnsupported:
                assert param_id not in allowed
            else:
                assert param_id in allowed
    assert runtime.EXPERIMENTAL_EFFECTIVE_HOOKS[0xF7] == frozenset()
    assert all(runtime.EXPERIMENTAL_EFFECTIVE_HOOKS[feature]
               == frozenset((7, 8)) for feature in (0xB7, 0x97, 0xD7))


def test_manual_set_uses_effective_hook_gate_before_transport():
    runtime = _load_module()
    driver = FakeDriver()
    extra = runtime.BoxCfsRuntime.__new__(runtime.BoxCfsRuntime)
    extra._driver = lambda: driver
    meta = {"version": 2, "count": 28, "features": 0xD7}
    extra._probe = lambda drv: meta
    extra._v321_claim_write = lambda info: nullcontext()
    extra._record_meta = lambda info: None
    extra.last_values = {}
    extra.override_mask = 0
    extra.last_error = None

    for name, value in (
        ("rfid_neighbor_detect_delay_ms", 401),
        ("hub_pullback_timeout_ms", 5500),
        ("feeder_forward_speed", 255),
    ):
        try:
            extra.cmd_set(FakeGcmd(name, value))
        except RuntimeError as exc:
            assert "not connected" in str(exc).lower()
        else:
            raise AssertionError("Fake CFS tuning ID was admitted: " + name)
        assert driver.writes == []

    gcmd = FakeGcmd("hub_transition_wait_ms", 3300)
    extra.cmd_set(gcmd)
    assert driver.writes == [(7, 3300)]
    assert "hub_transition_wait_ms" in gcmd.messages[0]

    driver.writes.clear()
    meta["features"] = 0xF7
    try:
        extra.cmd_set(FakeGcmd("hub_transition_wait_ms", 3300))
    except RuntimeError as exc:
        assert "not connected" in str(exc).lower()
    else:
        raise AssertionError("v3.19 ROM-only ID7 SET was admitted")
    assert driver.writes == []


def test_diagnostic_info_explicitly_marks_storage_only():
    runtime = _load_module()
    extra = runtime.BoxCfsRuntime.__new__(runtime.BoxCfsRuntime)
    values = {p.name: p.default or 0 for p in runtime.PARAMETERS}
    result = extra._format_groups(
        {"version": 2, "count": 28, "features": 0xD7, "override_mask": 0},
        values)
    assert "effective stock hooks=7,8" in result
    assert "STORED-ONLY" in result
    assert "HOST PLACEHOLDER" in result


if __name__ == "__main__":
    test_bench_only_ids_never_become_motor_controls()
    test_manual_set_uses_effective_hook_gate_before_transport()
    test_diagnostic_info_explicitly_marks_storage_only()
    print("PASS: experimental capabilities and true manual SET paths")
