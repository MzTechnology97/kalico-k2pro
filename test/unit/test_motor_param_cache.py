"""Cached override/calibration values and the explicit MOTOR_STATUS refresh.

Fake axes count every read; no packet leaves the host.
"""

import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import motor_control as mc  # noqa: E402

KP = "controller_pos_loop_pid_param_kp"
KI = "controller_spd_loop_pid_param_ki"
TRACK = "protection_param_prt_track_max_err"
FILTER = "controller_cur_filter_param_fc"


def action(op, key, target=1.0, read=1.0, wrote=None, param_id=10):
    return {
        "axis": "x",
        "key": key,
        "param_id": param_id,
        "op": op,
        "override_value": target,
        "current_value": read,
        "wrote_value": wrote,
    }


def cal_entries(axis, offset=1.2, err_deg=0.4):
    def param(key):
        return SimpleNamespace(key=key)

    return [
        mc.calibration_param_result(
            axis, param("param_elec_offset"), 9, offset
        ),
        mc.calibration_param_result(
            axis, param("param_elec_offset_err_deg"), 25, err_deg
        ),
    ]


# --- MotorParamCache ----------------------------------------------------------


def test_startup_actions_become_readbacks():
    cache = mc.MotorParamCache(mc.ALL_AXES)
    cache.new_session()
    cache.record_override_actions(
        "x",
        [
            action("read_ok", KP, target=300.0, read=300.0),
            action("write", KI, target=5.5, read=5.0, wrote=5.5),
            action("write", TRACK, target=0.3, read=0.2, wrote=0.3),
            action("verify_mismatch", TRACK, target=0.3, read=0.25),
            action("read_error", FILTER, target=900, read=None, wrote="T/O"),
            action("apply_error", "__apply__", param_id=-1, wrote="no ack"),
        ],
        "startup",
        5.0,
    )
    entries = cache.overrides["x"]
    assert entries[KP]["match"] is True and entries[KP]["phase"] == "read"
    assert entries[KI] == {
        "target": 5.5,
        "read": 5.5,
        "match": True,
        "error": None,
        "phase": "verify",
        "source": "startup",
        "at": 5.0,
        "session": 1,
    }
    assert entries[TRACK]["match"] is False and entries[TRACK]["read"] == 0.25
    assert (
        entries[FILTER]["match"] is None and entries[FILTER]["error"] == "T/O"
    )
    assert cache.apply_errors["x"]["error"] == "no ack"
    assert "__apply__" not in entries


def test_write_error_records_the_value_read_before():
    cache = mc.MotorParamCache(mc.ALL_AXES)
    cache.record_override_actions(
        "y",
        [action("write_error", KP, target=320.0, read=300.0, wrote="T/O")],
        "startup",
        1.0,
    )
    entry = cache.overrides["y"][KP]
    assert entry["match"] is False and entry["read"] == 300.0
    assert entry["phase"] == "write" and entry["error"] == "T/O"


def test_calibration_is_cached_with_its_judgement():
    cache = mc.MotorParamCache(mc.ALL_AXES)
    cache.record_calibration("e", cal_entries("e", offset=0.0), "startup", 2.0)
    snap = cache.snapshot(12.0)["axes"]["e"]["calibration"]
    assert snap["summary"]["state"] == "suspect"
    assert snap["age"] == 10.0 and snap["source"] == "startup"


def test_new_session_keeps_values_but_marks_them_old():
    cache = mc.MotorParamCache(mc.ALL_AXES)
    cache.new_session()
    cache.record_override(
        "x",
        KP,
        target=300.0,
        read=300.0,
        match=True,
        error=None,
        phase="read",
        source="manual",
        now=1.0,
    )
    cache.new_session()
    entry = cache.snapshot(2.0)["axes"]["x"]["overrides"][KP]
    assert entry["current"] is False and entry["read"] == 300.0


def test_snapshot_is_independent_and_nan_safe():
    cache = mc.MotorParamCache(mc.ALL_AXES)
    cache.record_calibration(
        "x", cal_entries("x", offset=float("nan")), "refresh", 1.0
    )
    cache.record_override(
        "x",
        KP,
        target=300.0,
        read=float("inf"),
        match=False,
        error=None,
        phase="read",
        source="manual",
        now=1.0,
    )
    snap = cache.snapshot(1.0)
    json.dumps(snap, allow_nan=False)
    assert snap["axes"]["x"]["overrides"][KP]["read"] is None
    snap["axes"]["x"]["overrides"][KP]["target"] = 0
    assert cache.overrides["x"][KP]["target"] == 300.0


# --- MOTOR_STATUS cache and refresh -------------------------------------------


class Rig:
    def __init__(self, read_seconds=0.0, fail=()):
        self.mc = mc.MotorControl.__new__(mc.MotorControl)
        self.clock = 100.0
        self.reads = []
        self.mc.reactor = SimpleNamespace(monotonic=lambda: self.clock)
        self.mc.param_cache = mc.MotorParamCache(mc.ALL_AXES)
        self.mc.param_cache.new_session()

        def read_calibration_params(axis, timeout):
            self.reads.append(axis)
            self.clock += read_seconds
            if axis in fail:
                raise TimeoutError("no response")
            return cal_entries(axis)

        self.mc.axes = SimpleNamespace(
            read_calibration_params=read_calibration_params
        )


def test_cache_mode_never_reads():
    rig = Rig()
    status = rig.mc._cached_calibration_status()
    assert rig.reads == []
    assert all("not read yet" in entry["skipped"] for entry in status)
    line = rig.mc._format_calibration_status_line(status[0])
    assert "MOTOR_STATUS REFRESH=1" in line


def test_refresh_reads_only_calibration_and_fills_the_cache():
    rig = Rig()
    fresh = rig.mc._collect_calibration_status()
    assert rig.reads == ["x", "y", "e"]
    assert fresh[0]["values"]["param_elec_offset"]["value"] == 1.2
    rig.clock += 30
    cached = rig.mc._cached_calibration_status()
    assert rig.reads == ["x", "y", "e"]
    assert cached[1]["cached"] is True and cached[1]["age"] == 30.0
    assert cached[1]["source"] == "refresh" and cached[1]["current"] is True
    line = rig.mc._format_calibration_status_line(cached[1])
    assert "offset=" in line and "cached 30s ago (refresh)" in line


def test_refresh_budget_skips_remaining_axes():
    rig = Rig(read_seconds=4.0)
    result = rig.mc._collect_calibration_status(budget=6.0)
    assert rig.reads == ["x", "y"]
    assert "budget" in result[2]["skipped"]


def test_refresh_read_failure_is_kept_as_an_error():
    rig = Rig(fail=("y",))
    rig.mc._collect_calibration_status()
    cached = rig.mc._cached_calibration_status()
    assert "no response" in cached[1]["error"]
    assert "error" not in cached[0]
    summary = rig.mc.param_cache.calibration["y"]["summary"]
    assert summary["state"] == "read_failed"


def test_cache_from_a_previous_startup_is_labelled():
    rig = Rig()
    rig.mc._collect_calibration_status()
    rig.mc.param_cache.new_session()
    entry = rig.mc._cached_calibration_status()[0]
    assert entry["current"] is False
    assert "previous startup" in rig.mc._format_calibration_status_line(entry)


class FakeGcmd:
    def __init__(self, **params):
        self.params = params
        self.lines = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))

    def respond_info(self, line):
        self.lines.append(line)


@pytest.mark.parametrize(
    "params, refresh", [({}, False), ({"REFRESH": "1"}, True)]
)
def test_motor_status_uses_the_cache_unless_refresh(params, refresh):
    rig = Rig()
    seen = []
    rig.mc._build_status_snapshot = lambda refresh=False: (
        seen.append(refresh) or {"calibration_source": "x"}
    )
    rig.mc.cmd_MOTOR_STATUS(FakeGcmd(VERBOSE="1", **params))
    assert seen == [refresh]
