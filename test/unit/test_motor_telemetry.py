"""Motor MCU temperature samples and cached motor_control status.

Host-only: the reactor, printer, config and motor clients are fakes and no
test sends a packet.
"""

import json
import math
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import motor_control as mc  # noqa: E402


class ConfigError(Exception):
    pass


class FakeConfig:
    """The parts of ConfigWrapper the hub uses."""

    def __init__(self, name="motor_control"):
        self.name = name

    def get_name(self):
        return self.name

    def getsection(self, name):
        return FakeConfig(name)

    def get(self, option, default=None):
        return default

    def error(self, msg):
        return ConfigError(msg)


class FakeHeaters:
    def __init__(self):
        self.sensors = []

    def register_sensor(self, config, psensor, gcode_id=None):
        self.sensors.append((config.get_name(), psensor))


class FakePrinter:
    def __init__(self, existing=()):
        self.objects = {name: object() for name in existing}
        self.heaters = FakeHeaters()

    def add_object(self, name, obj):
        if name in self.objects:
            raise ConfigError("Printer object '%s' already created" % name)
        self.objects[name] = obj

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def load_object(self, config, name):
        assert name == "heaters"
        return self.heaters


class Harness:
    def __init__(self, existing=()):
        self.clock = 100.0
        self.fail = False
        self.value = 42.0
        self.queries = []
        reactor = SimpleNamespace(
            NEVER=float("inf"),
            register_timer=lambda callback: callback,
            update_timer=lambda *args: None,
            monotonic=lambda: self.clock,
        )
        self.printer = FakePrinter(existing)
        self.replacement = SimpleNamespace(
            reactor=reactor,
            printer=self.printer,
            is_ready=True,
            motor_params_init=True,
            axes=SimpleNamespace(target=self.target),
        )
        self.hub = mc.Mot2TempSensorHub(self.replacement, FakeConfig())

    def target(self, axis):
        return SimpleNamespace(
            addr=axis, client=SimpleNamespace(get_value=self.read)
        )

    def read(self, addr, index, **_kwargs):
        self.queries.append((addr, index))
        if self.fail:
            raise TimeoutError("read timed out")
        return self.value

    def poll_round(self):
        for _axis in mc.ALL_AXES:
            self.hub._poll(self.clock)

    def state(self, axis="x"):
        return self.hub.sample_status(axis, self.clock)


@pytest.fixture
def h():
    return Harness()


def test_sensors_are_registered_with_heaters(h):
    names = [name for name, _sensor in h.printer.heaters.sensors]
    assert names == [
        "temperature_sensor motor_X_MCU",
        "temperature_sensor motor_Y_MCU",
        "temperature_sensor motor_E_MCU",
    ]
    assert (
        h.printer.objects["temperature_sensor motor_X_MCU"]
        is h.hub.sensors["x"]
    )


def test_name_collision_has_a_readable_error():
    with pytest.raises(ConfigError, match=r"created by \[motor_control\]"):
        Harness(existing=["temperature_sensor motor_Y_MCU"])


def test_never_read_and_stopped(h):
    assert h.state()["state"] == "stopped"
    h.hub.start()
    sample = h.state()
    assert sample["state"] == "never"
    assert sample["valid"] is False
    assert sample["temperature"] is None


def test_poll_order_index_and_first_valid_read(h):
    h.hub.start()
    h.poll_round()
    assert h.queries == [("x", 17), ("y", 17), ("e", 17)]
    sample = h.state()
    assert sample["state"] == "current" and sample["valid"] is True
    assert sample["temperature"] == 42.0
    assert sample["session"] == sample["current_session"] == 1


def test_stale_after_two_rounds(h):
    h.hub.start()
    h.poll_round()
    h.clock += mc.TEMP_SAMPLE_MAX_AGE + 1
    assert h.state()["state"] == "stale"
    assert h.state()["valid"] is False


def test_failed_read_keeps_history_but_is_not_valid(h):
    h.hub.start()
    h.poll_round()
    h.fail = True
    h.hub._poll(h.clock)  # x
    sample = h.state()
    assert sample["state"] == "failed" and sample["valid"] is False
    assert sample["temperature"] == 42.0
    assert sample["read_errors"] == 1 and sample["consecutive_errors"] == 1
    assert sample["last_error"] == "read timed out"


def test_recovery_after_failure(h):
    h.hub.start()
    h.fail = True
    h.poll_round()
    assert h.state()["state"] == "failed"
    h.fail = False
    h.poll_round()
    sample = h.state()
    assert sample["state"] == "current"
    assert sample["consecutive_errors"] == 0 and sample["read_errors"] == 1


def test_stop_start_without_new_read_is_not_current(h):
    # The audit case: a sample from before stop/start must not come back
    # as valid, even if it is recent.
    h.hub.start()
    h.poll_round()
    h.hub.stop()
    assert h.state()["state"] == "stopped"
    h.hub.start()
    h.clock += 1
    sample = h.state()
    assert sample["state"] == "previous_session"
    assert sample["valid"] is False
    assert sample["temperature"] == 42.0
    h.poll_round()
    assert h.state()["state"] == "current"


def test_new_session_clears_consecutive_errors_only(h):
    h.hub.start()
    h.fail = True
    h.poll_round()
    h.hub.stop()
    h.hub.start()
    sample = h.state()
    assert sample["consecutive_errors"] == 0 and sample["read_errors"] == 1
    assert sample["state"] == "never"


@pytest.mark.parametrize("value", [0.0, -5.0])
def test_zero_and_negative_are_readings(h, value):
    h.value = value
    h.hub.start()
    h.poll_round()
    assert h.state()["valid"] is True
    assert h.hub.sensors["x"].temperature == value


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), None, "hot"]
)
def test_unusable_values_are_failed_reads(h, value):
    h.hub.start()
    h.poll_round()
    h.value = value
    h.poll_round()
    sample = h.state()
    assert sample["state"] == "failed"
    assert sample["temperature"] == 42.0
    json.dumps(h.hub.get_status(h.clock), allow_nan=False)


def test_standard_sensor_status_carries_validity(h):
    sensor = h.hub.sensors["y"]
    status = sensor.get_status(h.clock)
    assert status == {
        "temperature": 0.0,
        "measured_min_temp": 0.0,
        "measured_max_temp": 0.0,
        "valid": False,
        "state": "stopped",
        "sample_age": None,
    }
    h.hub.start()
    h.poll_round()
    h.clock += 3
    status = sensor.get_status(h.clock)
    assert status["temperature"] == 42.0 and status["valid"] is True
    assert status["sample_age"] == 3.0
    h.hub.stop()
    status = sensor.get_status(h.clock)
    assert status["temperature"] == 42.0 and status["valid"] is False


def test_status_reads_never_query(h):
    h.hub.start()
    h.poll_round()
    before = list(h.queries)
    for _ in range(5):
        h.hub.get_status(h.clock)
        h.hub.sensors["e"].get_status(h.clock)
    assert h.queries == before


def test_poll_waits_for_motor_readiness(h):
    h.replacement.is_ready = False
    h.hub.start()
    h.poll_round()
    assert h.queries == []
    assert h.state()["state"] == "never"


def test_motor_control_status_is_cached_and_serializable(h):
    h.hub.start()
    h.poll_round()
    validity = mc.ProtectionValidity(mc.ALL_AXES, mc.PROTECTION_STALE_AFTER)
    validity.new_session()
    validity.success("e", 95.0, {"active": True, "error_code": 256}, "test")
    fake = SimpleNamespace(
        reactor=h.replacement.reactor,
        motor_fault_detail={
            "e": {"error_code": 256, "warning_code": 4, "active": True}
        },
        protection_validity=validity,
        is_check_cut_pos_start=False,
        cut_state=False,
        _transport_ready_status=lambda: {},
        is_homing=False,
        is_ready=True,
        motor_params_init=True,
        _startup_started=True,
        _startup_complete=True,
        _startup_step_index=3,
        _startup_error=None,
        stall_monitor=SimpleNamespace(read_all=lambda: {"x": 0}),
        temp_sensors=h.hub,
    )
    before = list(h.queries)
    status = mc.MotorControl.get_status(fake, 100.0)
    assert status["faults"]["e"]["query_age"] == 5.0
    assert status["faults"]["x"]["queried"] is False
    assert status["temperatures"]["x"]["valid"] is True
    assert h.queries == before
    text = json.dumps(status, allow_nan=False)
    assert not any(math.isnan(v) for v in _floats(json.loads(text)))


def _floats(value):
    if isinstance(value, float):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _floats(item)
    elif isinstance(value, list):
        for item in value:
            yield from _floats(item)
