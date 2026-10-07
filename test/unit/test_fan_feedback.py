"""fan_feedback on native tachometer_pin fans (Jacob10383 k2-plus 071c813).

The K2 Pro uses the K2 Plus tachometer pins; these tests cover the policy:
configuration checks, the tachometer requirement and stall handling.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import fan_feedback as ff  # noqa: E402


class ConfigError(Exception):
    pass


class Fan:
    def __init__(self, rpm=0.0, speed=1.0):
        self.rpm = rpm
        self.speed = speed

    def get_status(self, eventtime):
        return {"rpm": self.rpm, "speed": self.speed}


class Reactor:
    NEVER = float("inf")

    def monotonic(self):
        return 100.0

    def register_timer(self, callback, waketime):
        return callback

    def register_async_callback(self, callback):
        callback(0)


class Printer:
    def __init__(self, fans, state="printing"):
        self.fans = fans
        self.state = state
        self.reactor = Reactor()
        self.messages = []
        self.scripts = []
        self.shutdowns = []
        self.pauses = []
        self.handlers = {}
        printer = self
        self.gcode = type(
            "GCode",
            (),
            {
                "register_command": lambda *a, **k: None,
                "respond_raw": lambda _s, msg: printer.messages.append(msg),
                "run_script_from_command": lambda _s, s: printer.scripts.append(
                    s
                ),
                "run_script": lambda _s, s: printer.scripts.append(s),
            },
        )()

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name == "gcode":
            return self.gcode
        if name == "webhooks":
            return type("W", (), {"register_endpoint": lambda *a: None})()
        return self.fans.get(name, default)

    def load_object(self, config, name):
        printer = self
        if name == "print_stats":
            return type(
                "PS", (), {"get_status": lambda _s, e: {"state": printer.state}}
            )()
        return type(
            "PR",
            (),
            {
                "pause_command_sent": False,
                "send_pause_command": lambda _s: printer.pauses.append(True),
            },
        )()

    def register_event_handler(self, event, handler):
        self.handlers[event] = handler

    def config_error(self, msg):
        return ConfigError(msg)

    def is_shutdown(self):
        return bool(self.shutdowns)

    def invoke_shutdown(self, msg):
        self.shutdowns.append(msg)


class Config:
    def __init__(self, printer, **values):
        self.printer = printer
        self.values = values

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.values.get(name, default)

    def getfloat(self, name, default=None, **_kw):
        return float(self.values.get(name, default))

    def error(self, msg):
        return ConfigError(msg)


FANS = ("heater_fan chamber_heater_fan", "heater_fan heatbreak_fan", "fan")


def make(state="printing", rpm=1500.0, **values):
    fans = {name: Fan(rpm) for name in FANS}
    printer = Printer(fans, state)
    feedback = ff.FanFeedback(Config(printer, **values))
    printer.handlers["klippy:ready"]()
    return printer, feedback, fans


def run(feedback, seconds):
    for _ in range(int(seconds / feedback.poll_interval)):
        feedback._poll(100.0)


def test_defaults_match_the_k2_fans():
    _printer, feedback, _fans = make()
    assert feedback.fan_objects == FANS
    assert feedback.shutdown_fans == {"heater_fan chamber_heater_fan"}
    assert feedback.pause_fans == {"heater_fan heatbreak_fan", "fan"}


def test_policy_must_name_known_fans():
    printer = Printer({})
    with pytest.raises(ConfigError, match="unknown fan_objects"):
        ff.FanFeedback(Config(printer, pause_fans="fan, ghost"))


def test_names_must_match_the_fans():
    printer = Printer({})
    with pytest.raises(ConfigError, match="fan_names count"):
        ff.FanFeedback(Config(printer, fan_names="one, two"))


def test_a_fan_without_tachometer_is_a_config_error():
    fans = {name: Fan() for name in FANS}
    fans["fan"].rpm = None
    printer = Printer(fans)
    feedback = ff.FanFeedback(Config(printer))
    with pytest.raises(ConfigError, match="fan needs tachometer_pin"):
        printer.handlers["klippy:ready"]()
    del feedback


def test_part_fan_stall_pauses_after_the_confirm_time():
    printer, feedback, fans = make()
    fans["fan"].rpm = 0.0
    run(feedback, feedback.confirm_secs - 1)
    assert printer.pauses == []
    run(feedback, 2)
    assert printer.pauses == [True]
    assert printer.scripts == ["PAUSE"]


def test_stall_only_counts_while_the_fan_is_commanded_on():
    printer, feedback, fans = make()
    fans["fan"].rpm = 0.0
    fans["fan"].speed = 0.0
    run(feedback, feedback.confirm_secs * 2)
    assert printer.pauses == [] and printer.messages == []


def test_stall_while_idle_only_warns():
    printer, feedback, fans = make(state="standby")
    fans["heater_fan heatbreak_fan"].rpm = 0.0
    run(feedback, feedback.confirm_secs + 1)
    assert printer.pauses == []
    assert any("STALL on heatbreak fan" in m for m in printer.messages)


def test_chamber_heater_fan_stall_shuts_down():
    printer, feedback, fans = make()
    fans["heater_fan chamber_heater_fan"].rpm = 0.0
    run(feedback, feedback.confirm_secs + 1)
    assert "M141 S0" in printer.scripts
    assert printer.shutdowns


def test_recovery_resets_the_stall_count():
    printer, feedback, fans = make()
    fans["fan"].rpm = 0.0
    run(feedback, feedback.confirm_secs - 1)
    fans["fan"].rpm = 1200.0
    run(feedback, 1)
    fans["fan"].rpm = 0.0
    run(feedback, feedback.confirm_secs - 1)
    assert printer.pauses == []
