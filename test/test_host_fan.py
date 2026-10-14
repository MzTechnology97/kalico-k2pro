import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import host_fan  # noqa: E402


class ConfigError(Exception):
    pass


class CommandError(Exception):
    pass


class FakeReactor:
    NOW = 0.0
    NEVER = 9999999999999999.0

    def __init__(self):
        self.timers = []

    def register_timer(self, callback, waketime):
        self.timers.append([callback, waketime])
        return len(self.timers) - 1

    def update_timer(self, timer, waketime):
        self.timers[timer][1] = waketime


class FakeGcmd:
    def __init__(self, **params):
        self.params = {k.upper(): str(v) for k, v in params.items()}
        self.replies = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_float(
        self, name, default=None, minval=None, maxval=None, above=None
    ):
        if name not in self.params:
            return default
        value = float(self.params[name])
        if minval is not None and value < minval:
            raise CommandError("%s below minimum" % name)
        if maxval is not None and value > maxval:
            raise CommandError("%s above maximum" % name)
        if above is not None and value <= above:
            raise CommandError("%s not above" % name)
        return value

    def respond_info(self, msg):
        self.replies.append(msg)

    def error(self, msg):
        return CommandError(msg)


class FakeGcode:
    def __init__(self):
        self.mux = {}
        self.messages = []

    def register_mux_command(self, cmd, key, value, func, desc=None):
        values = self.mux.setdefault(cmd, {})
        if value in values:
            raise ConfigError("mux %s %s already registered" % (cmd, value))
        values[value] = func

    def run(self, cmd, **params):
        gcmd = FakeGcmd(**params)
        key = (
            "TEMPERATURE_FAN" if cmd == "SET_TEMPERATURE_FAN_TARGET" else "FAN"
        )
        self.mux[cmd][gcmd.get(key)](gcmd)
        return gcmd

    def respond_info(self, msg):
        self.messages.append(msg)

    def respond_raw(self, msg):
        self.messages.append(msg)


class FakeHeaters:
    def __init__(self):
        self.available_sensors = []


class FakeConfigfile:
    def __init__(self):
        self.status_settings = {}


class FakePrinter:
    config_error = ConfigError

    def __init__(self):
        self.reactor = FakeReactor()
        self.objects = {
            "gcode": FakeGcode(),
            "heaters": FakeHeaters(),
            "configfile": FakeConfigfile(),
        }
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name):
        return self.objects[name]

    def load_object(self, config, name):
        return self.objects[name]

    def add_object(self, name, obj):
        assert name not in self.objects
        self.objects[name] = obj

    def register_event_handler(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def send_event(self, event):
        for callback in self.handlers.get(event, []):
            callback()


class FakeConfig:
    error = ConfigError

    def __init__(self, printer, name, options):
        self.printer = printer
        self.name = name
        self.options = options

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def get(self, option, default=None):
        return self.options.get(option, default)

    def getfloat(
        self, option, default=None, minval=None, maxval=None, above=None
    ):
        if option not in self.options:
            return default
        value = float(self.options[option])
        if minval is not None and value < minval:
            raise ConfigError(option)
        if maxval is not None and value > maxval:
            raise ConfigError(option)
        if above is not None and value <= above:
            raise ConfigError(option)
        return value

    def getchoice(self, option, choices, default=None):
        value = self.options.get(option, default)
        if value not in choices:
            raise ConfigError(option)
        return choices[value]


class Sysfs:
    """A CM5-like /sys: hwmon pwmfan and the cpu-thermal zone."""

    def __init__(self, root, temp=54.0, pwm=75, rpm=2600, mode="enabled"):
        self.root = root
        self.hwmon = root / "class" / "hwmon" / "hwmon2"
        self.zone = root / "class" / "thermal" / "thermal_zone0"
        other = root / "class" / "hwmon" / "hwmon0"
        for path in (self.hwmon, self.zone, other):
            path.mkdir(parents=True)
        (other / "name").write_text("cpu_thermal\n")
        (self.hwmon / "name").write_text("pwmfan\n")
        (self.zone / "type").write_text("cpu-thermal\n")
        self.set(temp=temp, pwm=pwm, rpm=rpm, mode=mode)

    def set(self, temp=None, pwm=None, rpm=None, mode=None):
        if temp is not None:
            (self.zone / "temp").write_text("%d\n" % int(temp * 1000))
        if pwm is not None:
            (self.hwmon / "pwm1").write_text("%d\n" % pwm)
        if rpm is not None:
            (self.hwmon / "fan1_input").write_text("%d\n" % rpm)
        if mode is not None:
            (self.zone / "mode").write_text(mode + "\n")

    @property
    def pwm(self):
        return int((self.hwmon / "pwm1").read_text())

    @property
    def mode(self):
        return (self.zone / "mode").read_text().strip()


def make(tmp_path, options=None, **state):
    sysfs = Sysfs(tmp_path / "sys", **state)
    printer = FakePrinter()
    opts = {"sysfs_root": str(sysfs.root), "kick_start_time": "0"}
    opts.update(options or {})
    fan = host_fan.HostFan(FakeConfig(printer, "host_fan cm5", opts))
    printer.send_event("klippy:connect")
    printer.send_event("klippy:ready")
    return fan, sysfs, printer


def test_target_curve():
    assert host_fan.target_speed(50, 55, 15, 0.3, 1.0) == 0.3
    assert host_fan.target_speed(70, 55, 15, 0.3, 1.0) == 1.0
    assert host_fan.target_speed(62.5, 55, 15, 0.3, 1.0) == pytest.approx(0.65)


def test_published_as_temperature_fan(tmp_path):
    fan, sysfs, printer = make(tmp_path)
    assert printer.objects["temperature_fan cm5"] is fan
    assert "temperature_fan cm5" in printer.objects["heaters"].available_sensors
    settings = printer.objects["configfile"].status_settings[
        "temperature_fan cm5"
    ]
    assert settings["max_temp"] == 100.0 and settings["min_temp"] == 0.0
    status = fan.get_status(0.0)
    assert status["temperature"] == 54.0 and status["rpm"] == 2600
    assert status["target"] == 55.0 and status["control"] == "target"


def test_takes_control_and_follows_the_curve(tmp_path):
    fan, sysfs, printer = make(tmp_path, temp=62.5)
    fan._update(1.0)
    assert sysfs.mode == "disabled"
    assert sysfs.pwm == round(0.65 * 255)
    sysfs.set(temp=80.0)
    fan._update(3.0)
    assert sysfs.pwm == 255
    assert fan.get_status(3.0)["full_speed"]


def test_deadband_avoids_small_writes(tmp_path):
    fan, sysfs, printer = make(tmp_path, temp=62.5)
    fan._update(1.0)
    first = sysfs.pwm
    sysfs.set(temp=62.6)
    fan._update(3.0)
    assert sysfs.pwm == first


def test_kick_start_from_stop(tmp_path):
    fan, sysfs, printer = make(
        tmp_path, {"kick_start_time": "0.5"}, temp=50.0, pwm=0
    )
    next_time = fan._update(1.0)
    assert sysfs.pwm == 255 and next_time == 1.5
    fan._update(1.5)
    assert sysfs.pwm == round(0.3 * 255)


def test_disconnect_and_shutdown_hand_back_to_kernel(tmp_path):
    fan, sysfs, printer = make(tmp_path, temp=60.0)
    fan._update(1.0)
    assert sysfs.mode == "disabled"
    printer.send_event("klippy:disconnect")
    assert sysfs.mode == "enabled"

    fan, sysfs, printer = make(tmp_path / "b", temp=60.0)
    fan._update(1.0)
    printer.send_event("klippy:shutdown")
    assert sysfs.mode == "enabled"
    assert fan._update(3.0) == FakeReactor.NEVER


def test_shutdown_speed_keeps_control(tmp_path):
    fan, sysfs, printer = make(tmp_path, {"shutdown_speed": "1.0"}, temp=50.0)
    fan._update(1.0)
    printer.send_event("klippy:shutdown")
    assert sysfs.mode == "disabled" and sysfs.pwm == 255


def test_manual_and_kernel_modes(tmp_path):
    fan, sysfs, printer = make(tmp_path, temp=50.0)
    gcode = printer.objects["gcode"]
    gcmd = gcode.run("SET_HOST_FAN", SPEED="0.8")
    fan._update(1.0)
    assert fan.mode == "manual" and sysfs.pwm == round(0.8 * 255)
    assert "manual 80%" in gcmd.replies[-1]
    # Without FAN= the first host fan answers; with FAN=cm5 too.
    gcode.run("SET_HOST_FAN", FAN="cm5", MODE="kernel")
    assert fan.mode == "kernel" and sysfs.mode == "enabled"
    fan._update(3.0)
    assert sysfs.mode == "enabled"
    gcode.run("SET_TEMPERATURE_FAN_TARGET", TEMPERATURE_FAN="cm5", TARGET="60")
    assert fan.mode == "target" and fan.target == 60.0
    with pytest.raises(CommandError):
        gcode.run("SET_HOST_FAN", MODE="off")


def test_manual_zero_still_full_speed_when_hot(tmp_path):
    fan, sysfs, printer = make(tmp_path, temp=50.0)
    printer.objects["gcode"].run("SET_HOST_FAN", SPEED="0")
    fan._update(1.0)
    assert sysfs.pwm == 0
    sysfs.set(temp=76.0)
    fan._update(3.0)
    assert sysfs.pwm == 255


def test_stall_warning(tmp_path):
    fan, sysfs, printer = make(tmp_path, {"stall_time": "4"}, temp=60.0, rpm=0)
    fan._update(1.0)
    fan._update(3.0)
    assert not fan.stalled
    fan._update(5.0)
    assert fan.stalled
    assert any("no tachometer" in m for m in printer.objects["gcode"].messages)
    sysfs.set(rpm=2000)
    fan._update(7.0)
    assert not fan.stalled


def test_read_only_sysfs_monitors_only(tmp_path):
    sysfs = Sysfs(tmp_path / "sys", temp=60.0)
    os.chmod(sysfs.hwmon / "pwm1", 0o444)
    try:
        if os.access(sysfs.hwmon / "pwm1", os.W_OK):
            pytest.skip("running as root: file modes are not enforced")
        printer = FakePrinter()
        fan = host_fan.HostFan(
            FakeConfig(printer, "host_fan cm5", {"sysfs_root": str(sysfs.root)})
        )
        printer.send_event("klippy:connect")
        printer.send_event("klippy:ready")
        assert fan.mode == "kernel" and not fan.writable
        fan._update(1.0)
        assert sysfs.mode == "enabled" and sysfs.pwm == 75
        with pytest.raises(CommandError):
            printer.objects["gcode"].run("SET_HOST_FAN", SPEED="1")
        assert any(
            "monitoring only" in m for m in printer.objects["gcode"].messages
        )
    finally:
        os.chmod(sysfs.hwmon / "pwm1", 0o644)


def test_missing_fan_is_not_fatal(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    printer = FakePrinter()
    fan = host_fan.HostFan(
        FakeConfig(printer, "host_fan cm5", {"sysfs_root": str(root)})
    )
    assert not fan.available and fan.mode == "kernel"
    assert fan.get_status(0.0)["available"] is False


def test_second_fan_needs_its_name(tmp_path):
    fan, sysfs, printer = make(tmp_path)
    other = host_fan.HostFan(
        FakeConfig(printer, "host_fan other", {"sysfs_root": str(sysfs.root)})
    )
    mux = printer.objects["gcode"].mux["SET_HOST_FAN"]
    assert mux[None] == fan.cmd_SET_HOST_FAN
    assert mux["other"] == other.cmd_SET_HOST_FAN
