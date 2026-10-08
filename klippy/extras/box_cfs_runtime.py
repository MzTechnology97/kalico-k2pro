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

All writes are RAM-only. The CFS firmware independently range-checks v2 values
and rejects SET/RESET while the stock RFID task is busy. EEPROM and physical
RFID tags are never written by this extra.
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
CONFIG_V2_DESCRIPTOR_SIZE = 16

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
    _p("feeder_forward_speed", 0, 255, 1, 255, "int", 0,
       "Feeder motor forward speed while pushing filament from the spool toward the CFS hub."),
    _p("hub_forward_speed", 1, 100, 1, 255, "int", 0,
       "Main hub motor forward speed during normal filament advance."),
    _p("hub_transition_speed", 2, 200, 1, 255, "int", 0,
       "Hub speed used by the stock state machine during load/unload transition phases."),
    _p("hub_insert_speed", 3, 155, 1, 255, "int", 0,
       "Hub forward speed during the initial filament insert/feed phase."),
    _p("feeder_reverse_speed", 4, 255, 1, 255, "int", 0,
       "Feeder motor reverse speed while pulling filament back toward the spool."),
    _p("hub_reverse_speed", 5, 80, 1, 255, "int", 0,
       "Main hub reverse speed during normal unload/retraction."),

    _p("reverse_detooth_enable", 6, None, 0, 1, "bool",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Enable or disable the stock reverse-detooth procedure; stock state is captured at runtime."),
    _p("hub_transition_wait_ms", 7, 3200, 10, 10000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Maximum/settling wait used by the stock hub transition routine."),
    _p("hub_pullback_timeout_ms", 8, 5000, 100, 30000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Timeout for the stock hub pull-back operation."),
    _p("buffer_fill_timeout_ms", 9, 30000, 1000, 120000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Timeout while waiting for the stock CFS buffer-fill operation."),

    _p("rfid_seek_speed", 10, 160, 1, 255, "int",
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE,
       "Hub speed used while seeking/positioning a spool near the RFID reader."),
    _p("rfid_measure_speed", 11, 120, 1, 255, "int",
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE,
       "Hub speed used during the RFID/geometry measurement phase."),
    _p("rfid_reposition_speed", 12, 100, 1, 255, "int",
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE,
       "Hub speed used while repositioning the spool during RFID handling."),
    _p("rfid_seek_move_ms", 13, 800, 50, 5000, "int",
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE,
       "Duration of the stock RFID seek movement."),
    _p("rfid_seek_settle_ms", 14, 200, 10, 2000, "int",
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE,
       "Settling delay after an RFID seek movement."),
    _p("rfid_validation_settle_ms", 15, 500, 10, 5000, "int",
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE,
       "Settling delay before/around stock RFID validation."),
    _p("rfid_reposition_initial_wait_ms", 16, 1000, 10, 5000, "int",
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE,
       "Initial wait before the stock RFID reposition loop."),
    _p("rfid_retry_count", 17, 5, 1, 20, "int",
       FLAG_ADVANCED | FLAG_RFID_SENSITIVE,
       "Number of stock RFID validation/reposition retries."),

    _p("load_retry_count", 18, 3, 1, 10, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Number of stock load/transition retry attempts."),
    _p("sensor_detect_timeout_ms", 19, 700, 50, 5000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Sensor-detection timeout used in both load and unload paths."),
    _p("sensor_transition_window_ms", 20, 300, 50, 2000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Sensor transition window used in both load and unload paths."),
    _p("odometer_stall_timeout_ms", 21, 500, 20, 5000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Maximum time with no odometer movement while feeding; effective resolution is 20 ms."),
    _p("feeding_timeout_ms", 22, 25000, 1000, 120000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Maximum total stock feeding time; effective resolution is 20 ms."),
    _p("pretension_insert_timeout_ms", 23, 10000, 1000, 60000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Timeout while pretensioning/inserting filament toward the hub."),
    _p("pretension_pullout_timeout_ms", 24, 10000, 1000, 60000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Timeout while pretensioning/pulling filament out from the hub."),
    _p("feeding_buffer_timeout_ms", 25, 3000, 100, 30000, "int",
       FLAG_ADVANCED | FLAG_SAFETY,
       "Timeout for the stock feeding stage while waiting for the buffer to become full."),
)

PARAM_BY_NAME = {p.name: p for p in PARAMETERS}
PARAM_BY_ID = {p.param_id: p for p in PARAMETERS}


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
        if len(reply.payload) != 8:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_INFO response must carry 8 bytes")
        version, count, desc_size, features = reply.payload[:4]
        override_mask = struct.unpack_from("<I", reply.payload, 4)[0]
        if version != CONFIG_V2_VERSION:
            raise CfsRuntimeConfigError(
                "unsupported CONFIG v2 version %d" % version)
        if desc_size != CONFIG_V2_DESCRIPTOR_SIZE:
            raise CfsRuntimeConfigError(
                "unsupported CONFIG v2 descriptor size %d" % desc_size)
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
        if len(reply.payload) != 8 or reply.payload[0] != param_id:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_GET response must carry matching id,value")
        return struct.unpack_from("<I", reply.payload, 4)[0]

    def set_v2(self, param_id, value, timeout=1.0):
        payload = bytes((SUB_CONFIG_V2_SET, param_id)) + struct.pack("<I", value)
        reply = self._exchange(payload, timeout)
        self._check_status(reply, "CONFIG_V2_SET")
        if len(reply.payload) != 8 or reply.payload[0] != param_id:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_SET response must carry matching id,value")
        applied = struct.unpack_from("<I", reply.payload, 4)[0]
        if applied != value:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_SET acknowledgement value does not match request")
        return applied

    def reset_v2(self, param_id=0xFF, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_V2_RESET, param_id), timeout)
        self._check_status(reply, "CONFIG_V2_RESET")
        if len(reply.payload) != 8 or reply.payload[0] != param_id:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_RESET response must carry matching id,value")
        return struct.unpack_from("<I", reply.payload, 4)[0]

    def describe_v2(self, param_id, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_V2_DESCRIBE, param_id), timeout)
        self._check_status(reply, "CONFIG_V2_DESCRIBE")
        if len(reply.payload) != 16 or reply.payload[0] != param_id:
            raise box_protocol.ProtocolError(
                "CONFIG_V2_DESCRIBE response must carry 16 matching bytes")
        default, minval, maxval = struct.unpack_from("<III", reply.payload, 4)
        return {
            "id": param_id,
            "type": reply.payload[1],
            "flags": reply.payload[2],
            "kind": reply.payload[3],
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


class BoxCfsRuntime:
    AUTO_RETRY_LIMIT = 20
    AUTO_RETRY_INTERVAL = 0.5

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.serial_name = config.get("serial", "serial485")
        self.address = config.getint("address", 1, minval=1, maxval=4)
        self.auto_apply = config.getboolean("auto_apply", True)

        # Only options that actually exist in the config are sent to the CFS.
        # A commented/omitted option therefore leaves the Creality stock value
        # untouched.
        self.overrides = {}
        for spec in PARAMETERS:
            if config.get(spec.name, None) is None:
                continue
            if spec.value_type == "bool":
                value = 1 if config.getboolean(spec.name) else 0
            else:
                value = config.getint(
                    spec.name, minval=spec.minval, maxval=spec.maxval)
            self.overrides[spec.param_id] = value

        self.serial = None
        self.supported = None
        self.protocol_version = None
        self.param_count = None
        self.override_mask = 0
        self.last_values = {}
        self.last_error = None
        self._auto_attempt = 0

        self.printer.register_event_handler(
            "serial_485:ready", self._serial_ready)
        self.gcode.register_command("BOX_CFS_CONFIG_INFO", self.cmd_info)
        self.gcode.register_command("BOX_CFS_CONFIG_APPLY", self.cmd_apply)
        self.gcode.register_command("BOX_CFS_CONFIG_SET", self.cmd_set)
        self.gcode.register_command("BOX_CFS_CONFIG_RESET", self.cmd_reset)

    def _serial_ready(self, *args):
        self.serial = self.printer.lookup_object(
            "serial_485 %s" % self.serial_name)
        self.protocol_version = None
        if self.auto_apply and self.overrides:
            self._auto_attempt = 0
            self.reactor.register_callback(
                self._auto_apply, self.reactor.monotonic() + 1.0)

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
        self.last_error = None

    def _probe(self, driver):
        meta = driver.probe()
        self._record_meta(meta)
        return meta

    @staticmethod
    def _validate_descriptor(spec, desc):
        if not (desc["flags"] & FLAG_WRITABLE):
            raise CfsRuntimeConfigError(
                "%s is not writable in CFS firmware" % spec.name)
        if not desc["min"] <= spec.minval <= spec.maxval <= desc["max"]:
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
        except CfsRuntimeBusy as exc:
            self.last_error = str(exc)
            if self._auto_attempt < self.AUTO_RETRY_LIMIT:
                self.reactor.register_callback(
                    self._auto_apply,
                    eventtime + self.AUTO_RETRY_INTERVAL)
            else:
                logging.warning(
                    "box_cfs_runtime: CFS stayed busy; runtime overrides not applied")
        except CfsRuntimeConfigError as exc:
            self.last_error = str(exc)
            if ("timed out" in str(exc).lower()
                    and self._auto_attempt < 3):
                self.reactor.register_callback(
                    self._auto_apply,
                    eventtime + self.AUTO_RETRY_INTERVAL)
                return
            self.supported = False
            logging.warning("box_cfs_runtime: %s", exc)
        except Exception as exc:
            self.last_error = str(exc)
            logging.exception("box_cfs_runtime: automatic apply failed")

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
            ("Advanced motion", (6, 7, 8, 9, 18, 19, 20, 21, 22, 23, 24, 25)),
            ("RFID-sensitive", range(10, 18)),
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
        return "\n".join(lines)

    def cmd_info(self, gcmd):
        try:
            meta, values = self._read_all()
        except Exception as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info(self._format_groups(meta, values))

    def cmd_apply(self, gcmd):
        try:
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
                desc = driver.describe_v2(spec.param_id)
                self._validate_descriptor(spec, desc)
                if not desc["min"] <= value <= desc["max"]:
                    raise CfsRuntimeConfigError(
                        "%s value %d outside firmware range %d..%d"
                        % (spec.name, value, desc["min"], desc["max"]))
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
        return {
            "supported": self.supported,
            "address": self.address,
            "auto_apply": self.auto_apply,
            "protocol_version": self.protocol_version,
            "parameter_count": self.param_count,
            "override_mask": self.override_mask,
            "configured_overrides": {
                PARAM_BY_ID[param_id].name: value
                for param_id, value in sorted(self.overrides.items())
            },
            "values": dict(self.last_values),
            "last_error": self.last_error,
        }


def load_config(config):
    return BoxCfsRuntime(config)
