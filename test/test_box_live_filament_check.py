"""Live filament check during a print (K2-OpenHost).

Pure checks plus the box_print_mapping timer path with fakes.
"""

import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_print_mapping as bpm  # noqa: E402
from extras.box_auto_mapping import live_filament_check  # noqa: E402


def tool(tool_id, length_mm, material="PLA", color="#FFFFFF"):
    return {"tool": tool_id, "material": material, "color": color,
            "length_mm": length_mm}


def slot(index, remaining_m, material="PLA", color="#FFFFFF"):
    return {"index": index, "material": material, "color": color,
            "present": True, "external": False, "rfid_remaining_m": remaining_m}


def test_single_tool_uses_the_filament_already_used():
    tools = [tool(0, 100000)]  # 100 m in the slicer -> 111 m with margin
    slots = [slot(1, 50.0)]
    [item] = live_filament_check(tools, slots, {0: 1}, False, used_m=40.0,
                                 progress=0.9)
    assert item["needed_m"] == 71.0 and not item["estimated"]
    assert item["available_m"] == 50.0 and item["short"]
    [item] = live_filament_check(tools, slots, {0: 1}, False, used_m=70.0,
                                 progress=0.0)
    assert item["needed_m"] == 41.0 and not item["short"]


def test_several_tools_scale_by_progress_and_are_estimated():
    tools = [tool(0, 90000), tool(1, 9000, color="#000000")]
    slots = [slot(0, 30.0), slot(1, 100.0, color="#000000")]
    items = live_filament_check(tools, slots, {0: 0, 1: 1}, False,
                                used_m=0.0, progress=0.5)
    by_tool = {item["tool"]: item for item in items}
    assert by_tool[0]["needed_m"] == 50.0 and by_tool[0]["estimated"]
    assert by_tool[0]["short"] and not by_tool[1]["short"]


def test_runout_swap_counts_identical_spools():
    tools = [tool(0, 100000)]
    slots = [slot(0, 50.0), slot(2, 80.0)]
    [item] = live_filament_check(tools, slots, {0: 0}, True, used_m=0.0,
                                 progress=0.0)
    assert item["available_m"] == 130.0 and item["includes_swap"]
    assert not item["short"]


def test_unknown_remaining_is_never_short():
    [item] = live_filament_check([tool(0, 100000)], [slot(0, None)], {0: 0},
                                 False, used_m=0.0, progress=0.0)
    assert item["available_m"] is None and not item["short"]


def test_unmapped_or_unknown_length_is_skipped():
    tools = [tool(0, None), tool(1, 5000)]
    assert live_filament_check(tools, [slot(0, 1.0)], {0: 0}, False, 0, 0) == []


# --- the timer path ------------------------------------------------------------


class Reactor:
    NEVER = 9.9e99

    def __init__(self):
        self.timers = {}

    def register_timer(self, callback, when=None):
        return callback

    def update_timer(self, timer, when):
        self.timers[timer] = when

    def monotonic(self):
        return 10.0


def make(state="printing", used_mm=40000.0, remaining_m=50.0, tool_map=None):
    m = bpm.BoxPrintMapping.__new__(bpm.BoxPrintMapping)
    m.messages = []
    m.gcode = SimpleNamespace(respond_info=m.messages.append)
    m.stats = SimpleNamespace(
        state=state, get_status=lambda e: {"filament_used": used_mm})
    m.sd = SimpleNamespace(
        current_file=SimpleNamespace(name="/gcodes/part.gcode"),
        get_status=lambda e: {"progress": 0.4})
    m.printer = SimpleNamespace(
        lookup_object=lambda name, default=None: {
            "print_stats": m.stats, "virtual_sdcard": m.sd}.get(name, default),
        get_reactor=Reactor,
    )
    m.box = SimpleNamespace(
        print_info={"filename": "part.gcode", "tools": [tool(0, 100000)]},
        snapshot=SimpleNamespace(loaded_slot=1),
        runout_swap_enabled=False,
        slot_label=lambda index: "Box 1, slot %d" % (index + 1),
    )
    m.change_engine = SimpleNamespace(tool_map=tool_map or {})
    m._base_get_status = lambda e: {"slots": [slot(1, remaining_m)]}
    m.mapping_warnings = [{"kind": "low_filament", "tool": 0, "slot": 1,
                           "needed_m": 111.0, "remaining_m": 50.0}]
    m.filament_check = {"active": False, "tools": []}
    m._live_warned = set()
    m._live_tools_cache = (None, [])
    return m


def test_short_tool_warns_once_and_is_published():
    m = make()
    m._run_live_check(10.0)
    status = m.filament_check
    assert status["active"] and status["tools"][0]["short"]
    kinds = [w["kind"] for w in m.mapping_warnings]
    assert kinds == ["low_filament", "low_filament_live"]
    assert len(m.messages) == 1 and "still needs about 71.0 m" in m.messages[0]
    m._run_live_check(70.0)
    assert len(m.messages) == 1  # once per tool and slot
    assert [w["kind"] for w in m.mapping_warnings].count("low_filament_live") == 1


def test_resolved_shortage_leaves_the_warnings():
    m = make()
    m._run_live_check(10.0)
    m._base_get_status = lambda e: {"slots": [slot(1, 300.0)]}
    m._run_live_check(70.0)
    assert [w["kind"] for w in m.mapping_warnings] == ["low_filament"]
    assert not m.filament_check["tools"][0]["short"]


def test_idle_clears_the_check_and_paused_keeps_it():
    m = make()
    m._run_live_check(10.0)
    m.stats.state = "paused"
    m._run_live_check(70.0)
    assert m.filament_check["active"]
    m.stats.state = "complete"
    m._run_live_check(130.0)
    assert m.filament_check == {"active": False, "tools": []}


def test_explicit_map_wins_over_the_loaded_slot():
    m = make(tool_map={0: 3})
    m._base_get_status = lambda e: {"slots": [slot(1, 1.0), slot(3, 500.0)]}
    m._run_live_check(10.0)
    assert m.filament_check["tools"][0]["slot"] == 3
    assert m.messages == []
