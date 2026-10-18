"""First contact of box_cfs_runtime with the CFS.

A CFS fresh from power-on answers at 0xFE until [box] has enumerated it
(A1/A0), so with [box] the first probe waits for box:ready instead of a fixed
delay after serial_485:ready. Mocks only: nothing talks to a device.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_cfs_runtime as runtime

FEATURES_V322 = 0xD7


class Reactor:
    def __init__(self):
        self.now = 10.0
        self.callbacks = []

    def monotonic(self):
        return self.now

    def register_callback(self, callback, waketime=None):
        self.callbacks.append((waketime if waketime is not None else self.now, callback))

    def run(self, until=None):
        """Run due callbacks in time order (including ones they schedule)."""
        while self.callbacks:
            self.callbacks.sort(key=lambda item: item[0])
            waketime, callback = self.callbacks[0]
            if until is not None and waketime > until:
                return
            self.callbacks.pop(0)
            self.now = max(self.now, waketime)
            callback(self.now)


class Gcode:
    def register_command(self, *args, **kwargs):
        pass


class Box:
    def __init__(self, online=()):
        self.drivers = {address: object() for address in online}


class Printer:
    def __init__(self, box=None):
        self.reactor = Reactor()
        self.objects = {"gcode": Gcode(), "serial_485 serial485": object()}
        if box is not None:
            self.objects["box"] = box
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


class Config:
    def __init__(self, printer, options):
        self.printer = printer
        self.options = options

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getint(self, name, default=None, minval=None, maxval=None):
        value = self.options.get(name, default)
        return None if value is None else int(value)

    def getboolean(self, name, default=None):
        value = self.options.get(name, default)
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)


class CfsDriver:
    """Times out until the CFS has an address, then answers as v3.22."""

    def __init__(self, features=FEATURES_V322):
        self.assigned = False
        self.features = features
        self.probes = 0
        self.writes = []

    def probe(self, timeout=1.0):
        self.probes += 1
        if not self.assigned:
            raise runtime.CfsRuntimeConfigError(
                "CFS runtime-config request timed out")
        return {"version": 2, "count": 28, "descriptor_size": 10,
                "features": self.features, "override_mask": 0}

    def info_v2(self, timeout=1.0):
        return self.probe(timeout)

    def identity(self, timeout=0.5):
        return None

    def set_v1(self, *args):
        self.writes.append(("set_v1",) + args)

    def set_v2(self, *args):
        self.writes.append(("set_v2",) + args)


def make(box=None, options=None):
    printer = Printer(box)
    rt = runtime.BoxCfsRuntime(Config(printer, dict(options or {})))
    driver = CfsDriver()
    rt._driver = lambda: driver
    printer.send_event("klippy:connect")
    return rt, printer, driver


def test_with_box_no_probe_before_enumeration():
    rt, printer, driver = make(Box(online=(1,)), {"hub_forward_speed": "100"})
    printer.send_event("serial_485:ready")
    printer.reactor.run()
    assert driver.probes == 0
    assert rt.supported is None


def test_probe_after_box_ready_reads_the_feature_byte():
    rt, printer, driver = make(Box(online=(1,)))
    printer.send_event("serial_485:ready")
    driver.assigned = True  # A1/A0 done
    printer.send_event("box:ready")
    printer.reactor.run()
    assert driver.probes == 1
    assert rt.supported is True
    assert rt.firmware_features == FEATURES_V322
    assert rt.last_error is None


def test_overrides_on_v322_are_not_applied_after_box_ready():
    rt, printer, driver = make(
        Box(online=(1,)), {"hub_forward_speed": "100", "load_retry_count": "1"})
    printer.send_event("serial_485:ready")
    driver.assigned = True
    printer.send_event("box:ready")
    printer.reactor.run()
    assert rt.supported is True  # experimental build: manual bench only
    assert driver.writes == []


def test_box_ready_without_the_address_online_does_not_probe():
    rt, printer, driver = make(Box(online=()))
    printer.send_event("serial_485:ready")
    printer.send_event("box:ready")
    printer.reactor.run()
    assert driver.probes == 0
    assert rt.supported is None


def test_rediscovery_restarts_the_contact_and_drops_stale_retries():
    box = Box(online=(1,))
    rt, printer, driver = make(box)
    printer.send_event("serial_485:ready")
    # A box:ready while the CFS is still unassigned (worst case): the first
    # attempt times out and schedules a retry.
    printer.send_event("box:ready")
    printer.reactor.run(until=printer.reactor.now)
    assert driver.probes == 1 and rt.supported is None
    assert "timed out" in rt.last_error
    # The CFS comes back and box enumerates it again.
    driver.assigned = True
    printer.send_event("box:ready")
    printer.reactor.run()
    # The new contact succeeds; the old retry chain does not run on top.
    assert rt.supported is True
    assert driver.probes == 2


def test_timeouts_give_up_after_three_attempts():
    rt, printer, driver = make(Box(online=(1,)))
    printer.send_event("serial_485:ready")
    printer.send_event("box:ready")
    printer.reactor.run()
    assert driver.probes == 3
    assert rt.supported is False
    assert "timed out" in rt.last_error


def test_without_box_keeps_the_delayed_contact():
    rt, printer, driver = make(None, {"hub_forward_speed": "100"})
    driver.assigned = True
    printer.send_event("serial_485:ready")
    assert printer.reactor.callbacks[0][0] == printer.reactor.now + 1.0
    printer.reactor.run()
    assert rt.supported is True
    assert driver.probes >= 1


class StockCfsDriver(CfsDriver):
    """Stock Creality firmware: 0x57 answers status 1 (invalid command)."""

    def probe(self, timeout=1.0):
        self.probes += 1
        raise runtime.CfsRuntimeConfigError("CONFIG_INFO returned status 1")


def test_stock_firmware_is_unsupported_at_once():
    for options in ({}, {"hub_forward_speed": "100"}):
        printer = Printer(Box(online=(1,)))
        rt = runtime.BoxCfsRuntime(Config(printer, options))
        driver = StockCfsDriver()
        rt._driver = lambda: driver
        printer.send_event("klippy:connect")
        printer.send_event("serial_485:ready")
        printer.send_event("box:ready")
        start = printer.reactor.now
        printer.reactor.run()
        assert driver.probes == 1  # no retries
        assert printer.reactor.now == start  # no delay
        assert rt.supported is False
        assert "status 1" in rt.last_error
        assert driver.writes == []


def _make_with_driver_factory(box, options, monkeypatch, assigned=True):
    """Build the runtime with its real _driver(), recording the address used."""
    printer = Printer(box)
    rt = runtime.BoxCfsRuntime(Config(printer, options))
    calls = []

    class Driver(CfsDriver):
        def __init__(self, serial, address):
            CfsDriver.__init__(self)
            self.assigned = assigned
            calls.append(address)

    monkeypatch.setattr(runtime, "CfsRuntimeDriver", Driver)
    printer.send_event("klippy:connect")
    return rt, printer, calls


def test_several_cfs_online_are_each_probed(monkeypatch):
    box = Box(online=(1, 2, 3))
    before = dict(box.drivers)
    rt, printer, calls = _make_with_driver_factory(box, {}, monkeypatch)
    printer.send_event("serial_485:ready")
    printer.send_event("box:ready")
    printer.reactor.run()
    assert sorted(calls) == [1, 2, 3]
    assert all(rt.units[a].supported is True for a in (1, 2, 3))
    status = rt.get_status(0.0)
    assert status["address"] == 1 and status["supported"] is True
    assert sorted(status["boxes"]) == ["1", "2", "3"]
    assert status["boxes"]["2"]["firmware_features"] == FEATURES_V322
    # The Box driver table is only read, never changed.
    assert box.drivers == before


def test_address_option_limits_to_one_cfs(monkeypatch):
    box = Box(online=(1, 2, 3))
    rt, printer, calls = _make_with_driver_factory(
        box, {"address": "1"}, monkeypatch)
    printer.send_event("serial_485:ready")
    printer.send_event("box:ready")
    printer.reactor.run()
    assert calls == [1]
    assert sorted(rt.get_status(0.0)["boxes"]) == ["1"]


def test_runtime_for_the_second_cfs_uses_its_address(monkeypatch):
    box = Box(online=(1, 2))
    rt, printer, calls = _make_with_driver_factory(
        box, {"address": "2"}, monkeypatch)
    printer.send_event("serial_485:ready")
    printer.send_event("box:ready")
    printer.reactor.run()
    assert calls == [2]
    assert rt.supported is True


def test_configured_cfs_missing_while_others_are_online(monkeypatch):
    box = Box(online=(1, 3))
    rt, printer, calls = _make_with_driver_factory(
        box, {"address": "2"}, monkeypatch)
    printer.send_event("serial_485:ready")
    printer.send_event("box:ready")
    printer.reactor.run()
    assert calls == []
    assert rt.supported is None


class Gcmd:
    def __init__(self, **params):
        self.params = params
        self.replies = []

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name)
        return default if value is None else int(value)

    def respond_info(self, msg):
        self.replies.append(msg)

    def error(self, msg):
        return ValueError(msg)


def _ready_runtime(monkeypatch, online, options=None):
    rt, printer, calls = _make_with_driver_factory(
        Box(online=online), dict(options or {}), monkeypatch)
    printer.send_event("serial_485:ready")
    printer.send_event("box:ready")
    printer.reactor.run()
    seen = []
    for address in rt.units:
        rt.units[address].cmd_info = (
            lambda gcmd, address=address: seen.append(address))
    return rt, seen


def test_commands_select_the_cfs_with_address(monkeypatch):
    rt, seen = _ready_runtime(monkeypatch, (1, 2))
    gcmd = Gcmd()
    rt._dispatch(gcmd, "cmd_info")  # default: first online
    gcmd2 = Gcmd(ADDRESS="2")
    rt._dispatch(gcmd2, "cmd_info")
    assert seen == [1, 2]
    # With several CFS online the reply says which one answered.
    assert gcmd.replies == ["CFS 1:"] and gcmd2.replies == ["CFS 2:"]


def test_single_cfs_reply_is_unchanged(monkeypatch):
    rt, seen = _ready_runtime(monkeypatch, (1,))
    gcmd = Gcmd()
    rt._dispatch(gcmd, "cmd_info")
    assert seen == [1] and gcmd.replies == []


def test_command_for_an_offline_cfs_is_refused(monkeypatch):
    rt, seen = _ready_runtime(monkeypatch, (1, 2))
    try:
        rt._dispatch(Gcmd(ADDRESS="3"), "cmd_info")
    except ValueError as exc:
        assert "not online" in str(exc)
    else:
        raise AssertionError("an offline CFS must be refused")
    assert seen == []


def test_address_option_refuses_other_cfs(monkeypatch):
    rt, seen = _ready_runtime(monkeypatch, (1, 2), {"address": "1"})
    try:
        rt._dispatch(Gcmd(ADDRESS="2"), "cmd_info")
    except ValueError as exc:
        assert "limited to CFS 1" in str(exc)
    else:
        raise AssertionError("address: 1 must refuse CFS 2")
    rt._dispatch(Gcmd(), "cmd_info")
    assert seen == [1]
