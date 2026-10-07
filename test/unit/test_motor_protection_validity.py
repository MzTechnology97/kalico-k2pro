"""Validity of cached protection data and confirmation of fault clears.

The transport is a fake; every packet is counted and none leaves the host.
"""

import json
import pathlib
import struct
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import motor_control as mc  # noqa: E402
from extras import serial_485  # noqa: E402

ADDRS = {"x": 0x81, "y": 0x82, "e": 0x83}
HEALTHY = struct.pack("<II", 0, 0)
TRACKING = struct.pack("<II", 1 << 8, 0)
TIMEOUT = None  # a queued None makes the fake transport time out


def answer(axis, payload, status=0x00):
    body = bytes([len(payload) + 3, status, mc.FUNC_PROTECTION]) + payload
    return (
        bytes([serial_485.PACK_HEAD, ADDRS[axis]])
        + body
        + bytes([serial_485.crc8(body)])
    )


def healthy(axis):
    return answer(axis, HEALTHY)


def fault(axis):
    return answer(axis, TRACKING, status=mc.FRAME_STATUS_ERROR)


class FakeTransport:
    def __init__(self):
        self.queue = []
        self.sent = 0

    def send(self, packet, timeout, attempts, response_timeout=None):
        self.sent += 1
        return self.queue.pop(0) if self.queue else None


class Rig:
    def __init__(self):
        self.mc = mc.MotorControl.__new__(mc.MotorControl)
        self.clock = 1000.0
        self.mc.reactor = SimpleNamespace(monotonic=lambda: self.clock)
        self.mc.protection_validity = mc.ProtectionValidity(
            mc.ALL_AXES, mc.PROTECTION_STALE_AFTER
        )
        self.mc.protection_validity.new_session()
        self.mc.motor_error_code = {}
        self.mc.motor_warning_code = {}
        self.mc.motor_fault_detail = {}
        self.transport = {axis: FakeTransport() for axis in ADDRS}
        targets = {
            axis: mc.MotorAxisTarget(
                axis=axis,
                addr=addr,
                client=mc.MotorFirmwareClient(
                    self.transport[axis], framed=True
                ),
            )
            for axis, addr in ADDRS.items()
        }
        self.mc.axes = SimpleNamespace(
            check_protection=lambda axes, data, timeout: {
                axis: targets[axis].protection(data=data, timeout=timeout)
                for axis in axes
            },
            clear_fault_latches=lambda axes, data, timeout: {
                axis: targets[axis].clear_err_warn_code(timeout=timeout)
                for axis in axes
            },
        )

    def queue(self, axis, *responses):
        self.transport[axis].queue.extend(responses)

    def validity(self, axis):
        return self.mc.protection_validity.status(axis, self.clock)

    def query(self, *axes):
        return self.mc.query_protection_status(axes=axes or mc.ALL_AXES)

    def sent(self):
        return sum(t.sent for t in self.transport.values())


@pytest.fixture
def rig():
    return Rig()


def test_never_queried_is_unknown(rig):
    status = rig.validity("x")
    assert status["state"] == "unknown" and status["valid"] is False
    assert status["last_success"] is None and status["query_age"] is None


def test_valid_answer_is_current(rig):
    rig.queue("x", healthy("x"))
    rig.query("x")
    status = rig.validity("x")
    assert status["state"] == "current" and status["valid"] is True
    assert status["source"] == "query"


def test_new_session_makes_old_data_unknown(rig):
    rig.queue("x", healthy("x"))
    rig.query("x")
    rig.mc.protection_validity.new_session()
    status = rig.validity("x")
    assert status["state"] == "unknown"
    assert status["last_success"] == 1000.0
    assert status["session"] == 1 and status["current_session"] == 2


def test_stale_threshold(rig):
    rig.queue("x", healthy("x"))
    rig.query("x")
    assert mc.PROTECTION_STALE_AFTER == 129.0
    rig.clock += mc.PROTECTION_STALE_AFTER
    assert rig.validity("x")["state"] == "current"
    rig.clock += 1
    assert rig.validity("x")["state"] == "stale"


def test_failed_query_after_healthy_keeps_data_but_not_current(rig):
    rig.queue("x", healthy("x"))
    rig.query("x")
    rig.queue("x", TIMEOUT, TIMEOUT)
    with pytest.raises(RuntimeError):
        rig.query("x")
    status = rig.validity("x")
    assert status["state"] == "query_failed" and status["valid"] is False
    assert status["last_success"] == 1000.0
    assert status["total_errors"] == 1 and status["consecutive_errors"] == 1
    assert "no response" in status["last_error"]


def test_failed_query_after_fault_keeps_the_fault(rig):
    rig.queue("x", fault("x"))
    rig.query("x")
    rig.queue("x", TIMEOUT, TIMEOUT)
    with pytest.raises(RuntimeError):
        rig.query("x")
    assert rig.mc.motor_fault_detail["x"]["error_code"] == 256
    assert rig.validity("x")["state"] == "query_failed"
    assert rig.validity("x")["last_confirmed_fault"]["error_code"] == 256


def test_clear_without_ack_is_pending_until_a_valid_query(rig):
    rig.queue("x", fault("x"))
    rig.query("x")
    rig.mc.clear_fault_latches(axes=("x",))
    status = rig.validity("x")
    assert status["state"] == "clear_pending" and status["valid"] is False
    assert status["clear"]["result"] == "pending"
    # The confirmed fault is not erased by an unacknowledged command.
    assert rig.mc.motor_fault_detail["x"]["error_code"] == 256
    rig.clock += 1
    rig.queue("x", healthy("x"))
    rig.query("x")
    status = rig.validity("x")
    assert status["state"] == "current"
    assert status["clear"]["result"] == "confirmed"
    assert status["clear"]["verified_at"] == 1001.0
    assert "x" not in rig.mc.motor_fault_detail
    assert status["last_confirmed_fault"]["error_code"] == 256


def test_clear_with_persistent_fault(rig):
    rig.queue("x", fault("x"))
    rig.query("x")
    rig.mc.clear_fault_latches(axes=("x",))
    rig.queue("x", fault("x"))
    rig.query("x")
    status = rig.validity("x")
    assert status["clear"]["result"] == "persistent"
    assert status["state"] == "current"
    assert rig.mc.motor_fault_detail["x"]["error_code"] == 256


def test_clear_recheck_timeout_stays_pending(rig):
    rig.queue("x", fault("x"))
    rig.query("x")
    rig.mc.clear_fault_latches(axes=("x",))
    rig.queue("x", TIMEOUT, TIMEOUT)
    with pytest.raises(RuntimeError):
        rig.query("x")
    status = rig.validity("x")
    assert status["state"] == "clear_pending"
    assert status["clear"]["result"] == "pending"
    assert status["clear"]["recheck_errors"] == 1
    assert "no response" in status["clear"]["last_recheck_error"]


def test_unverified_recheck_is_not_a_confirmed_clear(rig):
    rig.queue("x", fault("x"))
    rig.query("x")
    rig.mc.clear_fault_latches(axes=("x",))
    rig.queue("x", answer("x", HEALTHY, status=mc.FRAME_STATUS_ERROR))
    with pytest.raises(mc.ProtectionResponseError):
        rig.query("x")
    assert rig.validity("x")["clear"]["result"] == "pending"


def test_new_session_abandons_a_pending_clear(rig):
    rig.queue("x", fault("x"))
    rig.query("x")
    rig.mc.clear_fault_latches(axes=("x",))
    rig.mc.protection_validity.new_session()
    status = rig.validity("x")
    assert status["clear"]["result"] == "abandoned"
    assert status["state"] == "unknown"


def test_group_query_keeps_answers_of_axes_that_replied(rig):
    rig.queue("x", fault("x"))
    rig.queue("y", TIMEOUT, TIMEOUT)
    rig.queue("e", healthy("e"))
    with pytest.raises(mc.ProtectionQueryError) as exc:
        rig.query()
    assert set(exc.value.partial) == {"x", "e"}
    assert set(exc.value.errors) == {"y"}
    assert rig.validity("x")["state"] == "current"
    assert rig.validity("e")["state"] == "current"
    assert rig.validity("y")["state"] == "query_failed"
    assert rig.mc.motor_fault_detail["x"]["error_code"] == 256


def test_single_axis_failure_keeps_its_original_exception(rig):
    rig.queue("y", answer("y", bytes(4)))
    with pytest.raises(mc.ProtectionResponseError):
        rig.query("y")


def test_status_is_a_snapshot_and_does_no_io(rig):
    rig.queue("x", fault("x"))
    rig.query("x")
    rig.mc.clear_fault_latches(axes=("x",))
    before = rig.sent()
    status = rig.validity("x")
    status["clear"]["result"] = "tampered"
    status["last_confirmed_fault"]["error_code"] = 0
    again = rig.validity("x")
    assert again["clear"]["result"] == "pending"
    assert again["last_confirmed_fault"]["error_code"] == 256
    json.dumps(again, allow_nan=False)
    assert rig.sent() == before


def test_periodic_poll_records_its_source(rig):
    rig.mc._handle_active_fault = lambda *a, **k: None
    rig.mc._emit_runtime_warning = lambda *a, **k: None
    for axis in ADDRS:
        rig.queue(axis, healthy(axis))
    rig.mc._process_protection_poll()
    assert {rig.validity(a)["source"] for a in ADDRS} == {"periodic_poll"}
