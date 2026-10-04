# K2 Pro closed-loop motor board MCU temperature as a temperature sensor
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Standard temperature sensor for the MCU of a K2 Pro motor board.

    [temperature_sensor motor_X_MCU]
    sensor_type: motor_mcu
    motor_axis: x        # x, y or e

The value comes from [motor_control], which already reads each motor MCU
temperature (one axis every 6 s, each axis every 18 s). The sensor does not
talk to the board itself, so declaring it adds no RS-485 traffic. Like any
temperature_sensor it shows up in the UI's sensor list and accepts min_temp
and max_temp (outside them Klipper shuts down, as for temperature_host).
"""

from __future__ import annotations

import logging

from .danger_options import get_danger_options

MOTOR_AXES = {"x": "x", "y": "y", "e": "e"}
# Each axis is read every 3 x 6 s by motor_control.
REPORT_TIME = 18.0


class MotorMcuTemperature:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.axis = config.getchoice("motor_axis", MOTOR_AXES)
        self.temp = 0.0
        self.min_temp = self.max_temp = 0.0
        self._callback = None
        self.attached = False
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )

    def _handle_connect(self):
        motor_control = self.printer.lookup_object("motor_control", None)
        hub = getattr(motor_control, "temp_sensors", None)
        if hub is None or not hasattr(hub, "attach"):
            logging.warning(
                "motor_mcu_temperature %s: no [motor_control] temperature "
                "polling; the sensor stays at 0",
                self.name,
            )
            return
        hub.attach(self.axis, self)
        self.attached = True

    def setup_minmax(self, min_temp, max_temp):
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, cb):
        self._callback = cb

    def get_report_time_delta(self):
        return REPORT_TIME

    def note_sample(self, temp):
        """Called by motor_control on the reactor after a good read."""
        self.temp = float(temp)
        if (
            self.temp < self.min_temp or self.temp > self.max_temp
        ) and not get_danger_options().temp_ignore_limits:
            self.printer.invoke_shutdown(
                "Motor %s MCU temperature %0.1f outside range of %0.1f:%.01f"
                % (self.axis.upper(), self.temp, self.min_temp, self.max_temp)
            )
            return
        if self._callback is not None:
            mcu = self.printer.lookup_object("mcu")
            now = self.reactor.monotonic()
            self._callback(mcu.estimated_print_time(now), self.temp)

    def get_status(self, eventtime):
        return {"temperature": round(self.temp, 2)}


def load_config(config):
    pheaters = config.get_printer().load_object(config, "heaters")
    pheaters.add_sensor_factory("motor_mcu", MotorMcuTemperature)
