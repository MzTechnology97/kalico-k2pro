import pathlib
import sys
import urllib.error
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box as box_module
from extras.box import Box
from extras.box_lane_data import MoonrakerDatabase

RECORD = "0" * 40
FIELDS = {"mat_id": "105628"}


class InsertReadingBox(Box):
    rfid_insert_reading_enabled = True


def make_box(profiles, active=()):
    box = InsertReadingBox.__new__(InsertReadingBox)
    box.rfid_live_slots = set()
    box.rfid_percent = {}
    box.rfid_reported_percent = {}
    box.rfid_spools = {}
    box.unknown_rfid = {}
    box.rfid_pending = set()
    box.rfid_snapshot = {}
    box.rfid_seen_invalid = set()
    box.rfid_fallback_tried = set()
    box.rfid_cleared_on_insert = set()
    box.rfid_insert_time = {}
    box.reactor = SimpleNamespace(monotonic=lambda: 1000.0)
    box.profiles = dict(profiles)
    box.profile = lambda slot: box.profiles.get(slot, {})
    box.clear_profile = lambda slot: box.profiles.pop(slot, None)
    box._defer_active_slot_clear = lambda slot: slot in active
    box._runtime_slot_key = lambda slot: slot
    box._invalidate_spoolman = lambda slot: None
    box.cache_records = {}

    def snapshot_cache(slot):
        if slot in box.cache_records:
            box.rfid_snapshot[slot] = box.cache_records[slot]

    box._snapshot_rfid_cache = snapshot_cache
    return box


def test_insertion_drops_the_previous_rfid_profile():
    box = make_box({2: {"source": "rfid", "name": "Bambulab PETG HF"}})

    box._rfid_inserted(2)

    assert 2 not in box.profiles
    assert box.rfid_cleared_on_insert == {2}
    assert box.rfid_pending == {2}
    assert box.rfid_insert_time == {2: 1000.0}


def test_insertion_keeps_a_manual_profile():
    box = make_box({1: {"source": "manual", "name": "PLA white"}})

    box._rfid_inserted(1)

    assert box.profiles[1]["name"] == "PLA white"
    assert box.rfid_cleared_on_insert == set()


def test_insertion_keeps_the_print_source():
    box = make_box({0: {"source": "rfid"}}, active={0})

    box._rfid_inserted(0)

    assert 0 in box.profiles


def make_read_box():
    box = make_box({})
    box.cache_records[3] = RECORD
    box._query_rfid_sample = lambda slot: (RECORD, FIELDS)
    box.applied = []

    def apply_record(slot, record, fields):
        box.applied.append(slot)
        return True

    box._apply_rfid_record = apply_record
    box._read_rfid_remaining = lambda slot: None
    return box


def test_same_spool_put_back_gets_its_profile_when_the_read_completes():
    box = make_read_box()
    box.profiles[3] = {"source": "rfid"}
    box._rfid_inserted(3)

    # Poll before the CFS read: the cache still holds the same record.
    assert box._read_rfid_result(3) == "stale"
    assert box.applied == []

    # The CFS reports the read done: the unchanged record is applied.
    assert box._read_rfid_result(3, read_complete=True) == "record"
    assert box.applied == [3]
    assert box.rfid_cleared_on_insert == set()
    assert box.rfid_insert_time == {}


def test_unchanged_record_without_a_drop_stays_stale():
    box = make_read_box()
    box.profiles[3] = {"source": "manual"}
    box._rfid_inserted(3)

    assert box._read_rfid_result(3, read_complete=True) == "stale"
    assert box.applied == []


def test_fast_poll_while_a_fresh_insertion_is_read():
    box = make_box({})
    box.rfid_pending = {1}
    box.rfid_insert_time = {1: 1000.0}

    assert box._insertion_being_read(1000.0 + 10.0)
    assert not box._insertion_being_read(1000.0 + box_module.INSERT_FAST_POLL)
    box.rfid_pending = set()
    assert not box._insertion_being_read(1000.0 + 10.0)


def test_lane_delete_of_a_missing_key_is_not_an_error(monkeypatch):
    database = MoonrakerDatabase()

    def gone(method, query, body=None):
        raise urllib.error.HTTPError("url", 404, "not found", {}, None)

    monkeypatch.setattr(database, "_request", gone)
    database.delete("lane3")


def test_lane_delete_other_errors_still_raise(monkeypatch):
    database = MoonrakerDatabase()

    def broken(method, query, body=None):
        raise urllib.error.HTTPError("url", 500, "server error", {}, None)

    monkeypatch.setattr(database, "_request", broken)
    with pytest.raises(urllib.error.HTTPError):
        database.delete("lane3")
