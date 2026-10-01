# Copyright (C) 2026 MzTechnology97 and contributors
# Protocol references: grant0013/K2-OpenKlipper and Creality K2 research.
# This file is distributed under the terms of the GNU GPLv3 license.
"""K2 Pro CFS environment reader.

K2 Pro firmware does not return the legacy six-byte environment/state payload
from command 0x0A.  On this firmware 0x0A is the measuring query; the stock
wrapper obtains hardware/environment information from command 0x15 instead.

This helper composes the existing ``box`` object.  It performs only the
read-only 0x15 hardware-status query, publishes raw diagnostics for protocol
validation, and overlays temperature/humidity once a supported payload shape
has been identified.
"""

import logging

from extras import box_protocol


CMD_HARDWARE_STATUS = 0x15
DEFAULT_POLL = 5.0


def _klog(msg, *args, level=logging.info):
    level("box_environment: " + msg, *args)


class BoxEnvironment:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.box = self.printer.lookup_object("box", None)
        if self.box is None:
            raise config.error("[box_environment] requires [box] first")

        self.poll_interval = config.getfloat(
            "poll_interval", DEFAULT_POLL, minval=1.0)
        self.temp_c = None
        self.humidity_pct = None
        self.samples = {}
        self.last_error = None
        self.ready = False

        self._base_get_status = self.box.get_status
        self.box.get_status = self._box_get_status

        self.timer = self.reactor.register_timer(self._poll)
        self.printer.register_event_handler("box:ready", self._box_ready)
        self.printer.register_event_handler("klippy:disconnect", self._disconnect)
        self.printer.register_event_handler("klippy:shutdown", self._disconnect)
        self.gcode.register_command(
            "BOX_ENV_DEBUG", self.cmd_env_debug,
            desc="Show raw K2 Pro CFS hardware/environment status")

    def _box_ready(self, *args):
        self.ready = True
        self.reactor.update_timer(
            self.timer, self.reactor.monotonic() + 0.25)

    def _disconnect(self, *args):
        self.ready = False
        self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def _query_driver(self, address, driver):
        # BoxDriver._exchange is the common transport-backed request path used
        # by all typed queries.  0x15 has no command payload on K2 firmware.
        frame = driver._exchange(CMD_HARDWARE_STATUS, (), timeout=0.5)
        if not frame:
            return None
        reply = box_protocol.decode_reply(
            frame, address, CMD_HARDWARE_STATUS,
            context="hardware_status")
        return reply

    @staticmethod
    def _decode_environment(payload):
        """Return (temperature, humidity) for a validated payload or None.

        Payload formats are intentionally conservative.  Unknown frames stay
        visible through ``environment_raw`` and are never guessed into user
        facing temperature/humidity values.
        """
        data = bytes(payload)

        # K2 Pro hardware-status layout will be enabled after live capture has
        # confirmed its offsets.  Keep the reader strict until then.
        return None

    def _sample(self):
        samples = {}
        environment = None
        for address, driver in sorted(self.box.drivers.items()):
            reply = self._query_driver(address, driver)
            if reply is None:
                samples[str(address)] = None
                continue
            samples[str(address)] = {
                "status": reply.status,
                "payload": reply.payload.hex(),
                "raw": reply.raw.hex(),
            }
            decoded = self._decode_environment(reply.payload)
            if decoded is not None and environment is None:
                environment = decoded

        self.samples = samples
        if environment is not None:
            self.temp_c, self.humidity_pct = environment
        self.last_error = None

    def _poll(self, eventtime):
        if not self.ready or not self.box.drivers_ready:
            return eventtime + self.poll_interval
        try:
            self._sample()
        except Exception as exc:
            self.last_error = str(exc)
            _klog("hardware-status poll failed: %s", exc, level=logging.warning)
        return eventtime + self.poll_interval

    def _box_get_status(self, eventtime):
        status = dict(self._base_get_status(eventtime))
        if self.temp_c is not None:
            status["temp_c"] = self.temp_c
        if self.humidity_pct is not None:
            status["humidity_pct"] = self.humidity_pct
        status["environment_raw"] = dict(self.samples)
        status["environment_error"] = self.last_error
        return status

    def cmd_env_debug(self, gcmd):
        try:
            self._sample()
        except Exception as exc:
            raise gcmd.error("[BOX]: environment query failed: %s" % exc)
        gcmd.respond_info(
            "[BOX]: environment raw=%s temp=%sC humidity=%s%%"
            % (self.samples, self.temp_c, self.humidity_pct))


def load_config(config):
    return BoxEnvironment(config)
