import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_rfid_bambu
from extras.box_rfid_bambu import BambuRfidError, BambuStockBusy, BoxRfidBambu


class FakeReactor:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


def make_reader(outcomes):
    reactor = FakeReactor()
    reader = BoxRfidBambu.__new__(BoxRfidBambu)
    reader.printer = SimpleNamespace(get_reactor=lambda: reactor)
    reader.waits = []

    def wait(delay):
        reader.waits.append(delay)
        reactor.now += delay

    def arm(global_slot, address, local_slot):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    reader._wait = wait
    reader._arm_stock_capture = arm
    return reader


def test_waits_while_the_stock_task_is_busy():
    busy = BambuStockBusy("CFS stock RFID manager is busy on logical slot 3")
    reader = make_reader([busy, busy, "armed"])
    assert reader._arm_when_stock_idle(1, 1, 1) == "armed"
    assert reader.waits == [box_rfid_bambu.STOCK_IDLE_POLL] * 2


def test_gives_up_after_the_timeout():
    polls = int(box_rfid_bambu.STOCK_IDLE_TIMEOUT / box_rfid_bambu.STOCK_IDLE_POLL)
    busy = BambuStockBusy("busy on logical slot 3")
    reader = make_reader([busy] * (polls + 2))
    with pytest.raises(BambuStockBusy):
        reader._arm_when_stock_idle(1, 1, 1)
    assert sum(reader.waits) >= box_rfid_bambu.STOCK_IDLE_TIMEOUT


def test_other_errors_are_not_retried():
    reader = make_reader([BambuRfidError("CFS RFID INFO timed out")])
    with pytest.raises(BambuRfidError, match="INFO timed out"):
        reader._arm_when_stock_idle(1, 1, 1)
    assert reader.waits == []
