import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import EXTERNAL_PROFILE_KEY, Box, BoxStore


def make_box(tmp_path, drivers=(), known=None, box_count=4):
    box = Box.__new__(Box)
    box.store = BoxStore(str(tmp_path / "filament_box.json"))
    if known:
        box.store.set_known_addresses(known)
    box.drivers = {address: object() for address in drivers}
    box.box_count = box_count
    return box


def test_external_slot_follows_the_online_cfs(tmp_path):
    box = make_box(tmp_path, drivers=(1,))
    assert box.external_slot == 4


def test_before_enumeration_slot_one_is_not_the_external_spool(tmp_path):
    box = make_box(tmp_path, known={1: b"\x5f\x98"})

    assert box.external_slot == 4
    assert box._runtime_slot_key(0) == 0
    assert box._runtime_slot_key(4) == EXTERNAL_PROFILE_KEY


def test_before_the_first_enumeration_box_count_is_used(tmp_path):
    box = make_box(tmp_path, box_count=2)
    assert box.external_slot == 8
