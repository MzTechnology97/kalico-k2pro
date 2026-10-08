import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import Box, BoxStore


class FakeGcmd:
    class error(Exception):
        pass

    def __init__(self, **params):
        self.params = params

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))

    def get_float(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name, default)
        if value is None:
            raise self.error("missing %s" % name)
        return float(value)


def make_box(tmp_path, default=330.0):
    box = Box.__new__(Box)
    box.store = BoxStore(str(tmp_path / "filament_box.json"))
    box.third_party_rfid_length_default = default
    box.rfid_spools = {}
    box.rfid_percent = {}
    box.rfid_estimate_dirty = False
    box.infos = []
    box._info = lambda gcmd, msg: box.infos.append(msg)
    box.persisted = 0

    def persist(force=False):
        box.persisted += 1

    box._persist_rfid_estimates = persist
    return box


def test_length_priority_tag_profile_setting_default(tmp_path):
    box = make_box(tmp_path)

    assert box._third_party_length_m({"filament_length_m": 250}, {}) == (
        250.0, "tag")
    assert box._third_party_length_m(
        {}, {"nominal_length_m": 400}) == (400.0, "profile")
    assert box._third_party_length_m({}, {}) == (330.0, "default")

    box.store.set_setting("third_party_rfid_length_m", 350.0)
    assert box._third_party_length_m({}, {}) == (350.0, "default")
    # The profile still wins over the general value.
    assert box._third_party_length_m(
        {}, {"nominal_length_m": 400}) == (400.0, "profile")


def test_zero_turns_the_default_off(tmp_path):
    box = make_box(tmp_path)
    box.store.set_setting("third_party_rfid_length_m", 0.0)

    assert box._third_party_length_m({}, {}) == (0, None)
    assert box._third_party_length_m(
        {}, {"nominal_length_m": 400}) == (400.0, "profile")


def test_invalid_saved_value_falls_back_to_box_cfg(tmp_path):
    box = make_box(tmp_path, default=300.0)
    box.store.set_setting("third_party_rfid_length_m", "abc")
    assert box.third_party_rfid_length_m == 300.0
    box.store.set_setting("third_party_rfid_length_m", 0.5)
    assert box.third_party_rfid_length_m == 300.0


def test_set_command_saves_and_rescales_default_spools(tmp_path):
    box = make_box(tmp_path)
    box.rfid_spools = {
        0: {"total_mm": 330000.0, "remaining_mm": 165000.0,
            "length_source": "default"},
        1: {"total_mm": 400000.0, "remaining_mm": 100000.0,
            "length_source": "profile"},
    }

    box.cmd_third_party_length(FakeGcmd(LENGTH_M=400))

    assert box.store.setting("third_party_rfid_length_m") == 400.0
    assert box.rfid_spools[0]["total_mm"] == 400000.0
    assert box.rfid_spools[0]["remaining_mm"] == pytest.approx(200000.0)
    assert box.rfid_percent[0] == pytest.approx(50.0)
    # A profile length is not the general value: untouched.
    assert box.rfid_spools[1]["total_mm"] == 400000.0
    assert box.rfid_spools[1]["remaining_mm"] == 100000.0
    assert box.persisted == 1
    assert "1 spool(s) updated" in box.infos[-1]


def test_reset_command_goes_back_to_box_cfg(tmp_path):
    box = make_box(tmp_path, default=330.0)
    box.store.set_setting("third_party_rfid_length_m", 400.0)

    box.cmd_third_party_length(FakeGcmd(RESET=1))

    assert box.third_party_rfid_length_m == 330.0
    assert "330 m" in box.infos[-1]


def test_set_command_rejects_sub_metre_lengths(tmp_path):
    box = make_box(tmp_path)
    with pytest.raises(FakeGcmd.error):
        box.cmd_third_party_length(FakeGcmd(LENGTH_M=0.5))


def test_set_to_zero_leaves_tracked_spools_alone(tmp_path):
    box = make_box(tmp_path)
    box.rfid_spools = {0: {"total_mm": 330000.0, "remaining_mm": 1000.0,
                           "length_source": "default"}}

    box.cmd_third_party_length(FakeGcmd(LENGTH_M=0))

    assert box.rfid_spools[0]["total_mm"] == 330000.0
    assert "off" in box.infos[-1]


def make_estimate_box(tmp_path, cfs_percent):
    box = make_box(tmp_path)
    box.rfid_spools = {2: {"key": "tag:BAMBU:PC:8BD9CFFC",
                           "total_mm": 330000.0, "remaining_mm": None}}

    def read_remaining(slot):
        if cfs_percent is not None:
            box._apply_reported_remaining(slot, cfs_percent)

    box.rfid_reported_percent = {}
    box._read_rfid_remaining = read_remaining
    return box


def test_new_third_party_spool_takes_the_cfs_percentage(tmp_path):
    box = make_estimate_box(tmp_path, cfs_percent=67)

    box._start_third_party_estimate(2, "default")

    spool = box.rfid_spools[2]
    assert spool["remaining_mm"] == pytest.approx(221100.0)
    assert box.rfid_percent[2] == pytest.approx(67.0)
    assert spool["length_source"] == "default"


def test_new_third_party_spool_without_cfs_value_starts_full(tmp_path):
    box = make_estimate_box(tmp_path, cfs_percent=None)

    box._start_third_party_estimate(2, "profile")

    assert box.rfid_spools[2]["remaining_mm"] == 330000.0
    assert box.rfid_percent[2] == 100.0
    assert box.persisted == 1


def test_saved_estimate_is_kept_and_capped_by_the_cfs(tmp_path):
    box = make_estimate_box(tmp_path, cfs_percent=50)
    box.rfid_spools[2]["remaining_mm"] = 100000.0

    box._start_third_party_estimate(2, "default")

    # min(saved 100 m, CFS 50 % of 330 m): the saved one is lower.
    assert box.rfid_spools[2]["remaining_mm"] == 100000.0


def test_spool_without_length_shows_the_cfs_percentage_only(tmp_path):
    box = make_estimate_box(tmp_path, cfs_percent=11)
    box.rfid_spools[2]["total_mm"] = None

    box._start_third_party_estimate(2, None)

    assert box.rfid_percent[2] == 11.0
    assert box.rfid_spools[2]["remaining_mm"] is None


def test_length_source_is_saved_with_the_estimate(tmp_path):
    box = make_box(tmp_path)
    box.rfid_spools = {0: {"key": "tag:BAMBU:PETG HF:763EA0C6",
                           "total_mm": 330000.0, "remaining_mm": 36300.0,
                           "length_source": "default"}}
    box.rfid_estimate_dirty = True

    Box._persist_rfid_estimates(box, force=True)

    saved = box.store.setting("rfid_estimates")["tag:BAMBU:PETG HF:763EA0C6"]
    assert saved == {"total_mm": 330000.0, "remaining_mm": 36300.0,
                     "length_source": "default"}
