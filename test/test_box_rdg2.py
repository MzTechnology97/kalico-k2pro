"""Passive RDG2 decoding and exact transport; never contacts a device."""
import json
import pathlib
import struct
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))
from extras.box_rfid_diag import RfidDiagDriver, RfidDiagError, decode_cfs_diag2_payload
from extras.serial_485 import crc8


def snapshot():
    p = bytearray(64)
    p[:4] = b"RDG2"
    p[4], p[8], p[9], p[28], p[29], p[30], p[32] = 1, 1, 4, 100, 100, 1, 1
    struct.pack_into("<IIff", p, 12, 1200, 327000, 10.0, -20000.0)
    p[62], p[63] = 1, 2
    return p


def frame(payload, status=0):
    b = bytes((0xF7, 1, len(payload) + 3, status, 0x57)) + bytes(payload)
    return b + bytes((crc8(b[2:]),))


class Wire:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def cmd_send_data_with_response(self, body, timeout):
        self.calls.append((body, timeout))
        return self.reply


def test_rdg2_reads_one_exact_frame_without_probe_or_rf():
    wire = Wire(frame(snapshot()))
    result = RfidDiagDriver(wire, 1).diag2_snapshot(1)
    assert wire.calls == [(bytes.fromhex("0105FF571501"), 1.0)]
    assert result["physical_slot"] == 2 and result["used_mm"] == 1200
    assert result["total_mm"] == 327000 and result["snapshot_mask_validated"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("field,value", [(4, 2), (59, 1), (63, 1)])
def test_rdg2_rejects_mismatched_slot_reserved_or_version(field, value):
    p = snapshot()
    p[field] = value
    with pytest.raises(RfidDiagError):
        decode_cfs_diag2_payload(p, expected_slot=1)


@pytest.mark.parametrize("payload", [bytes(63), b"BAD!" + bytes(60)])
def test_rdg2_rejects_shape_and_magic(payload):
    with pytest.raises(RfidDiagError):
        decode_cfs_diag2_payload(payload, expected_slot=1)


def test_rdg2_nonfinite_odometer_is_json_safe():
    p = snapshot()
    struct.pack_into("<ff", p, 20, float("nan"), float("inf"))
    result = decode_cfs_diag2_payload(p, expected_slot=1)
    assert result["checkpoint_mm"] is None
    assert result["cached_raw_odometer"] is None
    assert result["delta_since_checkpoint_approx_mm"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("response", [None, frame(b"", status=1)])
def test_rdg2_timeout_or_unsupported_does_not_retry(response):
    wire = Wire(response)
    with pytest.raises(RfidDiagError):
        RfidDiagDriver(wire, 1).diag2_snapshot(1)
    assert len(wire.calls) == 1
