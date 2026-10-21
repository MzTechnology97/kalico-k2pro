"""Slot map chosen in OrcaSlicer (K2OH_TOOL_MAP) at an ordinary print start."""

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_print_mapping as mapping_module  # noqa: E402
from extras.box_change import BoxChangeEngine  # noqa: E402

SLOTS = [
    {"index": 0, "present": True, "material": "PETG-CF", "color": "#000000"},
    {"index": 1, "present": True, "material": "PLA", "color": "#B1BEC6"},
    {"index": 2, "present": False, "material": "", "color": ""},
    {"index": 3, "present": True, "material": "PLA", "color": "#FF0000"},
    {"index": 4, "present": True, "external": True, "material": "", "color": ""},
]
FOOTER = (
    "; filament used [mm] = 1000.00,0.00,500.00\n"
    "; filament_type = PLA;PETG;PLA\n"
    "; filament_colour = #FF0000;#00FF00;#B1BEC6\n"
    '; filament_settings_id = "Bambu PLA Basic @K2";"X";"Y"\n'
)


class Gcode:
    def __init__(self):
        self.messages = []

    def respond_info(self, msg):
        self.messages.append(msg)

    def error(self, msg):
        return RuntimeError(msg)


class Box:
    observation_mode = False
    print_info = None
    external_slot = 4

    def __init__(self):
        self.registered = []

    def slot_label(self, slot):
        return "Box 1, slot %d" % (slot + 1)

    def is_valid_slot(self, slot):
        return 0 <= slot <= 4

    def _register_tools(self, tools):
        self.registered.extend(tools)

    def _mapped_slot(self, tool):
        return tool


class Printer:
    def __init__(self, objects):
        self.objects = objects

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def get_reactor(self):
        return types.SimpleNamespace(monotonic=lambda: 0.0)


def make(tmp_path, head_line, auto=True):
    gcode_dir = tmp_path / "gcodes"
    gcode_dir.mkdir()
    path = gcode_dir / "plate.gcode"
    lines = ["; HEADER_BLOCK_START\n"]
    if head_line:
        lines.append(head_line + "\n")
    lines += ["G28\n", "T0\n", "T2\n", FOOTER]
    path.write_text("".join(lines))
    sd = types.SimpleNamespace(current_file=types.SimpleNamespace(name=str(path)),
                               sdcard_dirname=str(gcode_dir))
    mapping = mapping_module.BoxPrintMapping.__new__(mapping_module.BoxPrintMapping)
    mapping.printer = Printer({"virtual_sdcard": sd})
    mapping.gcode = Gcode()
    mapping.box = Box()
    engine = BoxChangeEngine.__new__(BoxChangeEngine)
    engine.box = mapping.box
    engine.reset_print_mapping()
    mapping.change_engine = engine
    mapping.auto_map_prints = auto
    mapping.auto_map_block_unresolved = False
    mapping._explicit_start_in_progress = False
    mapping._base_get_status = lambda eventtime: {"slots": SLOTS}
    mapping.box.runout_swap_enabled = False
    mapping.box.notify = None
    return mapping


def test_read_tool_map(tmp_path):
    path = tmp_path / "a.gcode"
    path.write_text("; x\n; K2OH_TOOL_MAP = 0:3, 2:1\nG28\n")
    assert mapping_module.read_tool_map(str(path)) == {0: 3, 2: 1}
    path.write_text("; x\nG28\n")
    assert mapping_module.read_tool_map(str(path)) is None
    path.write_text("; K2OH_TOOL_MAP = 0:3,0:1\n")
    try:
        mapping_module.read_tool_map(str(path))
    except ValueError:
        pass
    else:
        raise AssertionError("a duplicate tool was accepted")


def test_slicer_map_replaces_the_automatic_one(tmp_path):
    mapping = make(tmp_path, "; K2OH_TOOL_MAP = 0:3,1:0,2:1")
    mapping._handle_file_loaded()
    # Only the used tools (T0, T2) are installed; T1 does not print.
    assert mapping.change_engine.tool_map == {0: 3, 2: 1}
    assert mapping.auto_mapping["source"] == "slicer"
    assert mapping.auto_mapping["state"] == "active"
    assert mapping.gcode.messages == []


def test_slicer_map_works_without_auto_mapping(tmp_path):
    mapping = make(tmp_path, "; K2OH_TOOL_MAP = 0:3,2:1", auto=False)
    mapping._handle_file_loaded()
    assert mapping.change_engine.tool_map == {0: 3, 2: 1}


def test_short_map_falls_back_to_automatic(tmp_path):
    mapping = make(tmp_path, "; K2OH_TOOL_MAP = 0:3")
    mapping._handle_file_loaded()
    assert mapping.auto_mapping["source"] == "auto"
    assert "T2 has no slot" in mapping.gcode.messages[0]
    assert "automatic" in mapping.gcode.messages[0]


def test_empty_slot_falls_back_to_automatic(tmp_path):
    mapping = make(tmp_path, "; K2OH_TOOL_MAP = 0:2,2:1")
    mapping._handle_file_loaded()
    assert mapping.auto_mapping["source"] == "auto"
    assert "Box 1, slot 3 is empty or offline" in mapping.gcode.messages[0]


def test_without_map_or_auto_nothing_is_installed(tmp_path):
    mapping = make(tmp_path, None, auto=False)
    mapping._handle_file_loaded()
    assert mapping.change_engine.tool_map == {}
    assert mapping.auto_mapping["state"] == "idle"
