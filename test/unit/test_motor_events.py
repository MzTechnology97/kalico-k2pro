"""Bounded motor event history through the real fault paths.

Fake transport, printer and G-code objects; nothing is sent.
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
ENCODER = struct.pack("<II", 1 << 1, 0)


def answer(axis, payload, status=0x00):
    body = bytes([len(payload) + 3, status, mc.FUNC_PROTECTION]) + payload
    return (
        bytes([serial_485.PACK_HEAD, ADDRS[axis]])
        + body
        + bytes([serial_485.crc8(body)])
    )


def fault(axis, payload=TRACKING):
    return answer(axis, payload, status=mc.FRAME_STATUS_ERROR)


class FakeTransport:
    def __init__(self):
        self.queue = []
        self.sent = 0

    def send(self, packet, timeout, attempts, response_timeout=None):
        self.sent += 1
        return self.queue.pop(0) if self.queue else None


class Rig:
    def __init__(self, capacity=50, print_state="printing"):
        m = self.mc = mc.MotorControl.__new__(mc.MotorControl)
        self.clock = 500.0
        self.raw = []
        self.shutdowns = []
        self.pauses = []
        m.reactor = SimpleNamespace(
            monotonic=lambda: self.clock,
            register_async_callback=lambda cb: None,
        )
        m.gcode = SimpleNamespace(
            respond_raw=self.raw.append,
            respond_info=self.raw.append,
            run_script=lambda s: None,
        )
        self.print_stats = SimpleNamespace(state=print_state)
        stepper_enable = SimpleNamespace(motor_debug_enable=lambda *a: None)
        m.printer = SimpleNamespace(
            lookup_object=lambda name, default=None: {
                "print_stats": self.print_stats,
                "stepper_enable": stepper_enable,
            }.get(name, default),
            invoke_shutdown=self.shutdowns.append,
        )
        m.pause_resume = SimpleNamespace(
            pause_command_sent=False,
            send_pause_command=lambda: self.pauses.append(True),
        )
        m._startup_started = True
        m._startup_complete = True
        m.is_homing = False
        m._is_homing_context_active = lambda: self.homing
        self.homing = False
        m.protection_validity = mc.ProtectionValidity(mc.ALL_AXES, 126.0)
        m.protection_validity.new_session()
        m.event_log = mc.MotorEventLog(capacity, clock=lambda: 1.7e9)
        m.motor_error_code = {}
        m.motor_warning_code = {}
        m.motor_fault_detail = {}
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
        m.axes = SimpleNamespace(
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

    def types(self):
        return [(e["type"], e["axis"]) for e in self.mc.event_log.snapshot()]

    def sent(self):
        return sum(t.sent for t in self.transport.values())


@pytest.fixture
def rig():
    return Rig()


def test_unchanged_fault_seen_by_every_poll_is_one_event(rig):
    for _ in range(4):
        rig.queue("x", fault("x"))
        rig.mc.query_protection_status(axes=("x",), source="periodic_poll")
        rig.clock += 60
    events = rig.mc.event_log.snapshot()
    assert len(events) == 1
    assert events[0]["count"] == 4
    assert events[0]["last_at"] == 680.0 and events[0]["at"] == 500.0
    assert events[0]["error_labels"] == ["excessive position tracking error"]


def test_a_different_code_is_a_new_event(rig):
    rig.queue("x", fault("x"), fault("x", ENCODER))
    rig.mc.query_protection_status(axes=("x",))
    rig.mc.query_protection_status(axes=("x",))
    assert len(rig.mc.event_log.snapshot()) == 2


def test_extruder_fault_pause_clear_and_confirmed_recheck(rig):
    rig.queue("e", fault("e"))
    detail = rig.mc.check_protection_code(axis="e", source="stall_pin:1")
    rig.queue("e", None, answer("e", HEALTHY))  # clear (no ACK), recheck
    rig.mc._handle_active_fault(detail, rig.clock, during_homing=False)
    assert rig.types() == [
        ("fault_detected", "e"),
        ("policy_recover", "e"),
        ("pause_requested", "e"),
        ("clear_requested", "e"),
        ("clear_confirmed", "e"),
    ]
    assert rig.pauses == [True]
    events = rig.mc.event_log.snapshot()
    assert events[3]["result"] == "not_acknowledged"
    assert events[0]["context"] == "printing"
    # The original reason is still there after the recovery.
    assert events[0]["error_labels"] == ["excessive position tracking error"]


def test_extruder_clear_with_persistent_fault(rig):
    rig.queue("e", fault("e"))
    detail = rig.mc.check_protection_code(axis="e", source="stall_pin:1")
    rig.queue("e", None, fault("e"))
    rig.mc._handle_active_fault(detail, rig.clock, during_homing=False)
    assert rig.types()[-1] == ("clear_persistent", "e")


def test_extruder_recheck_timeout(rig):
    rig.queue("e", fault("e"))
    detail = rig.mc.check_protection_code(axis="e", source="stall_pin:1")
    rig.queue("e", None)  # clear; the recheck then times out
    rig.mc._handle_active_fault(detail, rig.clock, during_homing=False)
    kinds = [kind for kind, _axis in rig.types()]
    assert kinds[-2:] == ["clear_requested", "query_failed"]
    assert "clear_confirmed" not in kinds


def test_xy_fault_during_homing(rig):
    rig.homing = True
    rig.mc._request_homing_fault_abort = lambda detail: True
    rig.queue("x", fault("x"))
    detail = rig.mc.check_protection_code(axis="x", source="stall_pin:1")
    rig.mc._handle_active_fault(detail, rig.clock, during_homing=True)
    assert rig.types() == [
        ("fault_detected", "x"),
        ("policy_homing_abort", "x"),
    ]
    assert rig.mc.event_log.snapshot()[0]["context"] == "homing"
    assert rig.shutdowns == []


def test_xy_fault_while_printing_records_the_shutdown(rig):
    rig.queue("y", fault("y"))
    detail = rig.mc.check_protection_code(axis="y", source="periodic_poll")
    rig.mc._handle_active_fault(detail, rig.clock, during_homing=False)
    assert rig.types()[-1] == ("policy_shutdown", "y")
    assert len(rig.shutdowns) == 1


def test_repeated_warning_is_counted(rig):
    warn = answer("e", struct.pack("<II", 0, 1 << 2), status=0x04)
    for _ in range(3):
        rig.queue("e", warn)
        rig.mc.query_protection_status(axes=("e",))
    events = rig.mc.event_log.snapshot()
    assert [(e["type"], e["count"]) for e in events] == [
        ("warning_detected", 3)
    ]
    assert events[0]["warning_labels"] == ["MCU overheating"]


def test_timeout_is_recorded_without_raw_bytes(rig):
    rig.queue("x", answer("x", bytes(4)))
    with pytest.raises(mc.ProtectionResponseError):
        rig.mc.query_protection_status(axes=("x",))
    event = rig.mc.event_log.snapshot()[0]
    assert event["type"] == "query_failed"
    assert "payload_hex" in event["error"] and "<hex>" in event["error"]


def test_scrub_error_text():
    text = "short response rx=f781060c0b00 crc 0x1f"
    assert mc.scrub_error_text(text) == "short response rx=<hex> crc 0x1f"


def test_overflow_keeps_the_newest_events():
    log = mc.MotorEventLog(10)
    for n in range(15):
        log.record("fault_detected", "x", float(n), error_code=1 << (n % 14))
    events = log.snapshot()
    assert len(events) == 10
    assert events[0]["seq"] == 6 and events[-1]["seq"] == 15


def test_new_session_starts_a_new_dedupe(rig):
    rig.queue("x", fault("x"), fault("x"))
    rig.mc.query_protection_status(axes=("x",))
    rig.mc.event_log.new_session()
    rig.mc.query_protection_status(axes=("x",))
    events = rig.mc.event_log.snapshot()
    assert [e["session"] for e in events] == [0, 1]


def test_snapshot_is_independent_serializable_and_free(rig):
    rig.queue("x", fault("x"))
    rig.mc.query_protection_status(axes=("x",))
    before = rig.sent()
    snap = rig.mc.event_log.snapshot()
    snap[0]["error_labels"].append("tampered")
    snap[0]["count"] = 99
    again = rig.mc.event_log.snapshot()
    assert again[0]["count"] == 1
    assert again[0]["error_labels"] == ["excessive position tracking error"]
    json.dumps(again, allow_nan=False)
    assert rig.mc.event_log.snapshot(0) == []
    assert rig.sent() == before


def test_history_failure_never_breaks_the_fault_path(rig):
    rig.mc.event_log = None  # recording raises inside _record_event
    rig.queue("x", fault("x"))
    result = rig.mc.query_protection_status(axes=("x",))
    assert result["x"]["has_error"] is True


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


def test_motor_events_command(rig):
    rig.queue("x", fault("x"), fault("x"))
    rig.mc.query_protection_status(axes=("x",))
    rig.mc.query_protection_status(axes=("x",))
    gcmd = FakeGcmd()
    rig.mc.cmd_MOTOR_EVENTS(gcmd)
    assert gcmd.lines[0].startswith("#1 fault_detected X")
    assert " x2 " in gcmd.lines[0]
    verbose = FakeGcmd(VERBOSE="1")
    rig.mc.cmd_MOTOR_EVENTS(verbose)
    payload = json.loads(verbose.lines[0][len("MOTOR_EVENTS ") :])
    assert payload["events"][0]["count"] == 2
