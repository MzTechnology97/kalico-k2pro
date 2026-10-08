# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Generic third-party MIFARE Classic spool decoder for K2-OpenHost.

The module deliberately keeps vendor parsing separate from RF transport:
- Creality's stock CFS RFID worker remains the only RF owner.
- API7 only injects temporary Key-A values and copies successful stock reads.
- Decoders provide candidate keys plus a parser for the captured bytes.
- No tag-write primitive is exposed here.

QIDI is the first hardware-validated decoder. Additional MIFARE Classic
formats (for example Snapmaker) can be registered without changing box.py's
fallback flow or duplicating the API7 transport.
"""

from dataclasses import dataclass

from extras import box_rfid_diag as diag
from extras.box_rfid_diag import MemoryReply, RfidDiagDriver


CAPTURE1_MAGIC = b"K2C3"
CAPTURE2_MAGIC = b"K2D3"
STOCK_MASK = 0x07

# QIDI public tag layout: sector 1 / absolute block 4.
# Codes are decimal in QIDI documentation and stored as one-byte values.
QIDI_MATERIALS = {
    1: "PLA",
    2: "PLA Matte",
    3: "PLA Metal",
    4: "PLA Silk",
    5: "PLA-CF",
    6: "PLA-Wood",
    7: "PLA Basic",
    8: "PLA Matte Basic",
    10: "Support For PLA",
    11: "ABS",
    12: "ABS-GF",
    13: "ABS-Metal",
    14: "ABS-Odorless",
    18: "ASA",
    19: "ASA-AERO",
    20: "ASA-CF",
    23: "PC",
    24: "UltraPA",
    25: "PA-CF",
    26: "UltraPA-CF25",
    27: "PA12-CF",
    30: "PAHT-CF",
    31: "PAHT-GF",
    32: "Support For PAHT",
    33: "Support For PET/PA",
    34: "PC/ABS-FR",
    35: "TPEE",
    36: "PEBA",
    37: "PET-CF",
    38: "PET-GF",
    39: "PETG Basic",
    40: "PETG Tough",
    41: "PETG Rapido",
    44: "PETG-CF",
    45: "PETG Translucent",
    46: "PPS-GF",
    47: "PVA",
    48: "TPU-AERO 64D",
    49: "TPU-Aero",
    50: "TPU 95A-HF",
}

QIDI_COLORS = {
    1: ("White", "#FAFAFA"),
    2: ("Black", "#060606"),
    3: ("Silver", "#D9E3ED"),
    4: ("Lime Green", "#5CF30F"),
    5: ("Spring Green", "#63E492"),
    6: ("Blue", "#2850FF"),
    7: ("Pink", "#FE98FE"),
    8: ("Yellow", "#DFD628"),
    9: ("Dark Green", "#228332"),
    10: ("Light Blue", "#99DEFF"),
    11: ("Royal Blue", "#1714B0"),
    12: ("Lavender", "#CEC0FE"),
    13: ("Gold", "#CADE4B"),
    14: ("Navy", "#1353AB"),
    15: ("Sky Blue", "#5EA9FD"),
    16: ("Purple", "#A878FF"),
    17: ("Coral", "#FE717A"),
    18: ("Red", "#FF362D"),
    19: ("Beige", "#E2DFCD"),
    20: ("Grey", "#898F9B"),
    21: ("Brown", "#6E3812"),
    22: ("Khaki", "#CAC59F"),
    23: ("Orange", "#F28636"),
    24: ("Tan", "#B87F2B"),
}

QIDI_MANUFACTURERS = {
    0: "Generic",
    1: "QIDI",
}

# Default MIFARE Key A. This exact path is hardware-validated on the real
# QIDI PET-CF spool used for this project.
QIDI_KEY_CANDIDATES = (
    bytes.fromhex("FFFFFFFFFFFF"),
)


class ThirdPartyRfidError(RuntimeError):
    pass


class ThirdPartyRfidUnsupported(ThirdPartyRfidError):
    pass


@dataclass(frozen=True)
class StockCapture:
    slot: int
    uid: bytes
    atqa: bytes
    sak: int
    hitmask: int
    okmask: int
    failmask: int
    block4: bytes
    block5: bytes

    @property
    def complete(self):
        return (self.hitmask, self.okmask, self.failmask) == (
            STOCK_MASK, STOCK_MASK, 0)


@dataclass(frozen=True)
class ThirdPartyTagData:
    slot: int
    uid: bytes
    atqa: bytes
    sak: int
    vendor: str
    identity_code: str
    material: str
    detailed_filament_type: str
    brand: str
    profile_name: str
    color: str
    color_name: str
    manufacturer: str
    material_code: int
    color_code: int
    manufacturer_code: int
    blocks: dict

    def as_dict(self):
        return {
            "slot": self.slot,
            "uid": self.uid.hex().upper(),
            "atqa": self.atqa.hex().upper(),
            "sak": self.sak,
            "vendor": self.vendor,
            "identity_code": self.identity_code,
            "material": self.material,
            "detailed_filament_type": self.detailed_filament_type,
            "brand": self.brand,
            "profile_name": self.profile_name,
            "color": self.color,
            "color_name": self.color_name,
            "manufacturer": self.manufacturer,
            "material_code": self.material_code,
            "color_code": self.color_code,
            "manufacturer_code": self.manufacturer_code,
            "filament_length_m": 0,
            "blocks": {
                int(k): bytes(v).hex().upper()
                for k, v in self.blocks.items()
            },
            "capture": "stock-task-api7",
        }


def _material_family(detail):
    text = str(detail or "").strip().upper()
    if not text:
        return ""
    # Preserve composite families when they are meaningful library materials.
    for token in (
            "PLA-CF", "PETG-CF", "PET-CF", "PET-GF", "ASA-CF",
            "ABS-GF", "PA12-CF", "PAHT-CF", "PAHT-GF", "PA-CF",
            "PPS-GF", "PC/ABS-FR", "ULTRAPA-CF25"):
        if token in text:
            return token
    for token in ("PETG", "PLA", "ABS", "ASA", "TPU", "PC", "PVA",
                  "TPEE", "PEBA", "ULTRAPA"):
        if text.startswith(token):
            return token
    return text


def _internal_candidate(reply):
    if not isinstance(reply, MemoryReply) or len(reply.data) != 76:
        raise ThirdPartyRfidError(
            "target must be a 76-byte CFS INTERNAL_RECORD")
    data = bytes(reply.data)
    atqa, uid, sak = data[60:62], data[62:66], data[74]
    if (atqa != b"\x04\x00" or sak != 0x08
            or uid == b"\x00\x00\x00\x00"):
        return None
    return atqa, uid, sak


def _decode_capture(slot, target_reply, cap1_reply, cap2_reply):
    for label, reply in (
            ("target", target_reply),
            ("capture1", cap1_reply),
            ("capture2", cap2_reply)):
        if not isinstance(reply, MemoryReply) or len(reply.data) != 76:
            raise ThirdPartyRfidError(
                "%s must be a 76-byte CFS INTERNAL_RECORD" % label)
    target = bytes(target_reply.data)
    cap1 = bytes(cap1_reply.data)
    cap2 = bytes(cap2_reply.data)
    candidate = _internal_candidate(target_reply)
    if candidate is None:
        raise ThirdPartyRfidError(
            "captured tag is not a conservative MIFARE Classic 1K candidate")
    atqa, uid, sak = candidate
    if cap1[4:8] != CAPTURE1_MAGIC or cap2[4:8] != CAPTURE2_MAGIC:
        raise ThirdPartyRfidError("API7 stock-capture scratch markers are missing")
    return StockCapture(
        slot=slot,
        uid=uid,
        atqa=atqa,
        sak=sak,
        hitmask=target[9],
        okmask=target[10],
        failmask=target[11],
        block4=cap1[8:20] + cap2[8:12],
        block5=cap2[12:16],
    )


class QidiDecoder:
    name = "QIDI"

    @staticmethod
    def key_candidates(_uid):
        return QIDI_KEY_CANDIDATES

    @staticmethod
    def parse(capture):
        if not capture.complete or len(capture.block4) != 16:
            return None
        material_code, color_code, manufacturer_code = capture.block4[:3]
        detail = QIDI_MATERIALS.get(material_code)
        color_entry = QIDI_COLORS.get(color_code)
        manufacturer = QIDI_MANUFACTURERS.get(manufacturer_code)
        if not detail or not color_entry or manufacturer is None:
            return None
        color_name, color = color_entry
        # Byte 3..15 are currently unused in the public QIDI layout. Do not
        # reject non-zero future extensions; the first three bytes identify it.
        brand = "QIDI" if manufacturer_code == 1 else manufacturer
        material = _material_family(detail)
        identity = "QIDI:%s" % detail.upper()
        return ThirdPartyTagData(
            slot=capture.slot,
            uid=capture.uid,
            atqa=capture.atqa,
            sak=capture.sak,
            vendor="QIDI",
            identity_code=identity,
            material=material,
            detailed_filament_type=detail,
            brand=brand,
            profile_name="%s %s" % (
                "Qidi" if brand.upper() == "QIDI" else brand, detail),
            color=color,
            color_name=color_name,
            manufacturer=manufacturer,
            material_code=material_code,
            color_code=color_code,
            manufacturer_code=manufacturer_code,
            blocks={4: capture.block4, 5: capture.block5},
        )


DECODERS = (
    QidiDecoder(),
)


def _tag_message(tag):
    d = tag.as_dict()
    blocks = d["blocks"]
    return (
        "Third-party RFID decoded vendor=%s slot=%s UID=%s ATQA=%s SAK=%02X "
        "detail=%r material=%s profile=%r color=%s color_name=%r "
        "material_code=0x%02X color_code=0x%02X manufacturer=%r "
        "manufacturer_code=0x%02X identity=%s block4=%s block5=%s"
        % (d["vendor"], d["slot"], d["uid"], d["atqa"], int(d["sak"]),
           d["detailed_filament_type"], d["material"], d["profile_name"],
           d["color"], d["color_name"], d["material_code"], d["color_code"],
           d["manufacturer"], d["manufacturer_code"], d["identity_code"],
           blocks.get(4, ""), blocks.get(5, "")))


class BoxRfidMifare:
    """Generic API7 MIFARE Classic third-party spool reader."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.serial_name = config.get("serial", "serial485")
        self.default_address = config.getint("address", 1, minval=1, maxval=4)
        self.serial = None
        self.last_tag = None
        self.last_decoder = None
        self.last_error = None

        self.printer.register_event_handler(
            "serial_485:ready", self._serial_ready)
        self.gcode.register_command(
            "BOX_RFID_MIFARE_READ", self.cmd_read,
            desc="Read a supported third-party MIFARE Classic spool via CFS API7")

    def _serial_ready(self, *args):
        self.serial = self.printer.lookup_object(
            "serial_485 %s" % self.serial_name)

    def _transport(self):
        if self.serial is None:
            self.serial = self.printer.lookup_object(
                "serial_485 %s" % self.serial_name, None)
        if self.serial is None:
            raise ThirdPartyRfidError("RS-485 transport is not ready")
        return self.serial

    def _hint_store(self):
        box = self.printer.lookup_object("box", None)
        store = None if box is None else getattr(box, "store", None)
        if store is None:
            return None, {}
        hints = store.setting("third_party_rfid_hints", {}) or {}
        return store, dict(hints) if isinstance(hints, dict) else {}

    def _remember_hint(self, uid, decoder_name):
        uid_hex = bytes(uid).hex().upper()
        store, hints = self._hint_store()
        if store is None or not uid_hex:
            return
        if hints.get(uid_hex) == decoder_name:
            return
        hints[uid_hex] = str(decoder_name)
        # Keep this cache bounded. These are routing hints, not spool history.
        if len(hints) > 128:
            for key in list(hints)[:-128]:
                hints.pop(key, None)
        store.set_setting("third_party_rfid_hints", hints)

    def _hint_for_uid(self, uid):
        _store, hints = self._hint_store()
        return hints.get(bytes(uid).hex().upper())

    def is_known_candidate(self, global_slot):
        try:
            auto_addr, _local = self._slot_address(global_slot)
            candidate = self._inspect_candidate(global_slot, auto_addr)
        except Exception:
            return False
        if candidate is None:
            return False
        _atqa, uid, _sak = candidate
        return bool(self._hint_for_uid(uid))

    @staticmethod
    def _slot_address(global_slot):
        if type(global_slot) is not int or not 0 <= global_slot < 16:
            raise ValueError("slot must be 0..15")
        return global_slot // 4 + 1, global_slot % 4

    @staticmethod
    def _require_api7(info):
        required = (
            diag.CAP_STOCK_STATE
            | diag.CAP_INTERNAL_RECORD
            | diag.CAP_STOCK_TASK_KEYS3
            | diag.CAP_STOCK_CAPTURE)
        if (info is None
                or info.api_version != diag.API_STOCK_CAPTURE
                or info.readers != 2
                or info.slots_per_reader != 2
                or info.max_read_index != 3
                or info.capabilities & required != required):
            raise ThirdPartyRfidUnsupported(
                "third-party MIFARE decoding requires validated API7 stock capture")

    def _clear(self, address, local_slot):
        try:
            with self._transport().request_session() as transport:
                RfidDiagDriver(
                    transport, address).clear_stock_task_keys(
                        local_slot, timeout=1.0)
        except Exception:
            pass

    def _inspect_candidate(self, global_slot, address):
        _auto_addr, local_slot = self._slot_address(global_slot)
        with self._transport().request_session() as transport:
            driver = RfidDiagDriver(transport, address)
            info = driver.info(timeout=1.0)
            self._require_api7(info)
            runtime = driver.runtime_info(timeout=1.0)
            if runtime is None:
                raise ThirdPartyRfidError("CFS RFID RUNTIME_INFO timed out")
            if runtime.secure_backend:
                raise ThirdPartyRfidUnsupported(
                    "API7 third-party capture is validated only on the legacy CFS RFID backend")
            state = driver.stock_state(timeout=1.0)
            if state is None:
                raise ThirdPartyRfidError("CFS RFID STOCK_STATE timed out")
            if state.busy:
                raise ThirdPartyRfidError(
                    "CFS stock RFID manager is busy on logical slot %d"
                    % state.active_slot)
            target = driver.internal_record(local_slot, timeout=1.0)
            return _internal_candidate(target)

    def _capture(self, global_slot, address, key_a):
        _auto_addr, local_slot = self._slot_address(global_slot)
        if len(key_a) != 6:
            raise ThirdPartyRfidError("Key A must be exactly 6 bytes")

        with self._transport().request_session() as transport:
            driver = RfidDiagDriver(transport, address)
            info = driver.info(timeout=1.0)
            self._require_api7(info)
            state = driver.stock_state(timeout=1.0)
            if state is None:
                raise ThirdPartyRfidError("CFS RFID STOCK_STATE timed out")
            if state.busy:
                raise ThirdPartyRfidError(
                    "CFS stock RFID manager is busy on logical slot %d"
                    % state.active_slot)
            target = driver.internal_record(local_slot, timeout=1.0)
            candidate = _internal_candidate(target)
            if candidate is None:
                raise ThirdPartyRfidError(
                    "CFS cache does not contain a MIFARE Classic 1K candidate")
            atqa, uid, sak = candidate
            armed = driver.arm_stock_task_keys3(
                local_slot, key_a * 3, timeout=1.0)
            if not isinstance(armed, MemoryReply) or bytes(armed.data) != uid:
                raise ThirdPartyRfidError("CFS API7 ARM_KEYS failed")

        box = self.printer.lookup_object("box", None)
        box_driver = None if box is None else getattr(
            box, "drivers", {}).get(address)
        if box_driver is None:
            self._clear(address, local_slot)
            raise ThirdPartyRfidError("CFS Box driver is not ready")

        try:
            if box_driver.force_rfid_read(1 << local_slot) is None:
                raise ThirdPartyRfidError("stock CFS RFID capture read timed out")
            with self._transport().request_session() as transport:
                driver = RfidDiagDriver(transport, address)
                target = driver.internal_record(local_slot, timeout=1.0)
                cap1 = driver.internal_record(
                    (local_slot + 2) % 4, timeout=1.0)
                cap2 = driver.internal_record(
                    (local_slot + 3) % 4, timeout=1.0)
                capture = _decode_capture(
                    global_slot, target, cap1, cap2)
                driver.clear_stock_task_keys(local_slot, timeout=1.0)
                return capture
        except Exception:
            self._clear(address, local_slot)
            raise

    def _prime_candidate(self, global_slot, address):
        candidate = self._inspect_candidate(global_slot, address)
        if candidate is not None:
            return candidate
        _auto_addr, local_slot = self._slot_address(global_slot)
        box = self.printer.lookup_object("box", None)
        box_driver = None if box is None else getattr(
            box, "drivers", {}).get(address)
        if box_driver is None:
            raise ThirdPartyRfidError("CFS Box driver is not ready")
        if box_driver.force_rfid_read(1 << local_slot) is None:
            raise ThirdPartyRfidError("stock CFS RFID cache-prime read timed out")
        candidate = self._inspect_candidate(global_slot, address)
        if candidate is None:
            raise ThirdPartyRfidUnsupported(
                "stock CFS record is not a MIFARE Classic 1K candidate")
        return candidate

    def read_tag(self, global_slot, address=None, prime=False):
        auto_addr, _local = self._slot_address(global_slot)
        address = auto_addr if address is None else address
        if prime:
            candidate = self._prime_candidate(global_slot, address)
        else:
            candidate = self._inspect_candidate(global_slot, address)
            if candidate is None:
                return None
        _atqa, uid, _sak = candidate

        hinted = self._hint_for_uid(uid)
        decoders = [
            decoder for decoder in DECODERS
            if hinted is None or decoder.name == hinted]
        if hinted and not decoders:
            self.last_error = "saved decoder hint %s is unavailable" % hinted
            return None

        errors = []
        for decoder in decoders:
            for key_a in decoder.key_candidates(uid):
                try:
                    capture = self._capture(global_slot, address, key_a)
                except ThirdPartyRfidUnsupported:
                    raise
                except Exception as exc:
                    errors.append("%s/%s: %s" % (
                        decoder.name, key_a.hex().upper(), exc))
                    continue
                if not capture.complete:
                    errors.append(
                        "%s/%s: hit=%02X ok=%02X fail=%02X"
                        % (decoder.name, key_a.hex().upper(),
                           capture.hitmask, capture.okmask, capture.failmask))
                    continue
                tag = decoder.parse(capture)
                if tag is None:
                    errors.append(
                        "%s/%s: capture did not match decoder"
                        % (decoder.name, key_a.hex().upper()))
                    continue
                self.last_tag = tag
                self.last_decoder = decoder.name
                self.last_error = None
                self._remember_hint(tag.uid, decoder.name)
                self.gcode.respond_info(_tag_message(tag))
                return tag

        self.last_tag = None
        self.last_decoder = None
        self.last_error = "; ".join(errors[-6:]) if errors else "no decoder matched"
        return None

    def try_auto_read(self, global_slot):
        if not self.is_known_candidate(global_slot):
            return None
        try:
            return self.read_tag(global_slot, prime=False)
        except ThirdPartyRfidUnsupported as exc:
            self.last_error = str(exc)
            return None

    def cmd_read(self, gcmd):
        slot = gcmd.get_int("SLOT", minval=0, maxval=15)
        address = gcmd.get_int("ADDRESS", None, minval=1, maxval=4)
        try:
            tag = self.read_tag(slot, address=address, prime=True)
        except Exception as exc:
            self.last_error = str(exc)
            raise gcmd.error("Third-party RFID read failed: %s" % exc)
        if tag is None:
            raise gcmd.error(
                "MIFARE Classic tag was read but no registered third-party decoder matched")
        d = tag.as_dict()
        applied = False
        if gcmd.get_int("APPLY", 1, minval=0, maxval=1):
            box = self.printer.lookup_object("box", None)
            apply_tag = None if box is None else getattr(
                box, "_apply_third_party_rfid_tag", None)
            if apply_tag is None:
                raise gcmd.error(
                    "Third-party tag decoded but Box integration is unavailable")
            applied = bool(apply_tag(slot, tag))
        gcmd.respond_info(
            "%s RFID slot=%d UID=%s material=%s detail=%s color=%s (%s) "
            "identity=%s applied=%s"
            % (d["vendor"], d["slot"], d["uid"], d["material"],
               d["detailed_filament_type"], d["color"], d["color_name"],
               d["identity_code"], applied))

    def get_status(self, _eventtime):
        return {
            "serial": self.serial_name,
            "transport_ready": self.serial is not None,
            "decoders": [decoder.name for decoder in DECODERS],
            "last_decoder": self.last_decoder,
            "last_error": self.last_error,
            "last_tag": None if self.last_tag is None
                        else self.last_tag.as_dict(),
        }


def load_config(config):
    return BoxRfidMifare(config)