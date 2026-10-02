import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import Box, BoxSnapshot, BoxStore


class FakePrintStats:
    def __init__(self, state="printing", filament_used=0.0):
        self.state = state
        self.filament_used = filament_used

    def get_status(self, _eventtime):
        return {
            "state": self.state,
            "filament_used": self.filament_used,
        }


class FakePrinter:
    def __init__(self, stats):
        self.stats = stats

    def lookup_object(self, name, default=None):
        if name == "print_stats":
            return self.stats
        return default


def make_tracking_box(tmp_path):
    stats = FakePrintStats()
    box = Box.__new__(Box)
    box.printer = FakePrinter(stats)
    box.store = BoxStore(str(tmp_path / "filament_box.json"))
    box.rfid_spools = {
        1: {
            "key": "spool:test",
            "fingerprint": "spool:test",
            "total_mm": 100000.0,
            "remaining_mm": 50000.0,
        }
    }
    box.rfid_percent = {1: 50.0}
    box.rfid_estimate_dirty = False
    box.last_rfid_estimate_save = 999999.0
    box.rfid_last_filament_used = None
    box.rfid_last_print_state = None
    box.rfid_last_usage_slot = None
    return box, stats


def test_print_usage_decrements_active_rfid_spool(tmp_path):
    box, stats = make_tracking_box(tmp_path)
    snap = BoxSnapshot(loaded_slot=1)

    stats.filament_used = 100.0
    box._track_rfid_usage(1.0, snap)

    stats.filament_used = 1100.0
    box._track_rfid_usage(2.0, snap)

    assert box.rfid_spools[1]["remaining_mm"] == 49000.0
    assert box.rfid_percent[1] == 49.0
    assert box.rfid_estimate_dirty is True


def test_tool_change_rebases_usage_instead_of_charging_wrong_spool(tmp_path):
    box, stats = make_tracking_box(tmp_path)
    box.rfid_spools[2] = {
        "key": "spool:second",
        "fingerprint": "spool:second",
        "total_mm": 100000.0,
        "remaining_mm": 80000.0,
    }
    box.rfid_percent[2] = 80.0

    stats.filament_used = 100.0
    box._track_rfid_usage(1.0, BoxSnapshot(loaded_slot=1))
    stats.filament_used = 600.0
    box._track_rfid_usage(2.0, BoxSnapshot(loaded_slot=2))

    assert box.rfid_spools[1]["remaining_mm"] == 50000.0
    assert box.rfid_spools[2]["remaining_mm"] == 80000.0

    stats.filament_used = 1600.0
    box._track_rfid_usage(3.0, BoxSnapshot(loaded_slot=2))
    assert box.rfid_spools[2]["remaining_mm"] == 79000.0


def test_rfid_estimate_persists_across_store_reload(tmp_path):
    box, _stats = make_tracking_box(tmp_path)
    box.rfid_spools[1]["remaining_mm"] = 43210.0
    box.rfid_estimate_dirty = True
    box._persist_rfid_estimates(force=True)

    reloaded = BoxStore(str(tmp_path / "filament_box.json"))
    saved = reloaded.setting("rfid_estimates", {})

    assert saved["spool:test"]["total_mm"] == 100000.0
    assert saved["spool:test"]["remaining_mm"] == 43210.0


def test_runout_prefers_compatible_spool_with_lowest_remaining():
    box = Box.__new__(Box)
    box.drivers = {1: object()}
    box.runout_active = False
    box.runout_origin = None

    slots = [
        {"index": 1, "present": True, "material": "PETG", "color": "#000000",
         "rfid_percent": 25.0, "rfid_active": True},
        {"index": 2, "present": True, "material": "PETG", "color": "#000000",
         "rfid_percent": 70.0, "rfid_active": True},
        {"index": 3, "present": True, "material": "PETG", "color": "#000000",
         "rfid_percent": 12.0, "rfid_active": True},
        {"index": 0, "present": True, "material": "PLA", "color": "#000000",
         "rfid_percent": 5.0, "rfid_active": True},
    ]

    status = box._runout_status(slots, BoxSnapshot(loaded_slot=1))

    assert status["chain"] == [3, 2]
    assert status["sequence"] == [1, 3, 2]
    assert status["strategy"] == "lowest_remaining_first"