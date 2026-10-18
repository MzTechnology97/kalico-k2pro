"""K2-OpenHost firmware identity (0x7A) and when runtime values are written.

Values reach a CFS only when it answers 0x7A with "K2OH" (format 1) and
reports the v3.23 feature byte, and only at the first contact (Klipper start,
or the CFS coming back) while the printer is not printing. The console has no
write command. Mocks only: nothing talks to a device.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_cfs_runtime as runtime
from extras.serial_485 import crc8

K2OH = {"magic": "K2OH", "format": 1, "variant": 4, "bench_level": 0,
        "flags": 2, "revision": "v3.23-k2oh", "version": "cfs0_000_153"}
OCFS = dict(K2OH, magic="OCFS", variant=2)


class Reactor:
    def __init__(self):
        self.now = 10.0
        self.callbacks = []

    def monotonic(self):
        return self.now

    def register_callback(self, callback, waketime=None):
        self.callbacks.append((self.now if waketime is None else waketime, callback))

    def run(self):
        while self.callbacks:
            self.callbacks.sort(key=lambda item: item[0])
            waketime, callback = self.callbacks.pop(0)
            self.now = max(self.now, waketime)
            callback(self.now)


class Gcode:
    def __init__(self):
        self.commands = {}

    def register_command(self, name, func, desc=None):
        self.commands[name] = func


class Box:
    def __init__(self, online=(1,)):
        self.drivers = {address: object() for address in online}


class Stats:
    def __init__(self, state="standby"):
        self.state = state

    def get_status(self, eventtime):
        return {"state": self.state}


class Printer:
    def __init__(self, state="standby"):
        self.reactor = Reactor()
        self.objects = {"gcode": Gcode(), "serial_485 serial485": object(),
                        "box": Box(), "print_stats": Stats(state)}
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def send_event(self, event):
        for callback in self.handlers.get(event, []):
            callback()


class ConfigError(Exception):
    pass


class Config:
    error = ConfigError

    def __init__(self, printer, options):
        self.printer = printer
        self.options = options

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getint(self, name, default=None, minval=None, maxval=None):
        value = self.options.get(name, default)
        if value is None:
            return None
        value = int(value)
        if (minval is not None and value < minval) or \
                (maxval is not None and value > maxval):
            raise ConfigError("%s out of range" % name)
        return value

    def getboolean(self, name, default=None):
        value = self.options.get(name, default)
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)


class Driver:
    def __init__(self, identity=K2OH, features=runtime.K2OH_FEATURES):
        self.identity_reply = identity
        self.features = features
        self.writes = []
        self.values = {}
        self.busy = 0
        self.readback_offset = 0

    def probe(self, timeout=1.0):
        return {"version": 2, "count": 28, "descriptor_size": 1,
                "features": self.features, "override_mask": 0}

    def identity(self, timeout=0.5):
        return self.identity_reply

    def set_v2(self, param_id, value, timeout=1.0):
        if self.busy:
            self.busy -= 1
            raise runtime.CfsRuntimeBusy("busy")
        self.writes.append((param_id, value))
        self.values[param_id] = value

    def get_v2(self, param_id, timeout=1.0):
        return self.values.get(param_id, 0) + self.readback_offset


def make(options=None, state="standby", **driver_args):
    printer = Printer(state)
    rt = runtime.BoxCfsRuntime(Config(printer, dict(options or {})))
    driver = Driver(**driver_args)
    rt._driver = lambda: driver
    printer.send_event("klippy:connect")
    printer.send_event("serial_485:ready")
    return rt, printer, driver


def contact(printer):
    printer.send_event("box:ready")
    printer.reactor.run()


OPTIONS = {"hub_insert_speed": "100", "insert_sensor_timeout_ms": "8000",
           "load_retry_count": "2"}


def test_identity_frame_is_decoded():
    payload = (b"K2OH" + bytes((1, 4, 0, 2)) + b"v3.23-k2oh\0\0"
               + b"cfs0_000_153")
    body = bytes((1, len(payload) + 3, 0, runtime.CMD_BUILD_INFO)) + payload
    frame = bytes((0xF7,)) + body + bytes((crc8(body[1:]),))

    class Serial:
        def cmd_send_data_with_response(self, body, timeout):
            assert body[3] == runtime.CMD_BUILD_INFO and body[2] == 0xFF
            return frame

    ident = runtime.CfsRuntimeDriver(Serial(), 1).identity()
    assert ident == K2OH
    assert runtime.is_k2oh(ident)
    assert not runtime.is_k2oh(OCFS) and not runtime.is_k2oh(None)


def test_no_answer_means_no_identity():
    class Serial:
        def cmd_send_data_with_response(self, body, timeout):
            return None

    assert runtime.CfsRuntimeDriver(Serial(), 1).identity() is None


def test_k2oh_firmware_gets_the_values_in_ms():
    rt, printer, driver = make(OPTIONS)
    contact(printer)
    assert driver.writes == [(3, 100), (8, 8000), (27, 2)]
    status = rt.get_status(0.0)
    assert status["k2oh"] is True and status["identity"]["revision"] == "v3.23-k2oh"
    assert status["apply_state"] == "applied 3 value(s)"
    assert status["configured_overrides"]["insert_sensor_timeout_ms"] == 8000


@pytest.mark.parametrize("driver_args, reason", [
    ({"identity": None}, "not K2-OpenHost firmware"),
    ({"identity": OCFS}, "not K2-OpenHost firmware (0x7A identity OCFS)"),
    ({"features": 0xD7}, "without the v3.23 contract"),
    ({"features": 0x2B, "identity": OCFS}, "not K2-OpenHost firmware"),
])
def test_other_firmware_gets_nothing(driver_args, reason):
    rt, printer, driver = make(OPTIONS, **driver_args)
    contact(printer)
    assert driver.writes == []
    assert reason in rt.get_status(0.0)["apply_state"]


def test_auto_apply_off_writes_nothing():
    rt, printer, driver = make(dict(OPTIONS, auto_apply="false"))
    contact(printer)
    assert driver.writes == []
    assert rt.apply_state == "skipped: auto_apply is off"


def test_not_while_printing_then_at_the_next_contact():
    rt, printer, driver = make(OPTIONS, state="printing")
    contact(printer)
    assert driver.writes == []
    assert rt.apply_state == "skipped: printer is printing"
    printer.objects["print_stats"].state = "standby"
    contact(printer)  # the CFS comes back with the printer idle
    assert len(driver.writes) == 3


def test_readback_mismatch_fails_without_retry():
    rt, printer, driver = make(OPTIONS)
    driver.readback_offset = 1
    contact(printer)
    assert len(driver.writes) == 1
    assert rt.apply_state.startswith("failed:")


def test_busy_cfs_is_retried():
    rt, printer, driver = make(OPTIONS)
    driver.busy = 2
    contact(printer)
    assert len(driver.writes) == 3
    assert rt.apply_state == "applied 3 value(s)"


def test_read_only_options_are_ignored():
    rt, printer, driver = make({"feeder_forward_speed": "255",
                                "buffer_fill_timeout_ms": "30000",
                                "hub_reverse_speed": "150"})
    assert rt.ignored_options == ["feeder_forward_speed", "buffer_fill_timeout_ms"]
    contact(printer)
    assert driver.writes == [(5, 150)]


def test_legacy_tick_names_become_ms():
    rt, printer, driver = make({"feed_total_timeout_20ms_ticks": "1500",
                                "odometer_stall_20ms_ticks": "30"})
    assert rt.overrides == {20: 30000, 22: 600}
    contact(printer)
    assert sorted(driver.writes) == [(20, 30000), (22, 600)]


def test_config_errors():
    with pytest.raises(ConfigError):
        make({"insert_sensor_timeout_ms": "700"})  # now ms: 700 < 1000
    with pytest.raises(ConfigError):
        make({"feed_total_timeout_ms": "25000",
              "feed_total_timeout_20ms_ticks": "1250"})


def test_rfid_pre_measure_default_is_the_stock_400_ms():
    assert runtime.K2OH_BY_ID[17].default == 400
    assert runtime.K2OH_BY_ID[8].default == 7000


def test_console_has_no_write_command():
    rt, printer, driver = make()
    commands = set(printer.objects["gcode"].commands)
    assert commands == {"BOX_CFS_CONFIG_INFO", "BOX_CFS_CONFIG_DIAG",
                        "BOX_CFS_CONFIG_SNAPSHOT"}
