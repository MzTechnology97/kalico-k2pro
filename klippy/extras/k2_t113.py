# K2-OpenHost: the printer's T113 seen from Kalico (client of k2oh-ctl)
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Telemetry and controls of the K2 Pro's T113 board from the external host.

The T113 runs k2oh-ctl (K2-OpenHost T113 bootstrap, slot B). This module
talks to it over the LAN; every request runs in a worker thread, so the
reactor never waits for the network.

    [k2_t113]
    host: 192.168.1.60      # T113 address; empty disables the module
    port: 7130
    token_file: ~/printer_data/config/k2oh_t113.token
    poll_interval: 10       # telemetry period in seconds, 0 = off
    estop_on_shutdown: off  # off | m112 | any: cut the MCU rail on shutdown
    auto_power_cycle: False # power-cycle the MCUs after a lost MCU link
                            # when no print was running, then restart
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

ESTOP_CHOICES = {"off": "off", "m112": "m112", "any": "any"}
REQUEST_TIMEOUT = 5.0
CYCLE_TIMEOUT = 30.0
AUTO_CYCLE_DELAY = 3.0
AUTO_CYCLE_MIN_INTERVAL = 600.0
LOST_MCU_TEXT = "Lost communication with MCU"


class T113Error(Exception):
    pass


class T113Client:
    """Blocking HTTP calls to k2oh-ctl; call it from worker threads only."""

    def __init__(self, host, port, token, opener=urllib.request.urlopen):
        self.base = "http://%s:%d" % (host, port)
        self.token = token
        self.opener = opener

    def request(self, method, path, body=None, timeout=REQUEST_TIMEOUT):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-K2OH-Token", self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get("error")
            except Exception:
                detail = None
            raise T113Error(
                "T113 %s %s: HTTP %d %s"
                % (method, path, exc.code, detail or exc.reason)
            )
        except (OSError, ValueError) as exc:
            raise T113Error("T113 %s %s: %s" % (method, path, exc))


class K2T113:
    def __init__(self, config, client_factory=T113Client):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.host = config.get("host", "").strip()
        self.port = config.getint("port", 7130, minval=1, maxval=65535)
        self.poll_interval = config.getfloat(
            "poll_interval", 10.0, minval=0.0, maxval=3600.0
        )
        self.estop_on_shutdown = config.getchoice(
            "estop_on_shutdown", ESTOP_CHOICES, "off"
        )
        self.auto_power_cycle = config.getboolean("auto_power_cycle", False)
        token = config.get("token", "").strip()
        if not token:
            token_file = os.path.expanduser(
                config.get(
                    "token_file", "~/printer_data/config/k2oh_t113.token"
                )
            )
            try:
                with open(token_file) as f:
                    token = f.read().strip()
            except OSError:
                token = ""
        self.enabled = bool(self.host and token)
        self.client = (
            client_factory(self.host, self.port, token)
            if self.enabled
            else None
        )
        self.telemetry = None
        self.telemetry_at = None
        self.last_error = (
            None
            if self.enabled
            else ("disabled: set host and the k2oh-ctl token")
        )
        self.last_action = None
        self._last_idle = False
        self._auto_cycle_at = None
        self._stop = threading.Event()
        self._poll_thread = None

        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler(
            "klippy:shutdown", self._handle_shutdown
        )
        self.printer.register_event_handler(
            "klippy:disconnect", self._handle_disconnect
        )
        for name, func, ready, desc in (
            (
                "T113_STATUS",
                self.cmd_T113_STATUS,
                True,
                "Show the T113 telemetry (last poll)",
            ),
            (
                "T113_BEEP",
                self.cmd_T113_BEEP,
                False,
                "Sound the printer buzzer: T113_BEEP [MS=200] [COUNT=1]",
            ),
            (
                "T113_BRIDGES_RESTART",
                self.cmd_T113_BRIDGES_RESTART,
                True,
                "Restart the T113 USB bridges: CONFIRM=1",
            ),
            (
                "T113_SCREEN_RESTART",
                self.cmd_T113_SCREEN_RESTART,
                True,
                "Restart HelixScreen on the printer",
            ),
            (
                "T113_MCU_POWER_CYCLE",
                self.cmd_T113_MCU_POWER_CYCLE,
                True,
                "Power-cycle the printer MCUs, then FIRMWARE_RESTART: CONFIRM=1",
            ),
        ):
            self.gcode.register_command(
                name, func, when_not_ready=ready, desc=desc
            )

    # --- background work ---------------------------------------------------
    def _spawn(self, target, *args):
        thread = threading.Thread(
            target=target, args=args, daemon=True, name="k2_t113"
        )
        thread.start()
        return thread

    def _from_thread(self, callback):
        """Run callback(eventtime) on the reactor."""
        self.reactor.register_async_callback(callback)

    def _respond(self, msg, error=False):
        if error:
            self._from_thread(lambda e: self.gcode.respond_raw("!! " + msg))
        else:
            self._from_thread(lambda e: self.gcode.respond_info(msg))

    def _handle_connect(self):
        # M300 only when no [gcode_macro M300] already provides it; macros are
        # registered while the config loads, so they are known by now.
        if "M300" not in self.gcode.ready_gcode_handlers:
            self.gcode.register_command(
                "M300",
                self.cmd_M300,
                desc="Beep: M300 [P<ms>] (the buzzer has a fixed tone)",
            )

    def _handle_ready(self):
        if (
            self.enabled
            and self.poll_interval > 0
            and self._poll_thread is None
        ):
            self._poll_thread = self._spawn(self._poll_loop)
        self._idle_timer = self.reactor.register_timer(
            self._sample_idle, self.reactor.NOW
        )

    def _handle_disconnect(self):
        self._stop.set()

    def _poll_loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.poll_interval)

    def refresh(self):
        """Fetch /status (worker thread)."""
        try:
            status = self.client.request("GET", "/status")
        except T113Error as exc:
            self.last_error = str(exc)
            return None
        self.telemetry = status
        self.telemetry_at = time.monotonic()
        self.last_error = None
        return status

    def _sample_idle(self, eventtime):
        # Remember whether a print was running, for auto_power_cycle: at the
        # shutdown event print_stats may already have moved to "error".
        print_stats = self.printer.lookup_object("print_stats", None)
        state = getattr(print_stats, "state", None)
        self._last_idle = state in ("standby", "complete", "cancelled", "error")
        return eventtime + 2.0

    # --- actions (worker threads) ----------------------------------------------
    def _action(
        self,
        label,
        method,
        path,
        body=None,
        timeout=REQUEST_TIMEOUT,
        after=None,
    ):
        def work():
            try:
                result = self.client.request(method, path, body, timeout)
            except T113Error as exc:
                self.last_action = {"action": label, "error": str(exc)}
                self._respond("%s failed: %s" % (label, exc), error=True)
                return
            self.last_action = {"action": label, "result": result}
            self._respond("%s: %s" % (label, result.get("detail") or "done"))
            if after is not None:
                after(result)

        if not self.enabled:
            raise self.gcode.error("k2_t113 is disabled: %s" % self.last_error)
        self._spawn(work)

    def beep(self, ms=200, count=1):
        """Non-blocking beep; safe to call from any extras code."""
        if not self.enabled:
            return False

        def work():
            try:
                self.client.request(
                    "POST", "/beep", {"ms": int(ms), "count": int(count)}
                )
            except T113Error as exc:
                logging.warning("k2_t113: beep failed: %s", exc)

        self._spawn(work)
        return True

    def _request_firmware_restart(self, _result=None):
        # Runs on the worker thread: give the MCUs a moment to boot after the
        # rail is back on, then hand the restart to the reactor.
        time.sleep(1.0)
        self._from_thread(
            lambda e: self.printer.request_exit("firmware_restart")
        )

    def _handle_shutdown(self):
        if not self.enabled:
            return
        message = self.printer.get_state_message()[0]
        if self.estop_on_shutdown == "any" or (
            self.estop_on_shutdown == "m112" and "M112" in message
        ):
            self._spawn(self._estop)
            return
        if (
            self.auto_power_cycle
            and LOST_MCU_TEXT in message
            and self._last_idle
        ):
            now = time.monotonic()
            if (
                self._auto_cycle_at is not None
                and now - self._auto_cycle_at < AUTO_CYCLE_MIN_INTERVAL
            ):
                logging.warning(
                    "k2_t113: auto power cycle skipped (one ran %.0f s ago)",
                    now - self._auto_cycle_at,
                )
                return
            self._auto_cycle_at = now
            self._spawn(self._auto_cycle)

    def _estop(self):
        try:
            self.client.request("POST", "/estop", {})
            logging.error("k2_t113: MCU rail cut after shutdown (estop)")
        except T113Error as exc:
            logging.error("k2_t113: estop request failed: %s", exc)

    def _auto_cycle(self):
        time.sleep(AUTO_CYCLE_DELAY)
        try:
            self.client.request(
                "POST", "/mcu/cycle", {"force": True}, timeout=CYCLE_TIMEOUT
            )
        except T113Error as exc:
            logging.error("k2_t113: automatic MCU power cycle failed: %s", exc)
            return
        logging.warning(
            "k2_t113: MCUs power-cycled after a lost link; restarting"
        )
        self._from_thread(
            lambda e: self.printer.request_exit("firmware_restart")
        )

    # --- status -------------------------------------------------------------------
    def get_status(self, eventtime):
        age = (
            None
            if self.telemetry_at is None
            else round(time.monotonic() - self.telemetry_at, 1)
        )
        return {
            "enabled": self.enabled,
            "host": self.host or None,
            "connected": self.enabled
            and self.last_error is None
            and self.telemetry is not None,
            "age": age,
            "error": self.last_error,
            "telemetry": self.telemetry,
            "last_action": self.last_action,
            "estop_on_shutdown": self.estop_on_shutdown,
            "auto_power_cycle": self.auto_power_cycle,
        }

    # --- G-code ---------------------------------------------------------------------
    def cmd_T113_STATUS(self, gcmd):
        if not self.enabled:
            gcmd.respond_info("T113: %s" % self.last_error)
            return
        t = self.telemetry
        if t is None:
            gcmd.respond_info(
                "T113 %s: no telemetry yet (%s)"
                % (self.host, self.last_error or "first poll pending")
            )
            return
        bridges = ", ".join(
            "%s %s" % (name, "up" if b.get("alive") else "DOWN")
            for name, b in sorted((t.get("bridges") or {}).items())
        )
        gadget = t.get("gadget") or {}
        gcmd.respond_info(
            "T113 %s slot %s: MCU power %s, SoC %s C, uptime %.0f s, UDISK "
            "free %s MB\nUSB gadget %s; bridges: %s%s"
            % (
                self.host,
                t.get("slot"),
                t.get("mcu_power"),
                t.get("soc_temp_c"),
                t.get("uptime_s") or 0,
                t.get("udisk_free_mb"),
                gadget.get("state") or "unbound",
                bridges or "unknown",
                ""
                if self.last_error is None
                else "\nlast poll failed: %s" % self.last_error,
            )
        )

    def cmd_T113_BEEP(self, gcmd):
        ms = gcmd.get_int("MS", 200, minval=20, maxval=3000)
        count = gcmd.get_int("COUNT", 1, minval=1, maxval=5)
        if not self.beep(ms, count):
            raise gcmd.error("k2_t113 is disabled: %s" % self.last_error)

    def cmd_M300(self, gcmd):
        ms = gcmd.get_int("P", 200, minval=20, maxval=3000)
        self.beep(ms, 1)

    @staticmethod
    def _confirm(gcmd, what):
        if gcmd.get_int("CONFIRM", 0) != 1:
            raise gcmd.error("%s interrupts the printer; add CONFIRM=1" % what)

    def cmd_T113_BRIDGES_RESTART(self, gcmd):
        self._confirm(gcmd, "T113_BRIDGES_RESTART")
        self._action(
            "T113 bridges restart",
            "POST",
            "/bridges/restart",
            {"force": self.printer.is_shutdown()},
        )

    def cmd_T113_SCREEN_RESTART(self, gcmd):
        self._action("HelixScreen restart", "POST", "/screen/restart", {})

    def cmd_T113_MCU_POWER_CYCLE(self, gcmd):
        self._confirm(gcmd, "T113_MCU_POWER_CYCLE")
        gcmd.respond_info(
            "Power-cycling the printer MCUs through the T113; "
            "Klipper restarts afterwards"
        )
        self._action(
            "MCU power cycle",
            "POST",
            "/mcu/cycle",
            {"force": self.printer.is_shutdown()},
            timeout=CYCLE_TIMEOUT,
            after=self._request_firmware_restart,
        )


def load_config(config):
    return K2T113(config)
