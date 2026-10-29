"""RH1 wire, owned queue, and real Box/RfidFallback entry-point tests."""
import pathlib
import struct
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_protocol
from extras import box_rfid_handoff as rh
from extras.box import Box as RealBox
from extras.box_rfid_bambu import BambuStockTagData
from extras.box_rfid_fallback import RfidFallback
from extras.box_rfid_vendors import BoxRfidVendors
from extras.serial_485 import crc8
from test_box_spool_profiles import manager, Transport, UID


def frame(payload=b"", status=0):
    b = bytes((0xF7, 1, len(payload) + 3, status, 0x57)) + payload
    return b + bytes((crc8(b[2:]),))


def state_bytes(state=rh.IDLE, slot=255, session=123, token=0, request=0,
                uid=b"\0" * 4, result=0, flags=3, generation=0):
    return (b"RHO1" + bytes((1, state, slot, result))
            + struct.pack("<III4sIIII", session, token, request, uid,
                          3000 if state == rh.WAIT_KEYS else 0,
                          flags, generation, 100))


class Wire:
    def __init__(self, payload):
        self.response = frame(payload)
        self.requests = []

    def cmd_send_data_with_response(self, body, timeout):
        self.requests.append((body, timeout))
        return self.response


def test_rhi1_info_and_short_exchange():
    w = Wire(b"RHI1" + bytes((1, 40, 4, 7)) + struct.pack("<HH", 5000, 10000))
    assert rh.HandoffDriver(w, 1).info() == rh.HandoffInfo(5000, 10000)
    assert w.requests[0][0][4:] == bytes((rh.INFO,))
    assert w.requests[0][1] == 0.5


def test_start_config_poll_and_key_payloads():
    w = Wire(state_bytes())
    d = rh.HandoffDriver(w, 1)
    d.configure(123)
    assert w.requests[-1][0][4:] == struct.pack(
        "<BBBBIHH", rh.CONFIG, 1, 0, 0, 123, 3000, 10000)
    d.poll(123)
    assert w.requests[-1][0][4:] == struct.pack("<BBBBI", rh.POLL, 0, 0, 0, 123)
    w.response = frame(state_bytes(rh.RUNNING, 2, request=7, flags=7))
    d.start(2, 123, 7)
    assert w.requests[-1][0][4:] == struct.pack("<BBBBII", rh.START, 2, 0, 0, 123, 7)
    state = rh.decode_state(state_bytes(rh.WAIT_KEYS, 2, token=8, uid=UID), 123)
    w.response = frame(state_bytes(rh.KEYS_READY, 2, token=8, uid=UID))
    d.answer(state, b"k" * 18)
    assert w.requests[-1][0][4:] == struct.pack(
        "<BBBBII4s", rh.KEYS, 2, 0, 0, 123, 8, UID) + b"k" * 18
    d.answer(state)
    assert len(w.requests[-1][0][4:]) == 16


@pytest.mark.parametrize("offset,value", [(4, 2), (5, 9), (6, 254), (7, 6), (28, 8)])
def test_state_shape_fails_closed(offset, value):
    p = bytearray(state_bytes())
    p[offset] = value
    with pytest.raises(box_protocol.ProtocolError):
        rh.decode_state(p, 123)


def test_wrong_session_and_missing_wait_token_are_rejected():
    with pytest.raises(box_protocol.ProtocolError):
        rh.decode_state(state_bytes(), 124)
    with pytest.raises(box_protocol.ProtocolError):
        rh.decode_state(state_bytes(rh.WAIT_KEYS, 0, uid=UID), 123)


class Provider:
    RFID_DECODER_NAME = "BAMBU"
    RFID_DECODER_KIND = "bambu"
    RFID_DECODER_PRIORITY = 10

    def __init__(self):
        self.decodes = 0
        self.uid_by_slot = {}
        self.callback = None
        self.capture_device = None

    def rfid_decoder_version(self):
        return "1"

    def rfid_decoder_read(self, *args):
        raise AssertionError("a second complete RFID scan was requested")

    def rfid_handoff_keys(self, uid):
        assert len(uid) == 4
        return b"k" * 18

    def rfid_handoff_decode(self, slot, address):
        self.decodes += 1
        if self.capture_device is not None:
            assert self.capture_device.capture_slot == slot, "capture was overwritten"
        if self.callback is not None:
            self.callback()
        return BambuStockTagData(slot, self.uid_by_slot.get(slot, UID), b"\x04\x00", 8,
                                 "PETG HF", "#112233", "#112233FF", {})


class Device(Transport):
    def __init__(self, geometry):
        super().__init__()
        self.geometry = geometry
        self.supported = True
        self.configure_supported = True
        self.session = 0
        self.enabled = False
        self.state = rh.IDLE
        self.slot = 255
        self.result = rh.NONE
        self.request = 0
        self.token = 0
        self.uid = b"\0" * 4
        self.generation = 0
        self.wire_calls = []
        self.starts = 0
        self.keys = 0
        self.lost_start_ack = False
        self.lost_key_ack = False
        self.native = False
        self.uid_by_slot = {}
        self.capture_slot = None
        for slot, sample in tuple(geometry.samples.items()):
            geometry.samples[slot] = replace(sample, generation=0, flags=0)

    def response(self):
        return state_bytes(
            self.state, self.slot, self.session, self.token, self.request,
            self.uid, self.result, (3 if self.enabled else 0) | (4 if self.request else 0),
            self.generation)

    def begin_auto(self, slot=0):
        self.state, self.slot, self.request = rh.RUNNING, slot, 0
        self.uid = self.uid_by_slot.get(slot, UID)
        self.result = rh.NONE
        self.generation = self.geometry.samples[slot].generation
        self.geometry.samples[slot] = replace(
            self.geometry.samples[slot], current_uid=self.uid, flags=0)

    def reboot(self):
        self.session, self.enabled = 0, False
        self.state, self.slot = rh.IDLE, 255
        self.request, self.token, self.generation = 0, 0, 0
        self.uid = b"\0" * 4

    def cmd_send_data_with_response(self, body, timeout):
        assert self.active, "exchange did not use the owned request queue"
        payload = bytes(body[4:])
        sub = payload[0]
        self.wire_calls.append(sub)
        if sub == rh.INFO:
            return frame(b"RHI1" + bytes((1, 40, 4, 7))
                         + struct.pack("<HH", 5000, 10000)) if self.supported else frame(status=1)
        session = struct.unpack_from("<I", payload, 4)[0]
        if sub == rh.CONFIG:
            if not self.configure_supported:
                return frame(status=1)
            self.session, self.enabled = session, bool(payload[1])
        elif session != self.session or not self.enabled:
            return frame(status=1)
        elif sub == rh.START:
            self.starts += 1
            self.slot = payload[1]
            self.request = struct.unpack_from("<I", payload, 8)[0]
            self.state = rh.RUNNING
            self.uid = self.uid_by_slot.get(self.slot, UID)
            self.generation = self.geometry.samples[self.slot].generation
            self.geometry.samples[self.slot] = replace(
                self.geometry.samples[self.slot], current_uid=self.uid, flags=0)
            if self.lost_start_ack:
                self.lost_start_ack = False
                return None
        elif sub == rh.POLL:
            if self.state == rh.RUNNING:
                if self.native:
                    self.state, self.result = rh.DONE, rh.NATIVE_OK
                else:
                    self.state = rh.WAIT_KEYS
                    self.token += 1
                    self.uid = self.uid_by_slot.get(self.slot, UID)
            elif self.state == rh.KEYS_READY:
                self.state, self.result = rh.DONE, rh.API7_OK
                self.generation = self.geometry.samples[self.slot].generation % 0xFFFFFFFF + 1
                self.geometry.samples[self.slot] = replace(
                    self.geometry.samples[self.slot], generation=self.generation, flags=19,
                    measured_uid=self.uid, current_uid=self.uid)
                self.capture_slot = self.slot
        elif sub in (rh.KEYS, rh.DECLINE):
            token = struct.unpack_from("<I", payload, 8)[0]
            assert token == self.token and payload[12:16] == self.uid
            if sub == rh.KEYS:
                self.keys += 1
                assert payload[16:] == b"k" * 18
                self.capture_slot = None
                self.state = rh.KEYS_READY
                if self.lost_key_ack:
                    self.lost_key_ack = False
                    return None
            else:
                self.state, self.result = rh.DONE, rh.DECLINED
        else:
            raise AssertionError("unexpected command %r" % payload)
        return frame(self.response())


class LegacyDriver:
    def __init__(self):
        self.force_calls = 0

    def force_rfid_read(self, mask):
        self.force_calls += 1
        return SimpleNamespace(status=0)

    def query_rfid_records(self, mask, timeout):
        name = box_protocol.RFID_SLOT_NAMES[0]
        return SimpleNamespace(status=0, records={name: "A" * 40},
                               fields={name: {"mat_id": "PLA"}})


def setup():
    owner, box, geometry, stats = manager()
    device = Device(geometry)
    owner.serial = device
    printer = box.printer
    printer.objects["serial_485 serial485"] = device
    provider = Provider()
    printer.objects["box_rfid_bambu"] = provider
    printer.lookup_objects = lambda: list(printer.objects.items())
    box.rfid_fallback = RfidFallback(box)
    box._global_slot = lambda address, local: (address - 1) * 4 + local
    box.is_physical_slot = lambda slot: 0 <= slot < 4
    box._info = lambda *args: None
    box._require_reply = lambda reply, *args: reply
    box.slot_label = lambda slot: "T%d" % slot
    box.gcode = None
    box.applied = []
    box.rfid_seen_invalid = set()
    box._query_rfid_sample = lambda slot: ("unknown", {})
    box._clear_rfid_watch = lambda slot: None
    box._rfid_record_ready = lambda sample: False
    box._try_pending_vendor_rfid_fallback = lambda slot: BoxRfidVendors._run_vendor_rfid_fallbacks(
        box, slot, automatic=True)

    def apply_tag(slot, tag, note=None):
        box.applied.append(tag.uid)
        box.add_spool(slot, tag.uid)
        owner.on_tag_decoded(slot, "bambu", tag.uid)
        box.rfid_live_slots.add(slot)
        return True

    box._apply_bambu_rfid_tag = apply_tag
    box._apply_rfid_record = lambda *args: True
    box.drivers[1] = LegacyDriver()

    def pause(until):
        assert not device.active, "reactor.pause while owning RS485 would deadlock"
        printer.reactor.now = until

    printer.reactor.pause = pause
    return owner, box, geometry, stats, device, provider


def test_real_box_manual_route_has_one_start_and_no_legacy_force():
    owner, box, geometry, stats, device, provider = setup()
    result = RealBox._force_rfid_results_locked(box, 1, box.drivers[1], 1,
                                               "manual", known_fastpath=True)
    assert result == {0}
    assert device.starts == 1 and device.keys == 1
    assert box.drivers[1].force_calls == 0
    assert provider.decodes == 1 and box.applied == [UID]
    assert box.rfid_fallback._cache()["tags"][UID.hex().upper()]["decoder"] == "BAMBU"


def test_automatic_route_reuses_done_capture_in_real_fallback():
    owner, box, geometry, stats, device, provider = setup()
    h = owner.handoff
    h._periodic(10.0)  # configure + lease
    device.begin_auto()
    h._periodic(10.25)  # WAIT -> KEYS
    h._periodic(10.5)  # DONE -> decode
    assert provider.decodes == 1
    result = RealBox._read_rfid_result(box, 0, read_complete=True)
    assert result == "BAMBU"
    assert box.drivers[1].force_calls == 0 and device.starts == 0
    assert provider.decodes == 1
    assert 0 in box.rfid_live_slots


@pytest.mark.parametrize("lost", ["lost_start_ack", "lost_key_ack"])
def test_lost_ack_recovers_by_poll_without_second_movement(lost):
    owner, box, geometry, stats, device, provider = setup()
    setattr(device, lost, True)
    assert owner.handoff.read_slots(1, (0,)) == {0}
    assert device.starts == device.keys == provider.decodes == 1
    assert box.drivers[1].force_calls == 0


def test_failed_handoff_never_falls_back_to_another_full_read():
    owner, box, geometry, stats, device, provider = setup()
    box.printer.objects.pop("box_rfid_bambu")
    assert RealBox._force_rfid_results_locked(
        box, 1, box.drivers[1], 1, "manual") == set()
    assert device.result == rh.DECLINED
    assert device.starts == 1 and device.keys == 0
    assert box.drivers[1].force_calls == 0
    assert BoxRfidVendors._run_vendor_rfid_fallbacks(box, 0, automatic=True) is None
    assert box.drivers[1].force_calls == 0


def test_legacy_firmware_preserves_existing_box_read_path():
    owner, box, geometry, stats, device, provider = setup()
    device.supported = False
    box._rfid_record_ready = lambda sample: True
    result = RealBox._force_rfid_results_locked(box, 1, box.drivers[1], 1, "manual")
    assert result == {0} and box.drivers[1].force_calls == 1
    assert device.starts == device.keys == 0


def test_secure_config_refusal_does_not_keep_probing_or_activate_rh():
    owner, box, geometry, stats, device, provider = setup()
    device.configure_supported = False
    owner.handoff._periodic(10.0)
    calls = len(device.wire_calls)
    owner.handoff._periodic(10.25)
    assert len(device.wire_calls) == calls
    assert not device.enabled and owner.handoff.read_slots(1, (0,)) is None


def test_cfs_reboot_renegotiates_new_session_without_rereading_cache(monkeypatch):
    owner, box, geometry, stats, device, provider = setup()
    values = iter((101, 102, 103))
    monkeypatch.setattr(rh.secrets, "randbits", lambda n: next(values))
    owner.handoff.units.clear()
    owner.handoff._periodic(10.0)
    old_session = device.session
    box.add_spool(0)
    old_remaining = box.rfid_spools[0]["remaining_mm"]
    device.reboot()
    geometry.samples[0] = replace(geometry.samples[0], flags=0,
                                  profile_id=0, applied_revision=0)
    owner.handoff._periodic(10.25)  # stale POLL rejected; forget session
    owner.handoff._periodic(10.5)   # fresh INFO/CONFIG/POLL
    assert device.session != old_session and device.enabled
    assert not device.starts and not device.keys
    box._apply_reported_remaining(0, 255)
    assert box.rfid_spools[0]["remaining_mm"] == old_remaining


@pytest.mark.parametrize("command,args", [
    ("configure", (123,)), ("poll", (123,)),
    ("start", (0, 123, 1)),
    ("answer", (rh.HandoffState(rh.WAIT_KEYS, 0, 0, 123, 1, 0, UID, 1000, 3, 1, 0), b"k"*18)),
])
def test_observation_blocks_every_mutating_exchange(command, args):
    owner, box, geometry, stats, device, provider = setup()
    box.observation_mode = True
    owner.handoff._periodic(10.0)
    with pytest.raises(rh.HandoffError):
        owner.handoff._exchange(1, command, *args)
    assert not device.wire_calls


@pytest.mark.parametrize("activity", ["printing", "paused", "load", "unload"])
def test_no_start_or_auth_during_unrelated_activity(activity):
    owner, box, geometry, stats, device, provider = setup()
    owner.handoff._periodic(10.0)
    if activity in ("printing", "paused"):
        stats.state = activity
    else:
        box.operation_depth = 1
        box.operation_progress = {"kind": activity}
    with pytest.raises(rh.HandoffBusy):
        owner.handoff.read_slots(1, (0,))
    device.begin_auto()
    owner.handoff._periodic(10.25)
    assert not device.starts and not device.keys
    assert device.result == rh.DECLINED


def test_measurement_generation_changed_during_capture_is_rejected():
    owner, box, geometry, stats, device, provider = setup()
    provider.callback = lambda: geometry.samples.__setitem__(
        0, replace(geometry.samples[0], generation=2))
    with pytest.raises(rh.HandoffError, match="superseded"):
        owner.handoff.read_slots(1, (0,))
    assert not box.applied
    assert box.drivers[1].force_calls == 0 and device.starts == 1


def test_completed_capture_from_previous_uid_is_not_reused():
    owner, box, geometry, stats, device, provider = setup()
    assert owner.handoff.read_slots(1, (0,)) == {0}
    geometry.samples[0] = replace(geometry.samples[0], current_uid=b"1234", generation=2)
    assert owner.handoff.fallback_result(0) == (True, None)
    assert provider.decodes == 1 and device.starts == 1


def test_real_bambu_handoff_decoder_reads_only_completed_capture(monkeypatch):
    from extras import box_rfid_bambu as bambu
    from extras.box_rfid_diag import MemoryReply
    owner, box, geometry, stats, device, provider = setup()
    records = {n: bytearray(76) for n in (0, 2, 3)}
    target, cap1, cap2 = records[0], records[2], records[3]
    target[9:12] = bytes((7, 7, 0))
    target[60:62], target[62:66], target[74] = b"\x04\x00", UID, 8
    cap1[4:8], cap2[4:8] = b"K2C3", b"K2D3"
    detail = b"PETG HF".ljust(16, b"\0")
    cap1[8:20], cap2[8:12], cap2[12:16] = detail[:12], detail[12:], bytes.fromhex("112233FF")
    calls = []

    class CaptureDriver:
        def __init__(self, transport, address):
            assert transport.active

        def internal_record(self, local, timeout):
            calls.append(("internal", local))
            return MemoryReply(0, b"", b"", bytes(records[local]))

        def clear_stock_task_keys(self, local, timeout):
            calls.append(("clear", local))

    monkeypatch.setattr(bambu, "RfidDiagDriver", CaptureDriver)
    decoder = bambu.BoxRfidBambu.__new__(bambu.BoxRfidBambu)
    decoder._transport = lambda: device
    tag = decoder.rfid_handoff_decode(0, 1)
    assert tag.uid == UID and tag.detailed_filament_type == "PETG HF"
    assert tag.color == "#112233"
    assert calls == [("internal", 0), ("internal", 2), ("internal", 3)]
    assert device.starts == device.keys == 0


def test_real_box_manual_guard_remains_owned_during_cooperative_wait():
    owner, box, geometry, stats, device, provider = setup()
    box.acquire_rfid_read = RealBox.acquire_rfid_read.__get__(box)
    box.release_rfid_read = RealBox.release_rfid_read.__get__(box)
    box._rfid_read_guard = RealBox._rfid_read_guard.__get__(box)
    box._force_rfid_results_locked = RealBox._force_rfid_results_locked.__get__(box)
    observed = []
    old_pause = box.printer.reactor.pause

    def pause(now):
        observed.append(box.rfid_read_owner)
        owner._service(now)
        old_pause(now)

    box.printer.reactor.pause = pause
    assert RealBox._force_rfid_results(box, 1, box.drivers[1], 1, "manual RH1") == {0}
    assert observed and all(item == "manual RH1" for item in observed)
    assert box.rfid_read_owner is None and device.starts == 1
    assert not [c for c in geometry.calls if c[0] == "set_catalog"]


def test_pending_auto_done_is_consumed_before_manual_start_on_another_slot():
    from test_box_spool_profiles import snapshot_bytes
    from extras import box_spool_profiles as sg
    owner, box, geometry, stats, device, provider = setup()
    other = bytes.fromhex("11223344")
    provider.uid_by_slot[1] = device.uid_by_slot[1] = other
    geometry.samples[1] = sg.decode_snapshot(snapshot_bytes(slot=1, uid=other), 1)
    owner.handoff._periodic(10.0)
    device.begin_auto()
    owner.handoff._periodic(10.25)  # automatic A has accepted keys, not decoded
    assert device.state == rh.KEYS_READY and provider.decodes == 0
    assert owner.handoff.read_slots(1, (1,)) == {1}
    assert box.applied == [UID, other]
    assert provider.decodes == 2 and device.starts == 1
    assert box.drivers[1].force_calls == 0


def queued_slots():
    from test_box_spool_profiles import snapshot_bytes
    from extras import box_spool_profiles as sg
    owner, box, geometry, stats, device, provider = setup()
    other = bytes.fromhex("11223344")
    provider.uid_by_slot[1] = device.uid_by_slot[1] = other
    geometry.samples[1] = sg.decode_snapshot(
        snapshot_bytes(slot=1, uid=other, generation=0, flags=0), 1)
    provider.capture_device = device
    owner.handoff._periodic(10.0)
    device.begin_auto()
    owner.handoff._periodic(10.25)
    assert device.state == rh.KEYS_READY and provider.decodes == 0
    return owner, box, geometry, stats, device, provider, other


def test_queued_auto_capture_is_drained_before_next_slots_keys():
    owner, box, geometry, stats, device, provider, other = queued_slots()
    # The autonomous CFS can finish A and begin B between host polls. RHO no
    # longer describes A, but its slot proof and capture must still be consumed
    # before sending B's keys, which clear global capture scratch.
    owner.handoff._exchange(1, "poll", device.session)
    assert device.capture_slot == 0
    device.begin_auto(1)
    owner.handoff._periodic(10.5)
    assert box.applied == [UID] and device.keys == 2
    owner.handoff._periodic(10.75)
    assert box.applied == [UID, other] and provider.decodes == 2
    assert device.starts == box.drivers[1].force_calls == 0


def test_global_job_may_change_while_slot_proof_stays_valid():
    owner, box, geometry, stats, device, provider, other = queued_slots()

    def next_slot_during_copy():
        provider.callback = None
        device.begin_auto(1)
        owner.handoff._periodic(10.6)
        assert device.keys == 1  # reentrant timer cannot overwrite A

    provider.callback = next_slot_during_copy
    owner.handoff._periodic(10.5)
    assert box.applied == [UID]
    owner.handoff._periodic(10.75)
    owner.handoff._periodic(11.0)
    assert box.applied == [UID, other]
    assert device.keys == 2 and device.starts == box.drivers[1].force_calls == 0


def test_pending_copy_retry_does_not_overwrite_capture_with_next_keys():
    owner, box, geometry, stats, device, provider, other = queued_slots()
    owner.handoff._exchange(1, "poll", device.session)
    device.begin_auto(1)

    def temporary_failure():
        provider.callback = None
        raise rh.HandoffError("temporary capture read failure")

    provider.callback = temporary_failure
    owner.handoff._periodic(10.5)
    assert device.keys == 1 and device.capture_slot == 0 and not box.applied
    owner.handoff._periodic(10.75)
    assert box.applied == [UID] and device.keys == 2
    owner.handoff._periodic(11.0)
    assert box.applied == [UID, other] and not device.starts


def test_pending_capture_with_changed_slot_uid_is_discarded():
    owner, box, geometry, stats, device, provider, other = queued_slots()
    owner.handoff._exchange(1, "poll", device.session)
    geometry.samples[0] = replace(geometry.samples[0], current_uid=b"9876")
    device.begin_auto(1)
    owner.handoff._periodic(10.5)
    owner.handoff._periodic(10.75)
    assert box.applied == [other] and provider.decodes == 1
    assert device.starts == box.drivers[1].force_calls == 0


def test_reentrant_manual_read_cannot_replace_capture_in_progress():
    owner, box, geometry, stats, device, provider, other = queued_slots()

    def manual_during_copy():
        with pytest.raises(rh.HandoffBusy):
            owner.handoff.read_slots(1, (1,))

    provider.callback = manual_during_copy
    owner.handoff._periodic(10.5)
    assert box.applied == [UID] and device.starts == 0


def test_auto_capture_owns_box_read_guard_against_legacy_helpers():
    owner, box, geometry, stats, device, provider, other = queued_slots()
    box.acquire_rfid_read = RealBox.acquire_rfid_read.__get__(box)
    box.release_rfid_read = RealBox.release_rfid_read.__get__(box)
    observed = []

    def helper_during_copy():
        observed.append(box.rfid_read_owner)
        with pytest.raises(RuntimeError, match="another CFS RFID read"):
            box.acquire_rfid_read("legacy helper")
        owner._service(10.6)

    provider.callback = helper_during_copy
    owner.handoff._periodic(10.5)
    assert observed == ["box_rfid_handoff"]
    assert box.rfid_read_owner is None and box.applied == [UID]
    assert not [c for c in geometry.calls if c[0] == "set_catalog"]


def test_auto_timer_does_not_borrow_another_readers_guard():
    owner, box, geometry, stats, device, provider = setup()
    owner.handoff._periodic(10.0)
    device.begin_auto()
    box.rfid_read_owner = "legacy helper"
    before = len(device.wire_calls)
    owner.handoff._periodic(10.25)
    assert len(device.wire_calls) == before
    assert device.keys == 0 and box.rfid_read_owner == "legacy helper"


def test_geometry_reconciliation_is_rescheduled_when_read_owner_releases():
    owner, box, geometry, stats, device, provider = setup()
    box.acquire_rfid_read = RealBox.acquire_rfid_read.__get__(box)
    box.release_rfid_read = RealBox.release_rfid_read.__get__(box)
    box._rfid_read_guard = RealBox._rfid_read_guard.__get__(box)
    box._force_rfid_results_locked = RealBox._force_rfid_results_locked.__get__(box)
    original = box._apply_bambu_rfid_tag

    def apply_while_reconcile_timer_fires(slot, tag, note=None):
        result = original(slot, tag, note)
        now = box.printer.reactor.monotonic()
        box.printer.reactor.update_timer(owner._timer, owner._service(now))
        assert owner._timer[1] > now  # writes are deferred while read-owned
        return result

    box._apply_bambu_rfid_tag = apply_while_reconcile_timer_fires
    assert RealBox._force_rfid_results(box, 1, box.drivers[1], 1, "manual RH1") == {0}
    assert box.rfid_read_owner is None
    assert owner._timer[1] == box.printer.reactor.monotonic()


def test_status_is_passive_and_distinguishes_unprobed_from_unsupported():
    owner, box, geometry, stats, device, provider = setup()
    before = list(device.wire_calls)
    status = owner.get_status(10.0)
    assert status["single_cycle_rfid"] is True
    assert status["handoff"] == {
        "enabled": True, "cfs": {}, "last_error": None}
    assert device.wire_calls == before

    device.supported = False
    owner.handoff._periodic(10.0)
    before = list(device.wire_calls)
    result = owner.get_status(10.25)["handoff"]["cfs"][1]
    assert result["supported"] is False
    assert result["configured"] is False
    assert result["request"] == 0 and result["pending_capture_count"] == 0
    assert "unsupported" in result["error"].lower()
    assert device.wire_calls == before


def test_status_whitelists_scalars_and_never_exposes_keys_or_capture_data():
    import json
    owner, box, geometry, stats, device, provider = setup()
    unit = owner.handoff._unit(1)
    secret = b"SENSITIVE_AUTH_KEY!"
    raw_capture = b"PRIVATE_CAPTURE_BYTES"
    unit.update(supported=True, configured=True, session=123, request=9,
                error="capture pending")
    unit["offers"][(123, 1)] = (provider, secret)
    unit["captures"][(123, 1)] = raw_capture
    unit["pending"][(123, 1)] = {"keys": secret, "raw": raw_capture}
    unit["pending"][(123, 2)] = {"raw": raw_capture}
    before = list(device.wire_calls)
    status = owner.get_status(11.0)
    entry = status["handoff"]["cfs"][1]
    assert entry == {
        "supported": True, "configured": True, "session": 123,
        "request": 9, "pending_capture_count": 2, "error": "capture pending"}
    encoded = json.dumps(status)
    assert secret.decode() not in encoded and raw_capture.decode() not in encoded
    assert all(word not in entry for word in ("offers", "captures", "keys", "raw"))
    assert device.wire_calls == before
    entry["configured"] = False
    assert unit["configured"] is True


def test_status_shows_transient_per_cfs_error_without_querying_again(monkeypatch):
    owner, box, geometry, stats, device, provider = setup()
    owner.handoff._periodic(10.0)

    def fail(address, method, *args):
        raise rh.HandoffError("simulated POLL timeout")

    monkeypatch.setattr(owner.handoff, "_exchange", fail)
    owner.handoff._periodic(10.25)
    status = owner.get_status(10.5)["handoff"]
    assert status["cfs"][1]["supported"] is True
    assert status["cfs"][1]["configured"] is True
    assert status["cfs"][1]["error"] == "simulated POLL timeout"
    assert status["last_error"] == "simulated POLL timeout"


def test_periodic_configures_before_first_scan_with_real_box_read_guard():
    owner, box, geometry, stats, device, provider = setup()
    box.acquire_rfid_read = RealBox.acquire_rfid_read.__get__(box)
    box.release_rfid_read = RealBox.release_rfid_read.__get__(box)
    observed = []
    exchange = device.cmd_send_data_with_response

    def send(body, timeout):
        observed.append((body[4], box.rfid_read_owner))
        return exchange(body, timeout)

    device.cmd_send_data_with_response = send
    owner.handoff._periodic(10.0)
    assert owner.handoff.units[1]["configured"] is True
    assert device.wire_calls == [rh.INFO, rh.CONFIG, rh.POLL]
    assert all(reader == "box_rfid_handoff" for sub, reader in observed)
    assert box.rfid_read_owner is None
    assert device.starts == device.keys == 0
    assert owner._can_mutate()
    # Geometry writers continue to see the real coordinator as a reader.
    from extras.box_spool_profiles import SpoolGeometryBusy
    with owner.handoff._operation(1):
        assert not owner._can_mutate()
        with pytest.raises(SpoolGeometryBusy):
            with owner._write_guard():
                raise AssertionError("geometry write entered during RFID ownership")


@pytest.mark.parametrize("activity", ["printing", "paused", "load", "reader", "observation"])
def test_periodic_real_guard_defers_config_and_retries_after_idle(activity):
    owner, box, geometry, stats, device, provider = setup()
    box.acquire_rfid_read = RealBox.acquire_rfid_read.__get__(box)
    box.release_rfid_read = RealBox.release_rfid_read.__get__(box)
    if activity in ("printing", "paused"):
        stats.state = activity
    elif activity == "load":
        box.operation_depth = 1
        box.operation_progress = {"kind": "load"}
    elif activity == "reader":
        box.acquire_rfid_read("other reader")
    else:
        box.observation_mode = True
    owner.handoff._periodic(10.0)
    assert rh.CONFIG not in device.wire_calls and rh.POLL not in device.wire_calls
    assert device.starts == device.keys == 0
    assert not owner.handoff.units.get(1, {}).get("configured")
    if activity == "reader":
        assert box.rfid_read_owner == "other reader"
        box.release_rfid_read()
    stats.state = "standby"
    box.operation_depth = 0
    box.operation_progress = None
    box.observation_mode = False
    owner.handoff._periodic(10.25)
    assert owner.handoff.units[1]["configured"]
    assert device.wire_calls.count(rh.CONFIG) == 1
    assert device.wire_calls[-1] == rh.POLL
    assert box.rfid_read_owner is None
    assert device.starts == device.keys == 0


@pytest.mark.parametrize("activity", ["printing", "load"])
def test_config_rechecks_idle_after_info_yields_to_host_activity(activity):
    owner, box, geometry, stats, device, provider = setup()
    box.acquire_rfid_read = RealBox.acquire_rfid_read.__get__(box)
    box.release_rfid_read = RealBox.release_rfid_read.__get__(box)
    exchange = device.cmd_send_data_with_response

    def send(body, timeout):
        result = exchange(body, timeout)
        if body[4] == rh.INFO:
            if activity == "printing":
                stats.state = "printing"
            else:
                box.operation_depth = 1
                box.operation_progress = {"kind": "load"}
        return result

    device.cmd_send_data_with_response = send
    owner.handoff._periodic(10.0)
    assert device.wire_calls == [rh.INFO]
    assert not owner.handoff.units[1]["configured"]
    stats.state = "standby"
    box.operation_depth = 0
    box.operation_progress = None
    owner.handoff._periodic(10.25)
    assert device.wire_calls == [rh.INFO, rh.CONFIG, rh.POLL]
    assert owner.handoff.units[1]["configured"]
    assert box.rfid_read_owner is None
    assert device.starts == device.keys == 0
