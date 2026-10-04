"""CFS slots published to Moonraker's lane_data namespace for OrcaSlicer."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box_lane_data import LaneDataPublisher, lanes_from_slots  # noqa: E402


class FakeDatabase:
    def __init__(self, existing=()):
        self.data = {key: {"lane": "9"} for key in existing}
        self.calls = []

    def keys(self):
        self.calls.append(("keys",))
        return set(self.data)

    def post(self, key, value):
        self.calls.append(("post", key))
        self.data[key] = value

    def delete(self, key):
        self.calls.append(("delete", key))
        self.data.pop(key, None)


def slot(index, material="PLA", color="#FFFFFF", present=True, external=False, **extra):
    return dict({"index": index, "material": material, "color": color,
                 "present": present, "external": external, "target_temp": 220,
                 "spoolman_id": None, "name": "Generic PLA", "brand": "Generic",
                 "filament_id": "00001"}, **extra)


def test_lanes_cover_occupied_physical_slots_with_a_material():
    lanes = lanes_from_slots([
        slot(0), slot(1, present=False), slot(2, material=""),
        slot(3, color="6c4e43", spoolman_id=12), slot(16, external=True),
    ])
    assert sorted(lanes) == ["lane1", "lane4"]
    assert lanes["lane1"]["lane"] == "0" and lanes["lane1"]["material"] == "PLA"
    assert lanes["lane1"]["nozzle_temp"] == 220
    assert lanes["lane4"]["color"] == "#6C4E43" and lanes["lane4"]["spool_id"] == 12
    assert lanes["lane4"]["vendor"] == "Generic" and lanes["lane4"]["filament_id"] == "00001"


def test_sync_removes_stale_lanes_and_writes_only_changes():
    db = FakeDatabase(existing=["lane1", "lane7"])
    publisher = LaneDataPublisher(database=db)
    publisher.update([slot(0), slot(1)])
    assert publisher.sync_once()
    assert sorted(db.data) == ["lane1", "lane2"]
    assert ("delete", "lane7") in db.calls

    db.calls.clear()
    publisher.update([slot(0), slot(1)])      # unchanged: nothing to do
    assert publisher.sync_once() and db.calls == []

    publisher.update([slot(0, color="#000000")])
    assert publisher.sync_once()
    assert db.calls == [("delete", "lane2"), ("post", "lane1")]
    assert db.data["lane1"]["color"] == "#000000"


def test_failed_write_is_retried_from_a_fresh_read():
    class Flaky(FakeDatabase):
        fail = True

        def post(self, key, value):
            if self.fail:
                raise OSError("moonraker down")
            super().post(key, value)

    db = Flaky()
    publisher = LaneDataPublisher(database=db)
    publisher.update([slot(0)])
    try:
        publisher.sync_once()
    except OSError:
        publisher._published = None
    db.fail = False
    assert publisher.sync_once()
    assert db.data["lane1"]["material"] == "PLA"
