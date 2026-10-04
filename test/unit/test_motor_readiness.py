"""Motor readiness: reachable, overrides verified, calibration read.

No calibration is run and no packet is sent: the axes object is a fake that
returns prepared apply/calibration results.
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
TRACK = "protection_param_prt_track_max_err"
REPORT = "protection_param_protect_report"
OTHER = "some_future_param"


def action(op, key=KP, target=300.0, read=300.0, wrote=None, param_id=10):
    return {
        "axis": "x",
        "key": key,
        "param_id": param_id,
        "op": op,
        "override_value": target,
        "current_value": read,
        "wrote_value": wrote,
    }


def calibration(axis="x", offset=1.2, err_deg=0.4, offset_error=None):
    def param(key):
        return SimpleNamespace(key=key)

    first = (
        mc.calibration_param_result(
            axis, param("param_elec_offset"), 9, error=offset_error
        )
        if offset_error
        else mc.calibration_param_result(
            axis, param("param_elec_offset"), 9, offset
        )
    )
    second = mc.calibration_param_result(
        axis, param("param_elec_offset_err_deg"), 25, err_deg
    )
    return [first, second]


# --- classification and summaries ---------------------------------------------


@pytest.mark.parametrize(
    "key, kind",
    [
        (KP, "critical"),
        ("controller_spd_loop_pid_param_ki", "critical"),
        ("controller_cur_filter_param_fc", "critical"),
        ("controller_leso_param_wp", "critical"),
        (TRACK, "critical"),
        ("protection_param_mcu_temp_max", "critical"),
        (REPORT, "diagnostic"),
        ("protection_param_warning_code_mask", "diagnostic"),
        (OTHER, "unclassified"),
    ],
)
def test_override_classification(key, kind):
    assert mc.classify_override_key(key) == kind


def test_all_read_back_is_verified():
    summary = mc.summarize_override_actions(
        [action("read_ok"), action("write", key=TRACK)]
    )
    assert summary["state"] == "verified"
    assert summary["checked"] == 2 and summary["writes"] == 1


def test_no_overrides_is_unknown():
    summary = mc.summarize_override_actions(
        [action("skip_no_value", target=None)]
    )
    assert summary["state"] == "unknown" and summary["checked"] == 0


@pytest.mark.parametrize(
    "op, confirmed",
    [
        ("read_error", False),
        ("verify_error", False),
        ("write_error", True),
        ("verify_mismatch", True),
    ],
)
def test_critical_problems(op, confirmed):
    summary = mc.summarize_override_actions([action(op, read=250.0)])
    assert summary["state"] == "degraded"
    assert summary["critical_problems"][0]["op"] == op
    assert summary["confirmed_critical_mismatch"] is confirmed


def test_apply_error_is_degraded_but_not_confirmed():
    summary = mc.summarize_override_actions(
        [action("apply_error", key="__apply__", param_id=-1)]
    )
    assert summary["state"] == "degraded"
    assert summary["confirmed_critical_mismatch"] is False


def test_diagnostic_and_unclassified_problems_are_separate():
    summary = mc.summarize_override_actions(
        [action("verify_mismatch", key=REPORT), action("read_error", key=OTHER)]
    )
    assert [p["key"] for p in summary["diagnostic_problems"]] == [REPORT]
    assert [p["key"] for p in summary["unclassified_problems"]] == [OTHER]
    assert summary["confirmed_critical_mismatch"] is False


def test_calibration_offset_near_zero_is_suspect():
    entries = calibration(offset=0.0)
    assert entries[0]["near_zero"] is True and entries[0]["judged"] is True
    assert mc.summarize_calibration(entries)["state"] == "suspect"


def test_small_calibration_error_is_a_good_result_not_a_warning():
    entries = calibration(offset=1.2, err_deg=0.01)
    assert entries[1]["near_zero"] is False and entries[1]["judged"] is False
    summary = mc.summarize_calibration(entries)
    assert summary["state"] == "read"
    assert summary["values"]["param_elec_offset_err_deg"] == 0.01


def test_calibration_read_failure():
    summary = mc.summarize_calibration(calibration(offset_error="timeout"))
    assert summary["state"] == "read_failed"
    assert summary["errors"] == [
        {"key": "param_elec_offset", "error": "timeout"}
    ]


def test_no_calibration_is_unknown():
    assert mc.summarize_calibration([])["state"] == "unknown"


# --- the startup apply step ---------------------------------------------------


class Rig:
    def __init__(self, policy="warn", actions=None, cal=None, raises=None):
        self.mc = mc.MotorControl.__new__(mc.MotorControl)
        self.messages = []
        self.mc.gcode = SimpleNamespace(respond_raw=self.messages.append)
        self.mc.override_policy = policy
        self.mc.axis_readiness = {
            axis: mc.blank_axis_readiness() for axis in mc.ALL_AXES
        }
        self.mc.is_ready = True
        self.mc.motor_params_init = True
        actions = actions or {}
        cal = cal or {}
        raises = raises or {}

        def apply_overrides(axis, timeout):
            if axis in raises:
                raise raises[axis]
            return SimpleNamespace(
                actions=actions.get(axis, [action("read_ok")])
            )

        self.mc.axes = SimpleNamespace(
            apply_overrides=apply_overrides,
            read_calibration_params=lambda axis, timeout: cal.get(
                axis, calibration(axis)
            ),
        )

    def run(self):
        return self.mc._startup_apply_axis_set_overrides(
            mc.ALL_AXES, override_timeout=1.0, calibration_timeout=1.0
        )

    def status(self):
        return self.mc._readiness_status()


def test_clean_startup_is_configured_and_operational():
    rig = Rig()
    rig.run()
    status = rig.status()["x"]
    assert status["configured"] is True
    assert status["calibration_verified"] is True
    assert status["operational"] is True and status["degraded"] is False
    assert status["reasons"] == []


def test_warn_policy_keeps_running_but_reports_degraded():
    rig = Rig(actions={"y": [action("verify_mismatch", read=250.0)]})
    rig.run()
    status = rig.status()["y"]
    assert status["operational"] is True
    assert status["configured"] is False and status["degraded"] is True
    assert "critical override not verified" in status["reasons"]
    assert status["parameters"]["confirmed_critical_mismatch"] is True


def test_block_policy_stops_on_a_confirmed_critical_mismatch():
    rig = Rig(
        policy="block", actions={"y": [action("verify_mismatch", read=250.0)]}
    )
    with pytest.raises(RuntimeError, match="override_policy: block"):
        rig.run()
    assert rig.mc.axis_readiness["y"]["blocked"] is True
    assert rig.status()["y"]["operational"] is False


def test_block_policy_does_not_stop_on_an_unconfirmed_read_error():
    rig = Rig(policy="block", actions={"x": [action("read_error", read=None)]})
    rig.run()
    status = rig.status()["x"]
    assert status["operational"] is True and status["degraded"] is True


def test_block_policy_ignores_diagnostic_mismatch():
    rig = Rig(
        policy="block",
        actions={"x": [action("verify_mismatch", key=REPORT, read=1)]},
    )
    rig.run()
    assert rig.status()["x"]["blocked"] is False


def test_apply_exception_marks_parameters_failed_and_raises():
    rig = Rig(raises={"e": TimeoutError("no response")})
    with pytest.raises(RuntimeError, match="startup runtime override failed"):
        rig.run()
    record = rig.mc.axis_readiness["e"]
    assert record["parameters"]["state"] == "failed"
    assert "overrides could not be applied" in record["reasons"]


def test_suspect_calibration_is_reported_not_blocking():
    rig = Rig(policy="block", cal={"x": calibration(offset=0.0)})
    rig.run()
    status = rig.status()["x"]
    assert status["calibration"]["state"] == "suspect"
    assert status["operational"] is True and status["degraded"] is True
    assert "calibration offset near zero" in status["reasons"]


def test_failed_calibration_read_is_reported():
    rig = Rig(cal={"e": calibration("e", offset_error="timeout")})
    rig.run()
    status = rig.status()["e"]
    assert status["calibration_verified"] is False
    assert "calibration not read" in status["reasons"]


def test_status_is_serializable_and_independent():
    rig = Rig(cal={"x": calibration(offset=float("nan"))})
    rig.run()
    status = rig.status()
    json.dumps(status, allow_nan=False)
    status["x"]["parameters"]["state"] = "tampered"
    assert rig.status()["x"]["parameters"]["state"] == "verified"


def test_not_ready_is_never_operational():
    rig = Rig()
    rig.run()
    rig.mc.is_ready = False
    status = rig.status()["x"]
    assert status["operational"] is False and status["degraded"] is False


def test_retry_resets_readiness():
    rig = Rig(actions={"y": [action("verify_mismatch", read=250.0)]})
    rig.run()
    m = rig.mc
    m.reactor = SimpleNamespace(
        update_timer=lambda *a: None, NEVER=float("inf")
    )
    m.temp_sensors = SimpleNamespace(stop=lambda: None)
    m._fault_cleanup_timer = m._protection_poll_timer = object()
    m.protection_validity = mc.ProtectionValidity(mc.ALL_AXES, 126.0)
    m._reset_startup_state(reset_retry_state=False)
    assert m.axis_readiness["y"] == mc.blank_axis_readiness()
    assert rig.status()["y"]["operational"] is False


@pytest.mark.parametrize("policy", ["warn", "block"])
def test_tuned_config_value_is_written_and_verified(policy):
    # Tuning: a new motor_control.cfg value differs from the board, the
    # override writes it, the readback matches. Nothing warns or blocks.
    tuned = [
        action("write", key=KP, target=320.0, read=300.0, wrote=320.0),
        action("write", key=TRACK, target=0.25, read=0.3, wrote=0.25),
    ]
    rig = Rig(policy=policy, actions={"x": tuned})
    rig.run()
    status = rig.status()["x"]
    assert status["configured"] is True and status["operational"] is True
    assert status["parameters"]["writes"] == 2
    assert rig.messages == []
