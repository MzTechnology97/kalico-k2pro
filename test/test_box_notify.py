import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import Box
from extras.box_print_mapping import BoxPrintMapping


class FakeGcode:
    def __init__(self, handlers=("_BOX_NOTIFY",), error=None):
        self.ready_gcode_handlers = {name: object() for name in handlers}
        self.scripts = []
        self.responses = []
        self.error = error

    def run_script(self, script):
        if self.error:
            raise self.error
        self.scripts.append(script)

    def respond_info(self, message):
        self.responses.append(message)


class FakeReactor:
    def __init__(self):
        self.callbacks = []

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def run(self):
        for callback in self.callbacks:
            callback(0.0)


def make_box(**gcode):
    box = Box.__new__(Box)
    box.gcode = FakeGcode(**gcode)
    box.reactor = FakeReactor()
    box.notify_macro = "_BOX_NOTIFY"
    return box


def test_event_runs_the_macro_after_the_caller():
    box = make_box()

    assert box.notify("clog", "CFS clog", 'Likely "clog" here') is True
    assert box.gcode.scripts == []

    box.reactor.run()
    assert box.gcode.scripts == [
        '_BOX_NOTIFY EVENT=clog TITLE="CFS clog" '
        'MESSAGE="Likely \'clog\' here"']


def test_no_macro_no_notification():
    box = make_box(handlers=())

    assert box.notify("clog", "CFS clog", "x") is False
    assert box.reactor.callbacks == []


def test_empty_option_turns_it_off():
    box = make_box()
    box.notify_macro = ""

    assert box.notify("runout", "CFS runout", "x") is False


def test_failing_macro_never_raises():
    box = make_box(error=RuntimeError("macro error"))

    box.notify("cfs_error", "CFS error", "x")
    box.reactor.run()


def test_whitespace_and_newlines_are_flattened():
    box = make_box()

    box.notify("low_filament", "CFS", "a\n  b\tc")
    box.reactor.run()

    assert box.gcode.scripts[0].endswith('MESSAGE="a b c"')


def test_low_filament_warning_is_notified_once():
    mapping = BoxPrintMapping.__new__(BoxPrintMapping)
    mapping.gcode = FakeGcode()
    calls = []
    mapping.box = SimpleNamespace(
        slot_label=lambda slot: "Box 1, slot %d" % (slot + 1),
        notify=lambda *args: calls.append(args))

    mapping._notify([
        {"kind": "low_filament", "tool": 0, "slot": 1, "needed_m": 12.0,
         "remaining_m": 6.0},
        {"kind": "material_variant", "tool": 1, "slot": 2,
         "tool_material": "PLA", "slot_material": "PLA-CF"},
    ])

    assert len(mapping.gcode.responses) == 2
    assert len(calls) == 1
    event, title, message = calls[0]
    assert event == "low_filament"
    assert "T0 needs about 12.0 m" in message
    assert "PLA-CF" not in message


def test_no_low_filament_no_notification():
    mapping = BoxPrintMapping.__new__(BoxPrintMapping)
    mapping.gcode = FakeGcode()
    calls = []
    mapping.box = SimpleNamespace(
        slot_label=lambda slot: "Box 1, slot %d" % (slot + 1),
        notify=lambda *args: calls.append(args))

    mapping._notify([])

    assert calls == []
