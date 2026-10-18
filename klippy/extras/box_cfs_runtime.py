# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Volatile runtime configuration for patched Creality K2 Pro CFS firmware.

This module is intentionally separate from box.py. It uses the existing
serial_485 request queue and never opens or owns the RS-485 device directly.

Firmware compatibility:
  v3.13 / Runtime Config API v1
    0x0D CONFIG_INFO
    0x0E CONFIG_SET
    0x0F CONFIG_RESET

  v3.14+ / Runtime Config API v2
    0x10 CONFIG_V2_INFO
    0x11 CONFIG_V2_GET
    0x12 CONFIG_V2_SET
    0x13 CONFIG_V2_RESET
    0x14 CONFIG_V2_DESCRIBE

All permitted writes are RAM-only. Experimental API2 firmware v3.19-v3.22
advertises 28 parameters but ONLY v3.20-v3.22 IDs 7/8 are connected to
the real stock execution path. Other advanced IDs are bench storage without
motion effect; they must never be presented as effective tuning knobs.
The MCU BOX_STATE guard is NOT a complete motor-activity interlock. Kalico
requires its host operation/RFID mutex for supervised writes. EEPROM and
physical RFID tags are never written by this extra.
"""

import logging
import struct
from collections import namedtuple

from extras import box_protocol
from extras.serial_485 import build_485_body


CMD_RFID_DIAG = 0x57

# Legacy API v1 (v3.13 compatibility)
SUB_CONFIG_INFO = 0x0D
SUB_CONFIG_SET = 0x0E
SUB_CONFIG_RESET = 0x0F
CONFIG_VERSION = 1
CONFIG_COUNT = 6

# Runtime Config API v2 (v3.14+)
SUB_CONFIG_V2_INFO = 0x10
SUB_CONFIG_V2_GET = 0x11
SUB_CONFIG_V2_SET = 0x12
SUB_CONFIG_V2_RESET = 0x13
SUB_CONFIG_V2_DESCRIBE = 0x14
CONFIG_V2_VERSION = 2
CONFIG_V2_DESCRIPTOR_SIZE = 1  # firmware INFO advertises catalog revision 1

# Verified effective stock hooks by exact experimental firmware feature byte.
# This is NOT a writable bitmask: stored-only RAM overrides are deliberately
# excluded even when a raw firmware CONFIG_V2_SET would accept them.
# Do not extend this table without proof of each real stock callsite.
EXPERIMENTAL_EFFECTIVE_HOOKS = {
    0xF7: frozenset(),        # v3.19: all 21 values are RAM bench only
    0xB7: frozenset((7, 8)), # v3.20: two proven original timing callsites
    0x97: frozenset((7, 8)), # v3.21: MCU BOX_STATE guard (insufficient alone)
    0xD7: frozenset((7, 8)), # v3.22: 4 extra GET-only opaque probes
}

STATUS_OK = 0
STATUS_BAD_REQUEST = 1
STATUS_STOCK_BUSY = 7

FLAG_WRITABLE = 1 << 0
FLAG_IDLE_ONLY = 1 << 1
FLAG_ADVANCED = 1 << 2
FLAG_RFID_SENSITIVE = 1 << 3
FLAG_SAFETY = 1 << 4

TYPE_BOOL = 0
TYPE_U8 = 1
TYPE_U16 = 2
TYPE_U32 = 4

KIND_TABLE_U8 = 0
KIND_ROOT_U8 = 1
KIND_SHADOW_U32 = 2

ParamSpec = namedtuple(
    "ParamSpec",
    "name param_id default minval maxval value_type flags description",
)


def _p(name, param_id, default, minval, maxval, value_type, flags, description):
    return ParamSpec(
        name, param_id, default, minval, maxval, value_type, flags, description)


PARAMETERS = (
    _p('feeder_forward_speed', 0, 255, 1, 255, 'int',
       0, 'Feeder forward speed; stock 255.'),
    _p('hub_forward_speed', 1, 100, 1, 255, 'int',
       0, 'Hub forward speed; stock 100.'),
    _p('hub_transition_speed', 2, 200, 1, 255, 'int',
       0, 'Hub transition speed; stock 200.'),
    _p('hub_insert_speed', 3, 155, 1, 255, 'int',
       0, 'Hub insert speed; stock 155.'),
    _p('feeder_reverse_speed', 4, 255, 1, 255, 'int',
       0, 'Feeder reverse speed; stock 255.'),
    _p('hub_reverse_speed', 5, 80, 1, 255, 'int',
       0, 'Hub reverse speed; stock 80.'),
    _p('reverse_detooth_enable', 6, None, 0, 1, 'bool',
       FLAG_ADVANCED | FLAG_SAFETY, 'Reverse detooth enable; stock runtime-captured.'),
    _p('hub_transition_wait_ms', 7, 3200, 100, 10000, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Hub transition wait ms; stock 3200.'),
    _p('insert_sensor_timeout_ms', 8, 700, 100, 5000, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Insert sensor timeout ms; stock 700.'),
    _p('hub_pullback_timeout_ms', 9, 5000, 500, 15000, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Hub pullback timeout ms; stock 5000.'),
    _p('unload_sensor_timeout_ms', 10, 1000, 100, 5000, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Unload sensor timeout ms; stock 1000.'),
    _p('rfid_seek_speed', 11, 160, 1, 255, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid seek speed; stock 160.'),
    _p('rfid_measure_speed', 12, 120, 1, 255, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid measure speed; stock 120.'),
    _p('rfid_reposition_speed', 13, 100, 1, 255, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid reposition speed; stock 100.'),
    _p('rfid_seek_move_ms', 14, 800, 100, 5000, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid seek move ms; stock 800.'),
    _p('rfid_seek_settle_ms', 15, 200, 10, 2000, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid seek settle ms; stock 200.'),
    _p('rfid_post_auth_wait_ms', 16, 1000, 100, 5000, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid post auth wait ms; stock 1000.'),
    _p('rfid_pre_measure_settle_ms', 17, 10, 1, 1000, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid pre measure settle ms; stock 10.'),
    _p('rfid_neighbor_detect_delay_ms', 18, 400, 50, 2000, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid neighbor detect delay ms; stock 400.'),
    _p('rfid_neighbor_settle_ms', 19, 100, 10, 1000, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid neighbor settle ms; stock 100.'),
    _p('feed_total_timeout_20ms_ticks', 20, 1250, 250, 3000, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Feed total timeout 20ms ticks; stock 1250.'),
    _p('feed_recovery_timeout_ms', 21, 3000, 500, 10000, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Feed recovery timeout ms; stock 3000.'),
    _p('odometer_stall_20ms_ticks', 22, 25, 5, 100, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Odometer stall 20ms ticks; stock 25.'),
    _p('rfid_neighbor_retry_count', 23, 3, 1, 10, 'int',
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE, 'Rfid neighbor retry count; stock 3.'),
    _p('reverse_detooth_timeout_ms', 24, 1000, 100, 10000, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Reverse detooth timeout ms; stock 1000.'),
    _p('buffer_fill_timeout_ms', 25, 30000, 1000, 60000, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Buffer fill timeout ms; stock 30000.'),
    _p('transition_detect_timeout_ms', 26, 300, 50, 5000, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Transition detect timeout ms; stock 300.'),
    _p('load_retry_count', 27, 3, 1, 10, 'int',
       FLAG_ADVANCED | FLAG_SAFETY, 'Load retry count; stock 3.'),
)

PARAM_BY_NAME = {p.name: p for p in PARAMETERS}
PARAM_BY_ID = {p.param_id: p for p in PARAMETERS}


# K2-OpenHost firmware identity (command 0x7A, OpenCFS-compatible layout).
# Runtime values are written only to a CFS that answers with this magic and
# format, and that reports K2OH_FEATURES in CONFIG_V2_INFO: the v3.23
# contract below (every time in milliseconds, the firmware converting to its
# stock unit). OpenCFS ("OCFS"), the stock firmware and v3.13-v3.22 get no
# writes.
CMD_BUILD_INFO = 0x7A
K2OH_MAGIC = "K2OH"
K2OH_FORMAT = 1
K2OH_FEATURES = 0xC7
FLAG_K2OH_WRITABLE = 1 << 5


def _k(name, param_id, default, minval, maxval, value_type, writable,
       description):
    flags = FLAG_K2OH_WRITABLE if writable else 0
    return ParamSpec(
        name, param_id, default, minval, maxval, value_type, flags, description)


# v3.23 K2-OpenHost catalogue. IDs 0 and 4 are read-only (the feeders are
# driven on/off, a speed has no effect); 10, 25 and 26 are read-only (their
# stock code is never executed).
K2OH_PARAMETERS = (
    _k('feeder_forward_speed', 0, 255, 1, 255, 'int', False,
       'Feeder forward speed; read-only, the feeders are on/off.'),
    _k('hub_forward_speed', 1, 100, 1, 255, 'int', True,
       'Hub forward speed; stock 100.'),
    _k('hub_transition_speed', 2, 200, 1, 255, 'int', True,
       'Hub transition speed; stock 200.'),
    _k('hub_insert_speed', 3, 155, 1, 255, 'int', True,
       'Hub insert speed; stock 155.'),
    _k('feeder_reverse_speed', 4, 255, 1, 255, 'int', False,
       'Feeder reverse speed; read-only, the feeders are on/off.'),
    _k('hub_reverse_speed', 5, 80, 1, 255, 'int', True,
       'Hub reverse speed; stock 80.'),
    _k('reverse_detooth_enable', 6, 1, 0, 1, 'bool', True,
       'Reverse de-tooth switch; stock on.'),
    _k('hub_transition_wait_ms', 7, 3200, 100, 10000, 'int', True,
       'Wait after the hub push before the buffer path, ms; stock 3200.'),
    _k('insert_sensor_timeout_ms', 8, 7000, 1000, 50000, 'int', True,
       'Time allowed for the hub sensor during insertion, ms; stock 7000.'),
    _k('hub_pullback_timeout_ms', 9, 5000, 500, 15000, 'int', True,
       'Buffer wait while unloading, ms; stock 5000.'),
    _k('unload_sensor_timeout_ms', 10, 1000, 100, 5000, 'int', False,
       'Read-only: stock code never executed.'),
    _k('rfid_seek_speed', 11, 160, 1, 255, 'int', True,
       'Hub speed while locating the tag; stock 160.'),
    _k('rfid_measure_speed', 12, 120, 1, 255, 'int', True,
       'Hub speed while measuring the spool; stock 120.'),
    _k('rfid_reposition_speed', 13, 100, 1, 255, 'int', True,
       'Hub speed while repositioning for a read; stock 100.'),
    _k('rfid_seek_move_ms', 14, 800, 100, 5000, 'int', True,
       'Tag locate move, ms; stock 800.'),
    _k('rfid_seek_settle_ms', 15, 200, 10, 2000, 'int', True,
       'Pause after a locate move, ms; stock 200.'),
    _k('rfid_post_auth_wait_ms', 16, 1000, 100, 5000, 'int', True,
       'Reverse between read attempts, ms; stock 1000.'),
    _k('rfid_pre_measure_settle_ms', 17, 400, 1, 2000, 'int', True,
       'Pause before reading the tag, ms; stock 400.'),
    _k('rfid_neighbor_detect_delay_ms', 18, 400, 50, 2000, 'int', True,
       'Neighbour spool move, ms; stock 400.'),
    _k('rfid_neighbor_settle_ms', 19, 100, 10, 1000, 'int', True,
       'Pause after the neighbour move, ms; stock 100.'),
    _k('feed_total_timeout_ms', 20, 25000, 5000, 60000, 'int', True,
       'Stage-4 feed time limit, ms; stock 25000.'),
    _k('feed_recovery_timeout_ms', 21, 3000, 500, 10000, 'int', True,
       'Stage-4 recovery time limit, ms; stock 3000.'),
    _k('odometer_stall_timeout_ms', 22, 500, 100, 2000, 'int', True,
       'Odometer stall limit, ms; stock 500.'),
    _k('rfid_neighbor_retry_count', 23, 3, 1, 10, 'int', True,
       'Tag locate attempts; stock 3.'),
    _k('reverse_detooth_timeout_ms', 24, 1000, 100, 10000, 'int', True,
       'Reverse de-tooth delay, ms; stock 1000.'),
    _k('buffer_fill_timeout_ms', 25, 30000, 1000, 60000, 'int', False,
       'Read-only: stock code never executed.'),
    _k('transition_detect_timeout_ms', 26, 300, 50, 5000, 'int', False,
       'Read-only: stock code never executed.'),
    _k('load_retry_count', 27, 3, 1, 10, 'int', True,
       'Pretension in/out cycles; stock 3.'),
)
K2OH_BY_NAME = {p.name: p for p in K2OH_PARAMETERS}
K2OH_BY_ID = {p.param_id: p for p in K2OH_PARAMETERS}
# Earlier option names in 20 ms ticks, still accepted (converted to ms).
K2OH_LEGACY_TICKS = {
    'feed_total_timeout_20ms_ticks': 'feed_total_timeout_ms',
    'odometer_stall_20ms_ticks': 'odometer_stall_timeout_ms',
}


def parse_identity(payload):
    """Decode a 32-byte 0x7A reply, or None."""
    payload = bytes(payload)
    if len(payload) != 32:
        return None

    def text(raw):
        return raw.rstrip(b"\0").decode("ascii", "replace")

    return {
        "magic": text(payload[0:4]),
        "format": payload[4],
        "variant": payload[5],
        "bench_level": payload[6],
        "flags": payload[7],
        "revision": text(payload[8:20]),
        "version": text(payload[20:32]),
    }


def is_k2oh(identity):
    return bool(identity) and identity.get("magic") == K2OH_MAGIC \
        and identity.get("format") == K2OH_FORMAT


class CfsRuntimeConfigError(RuntimeError):
    pass


class CfsRuntimeBusy(CfsRuntimeConfigError):
    pass


class CfsRuntimeUnsupported(CfsRuntimeConfigError):
    pass


class CfsRuntimeDriver:
    def __init__(self, serial, address):
        self.serial = serial
        self.address = address

    def identity(self, timeout=0.5):
        """The 0x7A build identity, or None (no answer, refused, malformed)."""
        body = build_485_body(
            self.address, CMD_BUILD_INFO, b"", header_byte=0xFF)
        frame = self.serial.cmd_send_data_with_response(body, timeout)
        if not frame:
            return None
        try:
            reply = box_protocol.decode_reply(
                frame, self.address, CMD_BUILD_INFO, context="build_info")
        except box_protocol.ProtocolError:
            return None
        if reply.status != STATUS_OK:
            return None
        return parse_identity(reply.payload)

    def _exchange(self, payload, timeout=1.0):
        body = build_485_body(
            self.address, CMD_RFID_DIAG, bytes(payload), header_byte=0xFF)
        frame = self.serial.cmd_send_data_with_response(body, timeout)
        if not frame:
            raise CfsRuntimeConfigError("CFS runtime-config request timed out")
        return box_protocol.decode_reply(
            frame, self.address, CMD_RFID_DIAG, context="cfs_runtime_config")

    @staticmethod
    def _check_status(reply, what, unsupported=False):
        if reply.status == STATUS_STOCK_BUSY:
            if reply.payload:
                raise box_protocol.ProtocolError(
                    "%s STOCK_BUSY response unexpectedly carries payload" % what)
            raise CfsRuntimeBusy(
                "%s rejected while stock CFS task is busy" % what)
        if unsupported and reply.status == STATUS_BAD_REQUEST:
            if reply.payload:
                raise box_protocol.ProtocolError(
                    "%s unsupported response unexpectedly carries payload" % what)
            raise CfsRuntimeUnsupported("%s is not supported" % what)
        if reply.status != STATUS_OK:
            if reply.payload:
                raise box_protocol.ProtocolError(
                    "%s error response unexpectedly carries payload" % what)
            raise CfsRuntimeConfigError(
                "%s returned status %d" % (what, reply.status))

    # ---- API v1 -----------------------------------------------------

    def info_v1(self, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_INFO,), timeout)
        self._check_status(reply, "CONFIG_INFO")
        if len(reply.payload) != 8:
            raise box_protocol.ProtocolError(
                "CONFIG_INFO response must carry 8 bytes")
        version, count = reply.payload[:2]
        if version != CONFIG_VERSION or count != CONFIG_COUNT:
            raise CfsRuntimeConfigError(
                "unsupported CFS runtime-config layout version=%d count=%d"
                % (version, count))
        return tuple(reply.payload[2:8])

    def set_v1(self, param_id, value, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_SET, param_id, value), timeout)
        self._check_status(reply, "CONFIG_SET")
        if len(reply.payload) != 2:
            raise box_protocol.ProtocolError(
                "CONFIG_SET response must carry id,value")
        if tuple(reply.payload) != (param_id, value):
            raise box_protocol.ProtocolError(
                "CONFIG_SET acknowledgement does not match request")

    def reset_v1(self, param_id=0xFF, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_RESET, param_id), timeout)
        self._check_status(reply, "CONFIG_RESET")
        if reply.payload:
            raise box_protocol.ProtocolError(
                "CONFIG_RESET success response must be empty")

    # Backward-compatible method names used by older tests/tools.
    info = info_v1
    set = set_v1
    reset = reset_v1

    # ---- API v2 -----------------------------------------------------

    def info_v2(self, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_V2_INFO,), timeout)
        self._check_status(reply, "CONFIG_V2_INFO", unsupported=True)
        if len(reply.payload) != 4:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_INFO response must carry 4 bytes")
        version, count, desc_size, features = reply.payload
        override_mask = 0  # v3.14 firmware does not expose an override mask
        if version != CONFIG_V2_VERSION:
            raise CfsRuntimeConfigError(
                "unsupported CONFIG v2 version %d" % version)
        if desc_size != CONFIG_V2_DESCRIPTOR_SIZE:
            raise CfsRuntimeConfigError(
                "unsupported CONFIG v2 catalog revision %d" % desc_size)
        return {
            "version": version,
            "count": count,
            "descriptor_size": desc_size,
            "features": features,
            "override_mask": override_mask,
        }

    def get_v2(self, param_id, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_V2_GET, param_id), timeout)
        self._check_status(reply, "CONFIG_V2_GET")
        if len(reply.payload) != 3 or reply.payload[0] != param_id:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_GET response must carry matching id,value")
        return struct.unpack_from("<H", reply.payload, 1)[0]

    def set_v2(self, param_id, value, timeout=1.0):
        payload = bytes((SUB_CONFIG_V2_SET, param_id)) + struct.pack("<H", value)
        reply = self._exchange(payload, timeout)
        self._check_status(reply, "CONFIG_V2_SET")
        if len(reply.payload) != 3 or reply.payload[0] != param_id:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_SET response must carry matching id,value")
        applied = struct.unpack_from("<H", reply.payload, 1)[0]
        if applied != value:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_SET acknowledgement value does not match request")
        return applied

    def reset_v2(self, param_id=0xFF, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_V2_RESET, param_id), timeout)
        self._check_status(reply, "CONFIG_V2_RESET")
        if reply.payload:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_RESET success response must be empty")
        return None

    def describe_v2(self, param_id, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_V2_DESCRIBE, param_id), timeout)
        self._check_status(reply, "CONFIG_V2_DESCRIBE")
        if len(reply.payload) != 8:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_DESCRIBE response must carry 8 bytes")
        default, minval, maxval = struct.unpack_from("<HHH", reply.payload, 2)
        return {
            "id": param_id,
            "type": reply.payload[0],
            "flags": reply.payload[1],
            "kind": None,
            "default": default,
            "min": minval,
            "max": maxval,
        }

    def probe(self, timeout=1.0):
        try:
            return self.info_v2(timeout)
        except CfsRuntimeUnsupported:
            values = self.info_v1(timeout)
            return {
                "version": 1,
                "count": CONFIG_COUNT,
                "descriptor_size": 0,
                "features": 0,
                "override_mask": 0,
                "values": values,
            }


# G-code commands and the unit method each one runs.
RUNTIME_COMMANDS = (
    ("BOX_CFS_CONFIG_INFO", "cmd_info"),
    ("BOX_CFS_CONFIG_DIAG", "cmd_diag"),
    ("BOX_CFS_CONFIG_SNAPSHOT", "cmd_snapshot"),
)
# No console write command: values come only from the config, and are
# written only at the first contact (Klipper start, or the CFS coming back
# while the printer is not printing). cmd_set/cmd_reset/cmd_apply are kept
# for the v3.19-v3.22 bench tests but are not registered.


class BoxCfsRuntime:
    AUTO_RETRY_LIMIT = 20
    AUTO_RETRY_INTERVAL = 0.5

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.serial_name = config.get("serial", "serial485")
        # address: N limits the runtime config to that CFS. Without it every
        # CFS that [box] brings online is handled, each with its own state.
        self.fixed_address = config.getint("address", None, minval=1, maxval=4)
        self.address = self.fixed_address or 1
        self.auto_apply = config.getboolean("auto_apply", True)

        # Only options that actually exist in the config are sent to the CFS.
        # A commented/omitted option therefore leaves the Creality stock value
        # untouched.
        # The values follow the v3.23 K2-OpenHost catalogue (times in ms):
        # only that firmware ever receives them (see _apply_block_reason).
        self.overrides = {}
        self.ignored_options = []
        for legacy, name in sorted(K2OH_LEGACY_TICKS.items()):
            if config.get(legacy, None) is None:
                continue
            if config.get(name, None) is not None:
                raise config.error(
                    "box_cfs_runtime: set %s or %s, not both" % (name, legacy))
            spec = K2OH_BY_NAME[name]
            ticks = config.getint(legacy, minval=1)
            value = ticks * 20
            if not spec.minval <= value <= spec.maxval:
                raise config.error(
                    "box_cfs_runtime: %s=%d (%d ms) outside %d..%d ms"
                    % (legacy, ticks, value, spec.minval, spec.maxval))
            logging.info("box_cfs_runtime: %s is now %s (ms): %d ticks = %d ms",
                         legacy, name, ticks, value)
            self.overrides[spec.param_id] = value
        for spec in K2OH_PARAMETERS:
            if config.get(spec.name, None) is None:
                continue
            if not spec.flags & FLAG_K2OH_WRITABLE:
                config.get(spec.name)  # accepted, so not an unknown option
                self.ignored_options.append(spec.name)
                logging.warning(
                    "box_cfs_runtime: %s is read-only on the CFS; ignored",
                    spec.name)
                continue
            if spec.value_type == "bool":
                value = 1 if config.getboolean(spec.name) else 0
            else:
                value = config.getint(
                    spec.name, minval=spec.minval, maxval=spec.maxval)
            self.overrides[spec.param_id] = value
        self.identity = None
        self.apply_state = None

        self.serial = None
        self.supported = None
        self.protocol_version = None
        self.param_count = None
        self.override_mask = 0
        self.firmware_features = 0
        self.last_values = {}
        self.last_error = None
        self._auto_attempt = 0
        self._probe_generation = 0
        self._box_present = False
        # One unit per CFS address; this object is the unit of self.address.
        self.units = {self.address: self}

        self.printer.register_event_handler("klippy:connect", self._connect)
        self.printer.register_event_handler(
            "serial_485:ready", self._serial_ready)
        self.printer.register_event_handler("box:ready", self._box_ready)
        for command, method in RUNTIME_COMMANDS:
            self.gcode.register_command(
                command,
                lambda gcmd, method=method: self._dispatch(gcmd, method),
                desc=self.cmd_RUNTIME_help)

    cmd_RUNTIME_help = (
        "CFS runtime configuration (read-only); ADDRESS=<1-4> selects the "
        "CFS (default: the configured one, or the first online)")

    def _unit(self, address):
        """The runtime unit of one CFS address, created on first use."""
        unit = self.units.get(address)
        if unit is None:
            unit = BoxCfsRuntime.__new__(BoxCfsRuntime)
            unit.printer = self.printer
            unit.reactor = self.reactor
            unit.gcode = self.gcode
            unit.serial_name = self.serial_name
            unit.fixed_address = self.fixed_address
            unit.address = address
            unit.auto_apply = self.auto_apply
            unit.overrides = self.overrides
            unit.ignored_options = self.ignored_options
            unit.identity = None
            unit.apply_state = None
            unit.serial = self.serial
            unit.supported = None
            unit.protocol_version = None
            unit.param_count = None
            unit.override_mask = 0
            unit.firmware_features = 0
            unit.last_values = {}
            unit.last_error = None
            unit._auto_attempt = 0
            unit._probe_generation = 0
            unit._box_present = self._box_present
            unit.units = self.units
            self.units[address] = unit
        return unit

    def _online(self):
        """Sorted CFS addresses [box] has online; None without [box]."""
        box = self.printer.lookup_object("box", None)
        if box is None:
            return None
        return sorted(getattr(box, "drivers", None) or {})

    def _default_address(self):
        if self.fixed_address is not None:
            return self.fixed_address
        online = self._online()
        return online[0] if online else self.address

    def _dispatch(self, gcmd, method):
        address = gcmd.get_int(
            "ADDRESS", self._default_address(), minval=1, maxval=4)
        if self.fixed_address is not None and address != self.fixed_address:
            raise gcmd.error(
                "box_cfs_runtime is limited to CFS %d (address option)"
                % self.fixed_address)
        online = self._online()
        if online is not None and address not in online:
            raise gcmd.error("CFS %d is not online" % address)
        if online is not None and len(online) > 1:
            gcmd.respond_info("CFS %d:" % address)
        getattr(self._unit(address), method)(gcmd)

    def _connect(self):
        # A CFS fresh from power-on answers at 0xFE until [box] has
        # enumerated it (A1/A0). With [box], the first contact waits for
        # box:ready, sent after every enumeration and rediscovery.
        self._box_present = self.printer.lookup_object("box", None) is not None

    def _serial_ready(self, *args):
        self.serial = self.printer.lookup_object(
            "serial_485 %s" % self.serial_name)
        self.protocol_version = None
        if not self._box_present:
            self._start_contact(self.reactor.monotonic() + 1.0)

    def _box_ready(self, *args):
        online = self._online() or []
        if self.fixed_address is not None:
            if self.fixed_address not in online:
                logging.info(
                    "box_cfs_runtime: CFS address %d is not online after "
                    "enumeration; not probed", self.fixed_address)
                return
            targets = [self.fixed_address]
        else:
            targets = online
        now = self.reactor.monotonic()
        for address in targets:
            self._unit(address)._start_contact(now)

    def _start_contact(self, waketime):
        # A new contact supersedes retries still pending from an earlier one.
        self._probe_generation += 1
        self._auto_attempt = 0
        self.supported = None
        self.last_error = None
        self._schedule(self._first_contact, waketime)

    def _schedule(self, callback, waketime):
        generation = self._probe_generation

        def run(eventtime):
            if generation == self._probe_generation:
                callback(eventtime)

        self.reactor.register_callback(run, waketime)

    def _first_contact(self, eventtime):
        self._probe_status(eventtime)

    def _probe_status(self, eventtime):
        # Read the API version, the feature byte and the 0x7A identity; then
        # apply the configured values if, and only if, this is K2-OpenHost
        # firmware and the printer is not printing.
        self._auto_attempt += 1
        try:
            driver = self._driver()
            meta = self._probe(driver)
        except CfsRuntimeConfigError as exc:
            self.last_error = str(exc)
            if ("timed out" in str(exc).lower()
                    and self._auto_attempt < 3):
                self._schedule(
                    self._probe_status, eventtime + self.AUTO_RETRY_INTERVAL)
                return
            self.supported = False
            logging.warning("box_cfs_runtime: %s", exc)
            return
        except Exception as exc:
            self.last_error = str(exc)
            self.supported = False
            logging.exception("box_cfs_runtime: probe failed")
            return
        try:
            self.identity = driver.identity()
        except Exception as exc:
            self.identity = None
            logging.info("box_cfs_runtime: no 0x7A identity: %s", exc)
        self._auto_attempt = 0
        self._apply_at_contact(driver, meta, eventtime)

    def _apply_block_reason(self, meta):
        if not self.auto_apply:
            return "auto_apply is off"
        if not self.overrides:
            return "no values configured"
        if not is_k2oh(self.identity):
            return ("not K2-OpenHost firmware (0x7A identity %s)"
                    % ((self.identity or {}).get("magic") or "none"))
        if meta.get("version") != 2 or meta.get("features") != K2OH_FEATURES:
            return ("K2-OpenHost firmware without the v3.23 contract "
                    "(features 0x%02X)" % meta.get("features", 0))
        stats = self.printer.lookup_object("print_stats", None)
        if stats is not None:
            state = stats.get_status(self.reactor.monotonic()).get("state")
            if state in ("printing", "paused"):
                return "printer is %s" % state
        return None

    def _apply_at_contact(self, driver, meta, eventtime):
        reason = self._apply_block_reason(meta)
        if reason is not None:
            self.apply_state = "skipped: " + reason
            logging.info("box_cfs_runtime: CFS %d: values not applied: %s",
                         self.address, reason)
            return
        try:
            applied = {}
            for param_id, value in sorted(self.overrides.items()):
                driver.set_v2(param_id, value)
                readback = driver.get_v2(param_id)
                if readback != value:
                    raise CfsRuntimeConfigError(
                        "%s readback %d, expected %d"
                        % (K2OH_BY_ID[param_id].name, readback, value))
                applied[K2OH_BY_ID[param_id].name] = readback
        except CfsRuntimeBusy as exc:
            self.apply_state = "waiting: " + str(exc)
            self._auto_attempt += 1
            if self._auto_attempt < self.AUTO_RETRY_LIMIT:
                self._schedule(
                    lambda e: self._apply_at_contact(driver, meta, e),
                    eventtime + self.AUTO_RETRY_INTERVAL)
            else:
                self.apply_state = "failed: CFS stayed busy"
                logging.warning("box_cfs_runtime: CFS %d stayed busy; "
                                "values not applied", self.address)
            return
        except Exception as exc:
            self.apply_state = "failed: %s" % exc
            self.last_error = str(exc)
            logging.warning("box_cfs_runtime: CFS %d: apply failed: %s",
                            self.address, exc)
            return
        self.last_values.update(applied)
        self.apply_state = "applied %d value(s)" % len(applied)
        logging.info("box_cfs_runtime: CFS %d: applied %s",
                     self.address, applied)

    def _transport(self):
        if self.serial is None:
            self.serial = self.printer.lookup_object(
                "serial_485 %s" % self.serial_name, None)
        if self.serial is None:
            raise CfsRuntimeConfigError("RS-485 transport is not ready")
        return self.serial

    def _driver(self):
        return CfsRuntimeDriver(self._transport(), self.address)

    def _record_meta(self, meta):
        self.supported = True
        self.protocol_version = meta["version"]
        self.param_count = meta["count"]
        self.override_mask = meta.get("override_mask", 0)
        self.firmware_features = meta.get("features", 0)
        self.last_error = None

    def _probe(self, driver):
        meta = driver.probe()
        self._record_meta(meta)
        return meta

    @staticmethod
    def _validate_descriptor(spec, desc):
        # The v3.14 firmware uses flags 0x01 (normal), 0x02
        # (stock-captured), 0x06 (RFID), 0x0A (safety).
        # These are not a writable-bit mask; firmware enforces idle
        # and value bounds when SET is requested.
        if not (desc["min"] <= spec.minval <= spec.maxval <= desc["max"]):
            raise CfsRuntimeConfigError(
                "%s host range does not fit firmware descriptor" % spec.name)

    def _apply_configured(self):
        driver = self._driver()
        meta = self._probe(driver)

        if meta["version"] == 1:
            unsupported = [
                PARAM_BY_ID[param_id].name
                for param_id in self.overrides if param_id >= CONFIG_COUNT
            ]
            if unsupported:
                raise CfsRuntimeUnsupported(
                    "CFS firmware v1 does not support configured parameters: %s"
                    % ", ".join(sorted(unsupported)))
            values = list(meta["values"])
            for param_id, value in sorted(self.overrides.items()):
                if values[param_id] != value:
                    driver.set_v1(param_id, value)
            values = list(driver.info_v1())
            self.last_values = {
                PARAM_BY_ID[i].name: values[i] for i in range(CONFIG_COUNT)
            }
            self.override_mask = 0
            return meta, self.last_values

        if meta["count"] < len(PARAMETERS):
            raise CfsRuntimeConfigError(
                "CFS CONFIG v2 exposes %d parameters, host expects at least %d"
                % (meta["count"], len(PARAMETERS)))

        # v3.19/v3.20/v3.21 features 0xF7/0xB7 are manual bench ONLY; skip all configured auto-apply,
        # including the six stock speeds from box.cfg.
        if meta.get("features", 0) in (0xF7, 0xB7, 0x97, 0xD7):
            raise CfsRuntimeUnsupported(
                "Experimental CFS v3.19-v3.22: automatic SET disabled; supervised hooks only")

        # Firmware API v2 advertises SET as bit 1. A read-only diagnostic
        # firmware must never receive automatic runtime write attempts.
        if self.overrides and not (meta.get("features", 0) & 0x02):
            raise CfsRuntimeUnsupported(
                "CFS firmware advertises READ-ONLY v2: skipping automatic SET")

        observed = {}
        for param_id, value in sorted(self.overrides.items()):
            spec = PARAM_BY_ID[param_id]
            desc = driver.describe_v2(param_id)
            self._validate_descriptor(spec, desc)
            current = driver.get_v2(param_id)
            if current != value:
                driver.set_v2(param_id, value)
                current = driver.get_v2(param_id)
            observed[spec.name] = current

        meta = driver.info_v2()
        self._record_meta(meta)
        self.last_values.update(observed)
        return meta, observed

    def _read_all(self):
        driver = self._driver()
        meta = self._probe(driver)
        if meta["version"] == 1:
            values = {
                PARAM_BY_ID[i].name: meta["values"][i]
                for i in range(CONFIG_COUNT)
            }
        else:
            count = min(meta["count"], len(PARAMETERS))
            values = {
                PARAM_BY_ID[i].name: driver.get_v2(i)
                for i in range(count)
            }
            meta = driver.info_v2()
            self._record_meta(meta)
        self.last_values = dict(values)
        return meta, values

    def _auto_apply(self, eventtime):
        self._auto_attempt += 1
        try:
            meta, values = self._apply_configured()
            logging.info(
                "box_cfs_runtime: applied volatile CFS config address=%d "
                "api=v%d values=%s",
                self.address, meta["version"], values)
        except CfsRuntimeUnsupported as exc:
            if "Experimental CFS v3.19-v3.22" in str(exc):
                # The v3.19/v3.20/v3.21 bench images deliberately refuse auto-apply.
                # It can still answer diagnostic GET and controlled manual SET.
                self.supported = True
                self.last_error = None
                logging.info("box_cfs_runtime: v3.19/v3.20/v3.21 manual bench; auto-apply skipped")
                return
            self.last_error = str(exc)
            self.supported = False
            logging.warning("box_cfs_runtime: %s", exc)
        except CfsRuntimeBusy as exc:
            self.last_error = str(exc)
            if self._auto_attempt < self.AUTO_RETRY_LIMIT:
                self._schedule(
                    self._auto_apply,
                    eventtime + self.AUTO_RETRY_INTERVAL)
            else:
                logging.warning(
                    "box_cfs_runtime: CFS stayed busy; runtime overrides not applied")
        except CfsRuntimeConfigError as exc:
            self.last_error = str(exc)
            if ("timed out" in str(exc).lower()
                    and self._auto_attempt < 3):
                self._schedule(
                    self._auto_apply,
                    eventtime + self.AUTO_RETRY_INTERVAL)
                return
            self.supported = False
            logging.warning("box_cfs_runtime: %s", exc)
        except Exception as exc:
            self.last_error = str(exc)
            logging.exception("box_cfs_runtime: automatic apply failed")

    @staticmethod
    def _require_effective_hook(meta, param_id):
        """Fail closed on ROM defaults / fake tunables in experimental images.

        Do NOT trust CONFIG_V2_SET success as evidence that any stock motor
        or RFID callsite is actually wired to a setting.
        """
        feature = meta.get("features", 0)
        if meta.get("version") != 2 or feature not in EXPERIMENTAL_EFFECTIVE_HOOKS:
            return
        if param_id not in EXPERIMENTAL_EFFECTIVE_HOOKS[feature]:
            hooks = sorted(EXPERIMENTAL_EFFECTIVE_HOOKS[feature])
            raise CfsRuntimeUnsupported(
                "Experimental CFS 0x%02X: ID %d is NOT connected to a "
                "stock motion/RFID callsite; SET blocked (effective IDs: %s)"
                % (feature, param_id,
                   ",".join(map(str, hooks)) if hooks else "none"))

    @staticmethod
    def _format_value(spec, value, override=False):
        suffix = "*" if override else ""
        if spec.value_type == "bool":
            value_text = "true" if value else "false"
        else:
            value_text = str(value)
        if spec.default is None:
            return "%s=%s%s(stock=runtime)" % (
                spec.name, value_text, suffix)
        return "%s=%s%s(stock=%s)" % (
            spec.name, value_text, suffix, spec.default)

    def _format_groups(self, meta, values):
        lines = [
            "CFS runtime config API v%d count=%d override_mask=0x%08X"
            % (meta["version"], meta["count"], meta.get("override_mask", 0))
        ]
        groups = (
            ("Motion", range(0, 6)),
            ("Advanced motion and safety", (6, 7, 8, 9, 10, 20, 21, 22, 24, 25, 26, 27)),
            ("RFID-sensitive", (11, 12, 13, 14, 15, 16, 17, 18, 19, 23)),
        )
        for title, ids in groups:
            parts = []
            for param_id in ids:
                spec = PARAM_BY_ID[param_id]
                if spec.name not in values:
                    continue
                overridden = bool(
                    meta.get("override_mask", 0) & (1 << param_id))
                parts.append(self._format_value(
                    spec, values[spec.name], overridden))
            if parts:
                lines.append("%s: %s" % (title, " ".join(parts)))
        feature = meta.get("features", 0)
        if meta.get("version") == 2 and feature in EXPERIMENTAL_EFFECTIVE_HOOKS:
            hooks = sorted(EXPERIMENTAL_EFFECTIVE_HOOKS[feature])
            lines.append(
                "EXPERIMENTAL 0x%02X: effective stock hooks=%s; IDs 0..6 "
                "READ-ONLY; IDs 9..27 STORED-ONLY bench values (not tuning). "
                "GET alone does not prove hardware effect; override_mask=0 "
                "is a HOST PLACEHOLDER."
                % (feature, ",".join(map(str, hooks)) if hooks else "none"))
        return "\n".join(lines)

    def cmd_info(self, gcmd):
        try:
            meta, values = self._read_all()
        except Exception as exc:
            raise gcmd.error(str(exc))
        ident = self.identity
        if ident:
            gcmd.respond_info(
                "CFS identity: %s format %d variant %d revision %s version %s"
                % (ident["magic"], ident["format"], ident["variant"],
                   ident["revision"], ident["version"]))
        else:
            gcmd.respond_info("CFS identity: none (no 0x7A answer)")
        gcmd.respond_info("Runtime values: %s" % (self.apply_state or "-"))
        gcmd.respond_info(self._format_groups(meta, values))

    def cmd_diag(self, gcmd):
        """Firmware identification, read-only GET and optional DESCRIBE.

        GET-only diagnostic firmware advertises no descriptor feature bit;
        in that case local host catalogue values are explicitly labelled.
        No SET, RESET, APPLY or motion commands are executed here.
        """
        try:
            drv = self._driver()
            meta = drv.probe()
            features = meta.get("features", 0)
            has_desc = bool(features & 0x08)
            lines = ["CFS_DIAG api=%d count=%d features=0x%02X source=%s"
                     % (meta["version"], meta["count"], features,
                        "MCU_DESCRIBE" if has_desc else "HOST_EXPECTED")]
            if meta["version"] == 1:
                lines.append("Stock v1 speeds: %s" % (list(meta["values"]),))
            else:
                all_ids = gcmd.get_int("ALL", 0, minval=0, maxval=1)
                ids = range(min(meta["count"], len(PARAMETERS))) if all_ids else (0, 6, 7, 8, 11, 20, 27)
                for param_id in ids:
                    value = drv.get_v2(param_id)
                    if has_desc:
                        desc = drv.describe_v2(param_id)
                        default, low, high = desc["default"], desc["min"], desc["max"]
                        typ = str(desc["type"])
                    else:
                        spec = PARAM_BY_ID[param_id]
                        default, low, high = spec.default, spec.minval, spec.maxval
                        typ = "host:%s" % spec.value_type
                    ok = low <= value <= high
                    lines.append("ID=%d GET=%d EXPECT=%s RANGE=%d..%d "
                                 "TYPE=%s STATUS=%s"
                                 % (param_id, value, default, low, high,
                                    typ, "OK" if ok else "INVALID"))
        except Exception as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info("\n".join(lines))

    def cmd_snapshot(self, gcmd):
        """Read-only, fixed-address MCU diagnostic snapshot for v3.22.

        Firmware GET 28..31 samples two opaque internal state structures
        (0x2000057E and 0x20003904). The bytes do NOT attest motor idle.
        No generic RAM fetch, SET/RESET, or user-controlled address exists.
        """
        try:
            driver = self._driver()
            meta = self._probe(driver)
            if (meta.get("version") != 2 or meta.get("count") != 28
                    or meta.get("features") != 0xD7):
                raise CfsRuntimeUnsupported(
                    "CFS diagnostic snapshot requires experimental "
                    "firmware signature 0xD7")
            box = self.printer.lookup_object("box", None)
            if box is None:
                raise CfsRuntimeBusy("CFS box unavailable for snapshot")
            box.acquire_rfid_read("BOX_CFS_DIAG_SNAPSHOT")
            try:
                # Only the four hard-coded IDs are ever transmitted. The
                # firmware restricts these IDs to read-only GET requests.
                words = [driver.get_v2(idx, timeout=1.5)
                         for idx in range(28, 32)]
            finally:
                box.release_rfid_read()
            gcmd.respond_info(
                "CFS V3.22 DIAG ONLY (not proof of idle): "
                "motor_candidate=%04X:%04X task_candidate=%04X:%04X"
                % tuple(words))
        except Exception as exc:
            raise gcmd.error(str(exc))

    def cmd_apply(self, gcmd):
        try:
            # v3.19/v3.20/v3.21 never accept bulk APPLY; use only explicit manual SET.
            driver = self._driver()
            metadata = self._probe(driver)
            if metadata.get("features", 0) in (0xF7, 0xB7, 0x97, 0xD7):
                raise CfsRuntimeUnsupported(
                    "v3.19-v3.22: BOX_CFS_CONFIG_APPLY disabled; supervised hooks only")
            meta, _values = self._apply_configured()
            meta, values = self._read_all()
        except Exception as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info(
            "CFS runtime config applied\n" + self._format_groups(meta, values))

    @staticmethod
    def _parse_gcode_value(gcmd, spec):
        if spec.value_type == "bool":
            raw = gcmd.get("VALUE", "").strip().lower()
            if raw in ("1", "true", "on", "yes"):
                return 1
            if raw in ("0", "false", "off", "no"):
                return 0
            raise gcmd.error(
                "VALUE for %s must be true/false or 1/0" % spec.name)
        return gcmd.get_int(
            "VALUE", minval=spec.minval, maxval=spec.maxval)

    def _v321_check_host_idle(self, expected_owner=None):
        """Fail-closed secondary host preflight for v3.21 manual writes.

        The MCU independently checks the LIVE Creality CMD_BOX_STATE source
        (0x200037D2 == IDLE) on each SET/RESET, avoiding any serial-cache race.
        This host guard is defense in depth, NOT a substitute for the MCU.
        """
        box = self.printer.lookup_object("box", None)
        sensor = self.printer.lookup_object(
            "filament_switch_sensor filament_sensor", None)
        print_stats = self.printer.lookup_object("print_stats", None)
        if box is None or sensor is None or print_stats is None:
            raise CfsRuntimeBusy("v3.21 write: box/sensor/print_stats unavailable")
        now = self.reactor.monotonic()
        data = box.get_status(now)
        filament = sensor.get_status(now)
        printing = print_stats.get_status(now)
        if (data.get("state") != "IDLE"
                or data.get("state_code") != 0
                or data.get("loaded_slot") != -1
                or (data.get("operation") or {}).get("active") is not False
                or filament.get("filament_detected") is not False
                or printing.get("state") != "standby"
                or getattr(box, "rfid_read_owner", None) != expected_owner):
            raise CfsRuntimeBusy(
                "v3.21 manual write requires empty CFS IDLE, clear head "
                "sensor, standby printer and no active RFID read")
        return box

    def _v321_claim_write(self, meta):
        """Return a context manager serializing runtime writes vs RFID reads."""
        from contextlib import contextmanager, nullcontext
        if meta.get("features") not in (0x97, 0xD7):
            return nullcontext()
        @contextmanager
        def claim():
            box = self._v321_check_host_idle()
            box.acquire_rfid_read("BOX_CFS_RUNTIME_WRITE")
            acquired_op_lock = False
            try:
                # Atomically reject a physical operation already in flight,
                # before starting SET/RESET. _operation() rejects a new
                # load/unload while this exclusive write claim is held.
                box.acquire_cfs_runtime_write("BOX_CFS_RUNTIME_WRITE")
                acquired_op_lock = True
                self._v321_check_host_idle(expected_owner="BOX_CFS_RUNTIME_WRITE")
                yield
            finally:
                try:
                    if acquired_op_lock:
                        box.release_cfs_runtime_write("BOX_CFS_RUNTIME_WRITE")
                finally:
                    box.release_rfid_read()
        return claim()

    def cmd_set(self, gcmd):
        name = gcmd.get("PARAM", "").strip().lower()
        spec = PARAM_BY_NAME.get(name)
        if spec is None:
            raise gcmd.error(
                "PARAM must be one of: %s" % ", ".join(sorted(PARAM_BY_NAME)))
        value = self._parse_gcode_value(gcmd, spec)
        try:
            driver = self._driver()
            meta = self._probe(driver)
            if meta["version"] == 1:
                if spec.param_id >= CONFIG_COUNT:
                    raise CfsRuntimeUnsupported(
                        "%s requires CFS Runtime Config API v2" % spec.name)
                driver.set_v1(spec.param_id, value)
                current = driver.info_v1()[spec.param_id]
                self.override_mask = 0
            else:
                self._require_effective_hook(meta, spec.param_id)
                if meta.get("features", 0) in EXPERIMENTAL_EFFECTIVE_HOOKS:
                    if spec.param_id < 7:
                        raise CfsRuntimeUnsupported(
                            "experimental CFS: stock IDs 0..6 are read-only; only connected hooks are writable")
                    desc = {"min": spec.minval, "max": spec.maxval}
                    # Firmware independently range-checks using ROM bounds.
                else:
                    desc = driver.describe_v2(spec.param_id)
                    self._validate_descriptor(spec, desc)
                if not desc["min"] <= value <= desc["max"]:
                    raise CfsRuntimeConfigError(
                        "%s value %d outside firmware range %d..%d"
                        % (spec.name, value, desc["min"], desc["max"]))
                with self._v321_claim_write(meta):
                    driver.set_v2(spec.param_id, value)
                current = driver.get_v2(spec.param_id)
                meta = driver.info_v2()
                self._record_meta(meta)
            self.last_values[spec.name] = current
            self.last_error = None
        except Exception as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info(
            "CFS runtime config: %s"
            % self._format_value(
                spec, current,
                bool(self.override_mask & (1 << spec.param_id))))

    def cmd_reset(self, gcmd):
        name = gcmd.get("PARAM", "ALL").strip().lower()
        if name == "all":
            spec = None
        else:
            spec = PARAM_BY_NAME.get(name)
            if spec is None:
                raise gcmd.error(
                    "PARAM must be ALL or one of: %s"
                    % ", ".join(sorted(PARAM_BY_NAME)))
        try:
            driver = self._driver()
            meta = self._probe(driver)
            if meta["version"] == 1:
                if spec is not None and spec.param_id >= CONFIG_COUNT:
                    raise CfsRuntimeUnsupported(
                        "%s requires CFS Runtime Config API v2" % spec.name)
                driver.reset_v1(0xFF if spec is None else spec.param_id)
                values = driver.info_v1()
                self.last_values = {
                    PARAM_BY_ID[i].name: values[i]
                    for i in range(CONFIG_COUNT)
                }
                self.override_mask = 0
            else:
                if meta.get("features", 0) in (0xF7, 0xB7, 0x97, 0xD7):
                    if spec is not None and spec.param_id < 7:
                        raise CfsRuntimeUnsupported(
                            "v3.19/v3.20/v3.21 reset only support advanced IDs 7..27")
                with self._v321_claim_write(meta):
                    driver.reset_v2(
                        0xFF if spec is None else spec.param_id)
                meta = driver.info_v2()
                self._record_meta(meta)
                if spec is None:
                    self.last_values = {}
                else:
                    self.last_values[spec.name] = driver.get_v2(spec.param_id)
            self.last_error = None
        except Exception as exc:
            raise gcmd.error(str(exc))

        if spec is None:
            gcmd.respond_info(
                "CFS runtime config: all volatile overrides reset")
        else:
            gcmd.respond_info(
                "CFS runtime config reset: %s"
                % self._format_value(
                    spec, self.last_values[spec.name],
                    bool(self.override_mask & (1 << spec.param_id))))

    def get_status(self, eventtime):
        status = self._unit(self._default_address())._unit_status()
        status["boxes"] = {
            str(address): {
                key: value for key, value in unit._unit_status().items()
                if key not in ("auto_apply", "configured_overrides")
            }
            for address, unit in sorted(self.units.items())
            if unit.supported is not None or unit.last_error
        }
        return status

    def _unit_status(self):
        return {
            "supported": self.supported,
            "address": self.address,
            "auto_apply": self.auto_apply,
            "protocol_version": self.protocol_version,
            "parameter_count": self.param_count,
            "override_mask": self.override_mask,
            "firmware_features": self.firmware_features,
            "configured_overrides": {
                K2OH_BY_ID[param_id].name: value
                for param_id, value in sorted(self.overrides.items())
            },
            "ignored_options": list(self.ignored_options),
            "identity": dict(self.identity) if self.identity else None,
            "k2oh": is_k2oh(self.identity),
            "apply_state": self.apply_state,
            "values": dict(self.last_values),
            "last_error": self.last_error,
        }


def load_config(config):
    return BoxCfsRuntime(config)
