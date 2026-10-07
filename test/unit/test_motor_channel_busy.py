"""The protection poll and the motor temperature poll share the nozzle channel.

Seen on the K2 Pro during an 8 h print: twice the protection poll fired while
the temperature poll waited for its answer on axis E, and logged
"transparent transport send already in progress" with a traceback. A busy
channel is not a motor failure: the polls wait for it or read the axis later.
"""

import logging
import pathlib
import struct
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import motor_control as mc  # noqa: E402
from extras import serial_485  # noqa: E402

HEALTHY = struct.pack("<II", 0, 0)
ADDRS = {"x": 0x81, "y": 0x82, "e": 0x83}


def frame(payload, addr, status=0x00, func=mc.FUNC_PROTECTION):
    body = bytes([len(payload) + 3, status, func]) + bytes(payload)
    return (bytes([serial_485.PACK_HEAD, addr]) + body
            + bytes([serial_485.crc8(body)]))


class Transport:
    """Busy for the first ``busy_for`` seconds of the controller clock."""

    def __init__(self, clock, busy_for=0.0, responses=(), raise_busy=False):
        self.clock = clock
        self.busy_until = clock() + busy_for
        self.responses = list(responses)
        self.raise_busy = raise_busy
        self.sent = []

    @property
    def busy(self):
        return self.clock() < self.busy_until

    def send(self, packet, timeout, attempts, response_timeout=None):
        if self.raise_busy:
            raise mc.TransportBusyError(
                "transparent transport send already in progress")
        self.sent.append(bytes(packet))
        return self.responses.pop(0) if self.responses else None


class Controller:
    def __init__(self, busy_for=None, raise_busy=()):
        busy_for = busy_for or {}
        self.mc = mc.MotorControl.__new__(mc.MotorControl)
        self.clock = 50.0
        self.pauses = []

        def pause(waketime):
            self.pauses.append(waketime - self.clock)
            self.clock = waketime
            return waketime

        self.mc.reactor = SimpleNamespace(
            monotonic=lambda: self.clock, pause=pause)
        self.mc.protection_validity = mc.ProtectionValidity(
            mc.ALL_AXES, mc.PROTECTION_STALE_AFTER)
        self.mc.protection_validity.new_session()
        self.mc.motor_error_code = {}
        self.mc.motor_warning_code = {}
        self.mc.motor_fault_detail = {}
        self.mc._handle_active_fault = lambda *a, **k: None
        self.mc._emit_runtime_warning = lambda *a: None
        self.events = []
        self.mc._record_event = lambda kind, axis, **kw: self.events.append(
            (kind, axis))
        self.transports = {}
        targets = {}
        for axis, addr in ADDRS.items():
            transport = Transport(
                lambda: self.clock, busy_for.get(axis, 0.0),
                [frame(HEALTHY, addr)], raise_busy=axis in raise_busy)
            self.transports[axis] = transport
            targets[axis] = mc.MotorAxisTarget(
                axis=axis, addr=addr,
                client=mc.MotorFirmwareClient(transport, framed=True))
        self.mc.axes = SimpleNamespace(
            target=lambda axis: targets[axis],
            check_protection=lambda axes, data, timeout: {
                axis: targets[axis].protection(data=data, timeout=timeout)
                for axis in axes
            },
        )

    def state(self, axis):
        return self.mc.protection_validity.status(axis, self.clock)["state"]


def test_poll_waits_for_a_short_busy_channel():
    ctl = Controller(busy_for={"e": 0.12})
    ctl.mc._process_protection_poll()
    assert all(len(t.sent) == 1 for t in ctl.transports.values())
    assert ctl.pauses and max(ctl.pauses) <= mc.PROTECTION_BUSY_STEP
    assert ctl.state("e") == "current"
    assert ("query_failed", "e") not in ctl.events


def test_poll_skips_an_axis_busy_past_the_wait(caplog):
    ctl = Controller(busy_for={"e": mc.PROTECTION_BUSY_WAIT + 5})
    with caplog.at_level(logging.INFO):
        ctl.mc._process_protection_poll()
    assert ctl.transports["e"].sent == []
    assert len(ctl.transports["x"].sent) == len(ctl.transports["y"].sent) == 1
    # Skipped, not failed: no failure recorded, no traceback.
    assert ("query_failed", "e") not in ctl.events
    assert ctl.mc.protection_validity.axes["e"]["total_errors"] == 0
    assert "skipped axis=e: channel busy" in caplog.text
    assert not any(r.exc_info for r in caplog.records)


def test_busy_rejection_during_the_query_is_logged_without_traceback(caplog):
    ctl = Controller(raise_busy=("e",))
    with caplog.at_level(logging.INFO):
        ctl.mc._process_protection_poll()
    assert "skipped axis=e: channel busy" in caplog.text
    assert not any(r.exc_info for r in caplog.records)
    assert ctl.state("x") == ctl.state("y") == "current"


def test_wait_budget_is_in_the_stale_threshold():
    assert mc.PROTECTION_STALE_AFTER == (
        2 * mc.PROTECTION_POLL_INTERVAL
        + len(mc.ALL_AXES) * (mc.PROTECTION_BUSY_WAIT
                              + mc.DEFAULT_ATTEMPTS * mc.MOTOR_COMMAND_TIMEOUT))


def test_transparent_adapter_reports_busy_and_raises_the_busy_error():
    adapter = mc.NozzleTransparentTransportAdapter.__new__(
        mc.NozzleTransparentTransportAdapter)
    adapter._send_busy = True
    adapter.stats = mc.TransparentTransportStats()
    adapter._now = lambda: 1.0
    assert adapter.busy is True
    with pytest.raises(mc.TransportBusyError, match="already in progress"):
        adapter.send(b"pkt")


def temp_hub(busy):
    clock = [10.0]
    reads = []
    reactor = SimpleNamespace(
        NEVER=float("inf"), register_timer=lambda cb: cb,
        update_timer=lambda *a: None, monotonic=lambda: clock[0])

    def get_value(*a, **k):
        reads.append(a[0])
        return 40.0

    def target(axis):
        return SimpleNamespace(addr=axis, client=SimpleNamespace(
            transport=SimpleNamespace(busy=busy[axis]), get_value=get_value))

    replacement = SimpleNamespace(
        reactor=reactor, is_ready=True, motor_params_init=True,
        axes=SimpleNamespace(target=target))
    hub = mc.Mot2TempSensorHub(replacement)
    hub.start()
    return hub, reads


def test_temperature_poll_reads_a_busy_axis_later():
    busy = {"x": True, "y": False, "e": False}
    hub, reads = temp_hub(busy)
    assert hub._poll(0) == 10.0 + mc.POLL_BUSY_RETRY
    assert reads == []
    assert hub.samples["x"]["read_errors"] == 0
    busy["x"] = False
    assert hub._poll(0) == 10.0 + mc.POLL_INTERVAL
    assert reads == ["x"]
