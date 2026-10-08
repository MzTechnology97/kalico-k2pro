import pathlib
import sys

import pytest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_rfid_diag
from extras.box import Box, BoxError, BoxSnapshot


class FakePrintStats:
    def __init__(self, state="standby"):
        self.state = state


class FakePrinter:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class InsertReadingBox(Box):
    rfid_insert_reading_enabled = True


class FakeFallback:
    """Stands in for box_rfid_fallback.RfidFallback."""

    def __init__(self, box, result=None):
        self.box = box
        self.result = result
        self.has_decoders = True
        self.known = {}
        self.calls = []

    def decoders(self):
        return [object()] if self.has_decoders else []

    def run(self, slot, automatic):
        self.calls.append((slot, automatic, self.box.rfid_read_owner))
        return self.result

    def run_known(self, slot):
        self.calls.append(("known", slot, self.box.rfid_read_owner))
        return self.known.get(slot)


def make_box(state="standby", loaded_slot=-1, result=None):
    stats = FakePrintStats(state)
    box = InsertReadingBox.__new__(InsertReadingBox)
    box.printer = FakePrinter({"print_stats": stats})
    box.gcode = None
    box.operation_depth = 0
    box.rfid_read_owner = None
    box.rfid_pending = {2}
    box.rfid_snapshot = {}
    box.rfid_seen_invalid = set()
    box.rfid_fallback_tried = set()
    box.rfid_live_slots = set()
    box.rfid_percent = {}
    box.snapshot = BoxSnapshot(data_ready=True, loaded_slot=loaded_slot)
    box.slot_label = lambda slot: "T%d" % slot
    box.is_physical_slot = lambda slot: True
    box._query_rfid_sample = lambda slot: ("unknown", None)
    box.rfid_fallback = FakeFallback(box, result)
    return box, stats


def test_unknown_pending_tag_gets_one_fallback_per_insertion():
    box, _stats = make_box()

    assert box._read_rfid_result(2) == "unknown"
    assert box._read_rfid_result(2) == "unknown"
    assert box._read_rfid_result(2) == "unknown"

    # One automatic run, not one stock reread per poll.
    assert box.rfid_fallback.calls == [
        (2, True, "automatic T2 fallback")]
    # The slot stays pending for a later valid Creality record.
    assert 2 in box.rfid_pending
    assert box.rfid_read_owner is None


def test_fallback_waits_for_the_print_to_end():
    box, stats = make_box(state="printing")

    box._read_rfid_result(2)
    stats.state = "paused"
    box._read_rfid_result(2)
    assert box.rfid_fallback.calls == []
    assert 2 not in box.rfid_fallback_tried

    stats.state = "complete"
    box._read_rfid_result(2)
    assert len(box.rfid_fallback.calls) == 1


def test_fallback_waits_while_filament_is_loaded():
    box, _stats = make_box(loaded_slot=0)

    box._read_rfid_result(2)
    box.snapshot = BoxSnapshot(data_ready=True, loaded_slot=-1, loaded_mask=0x1)
    box._read_rfid_result(2)
    assert box.rfid_fallback.calls == []

    box.snapshot = BoxSnapshot(data_ready=True, loaded_slot=-1)
    box._read_rfid_result(2)
    assert len(box.rfid_fallback.calls) == 1


def test_fallback_waits_for_box_operation_and_other_reads():
    box, _stats = make_box()

    box.operation_depth = 1
    box._read_rfid_result(2)
    box.operation_depth = 0
    box.rfid_read_owner = "manual T1 reread"
    box._read_rfid_result(2)
    assert box.rfid_fallback.calls == []
    assert 2 not in box.rfid_fallback_tried

    box.rfid_read_owner = None
    box._read_rfid_result(2)
    assert len(box.rfid_fallback.calls) == 1


def test_no_registered_decoder_means_no_attempt():
    box, _stats = make_box()
    box.rfid_fallback.has_decoders = False

    box._read_rfid_result(2)

    assert box.rfid_fallback.calls == []
    assert 2 not in box.rfid_fallback_tried


def test_reinsertion_allows_a_new_fallback():
    box, _stats = make_box()
    box.rfid_reported_percent = {}
    box.rfid_spools = {}
    box.unknown_rfid = {}
    box._runtime_slot_key = lambda slot: str(slot)
    box._invalidate_spoolman = lambda slot: None
    box._snapshot_rfid_cache = lambda slot: None

    box._read_rfid_result(2)
    box._rfid_inserted(2)
    box._read_rfid_result(2)

    assert len(box.rfid_fallback.calls) == 2


def test_successful_fallback_is_reported():
    box, _stats = make_box(result="BAMBU")

    assert box._read_rfid_result(2) == "BAMBU"


def test_fallback_exception_is_contained():
    box, _stats = make_box()

    def broken(slot, automatic):
        raise RuntimeError("bus lost")

    box.rfid_fallback.run = broken

    assert box._read_rfid_result(2) == "unknown"
    assert box.rfid_read_owner is None


def test_forced_read_refused_while_another_read_runs():
    box, _stats = make_box()
    box.rfid_read_owner = "automatic T2 fallback"

    class Driver:
        def force_rfid_read(self, mask):
            raise AssertionError("no CFS command may be sent")

    with pytest.raises(BoxError, match="automatic T2 fallback"):
        box._force_rfid_results(1, Driver(), 0x1, "manual T0 reread")
    assert box.rfid_read_owner == "automatic T2 fallback"


def test_forced_read_releases_the_claim_on_error():
    box, _stats = make_box()

    class Driver:
        def force_rfid_read(self, mask):
            raise RuntimeError("link lost")

    box._info = lambda *args: None
    with pytest.raises(RuntimeError):
        box._force_rfid_results(1, Driver(), 0x1, "manual T0 reread")
    assert box.rfid_read_owner is None


class FakeGcmd:
    class error(Exception):
        pass


def test_helper_guard_turns_a_busy_claim_into_a_gcode_error():
    box, _stats = make_box()
    box.rfid_read_owner = "manual T1 reread"
    printer = FakePrinter({"box": box})

    with pytest.raises(FakeGcmd.error, match="manual T1 reread"):
        with box_rfid_diag.box_rfid_read_guard(
                printer, FakeGcmd, "BOX_RFID_BAMBU_READ"):
            raise AssertionError("must not run")


def test_helper_guard_holds_and_releases_the_claim():
    box, _stats = make_box()
    printer = FakePrinter({"box": box})

    with box_rfid_diag.box_rfid_read_guard(
            printer, FakeGcmd, "BOX_RFID_MIFARE_READ"):
        assert box.rfid_read_owner == "BOX_RFID_MIFARE_READ"
    assert box.rfid_read_owner is None


def test_helper_guard_without_box_is_a_no_op():
    with box_rfid_diag.box_rfid_read_guard(
            FakePrinter(), FakeGcmd, "BOX_RFID_DIAG_POLL"):
        pass


def test_auto_gate_needs_three_info_timeouts_in_a_row():
    gate = box_rfid_diag.AutoFallbackGate("test")

    gate.info_timeout()
    gate.info_timeout()
    gate.info_ok()
    gate.info_timeout()
    gate.info_timeout()
    assert gate.disabled is None

    gate.info_timeout()
    assert "3 times" in gate.disabled


def test_auto_gate_keeps_the_first_reason():
    gate = box_rfid_diag.AutoFallbackGate("test")

    gate.disable("no API7")
    gate.disable("later")

    assert gate.disabled == "no API7"


class FakeCfsDriver:
    def __init__(self, records=None):
        self.masks = []
        self.records = records or {}

    def force_rfid_read(self, mask):
        self.masks.append(("force", mask))
        return SimpleNamespace(status=0)

    def query_rfid_records(self, mask, timeout=None):
        self.masks.append(("query", mask))
        return SimpleNamespace(status=0, records=self.records, fields={})


def make_reread_box():
    box, _stats = make_box()
    box._info = lambda *args: None
    box._require_reply = lambda reply, context, allowed=(0,): reply
    box._global_slot = lambda address, local: (address - 1) * 4 + local
    box.reactor = SimpleNamespace(pause=lambda t: None, monotonic=lambda: 0.0)
    return box


def test_manual_reread_of_known_tags_skips_the_stock_read():
    box = make_reread_box()
    box.rfid_fallback.known = {0: "BAMBU", 1: "MIFARE"}
    driver = FakeCfsDriver()

    applied = box._force_rfid_results(
        1, driver, 0x3, "BOX_INFO_REFRESH", known_fastpath=True)

    assert applied == {0, 1}
    assert driver.masks == []


def test_stock_read_only_covers_the_slots_left():
    box = make_reread_box()
    box.rfid_fallback.known = {0: "BAMBU"}
    driver = FakeCfsDriver()

    applied = box._force_rfid_results(
        1, driver, 0x3, "BOX_INFO_REFRESH", known_fastpath=True)

    assert applied == {0}
    assert driver.masks[0] == ("force", 0x2)
    assert all(mask == 0x2 for _kind, mask in driver.masks)
    # Slot 1 then went through the normal stock-first fallback.
    assert (1, False, "BOX_INFO_REFRESH") in box.rfid_fallback.calls


def test_deferred_insertion_read_never_uses_the_fast_path():
    box = make_reread_box()
    box.rfid_fallback.known = {0: "BAMBU"}
    driver = FakeCfsDriver()

    box._force_rfid_results(1, driver, 0x1, "deferred insertion")

    assert driver.masks[0] == ("force", 0x1)
    assert not any(call[0] == "known" for call in box.rfid_fallback.calls)
