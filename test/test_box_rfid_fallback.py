import pathlib
import sys
from contextlib import contextmanager

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_rfid_bambu, box_rfid_diag, box_rfid_mifare
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


def make_box(state="standby", loaded_slot=-1):
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
    box.auto_bambu_rfid_fallback = True
    box.auto_mifare_rfid_fallback = True
    box.snapshot = BoxSnapshot(data_ready=True, loaded_slot=loaded_slot)
    box.slot_label = lambda slot: "T%d" % slot
    box._query_rfid_sample = lambda slot: ("unknown", None)
    box.calls = []

    def bambu(slot):
        box.calls.append(("bambu", slot, box.rfid_read_owner))
        return False

    def mifare(slot):
        box.calls.append(("mifare", slot, box.rfid_read_owner))
        return False

    box._try_bambu_rfid_fallback = bambu
    box._try_mifare_rfid_fallback = mifare
    return box, stats


def test_unknown_pending_tag_gets_one_fallback_per_insertion():
    box, _stats = make_box()

    assert box._read_rfid_result(2) == "unknown"
    assert box._read_rfid_result(2) == "unknown"
    assert box._read_rfid_result(2) == "unknown"

    # One Bambu try and one MIFARE try, not one stock reread per poll.
    assert [call[0] for call in box.calls] == ["bambu", "mifare"]
    # The slot stays pending for a later valid Creality record.
    assert 2 in box.rfid_pending
    assert box.rfid_read_owner is None


def test_fallback_runs_under_the_read_claim():
    box, _stats = make_box()

    box._read_rfid_result(2)

    assert {call[2] for call in box.calls} == {"automatic T2 fallback"}


def test_fallback_waits_for_the_print_to_end():
    box, stats = make_box(state="printing")

    box._read_rfid_result(2)
    assert box.calls == []
    assert 2 not in box.rfid_fallback_tried

    stats.state = "paused"
    box._read_rfid_result(2)
    assert box.calls == []

    stats.state = "complete"
    box._read_rfid_result(2)
    assert [call[0] for call in box.calls] == ["bambu", "mifare"]


def test_fallback_waits_while_filament_is_loaded():
    box, _stats = make_box(loaded_slot=0)

    box._read_rfid_result(2)
    assert box.calls == []

    box.snapshot = BoxSnapshot(data_ready=True, loaded_slot=-1, loaded_mask=0x1)
    box._read_rfid_result(2)
    assert box.calls == []

    box.snapshot = BoxSnapshot(data_ready=True, loaded_slot=-1)
    box._read_rfid_result(2)
    assert len(box.calls) == 2


def test_fallback_waits_for_box_operation_and_other_reads():
    box, _stats = make_box()

    box.operation_depth = 1
    box._read_rfid_result(2)
    box.operation_depth = 0
    box.rfid_read_owner = "manual T1 reread"
    box._read_rfid_result(2)
    assert box.calls == []
    assert 2 not in box.rfid_fallback_tried

    box.rfid_read_owner = None
    box._read_rfid_result(2)
    assert len(box.calls) == 2


def test_fallback_off_when_both_vendors_are_disabled():
    box, _stats = make_box()
    box.auto_bambu_rfid_fallback = False
    box.auto_mifare_rfid_fallback = False

    box._read_rfid_result(2)

    assert box.calls == []


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

    assert [call[0] for call in box.calls] == [
        "bambu", "mifare", "bambu", "mifare"]


def test_successful_fallback_is_reported():
    box, _stats = make_box()
    box._try_bambu_rfid_fallback = lambda slot: True

    assert box._read_rfid_result(2) == "bambu"


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


class FakeTransport:
    @contextmanager
    def request_session(self):
        yield self


def make_bambu(monkeypatch, info):
    helper = box_rfid_bambu.BoxRfidBambu.__new__(box_rfid_bambu.BoxRfidBambu)
    helper.serial = FakeTransport()
    helper.last_error = None
    helper.last_unsupported = None
    helper.auto_gate = box_rfid_diag.AutoFallbackGate("Bambu RFID")
    helper.info_calls = 0

    class Driver:
        def __init__(self, transport, address):
            pass

        def info(self, timeout=None):
            helper.info_calls += 1
            return info

    monkeypatch.setattr(box_rfid_bambu, "RfidDiagDriver", Driver)
    return helper


def test_bambu_auto_read_stops_on_stock_firmware(monkeypatch):
    helper = make_bambu(monkeypatch, info=None)

    for _ in range(5):
        assert helper.try_auto_read(2) is None

    assert helper.info_calls == box_rfid_diag.AUTO_INFO_TIMEOUT_LIMIT
    assert helper.auto_gate.disabled


def test_bambu_auto_read_stops_on_unsupported_api(monkeypatch):
    class Info:
        api_version = box_rfid_diag.API_V21

    helper = make_bambu(monkeypatch, info=Info())

    assert helper.try_auto_read(2) is None
    assert helper.try_auto_read(2) is None

    assert helper.info_calls == 1
    assert "API7" in helper.auto_gate.disabled


def make_mifare(inspect):
    helper = box_rfid_mifare.BoxRfidMifare.__new__(box_rfid_mifare.BoxRfidMifare)
    helper.last_error = None
    helper.auto_gate = box_rfid_diag.AutoFallbackGate("Third-party MIFARE RFID")
    helper.inspect_calls = 0

    def inspect_candidate(global_slot, address):
        helper.inspect_calls += 1
        return inspect()

    helper._inspect_candidate = inspect_candidate
    return helper


def test_mifare_candidate_check_stops_on_unsupported_firmware():
    def inspect():
        raise box_rfid_mifare.ThirdPartyRfidUnsupported("no API7")

    helper = make_mifare(inspect)

    assert helper.is_known_candidate(2) is False
    assert helper.is_known_candidate(2) is False
    assert helper.inspect_calls == 1
    assert helper.auto_gate.disabled == "no API7"


def test_mifare_candidate_check_tolerates_a_single_info_timeout():
    def inspect():
        raise box_rfid_mifare.ThirdPartyRfidInfoTimeout("CFS RFID INFO timed out")

    helper = make_mifare(inspect)

    for _ in range(box_rfid_diag.AUTO_INFO_TIMEOUT_LIMIT - 1):
        assert helper.is_known_candidate(2) is False
    assert helper.auto_gate.disabled is None

    assert helper.is_known_candidate(2) is False
    assert helper.auto_gate.disabled


def test_mifare_requires_api7_reports_info_timeout_separately():
    with pytest.raises(box_rfid_mifare.ThirdPartyRfidInfoTimeout):
        box_rfid_mifare.BoxRfidMifare._require_api7(None)
