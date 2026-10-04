"""RS-485 link watchdog, the [k2_t113] client and the T113 RFID beep.

Fakes only: no serial port, no network, no threads left running.
"""

import json
import pathlib
import sys
import threading
from collections import deque
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import external_rfid_reader, k2_t113, serial_485  # noqa: E402


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


# --- LinkWatchdog -------------------------------------------------------------


def watchdog(clock):
    return serial_485.LinkWatchdog(lost_after=10.0, min_timeouts=3, clock=clock)


def test_first_answer_makes_the_link_ok():
    clock = Clock()
    link = watchdog(clock)
    assert link.evaluate() is None and link.state == "unknown"
    link.ok()
    assert link.evaluate() == ("unknown", "ok")


def test_a_few_timeouts_are_only_degraded():
    clock = Clock()
    link = watchdog(clock)
    link.ok()
    link.evaluate()
    link.timeout()
    link.timeout()
    clock.now += 30
    assert link.evaluate() == ("ok", "degraded")


def test_lost_needs_time_and_consecutive_timeouts():
    clock = Clock()
    link = watchdog(clock)
    link.ok()
    link.evaluate()
    for _ in range(5):
        link.timeout()
    clock.now += 9.9
    assert link.evaluate() == ("ok", "degraded")
    clock.now += 0.2
    assert link.evaluate() == ("degraded", "lost")
    assert link.lost_count == 1
    assert link.snapshot()["link_lost_for"] == 0.0


def test_one_silent_device_does_not_lose_the_link():
    # CFS absent: its requests time out, the motors keep answering.
    clock = Clock()
    link = watchdog(clock)
    for _ in range(20):
        link.timeout()
        link.timeout()
        link.ok()
        clock.now += 1
    assert link.evaluate() == ("unknown", "ok")


def test_lost_stays_lost_until_an_answer():
    clock = Clock()
    link = watchdog(clock)
    for _ in range(3):
        link.timeout()
    clock.now += 11
    assert link.evaluate() == ("unknown", "lost")
    link.timeout()
    clock.now += 5
    assert link.evaluate() is None
    link.ok()
    assert link.evaluate() == ("lost", "ok")
    assert link.snapshot()["link_lost_for"] is None


def test_never_answered_counts_from_start():
    clock = Clock()
    link = watchdog(clock)
    for _ in range(3):
        link.timeout()
    clock.now += 10.5
    assert link.evaluate()[1] == "lost"


def test_reset_starts_over_but_keeps_the_count():
    clock = Clock()
    link = watchdog(clock)
    for _ in range(3):
        link.timeout()
    clock.now += 11
    link.evaluate()
    link.reset()
    assert link.state == "unknown" and link.lost_count == 1
    assert link.snapshot()["consecutive_timeouts"] == 0


# --- the wrapper's reactions ------------------------------------------------------


class Printer:
    def __init__(self, state="printing"):
        self.print_stats = SimpleNamespace(state=state)
        self.pauses = []
        self.events = []
        self.shutdowns = []
        self.pause_resume = SimpleNamespace(
            pause_command_sent=False,
            send_pause_command=lambda: self.pauses.append(True),
        )

    def lookup_object(self, name, default=None):
        return {
            "print_stats": self.print_stats,
            "pause_resume": self.pause_resume,
        }.get(name, default)

    def send_event(self, name, *args):
        self.events.append(name)

    def invoke_shutdown(self, msg):
        self.shutdowns.append(msg)

    def is_shutdown(self):
        return False


def wrapper(action="pause", state="printing"):
    w = serial_485.Serial_485_Wrapper.__new__(serial_485.Serial_485_Wrapper)
    w.printer = Printer(state)
    w.raw, w.info, w.scripts, w.callbacks = [], [], [], []
    w.gcode = SimpleNamespace(
        respond_raw=w.raw.append,
        respond_info=w.info.append,
        run_script=w.scripts.append,
    )
    w.reactor = SimpleNamespace(register_async_callback=w.callbacks.append)
    w.serial_port = "/dev/ttyUSB2"
    w.link = watchdog(Clock())
    w.link_lost_action = action
    return w


def test_lost_while_printing_pauses():
    w = wrapper("pause")
    w._link_changed("degraded", "lost")
    assert w.printer.pauses == [True]
    assert len(w.callbacks) == 1
    w.callbacks[0](0.0)
    assert w.scripts == ["PAUSE"]
    assert "RS-485 link lost" in w.raw[0]
    assert w.printer.events == ["serial_485:link_lost"]


def test_lost_with_warn_only_warns():
    w = wrapper("warn")
    w._link_changed("ok", "lost")
    assert w.printer.pauses == [] and w.raw


def test_lost_with_shutdown_shuts_down_while_printing():
    w = wrapper("shutdown")
    w._link_changed("ok", "lost")
    assert len(w.printer.shutdowns) == 1 and w.raw == []


@pytest.mark.parametrize("action", ["pause", "shutdown"])
def test_lost_while_idle_only_warns(action):
    w = wrapper(action, state="standby")
    w._link_changed("ok", "lost")
    assert w.printer.pauses == [] and w.printer.shutdowns == []
    assert w.raw


def test_restored_is_reported():
    w = wrapper()
    w._link_changed("lost", "ok")
    assert w.info == ["RS-485 link restored"]
    assert w.printer.events == ["serial_485:link_restored"]


def test_wait_for_response_feeds_the_watchdog():
    w = serial_485.Serial_485_Wrapper.__new__(serial_485.Serial_485_Wrapper)
    w._rx_cond = threading.Condition()
    w._rx_frames = deque()
    w._stop_request_worker = threading.Event()
    w._request_stop_error = None
    w._stats = {"timeouts": 0, "rx_unmatched": 0}
    w.link = serial_485.LinkWatchdog()
    assert w._wait_for_response(0x01, 0x0A, 0.01) is None
    assert w.link.consecutive == 1 and w._stats["timeouts"] == 1
    frame = bytes([serial_485.PACK_HEAD, 0x01, 3, 0, 0x0A, 0])
    w._rx_frames.append(frame)
    assert w._wait_for_response(0x01, 0x0A, 0.5) == frame
    assert w.link.consecutive == 0 and w.link.last_ok is not None


# --- [k2_t113] ------------------------------------------------------------------


class Config:
    def __init__(self, printer, **values):
        self.printer = printer
        self.values = values

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.values.get(name, default)

    def getint(self, name, default=None, **_kw):
        return int(self.values.get(name, default))

    def getfloat(self, name, default=None, **_kw):
        return float(self.values.get(name, default))

    def getboolean(self, name, default=None):
        return bool(self.values.get(name, default))

    def getchoice(self, name, choices, default=None):
        return choices[self.values.get(name, default)]


class FakeClient:
    def __init__(self, host, port, token):
        self.args = (host, port, token)
        self.calls = []
        self.fail = None

    def request(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, body))
        if self.fail:
            raise k2_t113.T113Error(self.fail)
        if path == "/status":
            return {"slot": "B", "mcu_power": "on", "bridges": {}}
        return {"detail": "ok"}


class T113Printer:
    def __init__(self):
        self.handlers = {}
        self.exits = []
        self.message = ("Printer is ready", "ready")
        self.shutdown = False
        self.print_stats = SimpleNamespace(state="standby")
        self.reactor = SimpleNamespace(
            register_async_callback=lambda cb: cb(0.0),
            register_timer=lambda cb, when=None: cb,
            NOW=0.0,
        )
        self.commands = {}
        self.raw, self.info = [], []
        self.gcode = SimpleNamespace(
            ready_gcode_handlers={},
            register_command=self._register,
            respond_raw=self.raw.append,
            respond_info=self.info.append,
            error=RuntimeError,
        )

    def _register(self, name, func, when_not_ready=False, desc=None):
        self.commands[name] = (func, when_not_ready)
        self.gcode.ready_gcode_handlers[name] = func

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name == "gcode":
            return self.gcode
        if name == "print_stats":
            return self.print_stats
        return default

    def register_event_handler(self, name, func):
        self.handlers[name] = func

    def get_state_message(self):
        return self.message

    def is_shutdown(self):
        return self.shutdown

    def request_exit(self, kind):
        self.exits.append(kind)


@pytest.fixture
def t113(monkeypatch):
    monkeypatch.setattr(k2_t113.time, "sleep", lambda s: None)

    def make(**values):
        printer = T113Printer()
        values.setdefault("host", "10.0.0.5")
        values.setdefault("token", "secret-token-123456")
        obj = k2_t113.K2T113(
            Config(printer, **values), client_factory=FakeClient
        )
        obj._spawn = lambda target, *args: target(*args)
        return obj, printer

    return make


class Gcmd:
    def __init__(self, **params):
        self.params = params
        self.lines = []

    def get_int(self, name, default=None, **_kw):
        return int(self.params.get(name, default))

    def respond_info(self, line):
        self.lines.append(line)

    def error(self, msg):
        return RuntimeError(msg)


def test_disabled_without_host_or_token(t113):
    obj, _p = t113(host="")
    assert obj.enabled is False and obj.client is None
    assert obj.beep() is False
    assert obj.get_status(0)["enabled"] is False


def test_refresh_and_status(t113):
    obj, _p = t113()
    obj.refresh()
    status = obj.get_status(0)
    assert status["connected"] is True
    assert status["telemetry"]["slot"] == "B"
    json.dumps(status)


def test_refresh_failure_is_recorded(t113):
    obj, _p = t113()
    obj.client.fail = "connection refused"
    assert obj.refresh() is None
    assert obj.get_status(0)["error"] == "connection refused"


def test_beep(t113):
    obj, _p = t113()
    assert obj.beep(150, 2) is True
    assert obj.client.calls == [("POST", "/beep", {"ms": 150, "count": 2})]


def test_m300_only_without_a_macro(t113):
    obj, printer = t113()
    printer.gcode.ready_gcode_handlers["M300"] = object()
    obj._handle_connect()
    assert "M300" not in printer.commands
    obj2, printer2 = t113()
    obj2._handle_connect()
    assert "M300" in printer2.commands


def test_power_cycle_needs_confirm(t113):
    obj, _p = t113()
    with pytest.raises(RuntimeError, match="CONFIRM=1"):
        obj.cmd_T113_MCU_POWER_CYCLE(Gcmd())
    assert obj.client.calls == []


def test_power_cycle_then_firmware_restart(t113):
    obj, printer = t113()
    printer.shutdown = True
    obj.cmd_T113_MCU_POWER_CYCLE(Gcmd(CONFIRM=1))
    assert obj.client.calls == [("POST", "/mcu/cycle", {"force": True})]
    assert printer.exits == ["firmware_restart"]
    assert printer.commands["T113_MCU_POWER_CYCLE"][1] is True


def test_failed_power_cycle_does_not_restart(t113):
    obj, printer = t113()
    obj.client.fail = "HTTP 409 refused: a print is printing"
    obj.cmd_T113_MCU_POWER_CYCLE(Gcmd(CONFIRM=1))
    assert printer.exits == []
    assert "failed" in printer.raw[0]


def test_bridges_restart(t113):
    obj, _p = t113()
    obj.cmd_T113_BRIDGES_RESTART(Gcmd(CONFIRM=1))
    assert obj.client.calls == [("POST", "/bridges/restart", {"force": False})]


@pytest.mark.parametrize(
    "setting, message, cut",
    [
        ("off", "Shutdown due to M112 command", False),
        ("m112", "Shutdown due to M112 command", True),
        ("m112", "Lost communication with MCU 'mcu'", False),
        ("any", "Lost communication with MCU 'mcu'", True),
    ],
)
def test_estop_on_shutdown(t113, setting, message, cut):
    obj, printer = t113(estop_on_shutdown=setting)
    printer.message = (message, "shutdown")
    obj._handle_shutdown()
    assert (("POST", "/estop", {}) in obj.client.calls) is cut


def test_auto_power_cycle_only_when_idle_and_rate_limited(t113):
    obj, printer = t113(auto_power_cycle=True)
    printer.message = ("Lost communication with MCU 'nozzle_mcu'", "shutdown")
    obj._last_idle = False
    obj._handle_shutdown()
    assert obj.client.calls == []
    obj._last_idle = True
    obj._handle_shutdown()
    assert obj.client.calls == [("POST", "/mcu/cycle", {"force": True})]
    assert printer.exits == ["firmware_restart"]
    obj._handle_shutdown()
    assert len(obj.client.calls) == 1


def test_idle_sampling(t113):
    obj, printer = t113()
    printer.print_stats.state = "printing"
    obj._sample_idle(0.0)
    assert obj._last_idle is False
    printer.print_stats.state = "complete"
    obj._sample_idle(0.0)
    assert obj._last_idle is True


# --- RFID beep backend ------------------------------------------------------------


def reader(backend, t113_obj=None):
    r = external_rfid_reader.ExternalRfidReader.__new__(
        external_rfid_reader.ExternalRfidReader
    )
    r.beep_enabled = True
    r.beep_backend = backend
    r.printer = SimpleNamespace(
        lookup_object=lambda name, default=None: (
            t113_obj if name == "k2_t113" else default
        )
    )
    r.local = []
    r._beep_worker = lambda: r.local.append(True)
    return r


def test_rfid_beep_through_t113():
    calls = []
    r = reader(
        "t113", SimpleNamespace(beep=lambda ms: calls.append(ms) or True)
    )
    r._start_beep()
    assert calls == [200] and r.local == []


def test_rfid_beep_none_and_missing_t113():
    r = reader("none")
    r._start_beep()
    r2 = reader("t113", None)
    r2._start_beep()
    assert r.local == [] and r2.local == []
