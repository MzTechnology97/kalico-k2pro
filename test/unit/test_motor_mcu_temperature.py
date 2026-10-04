"""The motor_mcu standard temperature sensor and its link to motor_control."""

import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import motor_control as mc  # noqa: E402
from extras import motor_mcu_temperature as mmt  # noqa: E402


class Printer:
    def __init__(self, motor_control=None):
        self.handlers = {}
        self.shutdowns = []
        self.factories = {}
        self.motor_control = motor_control
        self.reactor = SimpleNamespace(monotonic=lambda: 50.0)
        self.heaters = SimpleNamespace(
            add_sensor_factory=lambda name, cls: self.factories.update(
                {name: cls}
            )
        )
        self.mcu = SimpleNamespace(estimated_print_time=lambda t: t + 1000)

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, name, func):
        self.handlers[name] = func

    def lookup_object(self, name, default=None):
        return {"motor_control": self.motor_control, "mcu": self.mcu}.get(
            name, default
        )

    def load_object(self, config, name):
        assert name == "heaters"
        return self.heaters

    def invoke_shutdown(self, msg):
        self.shutdowns.append(msg)


class Config:
    def __init__(
        self, printer, name="temperature_sensor motor_X_MCU", **values
    ):
        self.printer = printer
        self.name = name
        self.values = values

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def getchoice(self, name, choices, default=None):
        value = self.values.get(name, default)
        if value not in choices:
            raise ValueError("bad choice %r" % (value,))
        return choices[value]


def hub():
    reactor = SimpleNamespace(
        NEVER=float("inf"),
        register_timer=lambda cb: cb,
        update_timer=lambda *a: None,
        monotonic=lambda: 10.0,
    )
    replacement = SimpleNamespace(
        reactor=reactor,
        is_ready=True,
        motor_params_init=True,
        axes=SimpleNamespace(
            target=lambda axis: SimpleNamespace(
                addr=axis,
                client=SimpleNamespace(get_value=lambda *a, **k: 44.5),
            )
        ),
    )
    return mc.Mot2TempSensorHub(replacement)


def make_sensor(printer, axis="x", lo=-10.0, hi=90.0):
    sensor = mmt.MotorMcuTemperature(Config(printer, motor_axis=axis))
    sensor.setup_minmax(lo, hi)
    readings = []
    sensor.setup_callback(lambda t, temp: readings.append((t, temp)))
    return sensor, readings


def test_factory_is_registered():
    printer = Printer()
    mmt.load_config(Config(printer))
    assert printer.factories == {"motor_mcu": mmt.MotorMcuTemperature}


def test_bad_axis_is_a_config_error():
    with pytest.raises(ValueError):
        mmt.MotorMcuTemperature(Config(Printer(), motor_axis="z"))


def test_sensor_follows_motor_control_reads():
    temps = hub()
    printer = Printer(SimpleNamespace(temp_sensors=temps))
    sensor, readings = make_sensor(printer, "x")
    printer.handlers["klippy:connect"]()
    assert sensor.attached is True
    temps.start()
    temps._poll(0)  # x
    assert readings == [(1050.0, 44.5)]
    assert sensor.get_status(0) == {"temperature": 44.5}
    temps._poll(0)  # y: not this sensor
    assert len(readings) == 1


def test_out_of_range_shuts_down(monkeypatch):
    monkeypatch.setattr(
        mmt,
        "get_danger_options",
        lambda: SimpleNamespace(temp_ignore_limits=False),
    )
    printer = Printer()
    sensor, readings = make_sensor(printer, hi=40.0)
    sensor.note_sample(44.5)
    assert (
        printer.shutdowns and "Motor X MCU temperature" in printer.shutdowns[0]
    )
    assert readings == []


def test_without_motor_control_the_sensor_stays_idle():
    printer = Printer(None)
    sensor, readings = make_sensor(printer)
    printer.handlers["klippy:connect"]()
    assert sensor.attached is False and readings == []
    assert sensor.get_report_time_delta() == 18.0


def test_k2_config_declares_the_three_sensors():
    text = (ROOT / "config/k2/motor_control.cfg").read_text()
    for axis in "XYE":
        assert "[temperature_sensor motor_%s_MCU]" % axis in text
    assert text.count("sensor_type: motor_mcu") == 3
    sensors_cfg = (ROOT / "klippy/extras/temperature_sensors.cfg").read_text()
    assert "[motor_mcu_temperature]" in sensors_cfg
