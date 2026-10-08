import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_protocol
from extras.box import Box, BoxSnapshot, BoxStore


class FakeDriver:
    def __init__(self, values):
        self.values = values
        self.masks = []

    def query_rfid_remaining(self, mask, timeout=None):
        self.masks.append(mask)
        return SimpleNamespace(status=box_protocol.STATUS_OK,
                               values=dict(self.values))


def make_box(tmp_path, profiles, slot_keys, values=None):
    box = Box.__new__(Box)
    box.store = BoxStore(str(tmp_path / "filament_box.json"))
    box.store.set_setting("rfid_slot_keys", slot_keys)
    box.profile = lambda slot: profiles.get(slot, {})
    box._runtime_slot_key = lambda slot: slot
    box._address_slot = lambda slot: (slot // 4 + 1, slot % 4)
    box._global_slot = lambda address, local: (address - 1) * 4 + local
    box.rfid_live_slots = set()
    box.rfid_spools = {}
    box.rfid_percent = {}
    box.rfid_reported_percent = {}
    box.rfid_estimate_dirty = False
    box.operation_depth = 0
    box.snapshot = BoxSnapshot(data_ready=True, slot_mask=0x0F)
    box.drivers = {1: FakeDriver(values or {})}
    box._persist_rfid_estimates = lambda force=False: None
    return box


def test_restored_rfid_bay_is_polled_again(tmp_path):
    box = make_box(
        tmp_path,
        profiles={2: {"source": "rfid"}},
        slot_keys={"2": "tag:BAMBU:PC:8BD9CFFC"})

    assert box._resume_rfid_polling(2) is True
    assert box.rfid_live_slots == {2}


@pytest.mark.parametrize("profile, keys", [
    ({"source": "manual"}, {"1": "tag:X"}),
    ({"source": "library"}, {"1": "tag:X"}),
    ({"source": "rfid"}, {}),
    ({}, {}),
])
def test_other_bays_stay_unpolled(tmp_path, profile, keys):
    box = make_box(tmp_path, profiles={1: profile}, slot_keys=keys)

    assert box._resume_rfid_polling(1) is False
    assert box.rfid_live_slots == set()


def test_periodic_refresh_applies_the_cfs_percentage_after_restart(tmp_path):
    box = make_box(
        tmp_path,
        profiles={0: {"source": "rfid"}, 2: {"source": "rfid"}},
        slot_keys={"0": "tag:BAMBU:PETG HF:763EA0C6",
                   "2": "tag:BAMBU:PC:8BD9CFFC"},
        values={"A": 12, "B": 255, "C": 60, "D": 255})
    # Slot 2 restored with a saved estimate, slot 0 without one.
    box.rfid_spools[2] = {"total_mm": 345000.0, "remaining_mm": 227700.0}
    box._resume_rfid_polling(0)
    box._resume_rfid_polling(2)

    box._refresh_rfid_remaining()

    assert box.drivers[1].masks == [0x05]
    assert box.rfid_percent[0] == 12.0
    # The CFS percentage caps the saved estimate: 60 % of 345 m < 227.7 m.
    assert box.rfid_spools[2]["remaining_mm"] == pytest.approx(207000.0)
    assert box.rfid_percent[2] == pytest.approx(60.0)
