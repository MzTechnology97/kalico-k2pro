"""Strict decoding of MOT2 protection answers, through the whole chain.

The transport is a fake that returns prepared frames; no packet is sent.
"""

import pathlib
import struct
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import motor_control as mc  # noqa: E402
from extras import serial_485  # noqa: E402

ADDR = 0x81
HEALTHY = struct.pack("<II", 0, 0)


def frame(payload, status=0x00, addr=ADDR, func=mc.FUNC_PROTECTION, crc=None):
    body = bytes([len(payload) + 3, status, func]) + bytes(payload)
    if crc is None:
        crc = serial_485.crc8(body)
    return bytes([serial_485.PACK_HEAD, addr]) + body + bytes([crc])


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []

    def send(self, packet, timeout, attempts, response_timeout=None):
        self.sent.append(bytes(packet))
        return self.responses.pop(0) if self.responses else None


def client(*responses):
    return mc.MotorFirmwareClient(FakeTransport(*responses), framed=True)


# --- decoder ----------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, 2, 3, 4, 5, 6, 7, 9, 12])
def test_wrong_payload_length_is_unverified(length):
    with pytest.raises(mc.ProtectionResponseError, match="bytes, expected 8"):
        mc.decode_protection_payload(bytes(length), 0x00)


def test_short_zero_payload_is_not_healthy_without_status():
    # The audit case: 0/1/4 zero bytes used to decode as active=False.
    for length in (0, 1, 4):
        with pytest.raises(mc.ProtectionResponseError):
            mc.decode_protection_payload(bytes(length))


def test_complete_healthy_answer():
    result = mc.decode_protection_payload(HEALTHY, 0x00)
    assert result["active"] is False and result["has_error"] is False
    assert result["status_mismatch"] is False and result["stalled"] is False


def test_error_and_warning_masks():
    payload = struct.pack("<II", 1 << 8, 1 << 2)
    result = mc.decode_protection_payload(payload, 0x06)
    assert result["error_code"] == 256 and result["warning_code"] == 4
    assert result["active"] is True and result["has_error"] is True
    assert result["status_mismatch"] is False


def test_warning_only():
    result = mc.decode_protection_payload(struct.pack("<II", 0, 2), 0x04)
    assert result["active"] is True and result["has_error"] is False


def test_little_endian_uint32():
    result = mc.decode_protection_payload(
        bytes([0, 0, 0, 0x80, 0, 0, 0, 0]), 0x02
    )
    assert result["error_code"] == 0x80000000


@pytest.mark.parametrize("status", [0x08, 0x10, 0x80, 0xFF])
def test_unknown_status_bits_are_unverified(status):
    with pytest.raises(mc.ProtectionResponseError, match="unknown status"):
        mc.decode_protection_payload(HEALTHY, status)


def test_error_status_with_zero_masks_is_unverified():
    with pytest.raises(mc.ProtectionResponseError, match="error_code is 0"):
        mc.decode_protection_payload(HEALTHY, mc.FRAME_STATUS_ERROR)


def test_warning_status_with_zero_masks_is_unverified():
    with pytest.raises(mc.ProtectionResponseError, match="warning_code is 0"):
        mc.decode_protection_payload(HEALTHY, mc.FRAME_STATUS_WARNING)


def test_mask_without_latch_bit_still_counts_as_active():
    result = mc.decode_protection_payload(struct.pack("<II", 4, 0), 0x00)
    assert result["active"] is True and result["status_mismatch"] is True


def test_stall_bit_is_reported_separately():
    result = mc.decode_protection_payload(HEALTHY, mc.FRAME_STATUS_STALL)
    assert result["stalled"] is True and result["active"] is False


def test_unverified_query_data_value():
    with pytest.raises(mc.ProtectionResponseError, match="data=12"):
        mc.decode_protection_payload(HEALTHY, 0x00, data=12)


# --- client -----------------------------------------------------------------


def test_client_decodes_a_valid_frame():
    result = client(frame(struct.pack("<II", 0, 1), status=0x04)).protection(
        ADDR
    )
    assert result["warning_code"] == 1 and result["frame_status"] == 0x04


def test_client_rejects_truncated_payload_with_valid_crc():
    with pytest.raises(mc.ProtectionResponseError):
        client(frame(bytes(4))).protection(ADDR)


@pytest.mark.parametrize(
    "bad",
    [
        frame(HEALTHY, crc=0x00),
        frame(HEALTHY, addr=0x82),
        frame(HEALTHY, func=mc.FUNC_READ_ADDR),
    ],
)
def test_client_rejects_bad_crc_addr_func(bad):
    with pytest.raises(RuntimeError) as exc:
        client(bad, bad).protection(ADDR)
    assert not isinstance(exc.value, mc.ProtectionResponseError)


def test_client_retries_a_bad_crc_once():
    good = frame(HEALTHY)
    result = client(frame(HEALTHY, crc=0x00), good).protection(ADDR)
    assert result["active"] is False


# --- query, cache and periodic poll ------------------------------------------


class Controller:
    """A MotorControl with only the state the protection paths use."""

    def __init__(self, responses):
        self.mc = mc.MotorControl.__new__(mc.MotorControl)
        self.clock = 50.0
        self.mc.reactor = SimpleNamespace(monotonic=lambda: self.clock)
        self.mc.protection_validity = mc.ProtectionValidity(
            mc.ALL_AXES, mc.PROTECTION_STALE_AFTER
        )
        self.mc.protection_validity.new_session()
        self.mc.motor_error_code = {}
        self.mc.motor_warning_code = {}
        self.mc.motor_fault_detail = {}
        self.handled = []
        self.mc._handle_active_fault = lambda detail, eventtime, **kw: (
            self.handled.append(detail)
        )
        self.mc._emit_runtime_warning = lambda detail, warnings: None
        self.transports = {}
        targets = {}
        for axis, addr in (("x", 0x81), ("y", 0x82), ("e", 0x83)):
            transport = FakeTransport(*responses.get(axis, []))
            self.transports[axis] = transport
            targets[axis] = mc.MotorAxisTarget(
                axis=axis,
                addr=addr,
                client=mc.MotorFirmwareClient(transport, framed=True),
            )
        self.mc.axes = SimpleNamespace(
            target=lambda axis: targets[axis],
            check_protection=lambda axes, data, timeout: {
                axis: targets[axis].protection(data=data, timeout=timeout)
                for axis in axes
            },
        )


def test_unverified_answer_keeps_the_previous_fault():
    fault = frame(struct.pack("<II", 8, 0), status=0x02, addr=0x81)
    truncated = frame(bytes(4), addr=0x81)
    ctl = Controller({"x": [fault, truncated]})
    ctl.mc.query_protection_status(axes=("x",))
    assert ctl.mc.motor_fault_detail["x"]["error_code"] == 8
    with pytest.raises(mc.ProtectionResponseError):
        ctl.mc.query_protection_status(axes=("x",))
    assert ctl.mc.motor_fault_detail["x"]["error_code"] == 8
    assert ctl.mc.motor_error_code[1] == 8


def test_verified_healthy_answer_clears_the_fault():
    fault = frame(struct.pack("<II", 8, 0), status=0x02, addr=0x81)
    ctl = Controller({"x": [fault, frame(HEALTHY, addr=0x81)]})
    ctl.mc.query_protection_status(axes=("x",))
    ctl.mc.query_protection_status(axes=("x",))
    assert "x" not in ctl.mc.motor_fault_detail


def test_periodic_poll_does_not_read_unverified_as_healthy():
    fault = frame(struct.pack("<II", 8, 0), status=0x02, addr=0x82)
    ctl = Controller(
        {
            "x": [frame(HEALTHY, addr=0x81)],
            "y": [frame(HEALTHY, status=mc.FRAME_STATUS_ERROR, addr=0x82)],
            "e": [frame(HEALTHY, addr=0x83)],
        }
    )
    ctl.mc.motor_fault_detail["y"] = {"error_code": 8, "active": True}
    ctl.mc._process_protection_poll()
    # y answered "error" with zero masks: still faulted, not cleared.
    assert ctl.mc.motor_fault_detail["y"]["error_code"] == 8
    assert ctl.handled == []
    del fault


def test_periodic_poll_acts_on_a_verified_error():
    ctl = Controller(
        {
            "x": [frame(struct.pack("<II", 8, 0), status=0x02, addr=0x81)],
            "y": [frame(HEALTHY, addr=0x82)],
            "e": [frame(HEALTHY, addr=0x83)],
        }
    )
    ctl.mc._process_protection_poll()
    assert len(ctl.handled) == 1
    assert ctl.handled[0]["axes"]["x"]["has_error"] is True
