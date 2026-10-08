# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Bambu Lab RFID fallback support for K2-OpenHost.

Design constraints:
- Creality's stock CFS RFID path remains primary and untouched.
- Passive UID/ATQA/SAK inspection is always safe.
- Authenticated block reads are used only when the CFS diagnostic firmware
  explicitly advertises CAP_AUTH_A.
- There are no tag-write primitives in this module.
- Vendor parsing and profile selection happen entirely on the CM5.
"""

from dataclasses import dataclass
import hashlib
import hmac
import logging
import struct

from extras import box_rfid_diag as diag
from extras import box_protocol
from extras.box_rfid_diag import RfidDiagDriver, TagInfoReply, MemoryReply


BAMBU_HKDF_SALT = bytes.fromhex("9A759CF2C4F7CAFF222CB9769B41BC96")
BAMBU_HKDF_INFO = b"RFID-A\x00"
BAMBU_SECTORS = 16
BAMBU_KEY_LEN = 6
BAMBU_REQUIRED_BLOCKS = (1, 2, 4, 5, 6, 14)
BAMBU_CAPTURE1_MAGIC = b"K2C3"
BAMBU_CAPTURE2_MAGIC = b"K2D3"
BAMBU_STOCK_MASK = 0x07


class BambuRfidError(RuntimeError):
    pass


class BambuRfidUnsupported(BambuRfidError):
    pass


def _log_decoded_tag(tag, source):
    data = tag.as_dict()
    blocks = data.get("blocks") if isinstance(data.get("blocks"), dict) else {}
    logging.info(
        "box_rfid_bambu: decoded source=%s slot=%s uid=%s atqa=%s sak=%02X "
        "detail=%r material=%s profile=%r color=%s rgba=%s block4=%s block5=%s",
        source, data.get("slot"), data.get("uid", ""), data.get("atqa", ""),
        int(data.get("sak") or 0), data.get("detailed_filament_type", ""),
        data.get("material", ""), data.get("profile_name", ""),
        data.get("color", ""), data.get("color_rgba", ""),
        blocks.get(4, ""), blocks.get(5, ""))


@dataclass(frozen=True)
class BambuCandidate:
    slot: int
    uid: bytes
    atqa: bytes
    sak: int
    keys_a: tuple


@dataclass(frozen=True)
class BambuTagData:
    slot: int
    uid: bytes
    atqa: bytes
    sak: int
    variant_id: str
    material_id: str
    filament_type: str
    detailed_filament_type: str
    color: str
    color_rgba: str
    spool_weight_g: int
    filament_diameter_mm: float
    drying_temp_c: int
    drying_time_h: int
    bed_temp_c: int
    min_hotend_c: int
    max_hotend_c: int
    filament_length_m: int
    blocks: dict

    @property
    def brand(self):
        return "Bambulab"

    @property
    def profile_name(self):
        detail = self.detailed_filament_type.strip()
        return "Bambulab %s" % detail if detail else "Bambulab %s" % self.filament_type

    def as_dict(self):
        return {
            "slot": self.slot,
            "uid": self.uid.hex().upper(),
            "atqa": self.atqa.hex().upper(),
            "sak": self.sak,
            "variant_id": self.variant_id,
            "material_id": self.material_id,
            "filament_type": self.filament_type,
            "detailed_filament_type": self.detailed_filament_type,
            "material": normalize_material(self.filament_type, self.detailed_filament_type),
            "brand": self.brand,
            "profile_name": self.profile_name,
            "color": self.color,
            "color_rgba": self.color_rgba,
            "spool_weight_g": self.spool_weight_g,
            "filament_diameter_mm": self.filament_diameter_mm,
            "drying_temp_c": self.drying_temp_c,
            "drying_time_h": self.drying_time_h,
            "bed_temp_c": self.bed_temp_c,
            "min_hotend_c": self.min_hotend_c,
            "max_hotend_c": self.max_hotend_c,
            "filament_length_m": self.filament_length_m,
            "blocks": {int(k): bytes(v).hex().upper() for k, v in self.blocks.items()},
        }


@dataclass(frozen=True)
class BambuStockTagData:
    """Metadata that API7 can recover through Creality's stock RFID task."""

    slot: int
    uid: bytes
    atqa: bytes
    sak: int
    detailed_filament_type: str
    color: str
    color_rgba: str
    blocks: dict

    @property
    def brand(self):
        return "Bambulab"

    @property
    def material(self):
        return normalize_material("", self.detailed_filament_type)

    @property
    def profile_name(self):
        detail = self.detailed_filament_type.strip()
        return "Bambulab %s" % detail if detail else "Bambulab %s" % self.material

    def as_dict(self):
        return {
            "slot": self.slot,
            "uid": self.uid.hex().upper(),
            "atqa": self.atqa.hex().upper(),
            "sak": self.sak,
            "variant_id": "",
            "material_id": "",
            "filament_type": "",
            "detailed_filament_type": self.detailed_filament_type,
            "material": self.material,
            "brand": self.brand,
            "profile_name": self.profile_name,
            "color": self.color,
            "color_rgba": self.color_rgba,
            "spool_weight_g": None,
            "filament_diameter_mm": None,
            "drying_temp_c": None,
            "drying_time_h": None,
            "bed_temp_c": None,
            "min_hotend_c": None,
            "max_hotend_c": None,
            "filament_length_m": 0,
            "blocks": {int(k): bytes(v).hex().upper()
                       for k, v in self.blocks.items()},
            "capture": "stock-task-api7",
        }


def _hkdf_sha256(ikm, length, salt, info=b""):
    ikm = bytes(ikm)
    salt = bytes(salt)
    info = bytes(info)
    if not 0 <= length <= 255 * hashlib.sha256().digest_size:
        raise ValueError("invalid HKDF output length")
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out = bytearray()
    previous = b""
    counter = 1
    while len(out) < length:
        previous = hmac.new(
            prk, previous + info + bytes((counter,)), hashlib.sha256).digest()
        out.extend(previous)
        counter += 1
    return bytes(out[:length])


def derive_bambu_key_a(uid):
    uid = bytes(uid)
    if len(uid) != 4:
        raise ValueError("Bambu derivation currently requires a 4-byte UID")
    material = _hkdf_sha256(
        uid, BAMBU_SECTORS * BAMBU_KEY_LEN,
        BAMBU_HKDF_SALT, BAMBU_HKDF_INFO)
    return tuple(
        material[i:i + BAMBU_KEY_LEN]
        for i in range(0, len(material), BAMBU_KEY_LEN))


def is_mifare_classic_1k_candidate(tag):
    return (
        isinstance(tag, TagInfoReply)
        and tag.status == 0
        and len(tag.uid) == 4
        and tag.uid != b"\x00\x00\x00\x00"
        and tag.atqa == b"\x04\x00"
        and tag.sak == 0x08
    )


def _ascii(block):
    return bytes(block).decode("ascii", "replace").replace("\x00", " ").strip()


def _u16le(block, start):
    return int.from_bytes(bytes(block)[start:start + 2], "little")


def normalize_material(filament_type, detailed=""):
    """Return a conservative K2/OpenHost material family string."""
    primary = str(filament_type or "").strip().upper()
    detail = str(detailed or "").strip().upper()
    if primary:
        # Bambu block 2 normally contains the broad family (PLA, PETG, ABS...).
        return primary
    for token in (
            "PLA-CF", "PETG-CF", "PET-CF", "ASA-CF", "ABS-GF", "PA6-CF",
            "PAHT-CF", "PPA-CF", "PETG", "PLA", "ABS", "ASA", "PC", "TPU"):
        if token in detail:
            return token
    return detail.split(" ", 1)[0] if detail else ""


def parse_bambu_blocks(slot, tag, blocks):
    missing = [b for b in BAMBU_REQUIRED_BLOCKS if b not in blocks]
    if missing:
        raise BambuRfidError("missing Bambu RFID blocks: %s" % missing)
    clean = {int(k): bytes(v) for k, v in blocks.items()}
    if any(len(clean[b]) != 16 for b in BAMBU_REQUIRED_BLOCKS):
        raise BambuRfidError("Bambu RFID blocks must be exactly 16 bytes")

    b1, b2 = clean[1], clean[2]
    b4, b5, b6, b14 = clean[4], clean[5], clean[6], clean[14]
    diameter = struct.unpack("<f", b5[8:12])[0]
    if not (0.5 <= diameter <= 4.0):
        raise BambuRfidError("implausible Bambu filament diameter %.4f" % diameter)

    return BambuTagData(
        slot=slot,
        uid=bytes(tag.uid), atqa=bytes(tag.atqa), sak=int(tag.sak),
        variant_id=_ascii(b1[0:8]), material_id=_ascii(b1[8:16]),
        filament_type=_ascii(b2), detailed_filament_type=_ascii(b4),
        color="#" + b5[0:3].hex().upper(),
        color_rgba="#" + b5[0:4].hex().upper(),
        spool_weight_g=_u16le(b5, 4),
        filament_diameter_mm=float(diameter),
        drying_temp_c=_u16le(b6, 0), drying_time_h=_u16le(b6, 2),
        bed_temp_c=_u16le(b6, 6),
        max_hotend_c=_u16le(b6, 8), min_hotend_c=_u16le(b6, 10),
        filament_length_m=_u16le(b14, 4),
        blocks=clean,
    )


def _internal_bytes(reply, what):
    if not isinstance(reply, MemoryReply) or len(reply.data) != 76:
        raise BambuRfidError("%s must be a 76-byte INTERNAL_RECORD" % what)
    return bytes(reply.data)


def parse_bambu_stock_capture(slot, target_reply, cap1_reply, cap2_reply):
    """Decode API7 scratch capture without touching the RFID tag."""
    target = _internal_bytes(target_reply, "target")
    cap1 = _internal_bytes(cap1_reply, "capture record 1")
    cap2 = _internal_bytes(cap2_reply, "capture record 2")

    hitmask, okmask, failmask = target[9], target[10], target[11]
    if (hitmask, okmask, failmask) != (
            BAMBU_STOCK_MASK, BAMBU_STOCK_MASK, 0):
        raise BambuRfidError(
            "stock capture incomplete: hit=0x%02X ok=0x%02X fail=0x%02X"
            % (hitmask, okmask, failmask))
    if cap1[4:8] != BAMBU_CAPTURE1_MAGIC:
        raise BambuRfidError("stock capture record 1 marker is missing")
    if cap2[4:8] != BAMBU_CAPTURE2_MAGIC:
        raise BambuRfidError("stock capture record 2 marker is missing")

    atqa = target[60:62]
    uid = target[62:66]
    sak = target[74]
    if (atqa != b"\x04\x00" or sak != 0x08
            or len(uid) != 4 or uid == b"\x00\x00\x00\x00"):
        raise BambuRfidError(
            "captured tag is not a conservative MIFARE Classic 1K candidate")

    block4 = cap1[8:20] + cap2[8:12]
    rgba = cap2[12:16]
    detail = _ascii(block4)
    material = normalize_material("", detail)
    if not detail or not material:
        raise BambuRfidError("captured Bambu material detail is empty")

    return BambuStockTagData(
        slot=slot, uid=uid, atqa=atqa, sak=sak,
        detailed_filament_type=detail,
        color="#" + rgba[:3].hex().upper(),
        color_rgba="#" + rgba.hex().upper(),
        blocks={4: block4, 5: rgba},
    )


class BoxRfidBambu:
    """Bambu candidate inspector plus capability-gated authenticated reader."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.serial_name = config.get("serial", "serial485")
        self.default_address = config.getint("address", 1, minval=1, maxval=4)
        self.serial = None
        self.last_candidate = None
        self.last_tag = None
        self.last_error = None
        self.last_unsupported = None

        self.printer.register_event_handler("serial_485:ready", self._serial_ready)
        self.gcode.register_command(
            "BOX_RFID_BAMBU_DERIVE", self.cmd_derive,
            desc="Passively derive Bambu candidate Key A values from CFS cache")
        self.gcode.register_command(
            "BOX_RFID_BAMBU_READ", self.cmd_read,
            desc="Read and decode a Bambu tag using capability-gated Key A auth")
        self.gcode.register_command(
            "BOX_RFID_BAMBU_PROBE_READ", self.cmd_probe_read,
            desc="Diagnostic read-only Bambu auth/read using an explicit known UID")

    def _serial_ready(self, *args):
        self.serial = self.printer.lookup_object("serial_485 %s" % self.serial_name)

    def _transport(self):
        if self.serial is None:
            self.serial = self.printer.lookup_object(
                "serial_485 %s" % self.serial_name, None)
        if self.serial is None:
            raise BambuRfidError("RS-485 transport is not ready")
        return self.serial

    @staticmethod
    def _slot_address(global_slot):
        if type(global_slot) is not int or not 0 <= global_slot < 16:
            raise ValueError("slot must be 0..15")
        return global_slot // 4 + 1, global_slot % 4

    def _manual_slot(self, gcmd):
        slot = gcmd.get_int("SLOT", minval=0, maxval=15)
        address, local = self._slot_address(slot)
        explicit = gcmd.get_int("ADDRESS", None, minval=1, maxval=4)
        return slot, (address if explicit is None else explicit), local

    def _read_candidate(self, global_slot, address=None, transport=None,
                        allow_poll=False):
        auto_addr, local_slot = self._slot_address(global_slot)
        address = auto_addr if address is None else address
        driver = RfidDiagDriver(transport or self._transport(), address)
        tag = driver.cache(local_slot)
        if (tag is None or not is_mifare_classic_1k_candidate(tag)) and allow_poll:
            # One bounded active discovery attempt. This is used only after the
            # stock Creality path has already returned unknown and only when the
            # CFS firmware explicitly advertises CAP_POLL.
            tag = driver.poll(local_slot, timeout=1.0)
        if tag is None or not is_mifare_classic_1k_candidate(tag):
            return None
        keys = derive_bambu_key_a(tag.uid)
        self.last_candidate = BambuCandidate(
            global_slot, tag.uid, tag.atqa, tag.sak, keys)
        return tag, keys, driver, local_slot

    @staticmethod
    def _internal_candidate(reply):
        data = _internal_bytes(reply, "target")
        atqa, uid, sak = data[60:62], data[62:66], data[74]
        if (atqa != b"\x04\x00" or sak != 0x08
                or uid == b"\x00\x00\x00\x00"):
            raise BambuRfidError(
                "stock CFS record is not a Bambu-compatible MIFARE Classic 1K candidate")
        return atqa, uid, sak

    def _clear_stock_override(self, address, local_slot):
        try:
            with self._transport().request_session() as transport:
                RfidDiagDriver(transport, address).clear_stock_task_keys(
                    local_slot, timeout=1.0)
        except Exception:
            pass

    def _read_tag_stock_capture(self, global_slot, address=None):
        """Use API7 to run Bambu Key A through Creality's original RFID task."""
        auto_addr, local_slot = self._slot_address(global_slot)
        address = auto_addr if address is None else address

        with self._transport().request_session() as transport:
            driver = RfidDiagDriver(transport, address)
            info = driver.info(timeout=1.0)
            if info is None:
                raise BambuRfidError("CFS RFID INFO timed out")
            required = (diag.CAP_STOCK_STATE | diag.CAP_INTERNAL_RECORD
                        | diag.CAP_STOCK_TASK_KEYS3 | diag.CAP_STOCK_CAPTURE)
            if (info.api_version != diag.API_STOCK_CAPTURE
                    or info.readers != 2
                    or info.slots_per_reader != 2
                    or info.max_read_index != 3
                    or info.capabilities & required != required):
                raise BambuRfidUnsupported(
                    "CFS firmware does not expose validated API7 stock capture")
            runtime = driver.runtime_info(timeout=1.0)
            if runtime is None:
                raise BambuRfidError("CFS RFID RUNTIME_INFO timed out")
            if runtime.secure_backend:
                raise BambuRfidUnsupported(
                    "API7 Bambu stock capture is validated only on the legacy CFS RFID backend")
            state = driver.stock_state(timeout=1.0)
            if state is None:
                raise BambuRfidError("CFS RFID STOCK_STATE timed out")
            if state.busy:
                raise BambuRfidError(
                    "CFS stock RFID manager is busy on logical slot %d"
                    % state.active_slot)
            target = driver.internal_record(local_slot, timeout=1.0)
            atqa, uid, sak = self._internal_candidate(target)
            keys = derive_bambu_key_a(uid)
            sector1_key = keys[1]
            armed = driver.arm_stock_task_keys3(
                local_slot, sector1_key * 3, timeout=1.0)
            if not isinstance(armed, MemoryReply) or bytes(armed.data) != uid:
                status = "timeout" if armed is None else getattr(
                    armed, "status_name", "invalid")
                raise BambuRfidError(
                    "CFS API7 ARM_KEYS failed: %s" % status)
            self.last_candidate = BambuCandidate(
                global_slot, uid, atqa, sak, keys)

        box = self.printer.lookup_object("box", None)
        box_driver = None if box is None else getattr(box, "drivers", {}).get(address)
        if box_driver is None:
            self._clear_stock_override(address, local_slot)
            raise BambuRfidError("CFS Box driver is not ready")

        try:
            # This is Creality's original force-read command. API7 only swaps
            # the three Key A values inside the stock task and passively copies
            # block4 detail + block5 RGBA into otherwise-unused scratch bytes.
            force_reply = box_driver.force_rfid_read(1 << local_slot)
            if force_reply is None:
                raise BambuRfidError("stock CFS RFID reread timed out")

            with self._transport().request_session() as transport:
                driver = RfidDiagDriver(transport, address)
                target = driver.internal_record(local_slot, timeout=1.0)
                cap1 = driver.internal_record((local_slot + 2) % 4, timeout=1.0)
                cap2 = driver.internal_record((local_slot + 3) % 4, timeout=1.0)
                tagdata = parse_bambu_stock_capture(
                    global_slot, target, cap1, cap2)
                driver.clear_stock_task_keys(local_slot, timeout=1.0)
        except Exception:
            self._clear_stock_override(address, local_slot)
            raise

        self.last_tag = tagdata
        self.last_error = None
        self.last_unsupported = None
        _log_decoded_tag(tagdata, "api7-stock")
        return tagdata

    def read_tag(self, global_slot, address=None):
        """Read Bambu metadata without ever exposing a tag-write primitive."""
        auto_addr, _local_slot = self._slot_address(global_slot)
        address = auto_addr if address is None else address

        # API7 is the preferred K2-OpenHost path: all RF activity remains in
        # Creality's original CFS worker. API3 is retained for older diagnostics.
        with self._transport().request_session() as transport:
            info = RfidDiagDriver(transport, address).info(timeout=1.0)
        if info is None:
            raise BambuRfidError("CFS RFID INFO timed out")
        if info.api_version == diag.API_STOCK_CAPTURE:
            return self._read_tag_stock_capture(global_slot, address=address)

        with self._transport().request_session() as transport:
            driver = RfidDiagDriver(transport, address)
            if (info.api_version != diag.API_V21
                    or not (info.capabilities & diag.CAP_AUTH_A)):
                raise BambuRfidUnsupported(
                    "CFS firmware exposes neither API7 stock capture nor the validated API3 authenticated-read path")
            candidate = self._read_candidate(
                global_slot, address=address, transport=transport,
                allow_poll=bool(info.capabilities & diag.CAP_POLL))
            if candidate is None:
                raise BambuRfidError(
                    "slot is not a Bambu-compatible MIFARE Classic 1K candidate")
            tag, keys, driver, local_slot = candidate
            blocks = {}
            for block in BAMBU_REQUIRED_BLOCKS:
                key = keys[block // 4]
                reply = driver.read_auth_a(
                    local_slot, block, key, timeout=1.0)
                if not isinstance(reply, MemoryReply):
                    status = "timeout" if reply is None else reply.status_name
                    raise BambuRfidError(
                        "Bambu block %d read failed: %s" % (block, status))
                blocks[block] = bytes(reply.data)
            tagdata = parse_bambu_blocks(global_slot, tag, blocks)
            self.last_tag = tagdata
            self.last_error = None
            self.last_unsupported = None
            _log_decoded_tag(tagdata, "api3-direct")
            return tagdata

    def try_auto_read(self, global_slot):
        """Automatic fallback is intentionally API7 stock-task capture only."""
        try:
            address, _local = self._slot_address(global_slot)
            with self._transport().request_session() as transport:
                info = RfidDiagDriver(transport, address).info(timeout=1.0)
            if info is None:
                raise BambuRfidError("CFS RFID INFO timed out")
            if info.api_version != diag.API_STOCK_CAPTURE:
                raise BambuRfidUnsupported(
                    "automatic Bambu fallback requires API7 stock capture")
            return self._read_tag_stock_capture(global_slot, address=address)
        except BambuRfidUnsupported as exc:
            self.last_unsupported = str(exc)
            return None
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def cmd_derive(self, gcmd):
        slot, address, _local = self._manual_slot(gcmd)
        show_keys = gcmd.get_int("SHOW_KEYS", 0, minval=0, maxval=1)
        try:
            candidate = self._read_candidate(slot, address=address)
            if candidate is None:
                raise gcmd.error("slot does not contain a cached Bambu-compatible candidate")
            tag, keys, _driver, _local_slot = candidate
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            raise
        gcmd.respond_info(
            "Bambu RFID candidate slot=%d UID=%s ATQA=%s SAK=%02X; "
            "derived %d sector Key A values locally; no RF auth/read performed"
            % (slot, tag.uid.hex().upper(), tag.atqa.hex().upper(), tag.sak, len(keys)))
        if show_keys:
            for sector, key in enumerate(keys):
                gcmd.respond_info("Bambu candidate sector=%02d KeyA=%s" % (
                    sector, key.hex().upper()))

    def cmd_probe_read(self, gcmd):
        """Diagnostic only: one authenticated block read using an explicit UID.

        This bypasses the host-side cache candidate gate so firmware reader
        selection/probe can be validated after a CFS reboot. It never writes.
        """
        slot, address, local = self._manual_slot(gcmd)
        uid_text = str(gcmd.get("UID", "")).strip().replace(":", "").replace(" ", "")
        block = gcmd.get_int("BLOCK", 1, minval=0, maxval=63)
        try:
            uid = bytes.fromhex(uid_text)
        except Exception:
            raise gcmd.error("UID must be hexadecimal")
        if len(uid) != 4:
            raise gcmd.error("UID must be exactly 4 bytes")
        key_text = str(gcmd.get("KEY", "")).strip().replace(":", "").replace(" ", "")
        if key_text:
            try:
                key = bytes.fromhex(key_text)
            except Exception:
                raise gcmd.error("KEY must be hexadecimal")
            if len(key) != 6:
                raise gcmd.error("KEY must be exactly 6 bytes")
        else:
            keys = derive_bambu_key_a(uid)
            key = keys[block // 4]
        with self._transport().request_session() as transport:
            driver = RfidDiagDriver(transport, address)
            info = driver.info(timeout=1.0)
            if info is None or not (info.capabilities & diag.CAP_AUTH_A):
                raise gcmd.error("CFS diagnostic firmware does not expose authenticated RFID reads")
            reply = driver.read_auth_a(local, block, key, timeout=1.0)
        if reply is None:
            raise gcmd.error("Bambu diagnostic auth-read timed out")
        if not isinstance(reply, MemoryReply):
            raise gcmd.error(
                "Bambu diagnostic block %d failed: %s"
                % (block, reply.status_name))
        gcmd.respond_info(
            "Bambu diagnostic slot=%d UID=%s block=%d KeyA=%s data=%s"
            % (slot, uid.hex().upper(), block, key.hex().upper(),
               reply.data.hex().upper()))

    def cmd_read(self, gcmd):
        slot, address, _local = self._manual_slot(gcmd)
        try:
            tag = self.read_tag(slot, address=address)
        except BambuRfidUnsupported as exc:
            raise gcmd.error(str(exc))
        except BambuRfidError as exc:
            raise gcmd.error(str(exc))
        data = tag.as_dict()
        detail = [
            "Bambu RFID slot=%d" % slot,
            "UID=%s" % data["uid"],
            "material=%s" % data["material"],
            "detail=%s" % data["detailed_filament_type"],
            "color=%s" % data["color"],
        ]
        if data.get("variant_id"):
            detail.append("variant=%s" % data["variant_id"])
        if data.get("material_id"):
            detail.append("material_id=%s" % data["material_id"])
        if data.get("filament_length_m"):
            detail.append("length=%dm" % data["filament_length_m"])
        if data.get("spool_weight_g") is not None:
            detail.append("weight=%dg" % data["spool_weight_g"])
        if data.get("filament_diameter_mm") is not None:
            detail.append("diameter=%.3fmm" % data["filament_diameter_mm"])
        if (data.get("min_hotend_c") is not None
                and data.get("max_hotend_c") is not None):
            detail.append("hotend=%d-%dC" % (
                data["min_hotend_c"], data["max_hotend_c"]))
        if data.get("bed_temp_c") is not None:
            detail.append("bed=%dC" % data["bed_temp_c"])
        gcmd.respond_info(" ".join(detail))

    def get_status(self, _eventtime):
        c = self.last_candidate
        return {
            "serial": self.serial_name,
            "transport_ready": self.serial is not None,
            "last_error": self.last_error,
            "last_unsupported": self.last_unsupported,
            "last_candidate": None if c is None else {
                "slot": c.slot, "uid": c.uid.hex().upper(),
                "atqa": c.atqa.hex().upper(), "sak": c.sak,
                "derived_key_count": len(c.keys_a),
            },
            "last_tag": None if self.last_tag is None else self.last_tag.as_dict(),
        }


def load_config(config):
    return BoxRfidBambu(config)