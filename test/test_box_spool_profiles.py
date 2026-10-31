"""SGF1 protocol and cache continuity. No serial device or RF operations."""
import math
import pathlib
import struct
import sys
from contextlib import contextmanager
from dataclasses import replace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_protocol
from extras import box_spool_profiles as sg
from extras.box_rfid_estimates import BoxRfidEstimates
from extras.box import Box as ActualBox
from types import SimpleNamespace
from extras.serial_485 import crc8

UID = bytes.fromhex("AE2CE2A0")


def frame(payload=b"", status=0, address=1):
    value = bytes((0xF7, address, len(payload) + 3, status, sg.CMD)) + payload
    return value + bytes((crc8(value[2:]),))


def snapshot_bytes(slot=0, uid=UID, profile=0, revision=0, total=330000,
                   percent=85, raw=510.0, flags=19, generation=1, used=0):
    p = bytearray(64)
    p[:4] = b"SGD1"
    p[4:7] = bytes((slot, flags, profile))
    struct.pack_into("<I4sfI4sIII", p, 8, generation, uid, raw, 100,
                     uid, revision, total, used)
    p[40:44] = bytes((percent, percent, 1, 4))
    if profile:
        struct.pack_into("<HHI", p, 44, 3000, 5100, 0)
        p[52:54] = b"\x01\x01"
    struct.pack_into("<I", p, 56, 200)
    p[60:64] = bytes((1, 0, 1, 0))
    return bytes(p)


class Wire:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def cmd_send_data_with_response(self, body, timeout):
        self.requests.append((body, timeout))
        return self.reply


def driver(payload=b"", status=0):
    wire = Wire(frame(payload, status))
    return sg.SpoolGeometryDriver(wire, 1), wire


def test_protocol_info_and_length_feature():
    d, w = driver(b"SGF1" + bytes((1, 4, 4, 31)) + struct.pack("<HH", 64, 20))
    assert d.info().features & sg.SLOT_LENGTH
    assert w.requests[0][0][4:] == bytes((sg.INFO,))
    assert w.requests[0][1] <= 0.5


@pytest.mark.parametrize("bad", [
    b"", b"SGF1" + bytes(8),
    b"SGF1" + bytes((2, 4, 4, 31)) + struct.pack("<HH", 64, 20),
    b"SGF1" + bytes((1, 4, 4, 63)) + struct.pack("<HH", 64, 20),
    b"SGF1" + bytes((1, 4, 4, 31)) + struct.pack("<HH", 63, 20),
])
def test_bad_info_fails_closed(bad):
    with pytest.raises(box_protocol.ProtocolError):
        driver(bad)[0].info()


@pytest.mark.parametrize("status,error", [
    (7, sg.SpoolGeometryBusy), (1, sg.SpoolGeometryUnsupported),
    (2, box_protocol.ProtocolError), (3, box_protocol.ProtocolError),
])
def test_status_validation(status, error):
    with pytest.raises(error):
        driver(status=status)[0].info()
    with pytest.raises(box_protocol.ProtocolError):
        driver(b"unexpected", status)[0].info()


def test_catalog_shape_and_geometry_without_shared_length():
    p = b"SGC1" + bytes((1, 1)) + struct.pack("<HHHII", 3000, 5100, 0, 0, 7)
    d, w = driver(p)
    result = d.set_catalog(1, 7, 3000, 5100, 0)
    assert result.revision == 7 and result.total_mm == 0
    assert w.requests[0][0][4:] == struct.pack(
        "<BBBBIHHI", sg.SET_CATALOG, 1, 0, 0, 7, 3000, 5100, 0)


def test_slot_length_and_bind_wire_are_uid_bound():
    p = b"SGL1" + bytes((2, 1, 0, 0)) + UID + struct.pack("<I", 327000)
    d, w = driver(p)
    assert d.set_length(2, UID, 327000).total_mm == 327000
    assert w.requests[-1][0][4:] == struct.pack(
        "<BBBB4sI", sg.SET_LENGTH, 2, 0, 0, UID, 327000)
    d, w = driver(snapshot_bytes(slot=2, profile=1, revision=7, total=327000))
    assert d.bind(2, 1, UID, 1, 7).applied_revision == 7
    assert w.requests[-1][0][4:] == struct.pack(
        "<BBBB4sII", sg.BIND, 2, 1, 0, UID, 1, 7)


@pytest.mark.parametrize("slot,uid,total", [
    (True, UID, 327000), (4, UID, 327000), (0, b"", 327000),
    (0, b"1234567", 327000), (0, b"\0" * 4, 327000),
    (0, UID, 9999), (0, UID, 2000001), (0, UID, False),
])
def test_invalid_length_never_reaches_transport(slot, uid, total):
    d, w = driver()
    with pytest.raises((ValueError, TypeError)):
        d.set_length(slot, uid, total)
    assert not w.requests


@pytest.mark.parametrize("offset,value", [(7, 1), (54, 1), (60, 2), (42, 3), (53, 2)])
def test_snapshot_reserved_and_boolean_fields(offset, value):
    p = bytearray(snapshot_bytes())
    p[offset] = value
    with pytest.raises(box_protocol.ProtocolError):
        sg.decode_snapshot(p, 0)


def test_usable_measure_requires_finite_raw_matching_uid_and_generation():
    assert sg.decode_snapshot(snapshot_bytes(), 0).usable_measurement
    for raw in (float("nan"), float("inf"), 1.0, 2001.0):
        with pytest.raises(box_protocol.ProtocolError):
            sg.decode_snapshot(snapshot_bytes(raw=raw), 0)
    assert not sg.decode_snapshot(snapshot_bytes(flags=0, raw=float("nan")), 0).usable_measurement
    with pytest.raises(box_protocol.ProtocolError):
        sg.decode_snapshot(snapshot_bytes(generation=0), 0)


def test_vendor_configuration_is_independent_of_material_and_color():
    values = sg.parse_geometries("Bambulab = 1, 300, 510\nSUNLU = 2, 301.44, 540")
    assert values["bambu"] == sg.Geometry("bambu", 1, 3000, 5100)
    assert values["sunlu"].core_c10 == 3014
    assert not sg.parse_geometries("")


@pytest.mark.parametrize("bad", [
    "bambu = 0,300,510", "bambu = 5,300,510",
    "bambu = 1,nan,510", "bambu = 1,300,300",
    "bambu = 1,300,510\nBambulab = 2,300,510",
    "bambu = 1,300,510\nsunlu = 1,300,510",
])
def test_bad_vendor_configuration(bad):
    with pytest.raises(ValueError):
        sg.parse_geometries(bad)


class Reactor:
    NEVER = float("inf")

    def __init__(self):
        self.now = 10.0
        self.timers = []

    def monotonic(self):
        return self.now

    def register_timer(self, callback):
        timer = [callback, self.NEVER]
        self.timers.append(timer)
        return timer

    def update_timer(self, timer, when):
        timer[1] = when


class Store:
    def __init__(self):
        self.values = {}
        self.data = {"runtime": {}}

    def setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value

    def save(self):
        self.values.update(self.data["runtime"])


class Stats:
    def __init__(self):
        self.state = "standby"
        self.filament_used = 0.0

    def get_status(self, now):
        return {"state": self.state, "filament_used": self.filament_used}


class Box(BoxRfidEstimates):
    def __init__(self):
        self.drivers = {1: object()}
        self.operation_depth = 0
        self.operation_progress = None
        self.rfid_read_owner = None
        self._cfs_runtime_write_owner = None
        self.change_engine = SimpleNamespace(pending=None)
        self.observation_mode = False
        self.store = Store()
        self.rfid_spools = {}
        self.rfid_percent = {}
        self.rfid_reported_percent = {}
        self.rfid_estimate_dirty = False
        self.last_rfid_estimate_save = 999999
        self.rfid_last_print_state = None
        self.rfid_last_usage_slot = None
        self.rfid_last_filament_used = None
        self.rfid_last_encoder_mm = None
        self.rfid_last_usage_source = None
        self.rfid_live_slots = set()
        self.refreshes = []

    acquire_cfs_runtime_write = ActualBox.acquire_cfs_runtime_write
    release_cfs_runtime_write = ActualBox.release_cfs_runtime_write

    def _address_slot(self, slot):
        return slot // 4 + 1, slot % 4

    def _cfs_live_remaining(self, slot):
        return True

    def profile(self, slot):
        return {"source": "rfid", "rfid_code": "BAMBU:PETG HF"}

    def _read_rfid_remaining(self, slot):
        self.refreshes.append(slot)
        self._apply_reported_remaining(slot, self.firmware.samples[slot].percent)
        self._persist_rfid_estimates(force=True)

    def add_spool(self, slot, uid=UID, remaining=100000.0, total=327000.0):
        self.rfid_spools[slot] = {
            "key": "tag:BAMBU:PETG HF:" + uid.hex().upper(),
            "total_mm": total, "remaining_mm": remaining, "cfs_percent": 32}
        self.rfid_percent[slot] = remaining / total * 100.0


class Transport:
    def __init__(self):
        self.active = False

    @contextmanager
    def request_session(self):
        assert not self.active, "nested session would deadlock the owned queue"
        self.active = True
        try:
            yield self
        finally:
            self.active = False


class Firmware:
    def __init__(self):
        self.features = 31
        self.catalogs = {n: sg.Catalog(n, False, 0, 0, 0, 0) for n in range(1, 5)}
        self.samples = {0: sg.decode_snapshot(snapshot_bytes(), 0)}
        self.lengths = {}
        self.calls = []

    def info(self):
        self.calls.append(("info",))
        return sg.Info(1, 4, 4, self.features)

    def catalog(self, profile_id):
        self.calls.append(("get_catalog", profile_id))
        return self.catalogs[profile_id]

    def set_catalog(self, profile_id, revision, core, full, total=0):
        self.calls.append(("set_catalog", profile_id, revision, core, full, total))
        self.catalogs[profile_id] = sg.Catalog(
            profile_id, bool(revision), core, full, total,
            revision or self.catalogs[profile_id].high_water_revision)
        return self.catalogs[profile_id]

    def precise_snapshot(self, slot):
        self.calls.append(("precision_unsupported", slot))
        raise sg.SpoolGeometryUnsupported("legacy firmware fixture")

    def snapshot(self, slot):
        self.calls.append(("snapshot", slot))
        return self.samples.get(slot, sg.decode_snapshot(
            snapshot_bytes(slot=slot, flags=0, generation=0), slot))

    def length(self, slot):
        self.calls.append(("get_length", slot))
        return self.lengths.get(slot, sg.SlotLength(
            slot, False, self.samples[slot].current_uid, 0))

    def set_length(self, slot, uid, total):
        self.calls.append(("set_length", slot, uid, total))
        assert uid == self.samples[slot].current_uid
        self.lengths[slot] = sg.SlotLength(slot, bool(total), uid, total)
        return self.lengths[slot]

    def bind(self, slot, profile_id, uid, generation, revision):
        self.calls.append(("bind", slot, profile_id, uid, generation, revision))
        old = self.samples[slot]
        assert generation == old.generation and uid == old.current_uid
        if not profile_id:
            self.samples[slot] = replace(old, profile_id=0, applied_revision=0,
                                         catalog_enabled=False, binding_matches_uid=False)
            return self.samples[slot]
        geometry = self.catalogs[profile_id]
        assert revision == geometry.revision and self.lengths[slot].configured
        core, full = geometry.core_c10 / 10, geometry.full_c10 / 10
        percent = max(1, min(100, int(
            100 * (old.raw_circumference_mm ** 2 - core ** 2)
            / (full ** 2 - core ** 2) + 0.5)))
        self.samples[slot] = replace(
            old, profile_id=profile_id, applied_revision=revision,
            total_mm=self.lengths[slot].total_mm, initial_percent=percent,
            percent=percent, core_c10=geometry.core_c10, full_c10=geometry.full_c10,
            catalog_enabled=True, binding_matches_uid=True)
        return self.samples[slot]


class Printer:
    def __init__(self, box, transport, stats):
        self.reactor = Reactor()
        self.objects = {"box": box, "serial_485 serial485": transport, "print_stats": stats}
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, name, callback):
        self.handlers[name] = callback


class Config:
    def __init__(self, printer, options=None):
        self.printer = printer
        self.options = options or {}

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getboolean(self, name, default=None):
        return self.options.get(name, default)

    def getint(self, name, default=None, **kw):
        return self.options.get(name, default)

    def getfloat(self, name, default=None, **kw):
        return self.options.get(name, default)

    def error(self, value):
        return ValueError(value)


def manager(options=None):
    box, fw, transport, stats = Box(), Firmware(), Transport(), Stats()
    printer = Printer(box, transport, stats)
    box.printer, box.firmware = printer, fw
    options = dict(options or {})
    options.setdefault("vendor_geometries", "bambu = 1,300,510")
    extra = sg.BoxSpoolProfiles(Config(printer, options))
    extra._driver = lambda transport, address: fw
    printer.objects["box_spool_profiles"] = extra
    extra._serial_ready()
    extra._box_ready()
    return extra, box, fw, stats


def mutations(fw, name):
    return [c for c in fw.calls if c[0] == name]


def test_status_command_reads_one_snapshot_without_mutation_or_scan():
    extra, box, fw, stats = manager()
    messages = []

    class Command:
        def get_int(self, name, default, **bounds):
            return default

        def respond_info(self, text):
            messages.append(text)

        def error(self, text):
            return RuntimeError(text)

    extra.cmd_status(Command())
    assert fw.calls == [("snapshot", 0)]
    assert len(messages) == 1
    assert "raw_circumference_mm=510.000" in messages[0]
    assert "UID=" + UID.hex().upper() in messages[0]
    assert "flags=0x13" in messages[0]


def test_catalog_upload_is_once_and_revision_increases_from_high_water():
    extra, box, fw, stats = manager()
    fw.catalogs[1] = sg.Catalog(1, False, 0, 0, 0, 8)
    extra._service(10.0)
    extra._service(15.0)
    assert mutations(fw, "set_catalog") == [("set_catalog", 1, 9, 3000, 5100, 0)]


def test_cache_restore_without_measure_never_scans_or_forces_full():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    fw.samples[0] = replace(fw.samples[0], flags=0)
    extra._service(10.0)
    box._apply_reported_remaining(0, 84)
    assert box.rfid_spools[0]["remaining_mm"] == 100000.0
    assert box.rfid_spools[0]["remaining_source"] == "kalico"
    box._apply_reported_remaining(0, 255)
    assert box.rfid_spools[0]["remaining_mm"] == 100000.0
    assert not mutations(fw, "bind") and not mutations(fw, "set_length")


def test_fresh_measure_binds_without_rf_and_updates_gauge_immediately():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    extra.on_tag_decoded(0, "bambu", UID)
    extra._service(10.0)
    assert mutations(fw, "set_length") == [("set_length", 0, UID, 327000)]
    assert len(mutations(fw, "bind")) == 1
    assert box.refreshes == [0] and box.rfid_percent[0] == 100.0
    assert box.store.setting("rfid_estimates")[box.rfid_spools[0]["key"]]["remaining_mm"] == 327000.0


def test_used_spool_is_measured_not_reset_to_full():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    fw.samples[0] = replace(fw.samples[0], raw_circumference_mm=460.0)
    extra._service(10.0)
    assert box.rfid_percent[0] == 71.0


def test_material_lengths_share_one_vendor_geometry():
    extra, box, fw, stats = manager()
    other_uid = bytes.fromhex("11223344")
    box.add_spool(0, total=327000)
    box.add_spool(1, other_uid, total=330000)
    fw.samples[1] = sg.decode_snapshot(snapshot_bytes(slot=1, uid=other_uid), 1)
    extra._service(10.0)
    assert len(mutations(fw, "set_catalog")) == 1
    assert fw.samples[0].profile_id == fw.samples[1].profile_id == 1
    assert fw.samples[0].total_mm == 327000
    assert fw.samples[1].total_mm == 330000


def test_cfs_reset_revokes_authority_until_a_new_valid_measure():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    extra._service(10.0)
    box.rfid_spools[0]["remaining_mm"] = 100000
    fw.catalogs[1] = sg.Catalog(1, False, 0, 0, 0, 0)
    fw.lengths.clear()
    fw.samples[0] = sg.decode_snapshot(snapshot_bytes(flags=0), 0)
    box._apply_reported_remaining(0, 84)
    assert box.rfid_spools[0]["remaining_mm"] == 100000
    extra._service(15.0)
    assert len(mutations(fw, "set_catalog")) == 2
    assert len(mutations(fw, "bind")) == 1
    box._apply_reported_remaining(0, 84)
    assert box.rfid_spools[0]["remaining_mm"] == 100000


def test_reconcile_is_idempotent_and_does_not_erase_consumption():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    extra._service(10.0)
    fw.samples[0] = replace(fw.samples[0], used_mm=400, percent=99)
    box.rfid_spools[0]["remaining_mm"] = 326600
    extra._service(15.0)
    assert len(mutations(fw, "bind")) == 1
    assert fw.samples[0].used_mm == 400
    assert box.rfid_spools[0]["remaining_mm"] == 326600


def test_changed_uid_and_stale_percentage_cannot_overwrite_cache():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    extra._service(10.0)
    box.rfid_spools[0]["remaining_mm"] = 100000
    box._apply_reported_remaining(0, 84)  # stale query, snapshot already 100
    assert box.rfid_spools[0]["remaining_mm"] == 100000
    fw.samples[0] = replace(fw.samples[0], current_uid=b"1234")
    box._apply_reported_remaining(0, 100)
    extra._service(15.0)
    assert box.rfid_spools[0]["remaining_mm"] == 100000
    assert len(mutations(fw, "bind")) == 1


@pytest.mark.parametrize("kind", ["printing", "paused", "operation", "observation"])
def test_catalog_mutations_wait_for_idle(kind):
    extra, box, fw, stats = manager()
    if kind in ("printing", "paused"):
        stats.state = kind
    elif kind == "operation":
        box.operation_depth = 1
    else:
        box.observation_mode = True
    extra._service(10.0)
    assert not mutations(fw, "set_catalog")


def test_legacy_sgf1_does_not_get_shared_vendor_length():
    extra, box, fw, stats = manager()
    fw.features = 15
    box.add_spool(0)
    extra._service(10.0)
    assert not mutations(fw, "set_catalog")
    assert not extra.allows_cfs_remaining(0, 84)
    assert "per-slot length" in extra.last_error


def test_saved_vendor_identity_restores_without_tag_reread():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    extra.on_tag_decoded(0, "Bambulab", UID)
    saved = box.store.setting("rfid_geometry_vendors")
    extra.slot_identities.clear()
    assert extra._identity(0) == (box.rfid_spools[0]["key"], "bambu", UID)
    assert saved[box.rfid_spools[0]["key"]]["vendor"] == "bambu"


def test_local_odometer_continues_while_hardware_measure_is_missing():
    from types import SimpleNamespace
    extra, box, fw, stats = manager()
    box.add_spool(0)
    fw.samples[0] = replace(fw.samples[0], flags=0)
    extra._service(10.0)
    stats.state = "printing"
    snap = SimpleNamespace(loaded_slot=0, tracking=True, encoder_mm=1000.0)
    box.is_physical_slot = lambda slot: True
    box._track_rfid_usage(11.0, snap)
    stats.filament_used = 400
    snap.encoder_mm = 600.0
    box._track_rfid_usage(12.0, snap)
    box._apply_reported_remaining(0, 255)
    assert box.rfid_spools[0]["remaining_mm"] == 99600.0


def test_reassigned_geometry_unbinds_metadata_before_catalog_change():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    fw.catalogs[1] = sg.Catalog(1, True, 2900, 5000, 0, 2)
    fw.samples[0] = replace(fw.samples[0], profile_id=1, applied_revision=2,
                             core_c10=2900, full_c10=5000, catalog_enabled=True,
                             binding_matches_uid=True, used_mm=400)
    old_bind = fw.bind

    def no_stale_reanchor(slot, profile_id, *args):
        if profile_id and fw.samples[slot].used_mm:
            raise sg.SpoolGeometryBusy("consumption advanced")
        return old_bind(slot, profile_id, *args)

    fw.bind = no_stale_reanchor
    extra._service(10.0)
    operations = [c for c in fw.calls if c[0] in ("bind", "set_catalog")]
    assert operations[0][:3] == ("bind", 0, 0)
    assert operations[1][:3] == ("set_catalog", 1, 3)
    assert fw.samples[0].profile_id == 0 and fw.samples[0].used_mm == 400
    assert box.rfid_spools[0]["remaining_mm"] == 100000
    assert not extra.allows_cfs_remaining(0, 85)
    assert box._cfs_runtime_write_owner is None


def test_removed_vendor_entry_is_disabled_and_old_binding_cleared():
    extra, box, fw, stats = manager()
    fw.catalogs[2] = sg.Catalog(2, True, 3100, 5200, 0, 9)
    fw.samples[1] = sg.decode_snapshot(snapshot_bytes(
        slot=1, profile=2, revision=9, used=500), 1)
    extra._service(10.0)
    assert not fw.catalogs[2].enabled
    assert fw.catalogs[2].high_water_revision == 9
    assert fw.samples[1].profile_id == 0 and fw.samples[1].used_mm == 500


def test_empty_vendor_config_cleans_previous_ram_catalog_without_guessing():
    extra, box, fw, stats = manager({"vendor_geometries": ""})
    fw.catalogs[1] = sg.Catalog(1, True, 3000, 5100, 0, 2)
    extra._service(10.0)
    assert not fw.catalogs[1].enabled
    assert extra.handoff.enabled
    assert not [c for c in fw.calls if c[0] == "set_catalog" and c[2]]


def test_catalog_write_interlocks_real_box_operation_and_releases_on_error():
    from extras.box import BoxError
    extra, box, fw, stats = manager()
    old_set = fw.set_catalog
    observed = []

    def check_mutex(*args):
        observed.append(box._cfs_runtime_write_owner)
        with pytest.raises(BoxError, match="runtime config write"):
            with ActualBox._operation(box):
                raise AssertionError("operation entered while catalog was updating")
        return old_set(*args)

    fw.set_catalog = check_mutex
    extra._service(10.0)
    assert observed == ["box_spool_profiles"]
    assert box._cfs_runtime_write_owner is None


def test_busy_catalog_update_after_unbind_keeps_cache_and_retries_without_rf():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    fw.catalogs[1] = sg.Catalog(1, True, 2900, 5000, 0, 2)
    fw.samples[0] = replace(fw.samples[0], profile_id=1, applied_revision=2,
                             core_c10=2900, full_c10=5000, catalog_enabled=True,
                             binding_matches_uid=True, used_mm=400)
    old_set = fw.set_catalog
    fw.set_catalog = lambda *args: (_ for _ in ()).throw(
        sg.SpoolGeometryBusy("automatic read began"))
    extra._service(10.0)
    assert fw.samples[0].profile_id == 0
    assert box.rfid_spools[0]["remaining_mm"] == 100000
    assert box._cfs_runtime_write_owner is None
    fw.samples[0] = replace(fw.samples[0], flags=0)
    fw.set_catalog = old_set
    extra._service(15.0)
    assert fw.catalogs[1].revision == 3
    assert not box.refreshes


def test_geometry_waits_until_manual_rfid_owner_releases():
    extra, box, fw, stats = manager()
    box.rfid_read_owner = "manual RFID"
    extra._service(10.0)
    assert not mutations(fw, "set_catalog")
    box.rfid_read_owner = None
    extra._service(15.0)
    assert len(mutations(fw, "set_catalog")) == 1


def test_removed_geometry_can_disable_when_stale_measure_cannot_unbind():
    extra, box, fw, stats = manager({"vendor_geometries": ""})
    fw.catalogs[1] = sg.Catalog(1, True, 2900, 5000, 0, 2)
    fw.samples[0] = replace(fw.samples[0], profile_id=1, applied_revision=2,
                             current_uid=b"1234", measured_uid=UID)
    extra._service(10.0)
    assert not fw.catalogs[1].enabled
    assert not mutations(fw, "bind")


def test_catalog_reassignment_waits_when_old_binding_cannot_be_unbound_safely():
    extra, box, fw, stats = manager()
    box.add_spool(0)
    fw.catalogs[1] = sg.Catalog(1, True, 2900, 5000, 0, 2)
    fw.samples[0] = replace(fw.samples[0], profile_id=1, applied_revision=2,
                             current_uid=b"1234", measured_uid=UID)
    extra._service(10.0)
    assert fw.catalogs[1].revision == 2
    assert not mutations(fw, "set_catalog") and not mutations(fw, "bind")
    assert box.rfid_spools[0]["remaining_mm"] == 100000
    assert "consistent UID" in extra.last_error


def test_sgf_snapshot_uses_existing_diag_envelope_and_exact_subcommand():
    d, wire = driver(snapshot_bytes())
    assert d.snapshot(0).slot == 0
    assert wire.requests == [(bytes.fromhex("0105FF571700"), 0.5)]


def test_status_detail_reads_only_passive_internal_metadata_and_hides_scratch():
    extra, box, fw, stats = manager()
    messages, requests = [], []
    private = b"PRIVATE_RFID_SCRATCH"
    record = bytearray(76)
    record[:len(private)] = private
    record[75] = 4
    transport = extra._transport()

    def exchange(body, timeout):
        assert transport.active
        requests.append((bytes(body), timeout))
        return frame(bytes(record))

    transport.cmd_send_data_with_response = exchange

    class Command:
        def get_int(self, name, default, **bounds):
            return {"DETAIL": 1, "SLOT": 1}.get(name, default)

        def respond_info(self, text):
            messages.append(text)

        def error(self, text):
            return RuntimeError(text)

    extra.cmd_status(Command())
    assert fw.calls == [("snapshot", 1)]
    assert requests == [(bytes.fromhex("0106FF57060001"), 0.5)]
    assert len(messages) == 1 and "record_byte75=4" in messages[0]
    assert private.decode() not in messages[0]
    assert private.hex().upper() not in messages[0]
    assert "raw=" not in messages[0]


@pytest.mark.parametrize("response", [
    None,
    frame(status=1),
    frame(bytes(75)),
])
def test_status_detail_rejects_missing_failed_or_truncated_record(response):
    extra, box, fw, stats = manager()
    requests, messages = [], []
    transport = extra._transport()

    def exchange(body, timeout):
        requests.append(bytes(body))
        return response

    transport.cmd_send_data_with_response = exchange

    class Command:
        def get_int(self, name, default, **bounds):
            return 1 if name == "DETAIL" else default

        def respond_info(self, text):
            messages.append(text)

        def error(self, text):
            return RuntimeError(text)

    with pytest.raises(RuntimeError, match="CFS spool status failed"):
        extra.cmd_status(Command())
    assert requests == [bytes.fromhex("0106FF57060000")]
    assert fw.calls == [("snapshot", 0)]
    assert not messages
