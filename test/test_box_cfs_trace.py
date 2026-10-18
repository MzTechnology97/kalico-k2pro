"""Offline proof that passive CFS trace sends zero RS-485 commands."""

import ast
import logging
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_trace():
    path = (Path(__file__).resolve().parents[1]
            / "klippy" / "extras" / "box_cfs_trace.py")
    spec = spec_from_file_location("test_box_cfs_trace_module", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeReactor:
    NEVER = float("inf")

    def __init__(self):
        self.now = 10.0
        self.schedule = {}

    def register_timer(self, callback):
        self.callback = callback
        self.schedule[callback] = self.NEVER
        return callback

    def update_timer(self, timer, when):
        self.schedule[timer] = when

    def monotonic(self):
        return self.now

    def fire(self, at):
        self.now = at
        value = self.callback(at)
        self.schedule[self.callback] = value
        return value


class FakeGCode:
    def __init__(self):
        self.commands = {}

    def register_command(self, command, method):
        assert command not in self.commands
        self.commands[command] = method


class FakeCommand:
    def __init__(self, **args):
        self.args = args
        self.lines = []

    def get_int(self, name, default, minval=None, maxval=None):
        value = self.args.get(name, default)
        if minval is not None and value < minval:
            raise ValueError("below min")
        if maxval is not None and value > maxval:
            raise ValueError("above max")
        return value

    def error(self, message):
        return RuntimeError(message)

    def respond_info(self, message):
        self.lines.append(message)


class FakeBox:
    def __init__(self):
        self.snapshot = SimpleNamespace(
            data_ready=True, state_code=0, status_code=0,
            loaded_slot=-1, tracking=False, filament_detected=False,
            filament_sensor_error=None)
        self.operation_depth = 0
        self.operation_progress = None

    def unexpected_serial_call(self, *args, **kwargs):
        raise AssertionError("passive CFS tracing must NEVER open RS-485")


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()
        self.gcode = FakeGCode()
        self.box = FakeBox()
        self.listeners = []
        self.lookups = []

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        self.lookups.append(name)
        if name == "gcode":
            return self.gcode
        if name == "box":
            return self.box
        raise AssertionError("Unexpected transport or MCU lookup: " + name)

    def register_event_handler(self, name, cb):
        self.listeners.append((name, cb))

    def send_event(self, name, *args):
        return [handler(*args) for event, handler in self.listeners
                if event == name]


class FakeConfig:
    def __init__(self, printer, settings=None):
        self.printer = printer
        self.settings = settings or {}

    def get_printer(self):
        return self.printer

    def getfloat(self, name, default, minval=None, maxval=None):
        v = self.settings.get(name, default)
        if minval is not None:
            assert v >= minval
        if maxval is not None:
            assert v <= maxval
        return v

    def getint(self, name, default, minval=None, maxval=None):
        v = self.settings.get(name, default)
        if minval is not None:
            assert v >= minval
        if maxval is not None:
            assert v <= maxval
        return v


def setup_trace(settings=None):
    module = load_trace()
    printer = FakePrinter()
    trace = module.load_config(FakeConfig(printer, settings))
    return printer, trace


def test_disabled_until_manual_start_and_no_transport():
    printer, trace = setup_trace()
    assert not trace.running
    assert trace.history_max == 256
    assert printer.reactor.schedule[trace._timer] == printer.reactor.NEVER
    assert set(printer.gcode.commands) == {
        "BOX_CFS_TRACE_START", "BOX_CFS_TRACE_STOP",
        "BOX_CFS_TRACE_SHOW", "BOX_CFS_TRACE_CLEAR"}
    assert set(name for name, _ in printer.listeners) == {
        "klippy:shutdown", "klippy:disconnect",
        "box:cfs_operation_stage", "box:cfs_operation_end"}
    assert all(x in ("box", "gcode") for x in printer.lookups)

    cmd = FakeCommand()
    trace.cmd_start(cmd)
    assert trace.running and trace.samples == 1
    assert len(trace.history) == 1
    assert "NO RS-485" in cmd.lines[0] or "No RS-485" in cmd.lines[0]
    assert printer.reactor.schedule[trace._timer] == 10.2
    with pytest.raises(RuntimeError, match="already running"):
        trace.cmd_start(FakeCommand())

    # Observe cached state change only, no bus transaction.
    printer.box.operation_depth = 1
    printer.box.operation_progress = {
        "kind": "load", "slot": 1, "stage": "move_to_hub"}
    printer.reactor.fire(10.2)
    assert trace.samples == 2
    assert trace.changes == 2
    assert trace.history[-1]["cached"]["operation_stage"] == "move_to_hub"
    assert all(x in ("box", "gcode") for x in printer.lookups)


def test_capture_detects_scheduler_gap_not_falsely_implied_motor_idle():
    printer, trace = setup_trace()
    trace.cmd_start(FakeCommand())
    printer.box.operation_depth = 1
    printer.box.operation_progress = {
        "kind": "unload", "slot": 0, "stage": "return"}
    printer.reactor.fire(12.0)  # 2 sec schedule gap
    assert trace.poll_gaps == 1
    assert trace.max_gap_s == 2
    assert trace.history[-2]["reason"] == "MISSED_WINDOW"
    assert trace.history[-1]["cached"]["operation_active"] is True
    status = trace.get_status(12.0)
    assert status["not_mcu_motor_telemetry"]
    assert status["sampling_may_miss_motion"]
    assert status["reactor_gaps"] == 1
    show = FakeCommand(LIMIT=2)
    trace.cmd_show(show)
    assert "NO MCU MOTOR TELEMETRY" in show.lines[0]
    assert "MISSED_WINDOW" in show.lines[0]
    assert "NO missing-stage interpolation" in show.lines[0]
    stopped = FakeCommand()
    trace.cmd_stop(stopped)
    assert "longest gap" in stopped.lines[0]
    assert printer.reactor.schedule[trace._timer] == printer.reactor.NEVER
    trace.cmd_stop(FakeCommand())  # idempotent
    trace.cmd_clear(FakeCommand())
    assert trace.samples == 0
    assert list(trace.history) == []


def test_bounded_buffer_and_stop_on_disconnect():
    printer, trace = setup_trace({"max_events": 16, "sample_interval": 0.05})
    trace.cmd_start(FakeCommand())
    for n in range(40):
        printer.box.operation_progress = {
            "kind": "load", "slot": 1, "stage": "phase_%d" % n}
        printer.reactor.fire(10.05 + n * 0.05)
    assert len(trace.history) <= 16
    assert trace.samples == 41
    for event, callback in printer.listeners:
        if event == "klippy:disconnect":
            callback()
    assert not trace.running
    assert printer.reactor.schedule[trace._timer] == printer.reactor.NEVER
    # Clear is permitted only after disconnect has stopped capture.
    trace.cmd_clear(FakeCommand())
    assert not trace.history
    status = trace.get_status(50.)
    assert len(status["recent_events"]) <= 8


def test_fail_closed_if_reading_cached_host_state_raises():
    printer, trace = setup_trace()
    trace.cmd_start(FakeCommand())

    def fail():
        raise RuntimeError("host cache failure")

    trace._cached_state = fail
    result = printer.reactor.fire(10.2)
    assert result == printer.reactor.NEVER
    assert not trace.running
    assert "host cache failure" in trace.last_error
    assert all(x in ("box", "gcode") for x in printer.lookups)


def test_unavailable_box_refused_without_timer():
    printer, trace = setup_trace()
    printer.box = None
    with pytest.raises(RuntimeError, match="not available"):
        trace.cmd_start(FakeCommand())
    assert not trace.running
    assert printer.reactor.schedule[trace._timer] == printer.reactor.NEVER


def _load_real_box_operation_methods():
    """Execute exact Box operation methods without importing printer hardware."""
    source = (Path(__file__).resolve().parents[1]
              / "klippy" / "extras" / "box.py").read_text()
    tree = ast.parse(source)
    box_type = next(
        item for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "Box")
    required = {
        "_operation", "_set_operation_progress", "_emit_passive_cfs_event"}
    methods = [
        method for method in box_type.body
        if isinstance(method, ast.FunctionDef) and method.name in required]
    assert set(method.name for method in methods) == required
    cls = ast.ClassDef(
        name="IsolatedBoxMethods", bases=[], keywords=[],
        body=methods, decorator_list=[])
    module = ast.fix_missing_locations(
        ast.Module(body=[cls], type_ignores=[]))
    namespace = {
        "contextmanager": contextmanager,
        "_klog": lambda *args, **kwargs: None,
        "logging": logging,
        "BoxError": RuntimeError,
    }
    exec(compile(module, "box.py AST extracted", "exec"), namespace)
    return namespace["IsolatedBoxMethods"]


def test_real_box_operation_events_capture_all_stages_while_timer_blocked():
    """Stage callbacks run synchronously even if reactor cannot schedule."""
    printer, trace = setup_trace()
    cls = _load_real_box_operation_methods()
    box = cls()
    box.printer = printer
    box.snapshot = FakeBox().snapshot
    box._cfs_runtime_write_owner = None
    box.operation_depth = 0
    box.operation_progress = None
    box.clog_baseline = None
    printer.box = box
    trace.cmd_start(FakeCommand())
    # Simulate a synchronous physical operation. No timer fires at all.
    with box._operation():
        printer.reactor.now = 10.1
        box._set_operation_progress("load", 1, "feeding_to_buffer")
        printer.reactor.now = 10.3
        box._set_operation_progress("load", 1, "feeding_to_printhead")
        printer.reactor.now = 10.7
        box._set_operation_progress("load", 1, "verifying")
        assert box.operation_depth == 1
        assert trace.samples == 1
        assert trace.stage_events == 3
        assert [e["cached"]["operation_stage"] for e in trace.history
                if e["reason"] == "HOST_STAGE_EVENT"] == [
                    "feeding_to_buffer", "feeding_to_printhead", "verifying"]
    assert box.operation_depth == 0
    assert box.operation_progress is None
    assert trace.operation_ends == 1
    assert trace.history[-1]["reason"] == "HOST_OPERATION_END"
    assert trace.history[-1]["cached"]["operation_result"] == "unknown"
    assert trace.samples == 1  # no timed polling during motor wait
    assert all(name in ("box", "gcode") for name in printer.lookups)
    printer.reactor.fire(12.0)
    assert trace.poll_gaps == 1
    assert trace.get_status(12.)["host_stage_events"] == 3
    assert trace.get_status(12.)["host_operation_end_events"] == 1


def test_faulting_stage_listener_cannot_interrupt_actual_box_operation():
    printer, trace = setup_trace()
    cls = _load_real_box_operation_methods()
    box = cls()
    box.printer = printer
    box.snapshot = FakeBox().snapshot
    box._cfs_runtime_write_owner = None
    box.operation_depth = 0
    box.operation_progress = None
    box.clog_baseline = None
    printer.box = box
    trace.cmd_start(FakeCommand())

    def broken_listener(*args):
        raise RuntimeError("broken observer")

    # The Box boundary guards against observers raising into the CFS path.
    printer.register_event_handler("box:cfs_operation_stage", broken_listener)
    with box._operation():
        box._set_operation_progress("unload", 0, "retracting_to_cfs")
        assert box.operation_progress["stage"] == "retracting_to_cfs"
    assert box.operation_depth == 0
    assert trace.stage_events == 1
    assert trace.operation_ends == 1

    # The end event reports completion-of-context, NEVER success.
    with pytest.raises(ValueError):
        with box._operation():
            box._set_operation_progress("load", 2, "preparing")
            raise ValueError("simulated original operation failed")
    assert trace.operation_ends == 2
    assert trace.history[-1]["cached"]["operation_result"] == "unknown"


def test_stage_observer_failure_is_suppressed_and_disables_capture():
    printer, trace = setup_trace()
    trace.cmd_start(FakeCommand())

    def fail(*args):
        raise ValueError("trace ring allocation failed")

    trace._append = fail
    printer.send_event("box:cfs_operation_stage", "load", 1, "preparing")
    assert not trace.running
    assert trace.last_error == "ValueError: trace ring allocation failed"
    assert printer.reactor.schedule[trace._timer] == printer.reactor.NEVER
    assert all(name in ("box", "gcode") for name in printer.lookups)


def test_exact_host_stage_callback_intervals_even_with_no_timer_poll():
    printer, trace = setup_trace()
    real_box_cls = _load_real_box_operation_methods()
    box = real_box_cls()
    box.printer = printer
    box.snapshot = FakeBox().snapshot
    box._cfs_runtime_write_owner = None
    box.operation_depth = 0
    box.operation_progress = None
    box.clog_baseline = None
    printer.box = box
    trace.cmd_start(FakeCommand())

    with box._operation():
        printer.reactor.now = 10.1
        box._set_operation_progress("load", 0, "preparing")
        printer.reactor.now = 10.4
        box._set_operation_progress("load", 0, "feeding_to_buffer")
        printer.reactor.now = 11.2
        box._set_operation_progress("load", 0, "feeding_to_printhead")
        printer.reactor.now = 12.0
        box._set_operation_progress("load", 0, "verifying")
    stages = [r["cached"] for r in trace.history
              if r["reason"] == "HOST_STAGE_EVENT"]
    assert [r["previous_host_stage"] for r in stages] == [
        None, "preparing", "feeding_to_buffer", "feeding_to_printhead"]
    assert [r["previous_host_stage_interval_s"] for r in stages] == [
        None, 0.3, 0.8, 0.8]
    ending = trace.history[-1]
    assert ending["reason"] == "HOST_OPERATION_END"
    assert ending["cached"]["operation_result"] == "unknown"
    assert ending["cached"]["last_host_stage"] == "verifying"
    assert ending["cached"]["last_host_stage_interval_s"] == 0
    assert ending["cached"]["host_first_stage_to_end_s"] == 1.9
    assert trace.host_stage_intervals == 4
    assert trace.max_host_stage_interval_s == pytest.approx(0.8)
    assert trace.last_operation_host_span_s == 1.9
    # No timer-driven samples during the whole simulated load.
    assert trace.samples == 1
    assert trace.poll_gaps == 0
    assert all(x in ("box", "gcode") for x in printer.lookups)
    gcmd = FakeCommand(LIMIT=5)
    trace.cmd_show(gcmd)
    assert "previous_host_interval_s=0.8" in gcmd.lines[0]
    assert "host_first_stage_to_end_s=1.9" in gcmd.lines[0]
    status = trace.get_status(12.0)
    assert status["host_stage_intervals"] == 4
    assert status["host_intervals_are_not_motor_timings"] is True
    assert status["last_operation_host_span_s"] == 1.9


def test_host_intervals_reset_between_operations_and_clear():
    printer, trace = setup_trace()
    trace.cmd_start(FakeCommand())
    printer.reactor.now = 10.1
    printer.send_event("box:cfs_operation_stage", "load", 1, "preparing")
    printer.reactor.now = 10.6
    printer.send_event("box:cfs_operation_end")
    printer.reactor.now = 11.0
    printer.send_event("box:cfs_operation_stage", "unload", 1, "retracting")
    assert trace.history[-1]["cached"]["previous_host_stage"] is None
    printer.reactor.now = 12.0
    printer.send_event("box:cfs_operation_end")
    assert trace.last_operation_host_span_s == 1.0
    assert trace.host_stage_intervals == 2
    trace.cmd_stop(FakeCommand())
    trace.cmd_clear(FakeCommand())
    assert trace.host_stage_intervals == 0
    assert trace.max_host_stage_interval_s == 0
    assert trace.last_operation_host_span_s is None
    assert trace._stage_started_at is None


def test_non_monotonic_host_timestamp_disables_only_diagnostics():
    printer, trace = setup_trace()
    trace.cmd_start(FakeCommand())
    printer.reactor.now = 11
    printer.send_event("box:cfs_operation_stage", "load", 0, "preparing")
    printer.reactor.now = 10
    # If host monotonicity breaks, diagnostic capture stops and does NOT
    # issue a motor/serial request or throw into Box command execution.
    printer.send_event("box:cfs_operation_stage", "load", 0, "verifying")
    assert not trace.running
    assert "non-monotonic host" in trace.last_error
    assert trace.get_status(10)["host_intervals_are_not_motor_timings"] is True
    assert printer.reactor.schedule[trace._timer] == printer.reactor.NEVER
