"""SGP1 decoding, compatibility and cache/odometer continuity. No printer I/O."""
import struct
import pathlib
import sys
from dataclasses import replace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_protocol
from extras import box_spool_profiles as sg
from extras.box_rfid_estimates import _cfs_precise_token
from test_box_spool_profiles import UID, driver, manager, snapshot_bytes


def payload(slot=0, initial=163500, used=1000, valid=True):
    p = bytearray(snapshot_bytes(
        slot=slot, uid=UID, profile=1, revision=1, total=327000,
        percent=50, raw=418.38977, used=used))
    p[:4] = b"SGP1"
    p[7] = int(valid)
    p += struct.pack("<II", initial if valid else 0,
                     max(0, initial-used) if valid else 0)
    return bytes(p)


def setup():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    extra._service(10.0)  # reference legacy firmware, then attach an R4 fixture
    fw.samples[0] = sg.decode_precise_snapshot(payload(), 0).sample
    fw.precision = sg.decode_precise_snapshot(payload(), 0)

    def read(slot):
        fw.calls.append(("precision", slot))
        if isinstance(fw.precision, Exception):
            raise fw.precision
        return fw.precision

    fw.precise_snapshot = read
    extra.units[1].pop("precision_supported", None)
    return extra, box, fw, stats


def test_one_read_only_wire_request_and_no_vendor_or_percent_guess():
    d, wire = driver(payload())
    sample = d.precise_snapshot(0)
    assert wire.requests == [(bytes.fromhex("0105ff571d00"), 0.5)]
    assert sample.precise_valid
    assert sample.initial_mm == 163500 and sample.remaining_mm == 162500
    assert sample.sample.percent == 50
    assert sample.token == (UID.hex().upper(), 1, 1, 327000, 163500, 1000)


@pytest.mark.parametrize("bad", [b"", payload()[:-1], payload()+b"\0",
                                b"BAD!" + payload()[4:]])
def test_bad_precision_reply_shape_fails_closed(bad):
    with pytest.raises(box_protocol.ProtocolError):
        sg.decode_precise_snapshot(bad, 0)


@pytest.mark.parametrize("offset,value", [
    (4, 1), (5, 0x80), (7, 2), (42, 3), (54, 1), (60, 2),
    (6, 0), (52, 0), (53, 0), (61, 1), (43, 7), (5, 0),
])
def test_bad_precision_anchor_fails_closed(offset, value):
    p = bytearray(payload())
    p[offset] = value
    with pytest.raises(box_protocol.ProtocolError):
        sg.decode_precise_snapshot(p, 0)


@pytest.mark.parametrize("offset,value", [
    (64, 327001), (68, 162501), (32, 9999), (28, 0),
])
def test_bad_quantities_and_revision_fail_closed(offset, value):
    p = bytearray(payload())
    struct.pack_into("<I", p, offset, value)
    with pytest.raises(box_protocol.ProtocolError):
        sg.decode_precise_snapshot(p, 0)


def test_unavailable_is_distinct_from_a_valid_zero_estimate():
    unknown = sg.decode_precise_snapshot(payload(valid=False), 0)
    zero = sg.decode_precise_snapshot(payload(initial=100, used=200), 0)
    assert not unknown.precise_valid and unknown.remaining_mm == 0
    assert zero.precise_valid and zero.remaining_mm == 0
    p = bytearray(payload(valid=False))
    struct.pack_into("<I", p, 64, 1)
    with pytest.raises(box_protocol.ProtocolError):
        sg.decode_precise_snapshot(p, 0)


def test_mm_sample_seeds_without_rounding_to_integer_percent():
    extra, box, fw, _ = setup()
    box._apply_reported_remaining(0, 50)
    assert extra.units[1]["precision_supported"] is True
    assert box.rfid_reported_percent[0] == 50
    assert box.rfid_spools[0]["remaining_mm"] == 162500
    assert box.rfid_percent[0] == pytest.approx(100 * 162500 / 327000)
    assert box.rfid_spools[0]["remaining_source"] == "cfs"


def test_unchanged_or_replayed_counter_cannot_rewind_local_usage():
    _, box, fw, _ = setup()
    box._apply_reported_remaining(0, 50)
    box.rfid_spools[0]["remaining_mm"] = 162493
    box._apply_reported_remaining(0, 50)
    assert box.rfid_spools[0]["remaining_mm"] == 162493
    fw.precision = sg.decode_precise_snapshot(payload(used=990), 0)
    box._apply_reported_remaining(0, 50)
    assert box.rfid_spools[0]["remaining_mm"] == 162493


def test_mm_counter_advances_even_when_legacy_percent_does_not_change():
    _, box, fw, _ = setup()
    box._apply_reported_remaining(0, 50)
    fw.precision = sg.decode_precise_snapshot(payload(used=1020), 0)
    box._apply_reported_remaining(0, 50)
    assert box.rfid_reported_percent[0] == 50
    assert box.rfid_spools[0]["remaining_mm"] == 162480


@pytest.mark.parametrize("failure", [
    sg.SpoolGeometryError("timeout"),
    box_protocol.ProtocolError("malformed"),
    "unavailable", "different_uid", "wrong_total", "stale_revision",
])
def test_precision_failure_preserves_cache_without_integer_reseed(failure):
    extra, box, fw, _ = setup()
    box._apply_reported_remaining(0, 50)
    box.rfid_spools[0]["remaining_mm"] = 160000
    legacy_reads = len([c for c in fw.calls if c[0] == "snapshot"])
    if isinstance(failure, Exception):
        fw.precision = failure
    elif failure == "unavailable":
        fw.precision = sg.decode_precise_snapshot(payload(valid=False), 0)
    else:
        field = {"different_uid": {"current_uid": b"1234"},
                 "wrong_total": {"total_mm": 330000},
                 "stale_revision": {"applied_revision": 2}}[failure]
        fw.precision = replace(
            fw.precision, sample=replace(fw.precision.sample, **field))
    box._apply_reported_remaining(0, 50)
    assert box.rfid_spools[0]["remaining_mm"] == 160000
    assert box.rfid_spools[0]["remaining_source"] == "kalico"
    assert len([c for c in fw.calls if c[0] == "snapshot"]) == legacy_reads
    assert extra.units[1]["precision_supported"] is True


def test_legacy_r3_is_probed_once_and_keeps_existing_percentage_path():
    extra, box, fw, _ = manager()
    box.add_spool(0)
    extra._service(10.0)
    box._apply_reported_remaining(0, 100)
    extra._service(15.0)
    box._apply_reported_remaining(0, 100)
    assert extra.units[1]["precision_supported"] is False
    assert len([c for c in fw.calls if c[0] == "precision_unsupported"]) == 1
    assert box.rfid_spools[0]["remaining_mm"] == 327000


def test_valid_zero_does_not_call_runout_or_change_mapping():
    _, box, fw, _ = setup()
    fw.precision = sg.decode_precise_snapshot(payload(initial=100, used=200), 0)
    before = len(fw.calls)
    box._apply_reported_remaining(0, 50)
    assert fw.calls[before:] == [("precision", 0)]
    assert box.rfid_spools[0]["remaining_mm"] == 0
    assert box.rfid_percent[0] == 0
    assert not box.operation_depth


def test_precision_token_and_local_usage_survive_cache_restore():
    extra, box, fw, _ = setup()
    box._apply_reported_remaining(0, 50)
    box.rfid_spools[0]["remaining_mm"] = 162493
    box._runtime_slot_key = lambda slot: slot
    key = box.rfid_spools[0]["key"]
    box.store.set_setting("rfid_slot_keys", {"0": key})
    box._persist_rfid_estimates(force=True)
    saved = box.store.setting("rfid_estimates")[key]
    assert saved["remaining_mm"] == 162493
    assert isinstance(saved["cfs_precise_token"], list)
    box.rfid_spools.clear()
    assert box._restore_cached_rfid_slot(0)
    box._apply_reported_remaining(0, 50)
    assert box.rfid_spools[0]["remaining_mm"] == 162493


@pytest.mark.parametrize("bad", [
    None, [], ["bad"], ["nothex!!", 1, 1, 327000, 163500, 1000],
    ["00000000", 1, 1, 327000, 163500, 1000],
    [UID.hex(), True, 1, 327000, 163500, 1000],
    [UID.hex(), 1, 1, 9999, 500, 1000],
    [UID.hex(), 1, 1, 327000, 327001, 1000],
    [UID.hex(), 1, 1, 327000, 163500, -1],
])
def test_malformed_saved_precision_tokens_are_ignored(bad):
    assert _cfs_precise_token(bad) is None


def test_precision_status_is_passive_and_uses_one_requested_snapshot():
    extra, _, fw, _ = setup()
    messages = []

    class Command:
        def get_int(self, name, default, **bounds):
            return 1 if name == "PRECISION" else default

        def respond_info(self, message):
            messages.append(message)

        def error(self, text):
            return RuntimeError(text)

    before = len(fw.calls)
    extra.cmd_status(Command())
    assert fw.calls[before:] == [("precision", 0)]
    assert "precise_valid=1 initial_mm=163500 remaining_mm=162500" in messages[0]
