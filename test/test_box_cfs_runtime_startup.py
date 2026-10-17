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
        return int(self.options.get(name, default))

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
