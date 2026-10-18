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
        assert minval is None or self.value >= minval
        assert maxval is None or self.value <= maxval
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
    assert set(runtime.EXPERIMENTAL_EFFECTIVE_HOOKS) == {0xF7, 0xB7, 0x97, 0xD7}
    for feature, allowed in runtime.EXPERIMENTAL_EFFECTIVE_HOOKS.items():
        for param_id in range(28):
            try:
                runtime.BoxCfsRuntime._require_effective_hook(
                    {"version": 2, "features": feature}, param_id
                )
            except runtime.CfsRuntimeUnsupported:
                assert param_id not in allowed
            else:
                assert param_id in allowed
    assert runtime.EXPERIMENTAL_EFFECTIVE_HOOKS[0xF7] == frozenset()
    assert all(
        runtime.EXPERIMENTAL_EFFECTIVE_HOOKS[feature] == frozenset((7, 8))
        for feature in (0xB7, 0x97, 0xD7)
    )


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
    extra.identity = None  # v3.22 0xD7: no 0x7A identity
    values = {p.name: p.default or 0 for p in runtime.PARAMETERS}
    result = extra._format_groups(
        {"version": 2, "count": 28, "features": 0xD7, "override_mask": 0},
        values,
    )
    assert "effective stock hooks=7,8" in result
    assert "STORED-ONLY" in result
    assert "HOST PLACEHOLDER" in result


def test_reference_cfg_does_not_enable_fictitious_knobs():
    import re
    from pathlib import Path

    module = _load_module()
    root = Path(__file__).resolve().parents[1]
    # The distributed templates now document CFG6 (v323-cfg6, 0xE3).
    # Only its 23 actual firmware callsites are advertised as writable.
    # IDs 0/4 (GPIO feeders) and 10/25/26 (dead stock code) remain GET-only.
    effective = {
        p.name
        for p in module.K2OH_PARAMETERS
        if p.param_id in module.K2OH_CFG6_WRITABLE_IDS
    }
    reference_only = {p.name for p in module.K2OH_PARAMETERS} - effective

    def runtime_block(path):
        text = (root / path).read_text()
        start = re.search(
            r"^# (?:K2OH CFG6|CFS runtime (?:configuration|parameters))",
            text,
            re.M,
        )
        end = text.index("# CFS event notifications", start.start())
        return text[start.start() : end]

    def check_listing(block):
        # Effective: "#name: value  # IDn ...", within the catalogue limits.
        listed = re.findall(r"^#?([a-z0-9_]+): (\S+)\s+# ID(\d+)", block, re.M)
        assert {name for name, _, _ in listed} == effective
        for name, value, pid in listed:
            spec = module.K2OH_BY_NAME[name]
            assert spec.param_id == int(pid), name
            if spec.value_type != "bool":
                assert spec.minval <= int(value) <= spec.maxval, name
        # The others: "# name: value  # IDn ...", never in active form.
        others = set(re.findall(r"^# ([a-z0-9_]+): ", block, re.M))
        assert others == reference_only

    # Distributed profile: nothing enabled.
    block = runtime_block("config/k2/macros/box.cfg")
    check_listing(block)
    assert "#[box_cfs_runtime]" in block
    for line in block.splitlines():
        assert not line.strip() or line.lstrip().startswith("#"), line

    # Reference machine: only effective options may be active.
    block = runtime_block("config/k2/reference/k2pro-cm5/macros/box.cfg")
    check_listing(block)
    active = dict(re.findall(r"^([a-z0-9_]+):\s*(\S+)", block, re.M))
    assert active.pop("auto_apply", "false").lower() in ("true", "false")
    active.pop("address", None)
    assert set(active) <= effective, set(active) - effective


if __name__ == "__main__":
    test_bench_only_ids_never_become_motor_controls()
    test_manual_set_uses_effective_hook_gate_before_transport()
    test_diagnostic_info_explicitly_marks_storage_only()
    test_reference_cfg_does_not_enable_fictitious_knobs()
    print("PASS: capabilities, manual SET guards, and config catalog")
