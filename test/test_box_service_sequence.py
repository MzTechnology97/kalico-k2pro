import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import Box, BoxSnapshot  # noqa: E402
from extras.box_change import BoxChangeEngine  # noqa: E402


class Recorder:
    def __init__(self):
        self.steps = []

    def run_script_from_command(self, script):
        self.steps.append(script)


def make_engine(steps):
    engine = BoxChangeEngine.__new__(BoxChangeEngine)
    engine.gcode = steps
    engine._start_heat = lambda temperature: steps.steps.append("heat %d" % temperature)
    engine._enter_service = lambda request: None
    engine._check_abort = lambda *args: None
    engine._move_to_wastebin = lambda request: steps.steps.append("wastebin")
    engine._wait_for_heat = (
        lambda gcmd, temperature, generation: steps.steps.append("wait %d" % temperature))
    return engine


def test_heat_wait_happens_over_the_wastebin():
    # Unload and slot change: heat first, home, travel, then wait there.
    steps = Recorder()
    make_engine(steps)._start_heat_and_wait_at_wastebin(None, 250, 0)
    assert steps.steps == [
        "heat 250", "HOME_IF_NEEDED AXIS=XY", "wastebin", "wait 250"]


class GcmdError(Exception):
    pass


class FakeGcmd:
    def __init__(self, **params):
        self.params = params
        self.messages = []

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default)) if name in self.params else default

    def error(self, msg):
        return GcmdError(msg)

    def respond_info(self, msg, log=True):
        self.messages.append(msg)


class FakeDriver:
    def __init__(self, mask):
        self.mask = mask

    def query_slot_mask(self, timeout=0.5):
        return types.SimpleNamespace(value=self.mask)


def make_box(loaded_slot, mask=0b1111, loaded_mask=0):
    box = Box.__new__(Box)
    box.drivers = {1: FakeDriver(mask)}
    box.snapshot = BoxSnapshot(
        slot_mask=mask, loaded_slot=loaded_slot, loaded_mask=loaded_mask)
    box.reads = []
    box._require_reply = lambda reply, label: reply
    box._force_rfid_results = (
        lambda address, driver, selected, reason: box.reads.append(selected)
        or {s for s in range(4) if selected & (1 << s)})
    box._read_rfid_remaining = lambda slot: None
    box._info = lambda gcmd, msg: gcmd.respond_info(msg)
    return box


def test_reread_of_the_slot_loaded_in_the_printhead_is_refused():
    box = make_box(loaded_slot=0)
    with pytest.raises(GcmdError, match="loaded toward the printhead"):
        box.cmd_rfid_read_slot(FakeGcmd(SLOT=0))
    assert box.reads == []


def test_reread_of_a_slot_fed_out_of_the_cfs_is_refused():
    box = make_box(loaded_slot=-1, loaded_mask=0b1000)
    with pytest.raises(GcmdError, match="loaded toward the printhead"):
        box.cmd_rfid_read_slot(FakeGcmd(SLOT=3))
    assert box.reads == []


def test_reread_of_another_slot_still_works():
    box = make_box(loaded_slot=0)
    gcmd = FakeGcmd(SLOT=2)
    box.cmd_rfid_read_slot(gcmd)
    assert box.reads == [0b0100]


def test_refresh_skips_the_loaded_slot():
    box = make_box(loaded_slot=1)
    gcmd = FakeGcmd()
    box.cmd_info_refresh(gcmd)
    assert box.reads == [0b1101]
    assert any("T1 is loaded toward the printhead" in m for m in gcmd.messages)


def test_refresh_with_nothing_loaded_reads_every_slot():
    box = make_box(loaded_slot=-1)
    box.cmd_info_refresh(FakeGcmd())
    assert box.reads == [0b1111]


def test_box_units_report_the_state_payload_length():
    box = make_box(loaded_slot=-1)
    box.box_replies = {1: types.SimpleNamespace(
        status=0, box_state=2, temp_c=31, humidity_pct=38,
        payload=bytes([31, 38, 0, 2]))}
    box._global_slot = lambda address, local: (address - 1) * 4 + local
    units = box._box_unit_statuses()
    assert units[0]["state_payload_bytes"] == 4
    box.box_replies[1].payload = bytes(6)
    assert box._box_unit_statuses()[0]["state_payload_bytes"] == 6
    box.box_replies = {}
    assert box._box_unit_statuses()[0]["state_payload_bytes"] is None
