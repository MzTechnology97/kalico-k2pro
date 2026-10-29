# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Vendor spool geometry in CFS RAM, independent of per-spool estimates.

This optional extra never starts a scan just to recover a saved estimate.
Geometry is configured explicitly; no Bambu calibration is assumed.
"""

import logging
import math
import struct
from dataclasses import dataclass
from contextlib import contextmanager

from extras import box_protocol
from extras.serial_485 import build_485_body

CMD = 0x57
SLOT_LENGTH = 0x10
INFO, READ, SET_CATALOG, GET_CATALOG, BIND = range(0x16, 0x1B)
GET_LENGTH, SET_LENGTH = 0x1B, 0x1C


class SpoolGeometryError(RuntimeError):
    pass


class SpoolGeometryBusy(SpoolGeometryError):
    pass


class SpoolGeometryUnsupported(SpoolGeometryError):
    pass


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("%s must be %d..%d" % (name, low, high))
    return value


def _uid(value):
    if isinstance(value, str):
        try:
            value = bytes.fromhex(value)
        except ValueError:
            raise ValueError("UID must contain four hexadecimal bytes")
    value = bytes(value)
    if len(value) != 4 or not any(value):
        raise ValueError("UID must contain four nonzero identity bytes")
    return value


def vendor_name(value):
    value = str(value).strip().lower()
    return {"bambulab": "bambu", "bambu lab": "bambu"}.get(value, value)


@dataclass(frozen=True)
class Geometry:
    vendor: str
    profile_id: int
    core_c10: int
    full_c10: int


def parse_geometries(value):
    """Lines: vendor = profile_id, core_circumference_mm, full_circumference_mm."""
    geometries = {}
    identifiers = set()
    for line in str(value or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            vendor, fields = line.split("=", 1)
            vendor = vendor_name(vendor)
            raw_id, raw_core, raw_full = (x.strip() for x in fields.split(","))
            profile_id = int(raw_id)
            core, full = float(raw_core), float(raw_full)
            if not vendor or not all(
                    c.isalnum() or c in "_-" for c in vendor):
                raise ValueError("invalid vendor name")
            if not math.isfinite(core) or not math.isfinite(full):
                raise ValueError("circumferences must be finite")
            core_c10 = int(math.floor(core * 10.0 + 0.5))
            full_c10 = int(math.floor(full * 10.0 + 0.5))
            _integer(profile_id, 1, 4, "profile ID")
            _integer(core_c10, 500, 10000, "core circumference in 0.1 mm")
            _integer(full_c10, core_c10 + 100, 15000,
                     "full circumference in 0.1 mm")
            if vendor in geometries or profile_id in identifiers:
                raise ValueError("vendor and profile IDs must be unique")
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid vendor_geometries line %r: %s" % (line, exc))
        geometries[vendor] = Geometry(vendor, profile_id, core_c10, full_c10)
        identifiers.add(profile_id)
    return geometries


@dataclass(frozen=True)
class Info:
    version: int
    slots: int
    profiles: int
    features: int


@dataclass(frozen=True)
class Catalog:
    profile_id: int
    enabled: bool
    core_c10: int
    full_c10: int
    total_mm: int
    high_water_revision: int

    @property
    def revision(self):
        return self.high_water_revision if self.enabled else 0


@dataclass(frozen=True)
class SlotLength:
    slot: int
    configured: bool
    uid: bytes
    total_mm: int


@dataclass(frozen=True)
class Snapshot:
    slot: int
    flags: int
    profile_id: int
    generation: int
    measured_uid: bytes
    raw_circumference_mm: float
    measurement_tick: int
    current_uid: bytes
    applied_revision: int
    total_mm: int
    used_mm: int
    initial_percent: int
    percent: int
    valid: bool
    runtime_type: int
    core_c10: int
    full_c10: int
    catalog_total_mm: int
    catalog_enabled: bool
    binding_matches_uid: bool
    tick: int
    hardware_flag: int
    manager_flag: int
    box_state: int

    @property
    def usable_measurement(self):
        return ((self.flags & 3) == 3 and self.generation > 0
                and self.measured_uid == self.current_uid
                and any(self.current_uid)
                and math.isfinite(self.raw_circumference_mm)
                and 50.0 <= self.raw_circumference_mm <= 2000.0)


def decode_info(p):
    if (len(p) != 12 or p[:4] != b"SGF1"
            or p[4:7] != bytes((1, 4, 4))
            or p[7] not in (0x0F, 0x1F)
            or struct.unpack_from("<HH", p, 8) != (64, 20)):
        raise box_protocol.ProtocolError("unsupported SGF1 INFO shape")
    return Info(*p[4:8])


def decode_catalog(p, expected_id):
    if (len(p) != 20 or p[:4] != b"SGC1" or p[4] != expected_id
            or p[5] not in (0, 1) or p[10:12] != b"\0\0"):
        raise box_protocol.ProtocolError("invalid SGC1 catalog reply")
    core, full = struct.unpack_from("<HH", p, 6)
    total, revision = struct.unpack_from("<II", p, 12)
    if p[5]:
        if (not revision or not 500 <= core <= 10000
                or not core + 100 <= full <= 15000
                or (total and not 10000 <= total <= 2000000)):
            raise box_protocol.ProtocolError("invalid enabled SGC1 values")
    elif core or full or total:
        raise box_protocol.ProtocolError("disabled SGC1 has parameters")
    return Catalog(expected_id, bool(p[5]), core, full, total, revision)


def decode_length(p, expected_slot):
    if (len(p) != 16 or p[:4] != b"SGL1" or p[4] != expected_slot
            or p[5] not in (0, 1) or p[6:8] != b"\0\0"):
        raise box_protocol.ProtocolError("invalid SGL1 slot length reply")
    total = struct.unpack_from("<I", p, 12)[0]
    if (p[5] and (not any(p[8:12]) or not 10000 <= total <= 2000000)
            or not p[5] and total):
        raise box_protocol.ProtocolError("inconsistent SGL1 slot length")
    return SlotLength(expected_slot, bool(p[5]), p[8:12], total)


def decode_snapshot(p, expected_slot):
    if (len(p) != 64 or p[:4] != b"SGD1" or p[4] != expected_slot
            or p[5] & ~31 or p[6] > 4 or p[7]
            or p[42] not in (0, 1) or p[52] not in (0, 1)
            or p[53] not in (0, 1) or p[54:56] != b"\0\0"
            or p[60] != 1 or p[61] not in (0, 1)):
        raise box_protocol.ProtocolError("invalid SGD1 snapshot reply")
    values = struct.unpack_from("<II", p, 28)
    core, full = struct.unpack_from("<HH", p, 44)
    result = Snapshot(
        p[4], p[5], p[6], struct.unpack_from("<I", p, 8)[0], p[12:16],
        struct.unpack_from("<f", p, 16)[0], struct.unpack_from("<I", p, 20)[0],
        p[24:28], values[0], values[1], struct.unpack_from("<I", p, 36)[0],
        p[40], p[41], bool(p[42]), p[43], core, full,
        struct.unpack_from("<I", p, 48)[0], bool(p[52]), bool(p[53]),
        struct.unpack_from("<I", p, 56)[0], p[61], p[62], p[63])
    if result.flags & 3 == 3 and not result.usable_measurement:
        raise box_protocol.ProtocolError("SGD1 marks an invalid measure usable")
    return result


class SpoolGeometryDriver:
    """Bounded exchanges through the existing owned RS-485 request queue."""

    def __init__(self, transport, address):
        self.transport = transport
        self.address = _integer(address, 1, 4, "address")

    def _exchange(self, payload, timeout=0.5):
        body = build_485_body(self.address, CMD, payload, header_byte=0xFF)
        frame = self.transport.cmd_send_data_with_response(body, timeout)
        if not frame:
            raise SpoolGeometryError("CFS spool geometry request timed out")
        reply = box_protocol.decode_reply(
            frame, self.address, CMD, context="spool_geometry")
        if reply.status:
            if reply.payload:
                raise box_protocol.ProtocolError(
                    "SGF1 rejected request unexpectedly has a payload")
            if reply.status == 7:
                raise SpoolGeometryBusy("CFS is busy")
            if reply.status == 1:
                raise SpoolGeometryUnsupported(
                    "SGF1 unsupported, stale or invalid request")
            raise box_protocol.ProtocolError(
                "unexpected SGF1 status %d" % reply.status)
        return reply.payload

    def info(self):
        return decode_info(self._exchange(bytes((INFO,))))

    def snapshot(self, slot):
        _integer(slot, 0, 3, "slot")
        return decode_snapshot(self._exchange(bytes((READ, slot))), slot)

    def catalog(self, profile_id):
        _integer(profile_id, 1, 4, "profile ID")
        return decode_catalog(
            self._exchange(bytes((GET_CATALOG, profile_id))), profile_id)

    def set_catalog(self, profile_id, revision, core_c10, full_c10, total_mm=0):
        _integer(profile_id, 1, 4, "profile ID")
        _integer(revision, 0, 0xFFFFFFFF, "revision")
        if revision:
            _integer(core_c10, 500, 10000, "core circumference")
            _integer(full_c10, core_c10 + 100, 15000, "full circumference")
            if total_mm:
                _integer(total_mm, 10000, 2000000, "total mm")
            elif type(total_mm) is not int:
                raise ValueError("total mm must be an integer")
        elif core_c10 or full_c10 or total_mm:
            raise ValueError("disabled catalog parameters must be zero")
        payload = struct.pack("<BBBBIHHI", SET_CATALOG, profile_id, 0, 0,
                              revision, core_c10, full_c10, total_mm)
        return decode_catalog(self._exchange(payload), profile_id)

    def length(self, slot):
        _integer(slot, 0, 3, "slot")
        return decode_length(self._exchange(bytes((GET_LENGTH, slot))), slot)

    def set_length(self, slot, uid, total_mm):
        _integer(slot, 0, 3, "slot")
        if total_mm:
            _integer(total_mm, 10000, 2000000, "total mm")
        elif type(total_mm) is not int:
            raise ValueError("total mm must be an integer")
        payload = struct.pack("<BBBB4sI", SET_LENGTH, slot, 0, 0,
                              _uid(uid), total_mm)
        return decode_length(self._exchange(payload), slot)

    def bind(self, slot, profile_id, uid, generation, revision):
        _integer(slot, 0, 3, "slot")
        _integer(profile_id, 0, 4, "profile ID")
        _integer(generation, 1, 0xFFFFFFFF, "generation")
        _integer(revision, 1 if profile_id else 0,
                 0xFFFFFFFF if profile_id else 0, "revision")
        payload = struct.pack("<BBBB4sII", BIND, slot, profile_id, 0,
                              _uid(uid), generation, revision)
        return decode_snapshot(self._exchange(payload), slot)


class BoxSpoolProfiles:
    """Upload vendor geometries and reconcile only already acquired measures."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.serial_name = config.get("serial", "serial485")
        self.enabled = config.getboolean("enabled", True)
        self.fixed_address = config.getint("address", None, minval=1, maxval=4)
        self.interval = config.getfloat(
            "reconcile_interval", 5.0, minval=1.0, maxval=60.0)
        try:
            self.geometries = parse_geometries(config.get("vendor_geometries", ""))
        except ValueError as exc:
            raise config.error(str(exc))
        self.serial = None
        self.units = {}
        self.slot_identities = {}
        self.last_error = None
        self._generation = 0
        self._timer = self.reactor.register_timer(self._service)
        from extras.box_rfid_handoff import RfidHandoff
        self.handoff = RfidHandoff(self, config)
        self.printer.register_event_handler("serial_485:ready", self._serial_ready)
        self.printer.register_event_handler("box:ready", self._box_ready)
        gcode = self.printer.lookup_object("gcode", None)
        if gcode is not None:
            gcode.register_command(
                "BOX_SPOOL_GEOMETRY_STATUS", self.cmd_status,
                desc="Read the CFS spool measurement without scanning or moving")

    def _box(self):
        return self.printer.lookup_object("box", None)

    def _serial_ready(self, *args):
        self.serial = self.printer.lookup_object(
            "serial_485 %s" % self.serial_name, None)
        self._generation += 1
        self.units.clear()
        self.handoff.reset()

    def _box_ready(self, *args):
        self._generation += 1
        self.units.clear()
        self.handoff.reset()
        self.reactor.update_timer(self._timer, self.reactor.monotonic())

    def _transport(self):
        if self.serial is None:
            self.serial = self.printer.lookup_object(
                "serial_485 %s" % self.serial_name, None)
        if self.serial is None:
            raise SpoolGeometryError("RS-485 is not ready")
        return self.serial

    def _driver(self, transport, address):
        return SpoolGeometryDriver(transport, address)

    def _online(self):
        box = self._box()
        addresses = sorted(getattr(box, "drivers", {}))
        return [a for a in addresses
                if self.fixed_address is None or a == self.fixed_address]

    def _can_mutate(self):
        box = self._box()
        if (box is None or getattr(box, "observation_mode", False)
                or getattr(box, "operation_depth", 0)
                or getattr(box, "rfid_read_owner", None) is not None):
            return False
        stats = self.printer.lookup_object("print_stats", None)
        state = (stats.get_status(self.reactor.monotonic()).get("state")
                 if stats is not None else None)
        return state not in ("printing", "paused")

    @contextmanager
    def _write_guard(self):
        box = self._box()
        if not self._can_mutate():
            raise SpoolGeometryBusy("spool geometry waits for idle")
        owner = "box_spool_profiles"
        try:
            box.acquire_cfs_runtime_write(owner)
        except Exception as exc:
            raise SpoolGeometryBusy(str(exc))
        try:
            yield
        finally:
            box.release_cfs_runtime_write(owner)

    def _replace_catalog(self, driver, entry, geometry):
        with self._write_guard():
            # A numeric profile ID can be reassigned to another vendor. Clear
            # its old metadata binding first; BIND0 preserves runtime/counter.
            for local in range(4):
                sample = driver.snapshot(local)
                if sample.profile_id == entry.profile_id:
                    if (not sample.generation or not any(sample.current_uid)
                            or sample.measured_uid != sample.current_uid):
                        if geometry is None:
                            # Disabling is safe even when a stale measurement
                            # cannot satisfy the conservative BIND0 contract.
                            continue
                        raise SpoolGeometryBusy(
                            "catalog reassignment waits for a consistent UID measure")
                    unbound = driver.bind(local, 0, sample.current_uid,
                                          sample.generation, 0)
                    if unbound.profile_id:
                        raise box_protocol.ProtocolError("CFS unbind readback differs")
            if geometry is None:
                driver.set_catalog(entry.profile_id, 0, 0, 0, 0)
                actual = driver.catalog(entry.profile_id)
                if (actual.enabled or actual.core_c10 or actual.full_c10
                        or actual.total_mm
                        or actual.high_water_revision != entry.high_water_revision):
                    raise box_protocol.ProtocolError("CFS disabled catalog readback differs")
                return actual
            if entry.high_water_revision == 0xFFFFFFFF:
                raise SpoolGeometryError("catalog revision exhausted; CFS restart required")
            revision = entry.high_water_revision + 1
            driver.set_catalog(geometry.profile_id, revision,
                               geometry.core_c10, geometry.full_c10, 0)
            actual = driver.catalog(geometry.profile_id)
            if (not actual.enabled or actual.revision != revision
                    or (actual.core_c10, actual.full_c10, actual.total_mm)
                    != (geometry.core_c10, geometry.full_c10, 0)):
                raise box_protocol.ProtocolError("CFS vendor geometry readback differs")
            return actual

    def _sync_catalog(self, address):
        with self._transport().request_session() as transport:
            driver = self._driver(transport, address)
            info = driver.info()
            if not info.features & SLOT_LENGTH:
                raise SpoolGeometryUnsupported(
                    "vendor geometries require the per-slot length extension")
            revisions = {}
            desired = {g.profile_id: g for g in self.geometries.values()}
            for profile_id in range(1, 5):
                entry = driver.catalog(profile_id)
                geometry = desired.get(profile_id)
                if geometry is None:
                    if entry.enabled:
                        self._replace_catalog(driver, entry, None)
                    continue
                if not (entry.enabled
                        and (entry.core_c10, entry.full_c10, entry.total_mm)
                        == (geometry.core_c10, geometry.full_c10, 0)):
                    entry = self._replace_catalog(driver, entry, geometry)
                revisions[geometry.profile_id] = entry.revision
        self.units[address] = {"supported": True, "revisions": revisions}

    def on_tag_decoded(self, slot, vendor, uid, total_mm=None):
        vendor = vendor_name(vendor)
        if not self.enabled or vendor not in self.geometries:
            return
        uid = _uid(uid)
        box = self._box()
        spool = getattr(box, "rfid_spools", {}).get(slot, {})
        key = spool.get("key")
        if not key:
            return
        self.slot_identities[slot] = (key, vendor, uid)
        saved = dict(box.store.setting("rfid_geometry_vendors", {}) or {})
        saved[key] = {"vendor": vendor, "uid": uid.hex().upper()}
        box.store.set_setting("rfid_geometry_vendors", saved)
        self.reactor.update_timer(self._timer, self.reactor.monotonic())

    def _identity(self, slot):
        box = self._box()
        spool = getattr(box, "rfid_spools", {}).get(slot, {})
        key = spool.get("key")
        if not key:
            self.slot_identities.pop(slot, None)
            return None
        identity = self.slot_identities.get(slot)
        if identity is not None and identity[0] == key:
            return identity
        saved = box.store.setting("rfid_geometry_vendors", {}) or {}
        entry = saved.get(key) if isinstance(saved, dict) else None
        # Migration of existing Bambu decoder entries: the RFID code and
        # stable Bambu identity were both assigned by the existing decoder.
        if not isinstance(entry, dict):
            profile = box.profile(slot)
            if (str(profile.get("rfid_code", "")).startswith("BAMBU:")
                    and str(key).startswith("tag:BAMBU:")):
                entry = {"vendor": "bambu", "uid": str(key).rsplit(":", 1)[-1]}
        if isinstance(entry, dict):
            try:
                vendor, uid = vendor_name(entry.get("vendor")), _uid(entry.get("uid"))
            except (ValueError, TypeError):
                return None
            if vendor in self.geometries:
                identity = (key, vendor, uid)
                self.slot_identities[slot] = identity
                return identity
        return None

    def _anchor_matches(self, sample, geometry, uid, total_mm, revision):
        return (sample.current_uid == uid and sample.profile_id == geometry.profile_id
                and sample.applied_revision == revision
                and sample.catalog_enabled and sample.binding_matches_uid
                and sample.valid and sample.runtime_type == 4
                and sample.total_mm == total_mm
                and (sample.core_c10, sample.full_c10)
                == (geometry.core_c10, geometry.full_c10))

    def _sync_slot(self, slot):
        identity = self._identity(slot)
        if identity is None:
            return
        _key, vendor, uid = identity
        geometry = self.geometries[vendor]
        box = self._box()
        address, local = box._address_slot(slot)
        unit = self.units.get(address, {})
        revision = unit.get("revisions", {}).get(geometry.profile_id)
        if not revision:
            return
        spool = box.rfid_spools.get(slot, {})
        total = spool.get("total_mm")
        if not isinstance(total, (int, float)) or not math.isfinite(total):
            return
        total = int(round(total))
        if not 10000 <= total <= 2000000:
            return
        with self._transport().request_session() as transport:
            driver = self._driver(transport, address)
            sample = driver.snapshot(local)
            if self._anchor_matches(sample, geometry, uid, total, revision):
                return
            if (sample.current_uid != uid or not sample.usable_measurement
                    or not self._can_mutate()):
                return
            with self._write_guard():
                length = driver.length(local)
                if not (length.configured and length.uid == uid and length.total_mm == total):
                    length = driver.set_length(local, uid, total)
                    if not (length.configured and length.uid == uid
                            and length.total_mm == total):
                        raise box_protocol.ProtocolError("CFS slot length readback differs")
                sample = driver.bind(local, geometry.profile_id, uid,
                                     sample.generation, revision)
                if not self._anchor_matches(sample, geometry, uid, total, revision):
                    raise box_protocol.ProtocolError("CFS binding readback differs")
        return True

    def manages_slot(self, slot):
        if not self.enabled:
            return False
        if self._identity(slot) is not None:
            return True
        box = self._box()
        profile = box.profile(slot) if box is not None else {}
        code = str(profile.get("rfid_code", "")).split(":", 1)[0]
        return vendor_name(code) in self.geometries

    def allows_cfs_remaining(self, slot, value=None):
        """A passive proof before a CFS value may replace a saved estimate."""
        if not self.enabled:
            return True
        identity = self._identity(slot)
        if identity is None:
            return not self.manages_slot(slot)
        _key, vendor, uid = identity
        geometry = self.geometries[vendor]
        box = self._box()
        address, local = box._address_slot(slot)
        unit = self.units.get(address, {})
        revision = unit.get("revisions", {}).get(geometry.profile_id)
        if not revision:
            return False
        total = box.rfid_spools.get(slot, {}).get("total_mm")
        if not isinstance(total, (int, float)) or not math.isfinite(total):
            return False
        try:
            with self._transport().request_session() as transport:
                sample = self._driver(transport, address).snapshot(local)
            return self._anchor_matches(
                sample, geometry, uid, int(round(total)), revision) and (
                    value is None or sample.percent == value)
        except (SpoolGeometryError, box_protocol.ProtocolError, RuntimeError, OSError):
            return False

    def _service(self, eventtime):
        if not self.enabled:
            return self.reactor.NEVER
        box = self._box()
        if box is None or self.serial is None:
            return eventtime + self.interval
        if getattr(box, "operation_depth", 0):
            return eventtime + self.interval
        for address in self._online():
            try:
                self._sync_catalog(address)
                for slot in tuple(getattr(box, "rfid_spools", {})):
                    if box._address_slot(slot)[0] == address:
                        if self._sync_slot(slot):
                            box._read_rfid_remaining(slot)
                self.last_error = None
            except Exception as exc:
                if self.last_error != str(exc):
                    logging.info("box_spool_profiles: %s", exc)
                self.last_error = str(exc)
        return eventtime + self.interval

    def get_status(self, eventtime):
        return {"enabled": self.enabled,
                "vendors": sorted(self.geometries),
                "cfs": {address: dict(unit) for address, unit in self.units.items()},
                "last_error": self.last_error,
                "single_cycle_rfid": self.handoff.enabled,
                "handoff_error": self.handoff.last_error,
                "handoff": self.handoff.get_status(eventtime)}

    def cmd_status(self, gcmd):
        online = self._online()
        default = self.fixed_address or (online[0] if online else 1)
        address = gcmd.get_int("ADDRESS", default, minval=1, maxval=4)
        slot = gcmd.get_int("SLOT", 0, minval=0, maxval=3)
        detail = gcmd.get_int("DETAIL", 0, minval=0, maxval=1)
        extra = ""
        try:
            with self._transport().request_session() as transport:
                sample = self._driver(transport, address).snapshot(slot)
                if detail:
                    from extras.box_rfid_diag import RfidDiagDriver, MemoryReply
                    record = RfidDiagDriver(transport, address).internal_record(
                        slot, timeout=0.5)
                    if (not isinstance(record, MemoryReply) or record.status
                            or len(record.data) != 76):
                        raise SpoolGeometryError(
                            "passive internal record is unavailable or malformed")
                    # Only metadata byte75 is exposed; bytes4..19 can contain
                    # capture/key scratch and must never be printed here.
                    extra = " record_byte75=%d" % record.data[75]
        except Exception as exc:
            raise gcmd.error("CFS spool status failed: %s" % exc)
        gcmd.respond_info(
            "CFS %d slot %d: UID=%s raw_circumference_mm=%.3f "
            "generation=%d flags=0x%02X geometry_id=%d revision=%d "
            "total_mm=%d used_mm=%d initial=%d%% current=%d%%"
            % (address, slot, sample.current_uid.hex().upper(),
               sample.raw_circumference_mm, sample.generation, sample.flags,
               sample.profile_id, sample.applied_revision, sample.total_mm,
               sample.used_mm, sample.initial_percent, sample.percent) + extra)


def load_config(config):
    return BoxSpoolProfiles(config)
