"""Box changes taken over from Jacob10383 k2-plus-custom-firmware (2026-10-01..06).

- service moves (wastebin, clean, cut, retry tour) work with Z unhomed;
- filament_retry_moves is configurable and checked against the travel;
- clog detection can be turned off, threshold configurable, saved switch.
"""

import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box as box_module  # noqa: E402
from extras.box import Box  # noqa: E402


class ConfigError(Exception):
    pass


# --- filament_retry_moves -----------------------------------------------------


def test_retry_moves_parse():
    moves = box_module._parse_retry_moves("Y300, X300, Y50 X60", ConfigError)
    assert moves == ({"y": 300.0}, {"x": 300.0}, {"y": 50.0, "x": 60.0})


@pytest.mark.parametrize("text", ["Z10", "Y", "X1 X2", "Yabc", "Y300,,X1"])
def test_retry_moves_reject_bad_entries(text):
    with pytest.raises(ConfigError, match="filament_retry_moves"):
        box_module._parse_retry_moves(text, ConfigError)


class Toolhead:
    def __init__(self, homed="xyz", position=(0.0, 0.0, 0.0, 0.0)):
        self.homed = homed
        self.position = list(position)
        self.waits = 0

    def get_status(self, eventtime):
        return {
            "homed_axes": self.homed,
            "axis_minimum": (-13.0, -6.5, 0.0, 0.0),
            "axis_maximum": (302.0, 332.0, 300.0, 0.0),
        }

    def get_position(self):
        return list(self.position)

    def wait_moves(self):
        self.waits += 1


class Printer:
    def __init__(self, toolhead):
        self.objects = {"toolhead": toolhead}
        self.manual_moves = []
        printer = self
        self.objects["extended_zone_transform"] = types.SimpleNamespace(
            manual_move=lambda coord, speed: printer.manual_moves.append(
                (tuple(coord), speed)))
        self.objects["gcode_move"] = types.SimpleNamespace(
            get_status=lambda e: {"gcode_position": toolhead.position})

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def config_error(self, msg):
        return ConfigError(msg)


class GCode:
    def __init__(self):
        self.scripts = []

    def run_script_from_command(self, script):
        self.scripts.append(script)


def make_box(homed="xyz", position=(0.0, 0.0, 5.0, 0.0), moves="Y300, X300"):
    box = Box.__new__(Box)
    box.toolhead = Toolhead(homed, position)
    box.printer = Printer(box.toolhead)
    box.reactor = types.SimpleNamespace(monotonic=lambda: 0.0)
    box.gcode = GCode()
    box.wastebin_x, box.wastebin_y = 124.0, 329.0
    box.travel_velocity = 18000.0
    box.filament_retry_moves = box_module._parse_retry_moves(moves, ConfigError)
    box._info = lambda gcmd, msg: None
    return box


def test_k2_pro_retry_moves_fit_the_travel():
    make_box(moves="Y300, X300, Y50, X50")._check_retry_moves()


def test_k2_plus_default_is_rejected_on_the_k2_pro_travel():
    box = make_box(moves=box_module.DEFAULT_FILAMENT_RETRY_MOVES)
    with pytest.raises(ConfigError, match="Y350 is outside the Y travel"):
        box._check_retry_moves()


# --- service moves --------------------------------------------------------------


def test_service_move_with_z_homed_is_a_g0():
    box = make_box(homed="xyz")
    box._service_move(x=124.0, y=329.0, velocity=18000.0)
    assert box.gcode.scripts == ["G0 X124 Y329 F18000"]
    assert box.printer.manual_moves == []


def test_service_move_never_uses_exponent_form():
    box = make_box(homed="xyz")
    box._service_move(y=0.00001, velocity=600.0)
    assert box.gcode.scripts == ["G0 Y0 F600"]


def test_service_move_with_z_unhomed_goes_through_the_zone_transform():
    box = make_box(homed="xy")
    box._service_move(x=124.0, y=329.0, velocity=18000.0)
    assert box.gcode.scripts == []
    # Same X/Y target, mm/s, through the extended zone routing.
    assert box.printer.manual_moves == [((124.0, 329.0), 300.0)]


@pytest.mark.parametrize("homed", ["xyz", "xy"])
def test_wastebin_target_is_the_configured_position(homed, monkeypatch):
    box = make_box(homed=homed, position=(10.0, 10.0, 5.0, 0.0))
    monkeypatch.setattr(box_module, "save_motion_limits", lambda *a, **k: None)
    monkeypatch.setattr(
        box_module, "restore_motion_limits", lambda *a, **k: None)
    box.move_to_wastebin()
    if homed == "xyz":
        assert box.gcode.scripts[-1] == "G0 X124 Y329 F18000"
    else:
        assert box.printer.manual_moves[-1] == ((124.0, 329.0), 300.0)


@pytest.mark.parametrize("homed", ["xyz", "xy"])
def test_already_at_the_wastebin_does_not_move(homed):
    box = make_box(homed=homed, position=(124.0, 329.0, 5.0, 0.0))
    box.move_to_wastebin()
    assert box.gcode.scripts == [] and box.printer.manual_moves == []


# --- clog detection -------------------------------------------------------------


class Store:
    def __init__(self):
        self.data = {}
        self.saves = 0

    def setting(self, name, default=None):
        return self.data.get(name, default)

    def set_setting(self, name, value):
        self.data[name] = value
        self.saves += 1


class Gcmd:
    def __init__(self, **params):
        self.params = params
        self.messages = []

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))


def make_clog_box(default=True, length=80.0):
    box = Box.__new__(Box)
    box.store = Store()
    box.clog_detection_default = default
    box.clog_extruder_length = length
    box.clog_baseline = {"extruder": 0.0, "encoder": 0.0,
                         "last_extruder": 100.0, "last_encoder": 5.0}
    box.clog_event_count = 0
    box.last_clog = {}
    box.runout_active = False
    box.snapshot = types.SimpleNamespace(tracking=True)
    box.messages = []
    box._info = lambda gcmd, msg: box.messages.append(msg)
    return box


def test_config_value_is_the_default_until_saved():
    assert make_clog_box(default=True).clog_detection is True
    assert make_clog_box(default=False).clog_detection is False


def test_switch_saves_and_overrides_the_default():
    box = make_clog_box(default=True)
    box.cmd_clog_detection(Gcmd(ENABLE=0))
    assert box.clog_detection is False
    assert box.store.data == {"clog_detection_enabled": False}
    assert box.clog_baseline is None
    assert box.messages == ["Clog detection disabled"]
    box.cmd_clog_detection(Gcmd(ENABLE=1))
    assert box.clog_detection is True


def test_status_uses_the_configured_threshold():
    status = make_clog_box(length=120.0)._clog_status()
    assert status["enabled"] is True
    assert status["extruder_threshold_mm"] == 120.0
    # 100 mm fed with 5 mm of refill: below 120, not a clog.
    assert status["triggered"] is False and status["state"] == "active"
    status = make_clog_box(length=80.0)._clog_status()
    assert status["triggered"] is True and status["state"] == "triggered"


def test_status_reports_disabled():
    box = make_clog_box()
    box.store.set_setting("clog_detection_enabled", False)
    status = box._clog_status()
    assert status["state"] == "disabled" and status["enabled"] is False
