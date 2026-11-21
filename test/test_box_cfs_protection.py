"""CFS protections: a box going offline, filament at two hub lanes, a snapped
piece between slot and hub, and re-addressing a power-cycled box."""
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box as box_module
from extras.box import Box, BoxError, BoxSnapshot
from extras.box_addr import AutoAddressManager
from extras.box_change import BoxChangeEngine
from extras.box_protocol import AutoAddressReply


class FakeStats:
    def __init__(self, state="printing"):
        self.state = state
        self.print_start_time = 100.0


class FakePrinter:
    def __init__(self, stats):
        self.objects = {"print_stats": stats}

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class FakeEngine:
    """The recovery half of BoxChangeEngine, without a print."""

    block_resume = BoxChangeEngine.block_resume
    clear_resume_recovery = BoxChangeEngine.clear_resume_recovery
    _recovery_action = BoxChangeEngine._recovery_action

    def __init__(self):
        self.pending = None
        self.resume_recovery = None
        self.resume_prepared = True

    def _is_print_active(self):
        return False

    def recovery_notice(self):
        recovery = self.resume_recovery
        return "%s; %s" % (recovery.reason, self._recovery_action(
            {"target": recovery.target, "automatic": recovery.automatic}))


class Reply:
    def __init__(self, downstream_mask=0):
        self.downstream_mask = downstream_mask
        self.status = 0
        self.box_state = 0
        self.temp_c = None
        self.humidity_pct = None


class FakeDriver:
    def query_buffer(self, timeout=None):
        return None


def make_box(state="printing", drivers=(1,)):
    box = Box.__new__(Box)
    box.printer = FakePrinter(FakeStats(state))
    box.gcode = None
    box.drivers = {address: FakeDriver() for address in drivers}
    box.drivers_ready = True
    box.box_ids = {address: bytes([address]) * 12 for address in drivers}
    box.box_misses = {address: 0 for address in drivers}
    box.offline_handled = set()
    box.path_conflict = None
    box.conflict_paused = None
    box.snapped = None
    box.box_replies = {}
    box.rfid_presence = {address: 0x0F for address in drivers}
    box.path_owner = None
    box.change_engine = FakeEngine()
    box.warnings, box.infos, box.pauses = [], [], []
    box._warn = box.warnings.append
    box._info = lambda _responder, message: box.infos.append(message)
    box.pause_print = lambda *args, **kwargs: box.pauses.append(1)
    return box


# -- Hub lane conflict ------------------------------------------------------

def test_two_lanes_in_one_box_is_a_conflict_and_pauses_once():
    box = make_box()
    conflict = box._update_path_conflict({1: 0b0101}, {1: Reply(0b0101)})
    assert conflict[0] == (1,)
    assert conflict[1] == ("Filament in more than one hub lane: "
                           "Box 1, slot 1 and Box 1, slot 3")
    assert "Loads and unloads are refused" in box.warnings[0]
    box._pause_for_path_conflict()
    box._pause_for_path_conflict()
    assert box.pauses == [1]
    recovery = box.change_engine.resume_recovery
    assert recovery.automatic is False
    assert "pull out the filament" in box.change_engine.recovery_notice()


def test_lanes_in_two_boxes_are_a_conflict():
    box = make_box(drivers=(1, 2))
    conflict = box._update_path_conflict(
        {1: 0b0001, 2: 0b0010}, {1: Reply(1), 2: Reply(2)})
    assert conflict[0] == (1, 2)


def test_conflict_clears_its_own_recovery_only():
    box = make_box()
    box._update_path_conflict({1: 0b0011}, {1: Reply(3)})
    box._pause_for_path_conflict()
    assert box._update_path_conflict({1: 0b0001}, {1: Reply(1)}) is None
    assert box.change_engine.resume_recovery is None
    assert box.infos == ["Hub lane conflict cleared"]

    box.change_engine.block_resume("CFS fault", target=0, automatic=True)
    box._update_path_conflict({1: 0b0011}, {1: Reply(3)})
    box._update_path_conflict({}, {1: Reply()})
    assert box.change_engine.resume_recovery.reason == "CFS fault"


def test_conflict_holds_while_its_box_misses_a_reply():
    box = make_box()
    first = box._update_path_conflict({1: 0b0011}, {1: Reply(3)})
    assert box._update_path_conflict({}, {}) == first


def test_poll_read_publishes_but_loads_are_refused():
    box = make_box(state="standby")
    box.get_filament_sensor_state = lambda: (True, None)
    box._query_box_snapshot = lambda address, driver, topology: Reply(0b0011)
    box._presence_mask = lambda: 0x0F
    box._observe_fault = lambda address, reply: None
    box.tracking_owner = None
    box._fatal_episode = lambda address, status: False
    snap = box.read_live_state(strict=False)
    assert isinstance(snap, BoxSnapshot)
    with pytest.raises(BoxError, match="more than one hub lane"):
        box.read_live_state()


# -- Box offline --------------------------------------------------------------

def test_box_goes_offline_after_three_misses_and_comes_back():
    box = make_box()
    for _ in range(box_module.OFFLINE_POLLS):
        box._count_misses({})
    assert not box._box_online(1)
    assert box.warnings == ["CFS box 1 disconnected"]
    box._count_misses({1: Reply()})
    assert box._box_online(1)
    assert box.infos == ["CFS box 1 is back online"]


def test_offline_box_feeding_the_print_pauses_once_and_resume_retries():
    box = make_box()
    box.box_misses[1] = box_module.OFFLINE_POLLS
    snap = BoxSnapshot(loaded_slot=2)
    box._watch_loaded_box_offline(snap)
    box._watch_loaded_box_offline(snap)
    assert box.pauses == [1]
    recovery = box.change_engine.resume_recovery
    assert recovery.target == 2 and recovery.automatic is True
    assert "wait for CFS box 1 to reconnect" in box.warnings[-1]


def test_offline_box_outside_a_print_does_not_pause():
    box = make_box(state="standby")
    box.box_misses[1] = box_module.OFFLINE_POLLS
    box._watch_loaded_box_offline(BoxSnapshot(loaded_slot=2))
    assert box.pauses == [] and box.change_engine.resume_recovery is None


# -- Snapped piece -------------------------------------------------------------

def test_snapped_piece_is_reported_once():
    box = make_box(state="standby")
    box.box_replies = {1: Reply(0b0010)}
    snap = BoxSnapshot(loaded_slot=1, slot_mask=0b1101,
                       filament_detected=False)
    box._watch_snapped(snap)
    box._watch_snapped(snap)
    assert len(box.warnings) == 1
    assert "hub lane of Box 1, slot 2" in box.warnings[0]


def test_filament_at_the_inlet_or_head_is_not_snapped():
    box = make_box(state="standby")
    box.box_replies = {1: Reply(0b0010)}
    box._watch_snapped(BoxSnapshot(loaded_slot=1, slot_mask=0b0010,
                                   filament_detected=False))
    box._watch_snapped(BoxSnapshot(loaded_slot=1, slot_mask=0,
                                   filament_detected=True))
    assert box.warnings == [] and box.snapped is None


# -- Reconnect ---------------------------------------------------------------

class ReclaimClient:
    def __init__(self, uid):
        self.uid = uid
        self.address = None

    def discover(self):
        return AutoAddressReply(self.uid, 0)

    def assign(self, uid, address):
        self.address = address
        return True

    def query(self, address):
        if address != self.address:
            return None
        return AutoAddressReply(self.uid, 0)


def test_reclaim_gives_a_lost_box_its_own_address():
    uid = bytes.fromhex("5f983b4d1454b01247343534")
    client = ReclaimClient(uid)
    address, errors = AutoAddressManager(2).reclaim(client, {2: uid})
    assert address == 2 and errors == []


def test_reclaim_ignores_a_box_that_was_not_lost():
    client = ReclaimClient(bytes(12))
    address, errors = AutoAddressManager(1).reclaim(
        client, {1: bytes.fromhex("5f983b4d1454b01247343534")})
    assert address is None and client.address is None
    assert "was not online at startup" in errors[0]


def test_no_reconnect_while_printing():
    box = make_box()
    box.box_misses[1] = box_module.OFFLINE_POLLS
    box.last_reconnect = 0.0
    box.serial = object()
    box.observation_mode = False
    box.printer.objects["homing"] = None
    calls = []
    box.address_manager = types.SimpleNamespace(
        reclaim=lambda *args, **kwargs: calls.append(1) or (None, []))
    box._reconnect_lost_boxes(100.0)
    assert calls == []
    box.printer.objects["print_stats"].state = "paused"
    box.reactor = types.SimpleNamespace(monotonic=lambda: 0.0, pause=None)
    box.reconnect_noted = set()
    box._reconnect_lost_boxes(100.0)
    assert calls == [1]
