"""CFS live remaining (type-4 accounting in the CFS) versus the local estimate.

On firmware box_cfs_runtime identifies as live (LIVE_REMAINING_FIRMWARE) the
CFS percentage leads and the local consumption only fills its 1 % steps; on
every other firmware the CFS value can only lower the local estimate.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_cfs_runtime as runtime
from extras.box import Box, BoxSnapshot, BoxStore

LIVE = {"magic": "K2OH", "format": 1, "variant": 4, "bench_level": 0,
        "flags": 2, "revision": "v323-cfg5t", "version": "cfs0_000_153"}
CFG4 = dict(LIVE, revision="v323-cfg4")


class Stats:
    def __init__(self):
        self.state = "printing"
        self.filament_used = 0.0

    def get_status(self, _eventtime):
        return {"state": self.state, "filament_used": self.filament_used}


class Runtime:
    def __init__(self, live_addresses):
        self.live_addresses = set(live_addresses)

    def live_remaining(self, address):
        return address in self.live_addresses


class Printer:
    def __init__(self, objects):
        self.objects = objects

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


def make_box(tmp_path, live=(1,), remaining_mm=31000.0):
    stats = Stats()
    box = Box.__new__(Box)
    box.printer = Printer({"print_stats": stats,
                           "box_cfs_runtime": Runtime(live)})
    box.store = BoxStore(str(tmp_path / "filament_box.json"))
    box.drivers = {1: object(), 2: object()}
    box.rfid_spools = {1: {"key": "spool:a", "fingerprint": "spool:a",
                           "total_mm": 100000.0,
                           "remaining_mm": remaining_mm}}
    box.rfid_percent = {1: remaining_mm / 1000.0}
    box.rfid_reported_percent = {}
    box.rfid_estimate_dirty = False
    box.last_rfid_estimate_save = 999999.0
    box.rfid_last_filament_used = None
    box.rfid_last_print_state = None
    box.rfid_last_usage_slot = None
    return box, stats


def test_live_cfs_value_leads_up_and_down(tmp_path):
    box, _ = make_box(tmp_path)
    box._apply_reported_remaining(1, 45)  # measured at load, above Kalico
    assert box.rfid_spools[1]["remaining_mm"] == 45000.0
    assert box.rfid_spools[1]["remaining_source"] == "cfs"
    box._apply_reported_remaining(1, 44)
    assert box.rfid_percent[1] == 44.0


def test_local_consumption_fills_the_steps_until_the_next_one(tmp_path):
    box, stats = make_box(tmp_path)
    box._apply_reported_remaining(1, 45)
    snap = BoxSnapshot(loaded_slot=1)
    box._track_rfid_usage(1.0, snap)
    stats.filament_used = 400.0
    box._track_rfid_usage(2.0, snap)
    assert box.rfid_spools[1]["remaining_mm"] == 44600.0
    box._apply_reported_remaining(1, 45)  # same step: local value kept
    assert box.rfid_spools[1]["remaining_mm"] == 44600.0
    box._apply_reported_remaining(1, 44)  # next step: restart from the CFS
    assert box.rfid_spools[1]["remaining_mm"] == 44000.0


def test_no_value_keeps_the_local_estimate(tmp_path):
    box, _ = make_box(tmp_path)
    box._apply_reported_remaining(1, 45)
    box._apply_reported_remaining(1, 255)  # CFS restarted, not read yet
    assert box.rfid_spools[1]["remaining_mm"] == 45000.0
    assert box.rfid_spools[1]["remaining_source"] == "kalico"


def test_other_firmware_can_only_lower(tmp_path):
    box, _ = make_box(tmp_path, live=())
    box._apply_reported_remaining(1, 45)
    assert box.rfid_spools[1]["remaining_mm"] == 31000.0
    box._apply_reported_remaining(1, 20)
    assert box.rfid_spools[1]["remaining_mm"] == 20000.0
    assert box.rfid_spools[1]["remaining_source"] == "kalico"


def test_option_off_keeps_the_local_estimate(tmp_path):
    box, _ = make_box(tmp_path)
    box.cfs_live_remaining = False
    box._apply_reported_remaining(1, 45)
    assert box.rfid_spools[1]["remaining_mm"] == 31000.0


def test_live_is_per_cfs(tmp_path):
    box, _ = make_box(tmp_path, live=(2,))
    box._apply_reported_remaining(1, 45)  # slot 1 is on CFS 1
    assert box.rfid_spools[1]["remaining_mm"] == 31000.0


def test_restart_does_not_reanchor_an_unchanged_value(tmp_path):
    box, stats = make_box(tmp_path)
    box._apply_reported_remaining(1, 45)
    box.rfid_spools[1]["remaining_mm"] = 44600.0
    box._persist_rfid_estimates(force=True)
    saved = BoxStore(str(tmp_path / "filament_box.json")).setting(
        "rfid_estimates")["spool:a"]
    assert saved["cfs_percent"] == 45
    # Klipper restart: the estimate comes back with its last CFS step.
    box.rfid_spools[1] = {"key": "spool:a", "total_mm": 100000.0,
                          "remaining_mm": saved["remaining_mm"],
                          "cfs_percent": saved["cfs_percent"]}
    box.rfid_reported_percent = {}
    box._apply_reported_remaining(1, 45)
    assert box.rfid_spools[1]["remaining_mm"] == 44600.0


def test_runtime_identifies_live_firmware():
    assert runtime.live_remaining_firmware(LIVE, 0xE5)
    assert not runtime.live_remaining_firmware(LIVE, 0xD7)
    assert not runtime.live_remaining_firmware(CFG4, 0xD7)
    assert not runtime.live_remaining_firmware(
        dict(LIVE, magic="OCFS", variant=2), 0xE5)
    assert not runtime.live_remaining_firmware(None, 0xE5)
