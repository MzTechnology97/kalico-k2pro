import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_rfid_diag as diag


def test_init4_request_payload_shape():
    payload = diag.request_payload(
        diag.SUB_REMAIN_INIT4, logical_slot=3,
        uid=bytes.fromhex("AE2CE2A0"), total_mm=330000,
        initial_percent=87)
    assert payload[:3] == bytes((0x0B, 1, 1))
    assert payload[3:7] == bytes.fromhex("AE2CE2A0")
    assert payload[7] == 0
    assert int.from_bytes(payload[8:12], "little") == 330000
    assert payload[12] == 87
    assert len(payload) == 13


def test_clear4_request_payload_shape():
    assert diag.request_payload(
        diag.SUB_REMAIN_CLEAR4, logical_slot=2) == bytes((0x0C, 1, 0))


def test_init4_payload_rejects_bad_total_or_percent():
    for total in (9999, 2000001):
        try:
            diag.request_payload(
                diag.SUB_REMAIN_INIT4, logical_slot=0,
                uid=b"1234", total_mm=total, initial_percent=100)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid total accepted")
    for remaining in (0, 101):
        try:
            diag.request_payload(
                diag.SUB_REMAIN_INIT4, logical_slot=0,
                uid=b"1234", total_mm=100000,
                initial_percent=remaining)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid percentage accepted")


def native_type4_remaining(initial_percent, used_mm, total_mm):
    if not 1 <= initial_percent <= 100:
        raise ValueError
    if total_mm <= 0 or used_mm < 0:
        raise ValueError
    consumed = (used_mm * 100) // total_mm
    return max(initial_percent - consumed, 0)


def test_reverse_engineered_type4_formula():
    assert native_type4_remaining(100, 0, 330000) == 100
    assert native_type4_remaining(100, 3300, 330000) == 99
    assert native_type4_remaining(80, 33000, 330000) == 70
    assert native_type4_remaining(50, 999999, 330000) == 0


def test_type4_capability_does_not_overlap_existing_bits():
    assert diag.CAP_REMAIN_INIT4 == 0x04
    assert (diag.CAP_REMAIN_INIT4 & diag.CAP_REMAIN_STATE) == 0
    assert (diag.CAP_REMAIN_INIT4 & diag.CAP_STOCK_STATE) == 0
    assert (diag.CAP_REMAIN_INIT4 & diag.CAP_STOCK_CAPTURE) == 0
    assert (diag.CAP_REMAIN_INIT4 & diag.CAP_INTERNAL_RECORD) == 0