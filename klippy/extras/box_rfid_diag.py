# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Optional read-only diagnostics for the patched K2 Pro CFS RFID firmware.

This extra never opens the RS-485 serial device itself.  It uses the existing
``Serial_485_Wrapper`` request queue so Box/CFS and closed-loop traffic keep a
single owner on K2-OpenHost.

The firmware extension is intentionally vendor-neutral: Bambu/QIDI decoding
belongs on the host side and no tag-write primitive is exposed here.
"""

from contextlib import contextmanager
from dataclasses import dataclass

from extras import box_protocol
from extras.serial_485 import build_485_body


CMD_RFID_DIAG = 0x57

SUB_INFO = 0x00
SUB_CACHE = 0x01
SUB_POLL = 0x02
SUB_READ = 0x03
SUB_READ_AUTH_A = 0x04
SUB_STOCK_STATE = 0x05
SUB_INTERNAL_RECORD = 0x06
SUB_ARM_STOCK_TASK_KEYS3 = 0x07
SUB_CLEAR_STOCK_TASK_KEYS = 0x08
SUB_RUNTIME_INFO = 0x09
SUB_REMAIN_STATE = 0x0A

STATUS_OK = 0
STATUS_BAD_REQUEST = 1
STATUS_NO_TAG = 2
STATUS_ANTICOLLISION = 3
STATUS_SELECT = 4
STATUS_AUTH = 5
STATUS_READ = 6
STATUS_STOCK_BUSY = 7

STATUS_NAMES = {
    STATUS_OK: "OK",
    STATUS_BAD_REQUEST: "BAD_REQUEST",
    STATUS_NO_TAG: "NO_TAG",
    STATUS_ANTICOLLISION: "ANTICOLLISION_FAILED",
    STATUS_SELECT: "SELECT_FAILED",
    STATUS_AUTH: "AUTH_FAILED",
    STATUS_READ: "READ_FAILED",
    STATUS_STOCK_BUSY: "STOCK_RFID_BUSY",
}

API_V21 = 3
API_SINGLE_STOCK_TASK = 5
API_DUAL_STOCK_TASK = 6
API_STOCK_CAPTURE = 7
CAP_CACHE = 1 << 0
CAP_POLL = 1 << 1
CAP_READ = 1 << 2
CAP_AUTH_A = 1 << 3
CAP_CL2 = 1 << 4
# API7 v3.4 reuses bit4 for read-only stock remaining-state telemetry.
CAP_REMAIN_STATE = 1 << 4
CAP_STOCK_STATE = 1 << 5
CAP_STOCK_GUARD = 1 << 6
# API7 reuses bit6 to advertise passive stock-task capture telemetry.
CAP_STOCK_CAPTURE = 1 << 6
CAP_INTERNAL_RECORD = 1 << 7
# API6: bit3 advertises three-key stock-task override.
CAP_STOCK_TASK_KEYS3 = 1 << 3

ACTIVE_SUBCOMMANDS = frozenset((SUB_POLL, SUB_READ, SUB_READ_AUTH_A))


class RfidDiagError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiagReply:
    status: int
    payload: bytes
    raw: bytes

    @property
    def status_name(self):
        return STATUS_NAMES.get(self.status, "UNKNOWN_%02X" % self.status)


@dataclass(frozen=True)
class InfoReply(DiagReply):
    api_version: int
    readers: int
    slots_per_reader: int
    capabilities: int
    max_read_index: int
    cache_record_size: int


@dataclass(frozen=True)
class StockStateReply(DiagReply):
    auth_cache: int
    state_byte_1: int
    active_slot_raw: int
    state_byte_3: int

    @property
    def busy(self):
        return self.active_slot_raw < 4

    @property
    def active_slot(self):
        return self.active_slot_raw if self.busy else None


@dataclass(frozen=True)
class RuntimeInfoReply(DiagReply):
    hardware_flag: int
    legacy_block0: int
    legacy_block1: int
    legacy_block2: int

    @property
    def secure_backend(self):
        return bool(self.hardware_flag)

    @property
    def legacy_blocks(self):
        return (self.legacy_block0, self.legacy_block1, self.legacy_block2)


@dataclass(frozen=True)
class RemainingStateReply(DiagReply):
    stock_state: int
    stock_remaining: int
    internal_type: int
    runtime_type: int
    initial_percent: int
    flag21: int
    mode22: int
    valid23: int
    secondary_percent: int
    stock_record_first: int
    runtime_record_first: int
    used_mm: int
    total_mm: int
    alternate_total: int
    usage_mm: int
    status_word: int
    runtime_length_ascii: bytes
    stock_length_ascii: bytes


@dataclass(frozen=True)
class TagInfoReply(DiagReply):
    atqa: bytes
    cl1: bytes
    cl2: bytes
    private_work: bytes
    bcc: int
    sak: int
    private_tail: int
    uid: bytes


@dataclass(frozen=True)
class MemoryReply(DiagReply):
    data: bytes


def _byte(value, name):
    if type(value) is not int or not 0 <= value <= 0xFF:
        raise ValueError("%s must be 0..255" % name)
    return value


def _address(value):
    value = _byte(value, "address")
    if not 1 <= value <= 4:
        raise ValueError("CFS address must be 1..4")
    return value


def _reader_slot(logical_slot):
    if type(logical_slot) is not int or not 0 <= logical_slot <= 3:
        raise ValueError("slot must be 0..3")
    return logical_slot // 2, logical_slot % 2


def _key_a(value):
    if isinstance(value, str):
        text = value.strip().replace(" ", "").replace(":", "")
        if len(text) != 12:
            raise ValueError("Key A must be exactly 6 bytes / 12 hex digits")
        try:
            value = bytes.fromhex(text)
        except ValueError as exc:
            raise ValueError("Key A must be hexadecimal") from exc
    else:
        try:
            value = bytes(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Key A must be exactly 6 bytes") from exc
    if len(value) != 6:
        raise ValueError("Key A must be exactly 6 bytes")
    return value


def _keys_a3(value):
    if isinstance(value, str):
        text = value.strip().replace(" ", "").replace(":", "")
        if len(text) != 36:
            raise ValueError("Three Key A values must be exactly 18 bytes / 36 hex digits")
        try:
            value = bytes.fromhex(text)
        except ValueError as exc:
            raise ValueError("Three Key A values must be hexadecimal") from exc
    else:
        try:
            value = bytes(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Three Key A values must be exactly 18 bytes") from exc
    if len(value) != 18:
        raise ValueError("Three Key A values must be exactly 18 bytes")
    return value


def request_payload(subcommand, logical_slot=None, index=None, key_a=None):
    subcommand = _byte(subcommand, "subcommand")
    if subcommand in (SUB_INFO, SUB_STOCK_STATE, SUB_RUNTIME_INFO):
        return bytes((subcommand,))
    if logical_slot is None:
        raise ValueError("slot is required")
    reader, slot = _reader_slot(logical_slot)
    if subcommand in (
            SUB_CACHE, SUB_POLL, SUB_INTERNAL_RECORD, SUB_REMAIN_STATE):
        return bytes((subcommand, reader, slot))
    if subcommand == SUB_READ:
        return bytes((subcommand, reader, slot, _byte(index, "read index")))
    if subcommand == SUB_READ_AUTH_A:
        block = _byte(index, "authenticated block")
        if block > 63:
            raise ValueError("authenticated block must be 0..63")
        return bytes((subcommand, reader, slot, block)) + _key_a(key_a)
    if subcommand == SUB_ARM_STOCK_TASK_KEYS3:
        return bytes((subcommand, reader, slot)) + _keys_a3(key_a)
    if subcommand == SUB_CLEAR_STOCK_TASK_KEYS:
        return bytes((subcommand, reader, slot))
    if subcommand == SUB_RUNTIME_INFO:
        return bytes((subcommand,))
    raise ValueError("unsupported RFID diagnostic subcommand 0x%02x" % subcommand)


def request_body(address, subcommand, logical_slot=None, index=None, key_a=None):
    return build_485_body(
        _address(address), CMD_RFID_DIAG,
        request_payload(subcommand, logical_slot, index, key_a),
        header_byte=0xFF,
    )


def _diag_reply(frame, address):
    reply = box_protocol.decode_reply(frame, _address(address), CMD_RFID_DIAG,
                                      context="rfid_diag")
    if reply.status not in STATUS_NAMES:
        raise box_protocol.ProtocolError(
            "RFID diagnostic response has unknown status 0x%02x" % reply.status)
    return reply


def _require_ok_payload(reply, length, what):
    if reply.status != STATUS_OK:
        if reply.payload:
            raise box_protocol.ProtocolError(
                "%s error response unexpectedly carries %d payload bytes"
                % (what, len(reply.payload)))
        return False
    if len(reply.payload) != length:
        raise box_protocol.ProtocolError(
            "%s response must carry %d bytes, got %d"
            % (what, length, len(reply.payload)))
    return True


def decode_info(frame, address):
    reply = _diag_reply(frame, address)
    if not _require_ok_payload(reply, 6, "RFID INFO"):
        raise RfidDiagError("RFID INFO returned %s" % STATUS_NAMES[reply.status])
    p = reply.payload
    return InfoReply(reply.status, p, reply.raw, *p)


def decode_stock_state(frame, address):
    reply = _diag_reply(frame, address)
    if not _require_ok_payload(reply, 4, "RFID STOCK_STATE"):
        raise RfidDiagError(
            "RFID STOCK_STATE returned %s" % STATUS_NAMES[reply.status])
    p = reply.payload
    return StockStateReply(reply.status, p, reply.raw, *p)


def decode_runtime_info(frame, address):
    reply = _diag_reply(frame, address)
    if not _require_ok_payload(reply, 4, "RFID RUNTIME_INFO"):
        raise RfidDiagError(
            "RFID RUNTIME_INFO returned %s" % STATUS_NAMES[reply.status])
    p = reply.payload
    return RuntimeInfoReply(reply.status, p, reply.raw, *p)


def decode_remaining_state(frame, address):
    reply = _diag_reply(frame, address)
    if not _require_ok_payload(reply, 40, "RFID REMAIN_STATE"):
        raise RfidDiagError(
            "RFID REMAIN_STATE returned %s" % STATUS_NAMES[reply.status])
    p = reply.payload
    u32 = lambda off: int.from_bytes(p[off:off + 4], "little")
    return RemainingStateReply(
        reply.status, p, reply.raw,
        p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8],
        p[9], p[10],
        u32(12), u32(16), u32(20), u32(24), u32(28),
        bytes(p[32:36]), bytes(p[36:40]),
    )


def _uid_from_cache(cl1, cl2):
    if cl1 and cl1[0] == 0x88:
        return bytes(cl1[1:4] + cl2[:4])
    return bytes(cl1[:4])


def decode_tag_info(frame, address):
    reply = _diag_reply(frame, address)
    if not _require_ok_payload(reply, 16, "RFID tag-info"):
        return DiagReply(reply.status, reply.payload, reply.raw)
    p = reply.payload
    atqa = bytes(p[0:2])
    cl1 = bytes(p[2:6])
    cl2 = bytes(p[6:10])
    private_work = bytes(p[10:13])
    return TagInfoReply(
        reply.status, p, reply.raw,
        atqa, cl1, cl2, private_work, p[13], p[14], p[15],
        _uid_from_cache(cl1, cl2),
    )


def decode_memory(frame, address):
    reply = _diag_reply(frame, address)
    if not _require_ok_payload(reply, 16, "RFID memory-read"):
        return DiagReply(reply.status, reply.payload, reply.raw)
    return MemoryReply(reply.status, reply.payload, reply.raw, bytes(reply.payload))


def decode_internal_record(frame, address):
    reply = _diag_reply(frame, address)
    if not _require_ok_payload(reply, 76, "RFID internal-record"):
        return DiagReply(reply.status, reply.payload, reply.raw)
    return MemoryReply(reply.status, reply.payload, reply.raw, bytes(reply.payload))


def decode_arm_keys3(frame, address):
    reply = _diag_reply(frame, address)
    if reply.status == STATUS_OK:
        if len(reply.payload) != 4:
            raise box_protocol.ProtocolError(
                "RFID ARM_KEYS response must carry the 4-byte cached UID")
        return MemoryReply(reply.status, reply.payload, reply.raw,
                           bytes(reply.payload))
    if reply.payload:
        raise box_protocol.ProtocolError(
            "RFID ARM_KEYS error response unexpectedly carries payload")
    return DiagReply(reply.status, reply.payload, reply.raw)


def decode_clear_keys(frame, address):
    reply = _diag_reply(frame, address)
    if reply.status == STATUS_OK:
        if reply.payload:
            raise box_protocol.ProtocolError(
                "RFID CLEAR_KEYS success response must be empty")
        return reply
    if reply.payload:
        raise box_protocol.ProtocolError(
            "RFID CLEAR_KEYS error response unexpectedly carries payload")
    return reply


class RfidDiagDriver:
    """Policy-free transport client for opcode 0x57 on one CFS address."""

    def __init__(self, serial, address):
        self.serial = serial
        self.address = _address(address)

    def _exchange(self, subcommand, logical_slot=None, index=None, key_a=None,
                  timeout=1.0):
        body = request_body(
            self.address, subcommand, logical_slot=logical_slot,
            index=index, key_a=key_a)
        return self.serial.cmd_send_data_with_response(body, timeout)

    def info(self, timeout=1.0):
        frame = self._exchange(SUB_INFO, timeout=timeout)
        return None if not frame else decode_info(frame, self.address)

    def stock_state(self, timeout=1.0):
        frame = self._exchange(SUB_STOCK_STATE, timeout=timeout)
        return None if not frame else decode_stock_state(frame, self.address)

    def cache(self, slot, timeout=1.0):
        frame = self._exchange(SUB_CACHE, logical_slot=slot, timeout=timeout)
        return None if not frame else decode_tag_info(frame, self.address)

    def poll(self, slot, timeout=1.0):
        frame = self._exchange(SUB_POLL, logical_slot=slot, timeout=timeout)
        return None if not frame else decode_tag_info(frame, self.address)

    def internal_record(self, slot, timeout=1.0):
        frame = self._exchange(SUB_INTERNAL_RECORD, logical_slot=slot, timeout=timeout)
        return None if not frame else decode_internal_record(frame, self.address)

    def read(self, slot, index, timeout=1.0):
        frame = self._exchange(
            SUB_READ, logical_slot=slot, index=index, timeout=timeout)
        return None if not frame else decode_memory(frame, self.address)

    def read_auth_a(self, slot, block, key_a, timeout=1.0):
        frame = self._exchange(
            SUB_READ_AUTH_A, logical_slot=slot, index=block, key_a=key_a,
            timeout=timeout)
        return None if not frame else decode_memory(frame, self.address)

    def runtime_info(self, timeout=1.0):
        frame = self._exchange(SUB_RUNTIME_INFO, timeout=timeout)
        return None if not frame else decode_runtime_info(frame, self.address)

    def remaining_state(self, slot, timeout=1.0):
        frame = self._exchange(
            SUB_REMAIN_STATE, logical_slot=slot, timeout=timeout)
        return None if not frame else decode_remaining_state(
            frame, self.address)

    def arm_stock_task_key(self, slot, key_a, timeout=1.0):
        reader, local = _reader_slot(slot)
        payload = bytes((SUB_ARM_STOCK_TASK_KEYS3, reader, local)) + _key_a(key_a)
        body = build_485_body(self.address, CMD_RFID_DIAG, payload, header_byte=0xFF)
        frame = self.serial.cmd_send_data_with_response(body, timeout)
        return None if not frame else decode_arm_keys3(frame, self.address)

    def arm_stock_task_keys3(self, slot, keys_a, timeout=1.0):
        frame = self._exchange(
            SUB_ARM_STOCK_TASK_KEYS3, logical_slot=slot, key_a=keys_a,
            timeout=timeout)
        return None if not frame else decode_arm_keys3(frame, self.address)

    def clear_stock_task_keys(self, slot, timeout=1.0):
        frame = self._exchange(
            SUB_CLEAR_STOCK_TASK_KEYS, logical_slot=slot, timeout=timeout)
        return None if not frame else decode_clear_keys(frame, self.address)


class BoxRfidDiag:
    """Optional Kalico-facing safety/presentation layer for RfidDiagDriver."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.serial_name = config.get("serial", "serial485")
        self.default_address = config.getint("address", 1, minval=1, maxval=4)
        self.allow_active_rf = config.getboolean("allow_active_rf", False)
        self.require_idle = config.getboolean("require_idle", True)
        self.serial = None
        self.last_result = None
        self.last_error = None
        self.last_info = None

        self.printer.register_event_handler("serial_485:ready", self._serial_ready)
        commands = (
            ("BOX_RFID_DIAG_INFO", self.cmd_info),
            ("BOX_RFID_DIAG_STATE", self.cmd_state),
            ("BOX_RFID_DIAG_CACHE", self.cmd_cache),
            ("BOX_RFID_DIAG_INTERNAL", self.cmd_internal),
            ("BOX_RFID_DIAG_POLL", self.cmd_poll),
            ("BOX_RFID_DIAG_READ", self.cmd_read),
            ("BOX_RFID_DIAG_READ_AUTH_A", self.cmd_read_auth_a),
            ("BOX_RFID_DIAG_RUNTIME", self.cmd_runtime),
            ("BOX_RFID_DIAG_REMAIN_STATE", self.cmd_remaining_state),
            ("BOX_RFID_DIAG_ARM_KEY", self.cmd_arm_key),
            ("BOX_RFID_DIAG_CLEAR_KEY", self.cmd_clear_key),
            ("BOX_RFID_DIAG_ARM_KEYS", self.cmd_arm_keys),
            ("BOX_RFID_DIAG_CLEAR_KEYS", self.cmd_clear_keys),
            ("BOX_RFID_DIAG_STOCK_CAPTURE", self.cmd_stock_capture),
        )
        for name, handler in commands:
            self.gcode.register_command(name, handler)

    def _serial_ready(self, *args):
        self.serial = self.printer.lookup_object(
            "serial_485 %s" % self.serial_name)

    def _transport(self):
        if self.serial is None:
            self.serial = self.printer.lookup_object(
                "serial_485 %s" % self.serial_name, None)
        if self.serial is None:
            raise RfidDiagError("RS-485 transport is not ready")
        return self.serial

    def _address_param(self, gcmd):
        return gcmd.get_int("ADDRESS", self.default_address, minval=1, maxval=4)

    @staticmethod
    def _slot_param(gcmd):
        return gcmd.get_int("SLOT", minval=0, maxval=3)

    def _ensure_active_allowed(self, gcmd):
        if not self.allow_active_rf:
            raise gcmd.error(
                "Active CFS RFID diagnostics are disabled; set "
                "allow_active_rf: true only for controlled testing")
        if gcmd.get_int("CONFIRM", 0, minval=0, maxval=1) != 1:
            raise gcmd.error("Active CFS RFID diagnostics require CONFIRM=1")
        box = self.printer.lookup_object("box", None)
        if box is not None and getattr(box, "observation_mode", False):
            raise gcmd.error(
                "Active CFS RFID diagnostics are refused while Box observation_mode is enabled")
        if self.require_idle:
            stats = self.printer.lookup_object("print_stats", None)
            state = getattr(stats, "state", None)
            if state not in ("standby", "complete", "cancelled"):
                raise gcmd.error(
                    "Active CFS RFID diagnostics require an idle printer; print state is %s"
                    % (state if state is not None else "unavailable"))

    @staticmethod
    def _require_v21(gcmd, info):
        if info is None:
            raise gcmd.error("CFS RFID INFO timed out")
        required = CAP_STOCK_STATE | CAP_STOCK_GUARD
        if (info.api_version != API_V21
                or info.readers != 2
                or info.slots_per_reader != 2
                or info.cache_record_size != 16
                or info.max_read_index != 255
                or info.capabilities & required != required):
            raise gcmd.error(
                "Active CFS RFID diagnostics require the validated v2.1/API3 "
                "shape (api=3 readers=2 slots=2 cache=16 max_index=255 "
                "with STOCK_STATE+GUARD)")

    @staticmethod
    def _require_single_key_override_api(gcmd, info):
        if info is None:
            raise gcmd.error("CFS RFID INFO timed out")
        required = (CAP_STOCK_STATE | CAP_INTERNAL_RECORD |
                    CAP_STOCK_TASK_KEYS3)
        if (info.api_version != API_SINGLE_STOCK_TASK
                or info.readers != 2
                or info.slots_per_reader != 2
                or info.cache_record_size != 16
                or info.max_read_index != 0
                or info.capabilities & required != required):
            raise gcmd.error(
                "Stock-task single-Key-A override requires API5 shape "
                "(api=5 readers=2 slots=2 cache=16 max_index=0 "
                "with STATE+INTERNAL+KEY_OVERRIDE)")

    @contextmanager
    def _single_key_override_session(self, gcmd, address):
        self._ensure_active_allowed(gcmd)
        with self._transport().request_session() as transport:
            driver = RfidDiagDriver(transport, address)
            info = driver.info()
            self._require_single_key_override_api(gcmd, info)
            self.last_info = info
            state = driver.stock_state()
            if state is None:
                raise gcmd.error("CFS STOCK_STATE timed out")
            if state.busy:
                raise gcmd.error(
                    "CFS stock RFID manager is busy on logical slot %d"
                    % state.active_slot)
            yield driver

    @staticmethod
    def _require_key_override_api(gcmd, info):
        if info is None:
            raise gcmd.error("CFS RFID INFO timed out")
        required = (CAP_STOCK_STATE | CAP_INTERNAL_RECORD |
                    CAP_STOCK_TASK_KEYS3)
        if (info.api_version not in (API_DUAL_STOCK_TASK, API_STOCK_CAPTURE)
                or info.readers != 2
                or info.slots_per_reader != 2
                or info.cache_record_size != 16
                or info.max_read_index != 3
                or info.capabilities & required != required):
            raise gcmd.error(
                "Stock-task three-Key-A override requires API6/API7 shape "
                "(api=6|7 readers=2 slots=2 cache=16 max_index=3 "
                "with STATE+INTERNAL+KEYS3)")

    @contextmanager
    def _key_override_session(self, gcmd, address):
        self._ensure_active_allowed(gcmd)
        with self._transport().request_session() as transport:
            driver = RfidDiagDriver(transport, address)
            info = driver.info()
            self._require_key_override_api(gcmd, info)
            self.last_info = info
            state = driver.stock_state()
            if state is None:
                raise gcmd.error("CFS STOCK_STATE timed out")
            if state.busy:
                raise gcmd.error(
                    "CFS stock RFID manager is busy on logical slot %d"
                    % state.active_slot)
            yield driver

    @contextmanager
    def _active_session(self, gcmd, address):
        self._ensure_active_allowed(gcmd)
        with self._transport().request_session() as transport:
            driver = RfidDiagDriver(transport, address)
            info = driver.info()
            self._require_v21(gcmd, info)
            self.last_info = info
            state = driver.stock_state()
            if state is None:
                raise gcmd.error("CFS STOCK_STATE timed out")
            if state.busy:
                raise gcmd.error(
                    "CFS stock RFID manager is busy on logical slot %d"
                    % state.active_slot)
            yield driver

    def _remember(self, command, result=None, error=None):
        self.last_result = None if result is None else {
            "command": command,
            "status": getattr(result, "status", None),
            "status_name": getattr(result, "status_name", None),
        }
        self.last_error = None if error is None else str(error)

    @staticmethod
    def _require_response(gcmd, result, command):
        if result is None:
            raise gcmd.error("%s timed out" % command)
        return result

    @staticmethod
    def _show_tag(gcmd, label, result):
        if not isinstance(result, TagInfoReply):
            gcmd.respond_info("%s status=%s" % (label, result.status_name))
            return
        gcmd.respond_info(
            "%s status=OK UID=%s ATQA=%s SAK=%02X CL1=%s CL2=%s BCC=%02X raw=%s"
            % (label, result.uid.hex().upper(), result.atqa.hex().upper(),
               result.sak, result.cl1.hex().upper(), result.cl2.hex().upper(),
               result.bcc, result.payload.hex().upper()))

    def cmd_info(self, gcmd):
        address = self._address_param(gcmd)
        try:
            result = self._require_response(
                gcmd, RfidDiagDriver(self._transport(), address).info(),
                "CFS RFID INFO")
            self.last_info = result
            self._remember("info", result)
        except Exception as exc:
            self._remember("info", error=exc)
            raise
        gcmd.respond_info(
            "CFS RFID diag API=%d readers=%d slots/reader=%d caps=0x%02X "
            "max_index=%d cache_size=%d"
            % (result.api_version, result.readers, result.slots_per_reader,
               result.capabilities, result.max_read_index,
               result.cache_record_size))

    def cmd_state(self, gcmd):
        address = self._address_param(gcmd)
        try:
            result = self._require_response(
                gcmd, RfidDiagDriver(self._transport(), address).stock_state(),
                "CFS RFID STOCK_STATE")
            self._remember("state", result)
        except Exception as exc:
            self._remember("state", error=exc)
            raise
        gcmd.respond_info(
            "CFS RFID stock_state busy=%s active_slot=%s raw_slot=%d raw=%s"
            % (result.busy, result.active_slot, result.active_slot_raw,
               result.payload.hex().upper()))

    def cmd_cache(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        try:
            result = self._require_response(
                gcmd, RfidDiagDriver(self._transport(), address).cache(slot),
                "CFS RFID CACHE")
            self._remember("cache", result)
        except Exception as exc:
            self._remember("cache", error=exc)
            raise
        self._show_tag(gcmd, "CFS RFID cache slot=%d" % slot, result)

    def cmd_internal(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        try:
            result = self._require_response(
                gcmd, RfidDiagDriver(self._transport(), address).internal_record(slot),
                "CFS RFID INTERNAL_RECORD")
            self._remember("internal_record", result)
        except Exception as exc:
            self._remember("internal_record", error=exc)
            raise
        if isinstance(result, MemoryReply):
            gcmd.respond_info(
                "CFS RFID internal slot=%d len=%d raw=%s"
                % (slot, len(result.data), result.data.hex().upper()))
        else:
            gcmd.respond_info(
                "CFS RFID internal slot=%d status=%s"
                % (slot, result.status_name))

    def cmd_poll(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        try:
            with self._active_session(gcmd, address) as driver:
                result = self._require_response(
                    gcmd, driver.poll(slot), "CFS RFID POLL")
            self._remember("poll", result)
        except Exception as exc:
            self._remember("poll", error=exc)
            raise
        self._show_tag(gcmd, "CFS RFID poll slot=%d" % slot, result)

    def cmd_read(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        index = gcmd.get_int("INDEX", minval=0, maxval=255)
        try:
            with self._active_session(gcmd, address) as driver:
                result = self._require_response(
                    gcmd, driver.read(slot, index), "CFS RFID READ")
            self._remember("read", result)
        except Exception as exc:
            self._remember("read", error=exc)
            raise
        if isinstance(result, MemoryReply):
            gcmd.respond_info(
                "CFS RFID read slot=%d index=%d data=%s"
                % (slot, index, result.data.hex().upper()))
        else:
            gcmd.respond_info(
                "CFS RFID read slot=%d index=%d status=%s"
                % (slot, index, result.status_name))

    def cmd_read_auth_a(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        block = gcmd.get_int("BLOCK", minval=0, maxval=63)
        key = gcmd.get("KEY", None)
        try:
            key = _key_a(key)
        except ValueError as exc:
            raise gcmd.error(str(exc))
        try:
            with self._active_session(gcmd, address) as driver:
                result = self._require_response(
                    gcmd, driver.read_auth_a(slot, block, key),
                    "CFS RFID READ_AUTH_A")
            self._remember("read_auth_a", result)
        except Exception as exc:
            self._remember("read_auth_a", error=exc)
            raise
        if isinstance(result, MemoryReply):
            gcmd.respond_info(
                "CFS RFID auth-read slot=%d block=%d data=%s"
                % (slot, block, result.data.hex().upper()))
        else:
            gcmd.respond_info(
                "CFS RFID auth-read slot=%d block=%d status=%s"
                % (slot, block, result.status_name))

    def cmd_runtime(self, gcmd):
        address = self._address_param(gcmd)
        try:
            result = self._require_response(
                gcmd, RfidDiagDriver(self._transport(), address).runtime_info(),
                "CFS RFID RUNTIME_INFO")
            self._remember("runtime_info", result)
        except Exception as exc:
            self._remember("runtime_info", error=exc)
            raise
        gcmd.respond_info(
            "CFS RFID runtime secure_backend=%s hw_flag=%d legacy_blocks=%d,%d,%d raw=%s"
            % (result.secure_backend, result.hardware_flag,
               result.legacy_block0, result.legacy_block1, result.legacy_block2,
               result.payload.hex().upper()))

    def cmd_remaining_state(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        try:
            with self._transport().request_session() as transport:
                driver = RfidDiagDriver(transport, address)
                info = self._require_response(
                    gcmd, driver.info(), "CFS RFID INFO")
                self.last_info = info
                if (info.api_version != API_STOCK_CAPTURE
                        or not (info.capabilities & CAP_REMAIN_STATE)):
                    raise gcmd.error(
                        "CFS RFID REMAIN_STATE requires API7 with "
                        "REMAIN_STATE capability")
                result = self._require_response(
                    gcmd, driver.remaining_state(slot),
                    "CFS RFID REMAIN_STATE")
            self._remember("remain_state", result)
        except Exception as exc:
            self._remember("remain_state", error=exc)
            raise

        def ascii4(value):
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                return value.hex().upper()

        gcmd.respond_info(
            "CFS RFID remain_state slot=%d state=%d remaining=%d "
            "internal_type=%d runtime_type=%d initial=%d "
            "flags=%d,%d,%d secondary=%d stock0=0x%02X runtime0=0x%02X "
            "used_mm=%d total_mm=%d alternate_total=%d usage_mm=%d "
            "status=0x%08X runtime_len=%r stock_len=%r raw=%s"
            % (slot, result.stock_state, result.stock_remaining,
               result.internal_type, result.runtime_type,
               result.initial_percent, result.flag21, result.mode22,
               result.valid23, result.secondary_percent,
               result.stock_record_first, result.runtime_record_first,
               result.used_mm, result.total_mm, result.alternate_total,
               result.usage_mm, result.status_word,
               ascii4(result.runtime_length_ascii),
               ascii4(result.stock_length_ascii),
               result.payload.hex().upper()))

    def cmd_arm_key(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        try:
            key = _key_a(gcmd.get("KEY", None))
        except ValueError as exc:
            raise gcmd.error(str(exc))
        try:
            with self._single_key_override_session(gcmd, address) as driver:
                result = self._require_response(
                    gcmd, driver.arm_stock_task_key(slot, key),
                    "CFS RFID ARM_KEY")
            self._remember("arm_key", result)
        except Exception as exc:
            self._remember("arm_key", error=exc)
            raise
        if isinstance(result, MemoryReply):
            gcmd.respond_info(
                "CFS RFID stock-task key armed slot=%d UID=%s"
                % (slot, result.data.hex().upper()))
        else:
            gcmd.respond_info(
                "CFS RFID ARM_KEY slot=%d status=%s"
                % (slot, result.status_name))

    def cmd_clear_key(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        try:
            with self._transport().request_session() as transport:
                driver = RfidDiagDriver(transport, address)
                info = driver.info()
                self._require_single_key_override_api(gcmd, info)
                self.last_info = info
                state = driver.stock_state()
                if state is None:
                    raise gcmd.error("CFS STOCK_STATE timed out")
                if state.busy:
                    raise gcmd.error(
                        "CFS stock RFID manager is busy on logical slot %d"
                        % state.active_slot)
                result = self._require_response(
                    gcmd, driver.clear_stock_task_keys(slot),
                    "CFS RFID CLEAR_KEY")
            self._remember("clear_key", result)
        except Exception as exc:
            self._remember("clear_key", error=exc)
            raise
        gcmd.respond_info("CFS RFID stock-task key cleared slot=%d" % slot)

    def cmd_arm_keys(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        try:
            keys = b"".join(_key_a(gcmd.get("KEY%d" % i, None)) for i in range(3))
        except ValueError as exc:
            raise gcmd.error(str(exc))
        try:
            with self._key_override_session(gcmd, address) as driver:
                result = self._require_response(
                    gcmd, driver.arm_stock_task_keys3(slot, keys),
                    "CFS RFID ARM_KEYS")
            self._remember("arm_keys", result)
        except Exception as exc:
            self._remember("arm_keys", error=exc)
            raise
        if isinstance(result, MemoryReply):
            gcmd.respond_info(
                "CFS RFID stock-task keys armed slot=%d UID=%s"
                % (slot, result.data.hex().upper()))
        else:
            gcmd.respond_info(
                "CFS RFID ARM_KEYS slot=%d status=%s"
                % (slot, result.status_name))

    @staticmethod
    def _stock_candidate_from_record(reply):
        if not isinstance(reply, MemoryReply) or len(reply.data) != 76:
            return None
        data = bytes(reply.data)
        atqa, uid, sak = data[60:62], data[62:66], data[74]
        if (atqa != b"\x04\x00" or sak != 0x08
                or uid == b"\x00\x00\x00\x00"):
            return None
        return atqa, uid, sak

    def cmd_stock_capture(self, gcmd):
        """Run one generic API7 stock-task capture with explicit Key A values.

        This is intended for controlled read-only interoperability research
        (QIDI, Snapmaker, etc.). RF ownership remains with Creality's stock
        CFS worker; the host only supplies temporary keys and reads scratch.
        """
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        if gcmd.get_int("CONFIRM", 0, minval=0, maxval=1) != 1:
            raise gcmd.error("API7 stock capture requires CONFIRM=1")
        stats = self.printer.lookup_object("print_stats", None)
        state = getattr(stats, "state", None)
        if self.require_idle and state not in ("standby", "complete", "cancelled"):
            raise gcmd.error(
                "API7 stock capture requires an idle printer; print state is %s"
                % (state if state is not None else "unavailable"))
        try:
            keys = b"".join(
                _key_a(gcmd.get("KEY%d" % i, None)) for i in range(3))
        except ValueError as exc:
            raise gcmd.error(str(exc))

        box = self.printer.lookup_object("box", None)
        box_driver = None if box is None else getattr(box, "drivers", {}).get(address)
        if box_driver is None:
            raise gcmd.error("CFS Box driver is not ready")

        def inspect():
            with self._transport().request_session() as transport:
                driver = RfidDiagDriver(transport, address)
                info = driver.info()
                self._require_key_override_api(gcmd, info)
                if (info.api_version != API_STOCK_CAPTURE
                        or not (info.capabilities & CAP_STOCK_CAPTURE)):
                    raise gcmd.error(
                        "generic stock capture requires API7 with STOCK_CAPTURE")
                runtime = driver.runtime_info()
                if runtime is None:
                    raise gcmd.error("CFS RFID RUNTIME_INFO timed out")
                if runtime.secure_backend:
                    raise gcmd.error(
                        "API7 stock capture is validated only on the legacy CFS RFID backend")
                state_reply = driver.stock_state()
                if state_reply is None:
                    raise gcmd.error("CFS STOCK_STATE timed out")
                if state_reply.busy:
                    raise gcmd.error(
                        "CFS stock RFID manager is busy on logical slot %d"
                        % state_reply.active_slot)
                target = driver.internal_record(slot)
                return driver, self._stock_candidate_from_record(target)

        _driver, candidate = inspect()
        if candidate is None:
            # Prime the stock cache once so UID/ATQA/SAK are available before
            # deriving/arming any third-party key strategy.
            if box_driver.force_rfid_read(1 << slot) is None:
                raise gcmd.error("stock CFS RFID cache-prime read timed out")
            _driver, candidate = inspect()
            if candidate is None:
                raise gcmd.error(
                    "stock CFS record is not a MIFARE Classic 1K candidate after cache prime")

        atqa, uid, sak = candidate
        try:
            with self._transport().request_session() as transport:
                driver = RfidDiagDriver(transport, address)
                armed = driver.arm_stock_task_keys3(slot, keys)
                if not isinstance(armed, MemoryReply) or bytes(armed.data) != uid:
                    status = "timeout" if armed is None else getattr(
                        armed, "status_name", "invalid")
                    raise gcmd.error("CFS API7 ARM_KEYS failed: %s" % status)

            if box_driver.force_rfid_read(1 << slot) is None:
                raise gcmd.error("stock CFS RFID capture read timed out")

            with self._transport().request_session() as transport:
                driver = RfidDiagDriver(transport, address)
                target = driver.internal_record(slot)
                cap1 = driver.internal_record((slot + 2) % 4)
                cap2 = driver.internal_record((slot + 3) % 4)
                if not all(isinstance(x, MemoryReply) for x in (target, cap1, cap2)):
                    raise gcmd.error("API7 capture records are unavailable")
                t, c1, c2 = bytes(target.data), bytes(cap1.data), bytes(cap2.data)
                hit, ok, fail = t[9], t[10], t[11]
                marker1, marker2 = c1[4:8], c2[4:8]
                block4 = c1[8:20] + c2[8:12]
                block5 = c2[12:16]
                driver.clear_stock_task_keys(slot)
        except Exception:
            try:
                with self._transport().request_session() as transport:
                    RfidDiagDriver(transport, address).clear_stock_task_keys(slot)
            except Exception:
                pass
            raise

        gcmd.respond_info(
            "CFS RFID stock-capture slot=%d UID=%s ATQA=%s SAK=%02X "
            "hit=0x%02X ok=0x%02X fail=0x%02X marker1=%s marker2=%s "
            "block4=%s block5=%s"
            % (slot, uid.hex().upper(), atqa.hex().upper(), sak,
               hit, ok, fail, marker1.decode("ascii", "replace"),
               marker2.decode("ascii", "replace"),
               block4.hex().upper(), block5.hex().upper()))

    def cmd_clear_keys(self, gcmd):
        address, slot = self._address_param(gcmd), self._slot_param(gcmd)
        try:
            with self._transport().request_session() as transport:
                driver = RfidDiagDriver(transport, address)
                info = driver.info()
                self._require_key_override_api(gcmd, info)
                self.last_info = info
                state = driver.stock_state()
                if state is None:
                    raise gcmd.error("CFS STOCK_STATE timed out")
                if state.busy:
                    raise gcmd.error(
                        "CFS stock RFID manager is busy on logical slot %d"
                        % state.active_slot)
                result = self._require_response(
                    gcmd, driver.clear_stock_task_keys(slot),
                    "CFS RFID CLEAR_KEYS")
            self._remember("clear_keys", result)
        except Exception as exc:
            self._remember("clear_keys", error=exc)
            raise
        gcmd.respond_info("CFS RFID stock-task keys cleared slot=%d" % slot)

    def get_status(self, _eventtime):
        return {
            "serial": self.serial_name,
            "address": self.default_address,
            "allow_active_rf": self.allow_active_rf,
            "require_idle": self.require_idle,
            "transport_ready": self.serial is not None,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "api_version": None if self.last_info is None else self.last_info.api_version,
            "api_capabilities": None if self.last_info is None else self.last_info.capabilities,
        }


def load_config(config):
    return BoxRfidDiag(config)