"""temperature_fan_manual_floor with generic_fan: a Mainsail slider for the floor.

The floor shows as a [fan_generic] of the same name; the temperature_fan
stays the only owner of the fan pin.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import temperature_fan_manual_floor as tfmf  # noqa: E402


class ConfigError(Exception):
    pass


class GCode:
    def __init__(self):
        self.mux = {}

    def register_mux_command(self, cmd, key, value, func, desc=None):
        assert (cmd, value) not in self.mux, (cmd, value)
        self.mux[(cmd, value)] = (key, func)


class Printer:
    def __init__(self):
        self.gcode = GCode()
        self.objects = {"gcode": self.gcode}
        self.handlers = {}

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def add_object(self, name, obj):
        if name in self.objects:
            raise ConfigError("Printer object '%s' already created" % name)
        self.objects[name] = obj

    def register_event_handler(self, event, func):
        self.handlers[event] = func


class Config:
    def __init__(self, printer, **values):
        self.printer = printer
        self.values = values

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "temperature_fan_manual_floor chamber_exhaust_fans"

    def get(self, name, default=None):
        return self.values.get(name, default)

    def getfloat(self, name, default=None, **_kw):
        return float(self.values.get(name, default))

    def getboolean(self, name, default=None):
        return bool(self.values.get(name, default))


class GCmd:
    def __init__(self, **params):
        self.params = params

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_float(self, name, default=None, minval=None, maxval=None):
        value = float(self.params.get(name, default))
        assert minval <= value <= maxval
        return value

    def error(self, msg):
        return ValueError(msg)


def make(**values):
    printer = Printer()
    floor = tfmf.TemperatureFanManualFloor(Config(printer, **values))
    applied = []
    floor._apply_current_speed = lambda: applied.append(floor.manual_speed)
    return printer, floor, applied


def test_no_generic_fan_by_default():
    printer, _floor, _applied = make()
    assert "fan_generic chamber_exhaust_fans" not in printer.objects
    assert ("SET_FAN_SPEED", "chamber_exhaust_fans") not in printer.gcode.mux


def test_generic_fan_slider_sets_the_floor():
    printer, floor, applied = make(generic_fan=True)
    view = printer.objects["fan_generic chamber_exhaust_fans"]
    assert view.get_status(0) == {"speed": 0.0, "rpm": None}
    key, set_fan_speed = printer.gcode.mux[
        ("SET_FAN_SPEED", "chamber_exhaust_fans")
    ]
    assert key == "FAN"
    floor.temperature_fan = object()  # ready: the speed is applied
    set_fan_speed(GCmd(SPEED="0.4"))
    assert floor.manual_speed == 0.4 and applied == [0.4]
    assert view.get_status(0)["speed"] == 0.4


def test_m106_p3_moves_the_slider():
    printer, floor, _applied = make(generic_fan=True)
    _key, manual = printer.gcode.mux[
        ("SET_TEMPERATURE_FAN_MANUAL_SPEED", "chamber_exhaust_fans")
    ]
    manual(GCmd(SPEED="0.7"))
    status = printer.objects["fan_generic chamber_exhaust_fans"].get_status(0)
    assert status["speed"] == 0.7


def test_template_is_refused():
    printer, _floor, _applied = make(generic_fan=True)
    _key, set_fan_speed = printer.gcode.mux[
        ("SET_FAN_SPEED", "chamber_exhaust_fans")
    ]
    with pytest.raises(ValueError, match="SPEED only"):
        set_fan_speed(GCmd(TEMPLATE="x"))


def test_a_real_fan_generic_of_the_same_name_is_a_config_error():
    printer = Printer()
    printer.add_object("fan_generic chamber_exhaust_fans", object())
    with pytest.raises(ConfigError, match="already created"):
        tfmf.TemperatureFanManualFloor(Config(printer, generic_fan=True))
