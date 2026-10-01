# Copyright (C) 2026 MzTechnology97 and contributors
# Protocol references: grant0013/K2-OpenKlipper, Lamar1007/CFSTool,
# fake-name/cfs-reverse-engineering, and Creality's published K2 CFS firmware.
# This file is distributed under the terms of the GNU GPLv3 license.
"""K2 Pro CFS environment/protocol diagnostics.

The K2 Pro CFS 1.1.3 four-byte command-0x0A response carries temperature and
humidity in its first two bytes.  ``box_k2pro`` adapts that payload into the
canonical Box state before this module sees it.

Command 0x15 is a separate read-only GET_HARDWARE_STATUS request.  Firmware
analysis shows that its 16-byte payload is a hardware self-test/status vector,
not environment telemetry, so it remains exposed only as raw diagnostics.
Command 0x14 identifies the connected CFS firmware and serial.

Unknown layouts are never guessed into user-facing temperature/humidity.
"""

import logging

from extras import box_protocol


CMD_VERSION_SN = 0x14
CMD_HARDWARE_STATUS = 0x15
DEFAULT_POLL = 5.0


def _klog(msg, *args, level=logging.info):
    level("box_environment: " + msg, *args)


def _decode_version(payload):
    """Decode Creality's leading-three-digit app version + serial string."""
    try:
        text = bytes(payload).rstrip(b"\x00").decode("ascii")
    except (UnicodeDecodeError, ValueError):
        return {"text": None, "firmware": None, "serial": None}
    firmware = None
    serial = None
    if len(text) >= 3 and text[:3].isdigit():
        firmware = ".".join(text[:3])
        serial = text[3:] or None
    return {"text": text, "firmware": firmware, "serial": serial}


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
        self.samples = {}
        self.versions = {}
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
            desc="Show K2 Pro CFS environment and protocol diagnostics")

    def _box_ready(self, *args):
        self.ready = True
        self.reactor.update_timer(
            self.timer, self.reactor.monotonic() + 0.25)

    def _disconnect(self, *args):
        self.ready = False
        self.reactor.update_timer(self.timer, self.reactor.NEVER)

    @staticmethod
    def _query(address, driver, command, context, timeout=1.5):
        frame = driver._exchange(command, (), timeout=timeout)
        if not frame:
            return None
        return box_protocol.decode_reply(frame, address, command, context=context)

    def _query_version(self, address, driver):
        reply = self._query(address, driver, CMD_VERSION_SN, "version_sn")
        if reply is None:
            return None
        decoded = _decode_version(reply.payload)
        decoded.update({
            "status": reply.status,
            "payload": reply.payload.hex(),
            "raw": reply.raw.hex(),
        })
        return decoded

    def _query_hardware(self, address, driver):
        return self._query(
            address, driver, CMD_HARDWARE_STATUS, "hardware_status", timeout=0.75)

    def _sample(self, refresh_version=False):
        samples = {}
        for address, driver in sorted(self.box.drivers.items()):
            key = str(address)
            if refresh_version or key not in self.versions:
                version = self._query_version(address, driver)
                if version is not None:
                    self.versions[key] = version

            reply = self._query_hardware(address, driver)
            if reply is None:
                samples[key] = None
                continue
            samples[key] = {
                "status": reply.status,
                "payload": reply.payload.hex(),
                "raw": reply.raw.hex(),
            }

        self.samples = samples
        self.last_error = None

    def _poll(self, eventtime):
        if not self.ready or not self.box.drivers_ready:
            return eventtime + self.poll_interval
        try:
            self._sample()
        except Exception as exc:
            self.last_error = str(exc)
            _klog("CFS diagnostics poll failed: %s", exc, level=logging.warning)
        return eventtime + self.poll_interval

    def _box_get_status(self, eventtime):
        status = dict(self._base_get_status(eventtime))
        status["environment_source"] = (
            "box_state_0x0a"
            if status.get("temp_c") is not None
            and status.get("humidity_pct") is not None
            else None
        )
        status["environment_raw"] = dict(self.samples)
        status["cfs_versions"] = dict(self.versions)
        status["environment_error"] = self.last_error
        return status

    def cmd_env_debug(self, gcmd):
        try:
            self._sample(refresh_version=True)
        except Exception as exc:
            raise gcmd.error("[BOX]: CFS diagnostics failed: %s" % exc)
        status = self._base_get_status(self.reactor.monotonic())
        source = (
            "box_state_0x0a"
            if status.get("temp_c") is not None
            and status.get("humidity_pct") is not None
            else None
        )
        gcmd.respond_info(
            "[BOX]: versions=%s hardware=%s temp=%sC humidity=%s%% source=%s"
            % (self.versions, self.samples, status.get("temp_c"),
               status.get("humidity_pct"), source))


def load_config(config):
    return BoxEnvironment(config)
