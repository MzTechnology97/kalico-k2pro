"""Counters and latency of the E motor transport through the Nozzle MCU.

The Nozzle MCU, its serial link and the reactor are fakes driven by a
script: respond, time out, answer late, fail to send or answer with the
wrong payload type.
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import motor_control as mc  # noqa: E402


class FakeCompletion:
    def __init__(self, reactor):
        self.reactor = reactor
        self.result = None

    def wait(self, waketime):
        if self.result is None:
            self.reactor.clock = max(self.reactor.clock, waketime)
        return self.result


class FakeReactor:
    NEVER = 9e99

    def __init__(self):
        self.clock = 10.0

    def monotonic(self):
        return self.clock

    def completion(self):
        return FakeCompletion(self)

    def async_complete(self, completion, params):
        completion.result = params


class FakeNozzleMcu:
    def __init__(self):
        self.reactor = FakeReactor()
        self.script = []
        self.callbacks = {}
        self.sent = 0
        self._serial = self
        mcu = self

        class Cmd:
            class _cmd:
                @staticmethod
                def encode(values):
                    return b"encoded"

        self.cmd = Cmd

        class Printer:
            def get_reactor(self):
                return mcu.reactor

        self.printer = Printer()

    def create_oid(self):
        return 7

    def register_config_callback(self, callback):
        self.config_callback = callback

    def alloc_command_queue(self):
        return object()

    def add_config_cmd(self, cmd):
        pass

    def lookup_command(self, fmt, cq=None):
        return self.cmd

    def get_printer(self):
        return self.printer

    def register_response(self, callback, name, oid):
        self.callbacks[(name, oid)] = callback

    def raw_send_wait_ack(self, cmd, minclock, reqclock, cq):
        self.sent += 1
        step = self.script.pop(0)
        callback = self.callbacks.get(("transparent_response", 7))
        if step == "send_error":
            raise OSError("link down")
        if step == "respond":
            self.reactor.clock += 0.012
            callback({"#sent_time": self.reactor.clock, "read": b"\xf7ok"})
        elif step == "late":
            # Answer to an earlier request: sent before this query started.
            callback({"#sent_time": self.reactor.clock - 5, "read": b"old"})
        elif step == "bad_type":
            callback({"#sent_time": self.reactor.clock + 1, "read": "text"})
        # "timeout": nothing arrives


@pytest.fixture
def adapter():
    mcu = FakeNozzleMcu()
    adapter = mc.NozzleTransparentTransportAdapter(mcu)
    mcu.config_callback()
    adapter.mcu = mcu
    return adapter


def stats(adapter):
    return adapter.stats_status()


def test_unconfigured_send_is_a_send_error():
    mcu = FakeNozzleMcu()
    adapter = mc.NozzleTransparentTransportAdapter(mcu)
    with pytest.raises(RuntimeError, match="not configured"):
        adapter.send(b"pkt")
    s = adapter.stats_status()
    assert s["configured"] is False and s["send_errors"] == 1
    assert s["busy"] is False


def test_valid_response_counts_and_latency(adapter):
    adapter.mcu.script = ["respond"]
    assert adapter.send(b"pkt", timeout=1.0) == b"\xf7ok"
    s = stats(adapter)
    assert (s["sends"], s["wire_attempts"], s["responses"]) == (1, 1, 1)
    assert s["latency_ms"] == {
        "last": 12.0,
        "min": 12.0,
        "max": 12.0,
        "avg": 12.0,
    }
    assert s["last_result"] == "response" and s["busy"] is False


def test_timeout_after_all_wire_attempts(adapter):
    adapter.mcu.script = ["timeout", "timeout"]
    assert adapter.send(b"pkt", timeout=0.1, attempts=2) is None
    s = stats(adapter)
    assert s["sends"] == 1 and s["wire_attempts"] == 2
    assert s["timeouts"] == 2 and s["no_response"] == 1
    assert s["responses"] == 0 and s["latency_ms"]["last"] is None


def test_retry_counts_one_logical_send_two_wire_attempts(adapter):
    adapter.mcu.script = ["timeout", "respond"]
    assert adapter.send(b"pkt", timeout=0.1, attempts=2) == b"\xf7ok"
    s = stats(adapter)
    assert (s["sends"], s["wire_attempts"], s["timeouts"]) == (1, 2, 1)
    assert s["responses"] == 1 and s["no_response"] == 0


def test_late_response_is_not_matched(adapter):
    adapter.mcu.script = ["late"]
    assert adapter.send(b"pkt", timeout=0.1) is None
    s = stats(adapter)
    assert s["timeouts"] == 1 and s["responses"] == 0


def test_busy_rejection(adapter):
    adapter._send_busy = True
    with pytest.raises(RuntimeError, match="already in progress"):
        adapter.send(b"pkt")
    s = stats(adapter)
    assert s["busy_rejections"] == 1 and s["sends"] == 0
    assert s["last_result"] == "busy"


def test_send_error_restores_state(adapter):
    adapter.mcu.script = ["send_error"]
    with pytest.raises(OSError):
        adapter.send(b"pkt")
    s = stats(adapter)
    assert s["send_errors"] == 1 and s["busy"] is False
    assert "link down" in s["last_error"]
    assert adapter.mcu.callbacks[("transparent_response", 7)] is None


def test_wrong_payload_type_is_a_protocol_error(adapter):
    adapter.mcu.script = ["bad_type"]
    with pytest.raises(RuntimeError, match="payload type"):
        adapter.send(b"pkt")
    s = stats(adapter)
    assert s["protocol_errors"] == 1 and s["responses"] == 0
    assert s["busy"] is False


def test_callback_is_always_unregistered(adapter):
    adapter.mcu.script = ["respond", "timeout"]
    adapter.send(b"pkt")
    adapter.send(b"pkt", timeout=0.1)
    assert adapter.mcu.callbacks[("transparent_response", 7)] is None


def test_latency_aggregates_are_bounded_and_finite(adapter):
    adapter.mcu.script = ["respond"] * 50
    for _ in range(50):
        adapter.send(b"pkt")
    s = stats(adapter)
    assert s["responses"] == 50
    assert set(s["latency_ms"]) == {"last", "min", "max", "avg"}
    adapter.stats.latency(float("nan"))
    adapter.stats.latency(-1.0)
    assert stats(adapter)["latency_ms"]["min"] == 12.0
    json.dumps(stats(adapter), allow_nan=False)


def test_status_never_sends(adapter):
    adapter.mcu.script = ["respond"]
    adapter.send(b"pkt")
    sent = adapter.mcu.sent
    for _ in range(5):
        stats(adapter)
    assert adapter.mcu.sent == sent
