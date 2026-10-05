"""RFID spool estimates: per-bay for tags without a serial (K2-OpenHost)."""

import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import Box, BoxError, BoxSnapshot, BoxStore  # noqa: E402


class FakeGcmd:
    def __init__(self, params):
        self.params = params

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))

    def error(self, message):
        return BoxError(message)


def make_box(tmp_path):
    box = Box.__new__(Box)
    box.store = BoxStore(str(tmp_path / "filament_box.json"))
    box.drivers = {1: object()}
    box.rfid_spools, box.rfid_percent, box.rfid_reported_percent = {}, {}, {}
    box.rfid_estimate_dirty = False
    return box




class SpoolGcmd(FakeGcmd):
    def get_float(self, name, default=None, minval=None, maxval=None):
        return float(self.params.get(name, default))


GREY_PLA = {"supplier": "1B3D", "mat_id": "105628", "number": "000001",
            "color": "0B1BEC6", "len": "0330", "reserve": "000000"}
FULL_MM = 330000.0


def spool_box(tmp_path, state="standby", loaded=None):
    box = make_box(tmp_path)
    box.messages = []
    box._info = lambda responder, msg: box.messages.append(msg)
    box.gcode = object()
    box.printer = types.SimpleNamespace(
        lookup_object=lambda name, default=None: (
            types.SimpleNamespace(state=state) if name == "print_stats" else default))
    box.snapshot = BoxSnapshot(loaded_slot=loaded)
    box.rfid_spools = {}
    return box


def use(box, slot, remaining_mm):
    box.rfid_spools[slot]["remaining_mm"] = remaining_mm
    box.rfid_percent[slot] = 100.0 * remaining_mm / FULL_MM
    box.rfid_estimate_dirty = True
    box._persist_rfid_estimates(force=True)


def remove(box, slot):
    # what _rfid_removed does to the estimate state
    box.rfid_spools.pop(slot, None)
    box.rfid_percent.pop(slot, None)
    box._clear_rfid_slot_key(slot)
    box._persist_rfid_estimates(force=True)


def test_reinserted_generic_spool_starts_fresh_in_any_slot(tmp_path):
    box = spool_box(tmp_path)
    box._remember_rfid_spool(1, GREY_PLA)
    assert box.rfid_percent[1] == 100.0
    use(box, 1, 1059.0)
    old_key = box.rfid_spools[1]["key"]
    remove(box, 1)
    assert old_key not in box.store.setting("rfid_estimates")  # pruned
    box._remember_rfid_spool(3, GREY_PLA)  # same tag, another bay
    assert box.rfid_percent[3] == 100.0
    box._remember_rfid_spool(1, GREY_PLA)  # same tag back in its bay
    assert box.rfid_percent[1] == 100.0
    assert box.rfid_spools[1]["key"] != box.rfid_spools[3]["key"]


def test_spool_staying_in_the_bay_keeps_its_estimate(tmp_path):
    box = spool_box(tmp_path)
    box._remember_rfid_spool(1, GREY_PLA)
    use(box, 1, FULL_MM / 2)
    key = box.rfid_spools[1]["key"]
    box._remember_rfid_spool(1, GREY_PLA)  # manual reread, no removal
    assert box.rfid_spools[1]["key"] == key
    assert box.rfid_percent[1] == pytest.approx(50.0)
    # restart: a new Box on the same state file restores the bay
    again = spool_box(tmp_path)
    again.set_profile(1, {"material": "PLA", "color": "#B1BEC6", "source": "rfid"})
    assert again._restore_cached_rfid_slot(1)
    assert again.rfid_percent[1] == pytest.approx(50.0)


def test_identical_spools_side_by_side_are_independent(tmp_path):
    box = spool_box(tmp_path)
    box._remember_rfid_spool(1, GREY_PLA)
    box._remember_rfid_spool(3, GREY_PLA)
    use(box, 1, 1059.0)
    assert box.rfid_percent[3] == 100.0
    saved = box.store.setting("rfid_estimates")
    assert saved[box.rfid_spools[3]["key"]]["remaining_mm"] == FULL_MM


def test_real_serial_follows_the_spool_across_slots(tmp_path):
    box = spool_box(tmp_path)
    fields = dict(GREY_PLA, number="123456")
    box._remember_rfid_spool(1, fields)
    box.rfid_spools[1]["remaining_mm"] = FULL_MM / 4
    box.rfid_estimate_dirty = True
    box._persist_rfid_estimates(force=True)
    remove(box, 1)
    box._remember_rfid_spool(3, fields)
    assert box.rfid_percent[3] == pytest.approx(25.0)


def test_legacy_low_estimate_in_place_gets_a_hint(tmp_path):
    box = spool_box(tmp_path)
    fingerprint = box._rfid_spool_fingerprint(GREY_PLA)
    box._set_rfid_slot_key(3, fingerprint)  # key saved by the earlier release
    box.store.set_setting("rfid_estimates", {
        fingerprint: {"total_mm": FULL_MM, "remaining_mm": 1059.0}})
    box._remember_rfid_spool(3, GREY_PLA)
    assert box.rfid_percent[3] == pytest.approx(0.321, abs=1e-3)
    assert any("_BOX_RFID_SPOOL_NEW SLOT=3" in m for m in box.messages)


def test_spool_new_command(tmp_path):
    box = spool_box(tmp_path)
    fingerprint = box._rfid_spool_fingerprint(GREY_PLA)
    box._set_rfid_slot_key(1, fingerprint)
    box._set_rfid_slot_key(3, fingerprint)
    box.store.set_setting("rfid_estimates", {
        fingerprint: {"total_mm": FULL_MM, "remaining_mm": 1059.0}})
    box._remember_rfid_spool(1, GREY_PLA)
    box._remember_rfid_spool(3, GREY_PLA)  # legacy: both share one key
    box.cmd_rfid_spool_new(SpoolGcmd({"SLOT": 3}))
    assert box.rfid_percent[3] == 100.0
    assert box.rfid_percent[1] == pytest.approx(0.321, abs=1e-3)
    assert ":spool:" in box.rfid_spools[3]["key"]
    box.cmd_rfid_spool_new(SpoolGcmd({"SLOT": 1, "REMAINING": 50}))
    assert box.rfid_percent[1] == 50.0
    saved = box.store.setting("rfid_estimates")
    assert saved[box.rfid_spools[1]["key"]]["remaining_mm"] == pytest.approx(FULL_MM / 2)


def test_spool_new_refusals(tmp_path):
    box = spool_box(tmp_path)
    with pytest.raises(BoxError):
        box.cmd_rfid_spool_new(SpoolGcmd({"SLOT": 16}))  # external slot
    with pytest.raises(BoxError):
        box.cmd_rfid_spool_new(SpoolGcmd({"SLOT": 2}))  # no RFID spool
    box = spool_box(tmp_path, state="printing", loaded=3)
    box._remember_rfid_spool(3, GREY_PLA)
    with pytest.raises(BoxError):
        box.cmd_rfid_spool_new(SpoolGcmd({"SLOT": 3}))  # feeding the print
