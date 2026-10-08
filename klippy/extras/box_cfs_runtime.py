# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Volatile runtime configuration for the patched Creality K2 Pro CFS.

This module is intentionally separate from box.py.  It talks to the CFS
through the existing serial_485 request queue, so it never opens or owns the
RS-485 device directly.

Firmware v3.13 exposes a small API7 extension on opcode 0x57:
  0x0D CONFIG_INFO
  0x0E CONFIG_SET
  0x0F CONFIG_RESET

All writes are RAM-only.  The CFS firmware rejects SET/RESET while its stock
RFID task is busy.  EEPROM and physical RFID tags are never written here.
"""

import logging

from extras import box_protocol
from extras.serial_485 import build_485_body


CMD_RFID_DIAG = 0x57
SUB_CONFIG_INFO = 0x0D
SUB_CONFIG_SET = 0x0E
SUB_CONFIG_RESET = 0x0F

STATUS_OK = 0
STATUS_BAD_REQUEST = 1
STATUS_STOCK_BUSY = 7

CONFIG_VERSION = 1
CONFIG_COUNT = 6

# (cfg name, firmware id, stock raw default)
PARAMETERS = (
    ("feeder_forward_speed", 0, 255),
    ("hub_forward_speed", 1, 100),
    ("hub_transition_speed", 2, 200),
    ("hub_insert_speed", 3, 155),
    ("feeder_reverse_speed", 4, 255),
    ("hub_reverse_speed", 5, 80),
)
PARAM_BY_NAME = {name: (param_id, default)
                 for name, param_id, default in PARAMETERS}
PARAM_BY_ID = {param_id: (name, default)
               for name, param_id, default in PARAMETERS}


class CfsRuntimeConfigError(RuntimeError):
    pass


class CfsRuntimeBusy(CfsRuntimeConfigError):
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
    def _check_status(reply, what):
        if reply.status == STATUS_STOCK_BUSY:
            if reply.payload:
                raise box_protocol.ProtocolError(
                    "%s STOCK_BUSY response unexpectedly carries payload" % what)
            raise CfsRuntimeBusy("%s rejected while stock CFS task is busy" % what)
        if reply.status != STATUS_OK:
            if reply.payload:
                raise box_protocol.ProtocolError(
                    "%s error response unexpectedly carries payload" % what)
            raise CfsRuntimeConfigError(
                "%s returned status %d" % (what, reply.status))

    def info(self, timeout=1.0):
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

    def set(self, param_id, value, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_SET, param_id, value), timeout)
        self._check_status(reply, "CONFIG_SET")
        if len(reply.payload) != 2:
            raise box_protocol.ProtocolError(
                "CONFIG_SET response must carry id,value")
        if tuple(reply.payload) != (param_id, value):
            raise box_protocol.ProtocolError(
                "CONFIG_SET acknowledgement does not match request")

    def reset(self, param_id=0xFF, timeout=1.0):
        reply = self._exchange((SUB_CONFIG_RESET, param_id), timeout)
        self._check_status(reply, "CONFIG_RESET")
        if reply.payload:
            raise box_protocol.ProtocolError(
                "CONFIG_RESET success response must be empty")


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

        self.overrides = {}
        for name, param_id, _default in PARAMETERS:
            value = config.getint(name, None, minval=1, maxval=255)
            if value is not None:
                self.overrides[param_id] = value

        self.serial = None
        self.supported = None
        self.last_values = None
        self.last_error = None
        self._auto_attempt = 0

        self.printer.register_event_handler("serial_485:ready", self._serial_ready)
        self.gcode.register_command("BOX_CFS_CONFIG_INFO", self.cmd_info)
        self.gcode.register_command("BOX_CFS_CONFIG_APPLY", self.cmd_apply)
        self.gcode.register_command("BOX_CFS_CONFIG_SET", self.cmd_set)
        self.gcode.register_command("BOX_CFS_CONFIG_RESET", self.cmd_reset)

    def _serial_ready(self, *args):
        self.serial = self.printer.lookup_object(
            "serial_485 %s" % self.serial_name)
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

    def _read_info(self):
        values = self._driver().info()
        self.supported = True
        self.last_values = values
        self.last_error = None
        return values

    def _apply_configured(self):
        driver = self._driver()
        values = driver.info()
        self.supported = True
        for param_id, value in sorted(self.overrides.items()):
            if values[param_id] != value:
                driver.set(param_id, value)
        values = driver.info()
        self.last_values = values
        self.last_error = None
        return values

    def _auto_apply(self, eventtime):
        self._auto_attempt += 1
        try:
            values = self._apply_configured()
            logging.info(
                "box_cfs_runtime: applied volatile CFS config address=%d values=%s",
                self.address, values)
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
            logging.warning(
                "box_cfs_runtime: firmware does not expose runtime config: %s",
                exc)
        except Exception as exc:
            self.last_error = str(exc)
            logging.exception("box_cfs_runtime: automatic apply failed")

    @staticmethod
    def _format(values):
        parts = []
        for name, param_id, default in PARAMETERS:
            parts.append("%s=%d(stock=%d)" %
                         (name, values[param_id], default))
        return " ".join(parts)

    def cmd_info(self, gcmd):
        try:
            values = self._read_info()
        except Exception as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info("CFS runtime config: " + self._format(values))

    def cmd_apply(self, gcmd):
        try:
            values = self._apply_configured()
        except Exception as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info("CFS runtime config applied: " + self._format(values))

    def cmd_set(self, gcmd):
        name = gcmd.get("PARAM", "").strip().lower()
        if name not in PARAM_BY_NAME:
            raise gcmd.error(
                "PARAM must be one of: %s" % ", ".join(sorted(PARAM_BY_NAME)))
        value = gcmd.get_int("VALUE", minval=1, maxval=255)
        param_id, _default = PARAM_BY_NAME[name]
        try:
            driver = self._driver()
            driver.set(param_id, value)
            values = driver.info()
            self.supported = True
            self.last_values = values
            self.last_error = None
        except Exception as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info("CFS runtime config: " + self._format(values))

    def cmd_reset(self, gcmd):
        name = gcmd.get("PARAM", "ALL").strip().lower()
        if name == "all":
            param_id = 0xFF
        elif name in PARAM_BY_NAME:
            param_id = PARAM_BY_NAME[name][0]
        else:
            raise gcmd.error(
                "PARAM must be ALL or one of: %s"
                % ", ".join(sorted(PARAM_BY_NAME)))
        try:
            driver = self._driver()
            driver.reset(param_id)
            values = driver.info()
            self.supported = True
            self.last_values = values
            self.last_error = None
        except Exception as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info("CFS runtime config reset: " + self._format(values))

    def get_status(self, eventtime):
        return {
            "supported": self.supported,
            "address": self.address,
            "auto_apply": self.auto_apply,
            "configured_overrides": {
                PARAM_BY_ID[param_id][0]: value
                for param_id, value in sorted(self.overrides.items())
            },
            "values": (
                None if self.last_values is None else {
                    PARAM_BY_ID[param_id][0]: self.last_values[param_id]
                    for param_id in range(CONFIG_COUNT)
                }
            ),
            "last_error": self.last_error,
        }


def load_config(config):
    return BoxCfsRuntime(config)
