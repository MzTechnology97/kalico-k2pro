import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_rfid_bambu, box_rfid_diag, box_rfid_fallback
from extras import box_rfid_mifare
from extras.box_rfid_fallback import (
    RfidFallback, TagIdentity, identity_from_internal_record)

UID = bytes.fromhex("233A111D")


def record(uid=UID, atqa=b"\x04\x00", sak=0x08):
    data = bytearray(76)
    data[60:62] = atqa
    data[62:66] = uid
    data[74] = sak
    return SimpleNamespace(data=bytes(data))


def api7_info():
    return SimpleNamespace(
        api_version=box_rfid_diag.API_STOCK_CAPTURE,
        capabilities=box_rfid_fallback.REQUIRED_CAPS)


class FakeCfs:
    """What the CFS answers to the diag requests of the registry."""

    def __init__(self):
        self.info = api7_info()
        self.record = record()
        self.info_calls = 0
        self.record_calls = 0


class FakeStore:
    def __init__(self):
        self.settings = {}

    def setting(self, name, default=None):
        value = self.settings.get(name, default)
        return json.loads(json.dumps(value))

    def set_setting(self, name, value):
        # Box persists settings as JSON.
        self.settings[name] = json.loads(json.dumps(value))


class FakePrinter:
    def __init__(self, objects):
        self.objects = objects

    def lookup_objects(self):
        return list(self.objects.items())


class FakeBox:
    def __init__(self, decoders):
        self.printer = FakePrinter(
            {"decoder %d" % i: d for i, d in enumerate(decoders)})
        self.cfs = FakeCfs()
        self.drivers = {1: SimpleNamespace(serial=self.cfs)}
        self.store = FakeStore()
        self.gcode = None
        self.applied = []
        self.infos = []
        self.profiles = {}

    def profile(self, slot):
        return self.profiles.get(slot, {})

    def _address_slot(self, slot):
        return slot // 4 + 1, slot % 4

    def slot_label(self, slot):
        return "T%d" % slot

    def _apply_bambu_rfid_tag(self, slot, tag):
        self.applied.append(("bambu", slot, tag))
        return True

    def _apply_third_party_rfid_tag(self, slot, tag):
        self.applied.append(("generic", slot, tag))
        return True

    def _info(self, _gcode, msg):
        self.infos.append(msg)


class FakeDiagDriver:
    def __init__(self, serial, address):
        self.cfs = serial

    def info(self, timeout=1.0):
        self.cfs.info_calls += 1
        return self.cfs.info

    def internal_record(self, slot, timeout=1.0):
        self.cfs.record_calls += 1
        return self.cfs.record


@pytest.fixture(autouse=True)
def fake_diag_driver(monkeypatch):
    monkeypatch.setattr(box_rfid_fallback.diag, "RfidDiagDriver", FakeDiagDriver)


class FakeDecoder:
    RFID_DECODER_KIND = "generic"

    def __init__(self, name, priority=10, tag=None, cost=1, auto=True,
                 error=None, version="1"):
        self.RFID_DECODER_NAME = name
        self.RFID_DECODER_PRIORITY = priority
        self.tag = tag
        self.cost = cost
        self.auto = auto
        self.error = error
        self.version = version
        self.reads = []

    def rfid_decoder_version(self):
        return self.version

    def rfid_decoder_candidate(self, identity, automatic):
        return identity.mifare_classic_1k and (self.auto or not automatic)

    def rfid_decoder_cost(self, identity, automatic):
        return self.cost

    def rfid_decoder_read(self, slot, identity, max_reads, automatic):
        self.reads.append(max_reads)
        if self.error is not None:
            raise self.error
        used = min(self.cost, max_reads)
        return (self.tag if used == self.cost else None), used


def make(decoders, **kwargs):
    box = FakeBox(decoders)
    return box, RfidFallback(box, **kwargs)


def test_identity_comes_from_the_stock_internal_record():
    identity = identity_from_internal_record(record())
    assert identity == TagIdentity(UID, b"\x04\x00", 0x08)
    assert identity.uid_hex == "233A111D"
    assert identity.mifare_classic_1k
    assert identity_from_internal_record(record(uid=b"\x00" * 4)) is None
    assert identity_from_internal_record(SimpleNamespace(data=b"\x00")) is None
    assert identity_from_internal_record(None) is None
    assert not TagIdentity(UID, b"\x44\x00", 0x00).mifare_classic_1k


def test_new_tag_is_read_once_then_served_from_the_cache():
    tag = {"vendor": "QIDI", "material": "PLA", "blocks": {4: "AA", 5: "BB"}}
    decoder = FakeDecoder("QIDI", tag=tag)
    box, fallback = make([decoder])

    assert fallback.run(2, automatic=True) == "QIDI"
    assert fallback.run(2, automatic=True) == "QIDI"

    assert decoder.reads == [1]
    assert len(box.applied) == 2
    cached = box.applied[1][2]
    assert cached["material"] == "PLA"
    assert cached["blocks"] == {4: "AA", 5: "BB"}
    assert fallback.last_result == ("cache", "QIDI", "233A111D", 0)


def test_unrecognised_tag_is_not_retried_until_the_decoders_change():
    decoder = FakeDecoder("BAMBU")
    box, fallback = make([decoder])

    assert fallback.run(3, automatic=True) is None
    assert fallback.run(3, automatic=True) is None
    assert decoder.reads == [1]
    assert fallback.last_result == ("unknown-cached", None, "233A111D", 0)

    decoder.version = "2"
    assert fallback.run(3, automatic=True) is None
    assert decoder.reads == [1, 1]


def test_new_decoder_makes_an_unknown_tag_retryable():
    first = FakeDecoder("BAMBU")
    box, fallback = make([first])
    fallback.run(3, automatic=True)

    second = FakeDecoder("SNAPMAKER", priority=30, tag={"vendor": "S"})
    box.printer.objects["decoder 1"] = second
    assert fallback.run(3, automatic=True) is None
    # Budget 1: BAMBU spends it; SNAPMAKER waits for a manual reread.
    assert second.reads == []
    assert "SNAPMAKER" in box.infos[-1]

    assert fallback.run(3, automatic=False) == "SNAPMAKER"


def test_automatic_budget_caps_the_rereads():
    a = FakeDecoder("A", priority=1)
    b = FakeDecoder("B", priority=2)
    box, fallback = make([b, a], auto_budget=1)

    assert fallback.run(0, automatic=True) is None

    assert a.reads == [1]
    assert b.reads == []
    assert fallback.last_result[3] == 1
    # B was never tried: the UID must not be remembered as unknown.
    assert fallback._cache()["unknown"] == {}
    assert "not tried within 1 reread(s): B" in box.infos[-1]


def test_manual_budget_reaches_more_decoders():
    a = FakeDecoder("A", priority=1)
    b = FakeDecoder("B", priority=2, cost=2)
    c = FakeDecoder("C", priority=3)
    box, fallback = make([a, b, c], manual_budget=3)

    assert fallback.run(0, automatic=False) is None

    assert a.reads == [3]
    assert b.reads == [2]
    assert c.reads == []
    assert fallback.last_result[3] == 3


def test_partly_tried_decoder_is_not_remembered_as_unknown():
    decoder = FakeDecoder("MIFARE", cost=3)
    box, fallback = make([decoder], auto_budget=1)

    fallback.run(0, automatic=True)

    assert decoder.reads == [1]
    assert fallback._cache()["unknown"] == {}


def test_read_error_is_not_remembered_and_spends_the_budget():
    broken = FakeDecoder("A", priority=1, error=RuntimeError("busy"))
    other = FakeDecoder("B", priority=2)
    box, fallback = make([broken, other], auto_budget=1)

    assert fallback.run(0, automatic=True) is None

    assert other.reads == []
    assert fallback._cache()["unknown"] == {}
    assert "read error: A" in box.infos[-1]


def test_manual_run_reads_again_and_refreshes_the_cache():
    decoder = FakeDecoder("QIDI", tag={"vendor": "QIDI", "material": "PLA"})
    box, fallback = make([decoder])
    fallback.run(1, automatic=True)

    decoder.tag = {"vendor": "QIDI", "material": "PETG"}
    assert fallback.run(1, automatic=False) == "QIDI"
    assert decoder.reads == [1, 3]
    assert fallback._cache()["tags"]["233A111D"]["tag"]["material"] == "PETG"

    # The tag was rewritten as something nobody reads: the cache follows.
    decoder.tag = None
    assert fallback.run(1, automatic=False) is None
    cache = fallback._cache()
    assert "233A111D" not in cache["tags"]
    assert "233A111D" in cache["unknown"]


def test_automatic_candidates_exclude_unhinted_vendors():
    decoder = FakeDecoder("MIFARE", auto=False)
    box, fallback = make([decoder])

    fallback.run(0, automatic=True)

    assert decoder.reads == []
    # Nobody could try it automatically; remembered until decoders change.
    assert "233A111D" in fallback._cache()["unknown"]


def test_no_tag_means_no_reads():
    decoder = FakeDecoder("BAMBU")
    box, fallback = make([decoder])
    box.cfs.record = record(uid=b"\x00" * 4)

    assert fallback.run(0, automatic=True) is None
    assert decoder.reads == []


def test_firmware_without_api7_turns_the_automatic_path_off():
    decoder = FakeDecoder("BAMBU")
    box, fallback = make([decoder])
    box.cfs.info = SimpleNamespace(
        api_version=box_rfid_diag.API_V21, capabilities=0xA1)

    assert fallback.run(0, automatic=True) is None
    assert fallback.run(0, automatic=True) is None

    assert box.cfs.info_calls == 1
    assert box.cfs.record_calls == 0
    assert decoder.reads == []
    assert "no API7" in fallback.gate.disabled


def test_stock_firmware_silence_turns_the_automatic_path_off():
    box, fallback = make([FakeDecoder("BAMBU")])
    box.cfs.info = None

    for _ in range(5):
        assert fallback.run(0, automatic=True) is None

    assert box.cfs.info_calls == box_rfid_diag.AUTO_INFO_TIMEOUT_LIMIT
    assert fallback.gate.disabled


def test_firmware_info_is_asked_once():
    decoder = FakeDecoder("BAMBU")
    box, fallback = make([decoder])

    fallback.run(0, automatic=False)
    fallback.run(1, automatic=False)

    assert box.cfs.info_calls == 1


def test_disabled_decoder_is_skipped():
    bambu = FakeDecoder("BAMBU", priority=1)
    mifare = FakeDecoder("MIFARE", priority=2, tag={"vendor": "QIDI"})
    box, fallback = make([bambu, mifare], enabled={"BAMBU": False})

    assert fallback.run(0, automatic=True) == "MIFARE"
    assert bambu.reads == []


def test_bambu_tags_use_the_bambu_profile_builder():
    decoder = FakeDecoder("BAMBU", tag={"material": "PETG"})
    decoder.RFID_DECODER_KIND = "bambu"
    box, fallback = make([decoder])

    fallback.run(0, automatic=True)
    fallback.run(0, automatic=True)

    assert [kind for kind, _slot, _tag in box.applied] == ["bambu", "bambu"]


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(box_rfid_fallback, "CACHE_LIMIT", 2)
    box, fallback = make([FakeDecoder("A")])
    for uid in ("01", "02", "03"):
        identity = TagIdentity(bytes.fromhex(uid * 4), b"\x04\x00", 8)
        fallback.remember_unknown(identity, "A:1")

    assert list(fallback._cache()["unknown"]) == ["02020202", "03030303"]


def test_forget_and_clear():
    box, fallback = make([FakeDecoder("A")])
    fallback.remember_unknown(TagIdentity(UID, b"\x04\x00", 8), "A:1")

    assert fallback.forget("233a111d") is True
    assert fallback.forget("233A111D") is False
    fallback.remember_unknown(TagIdentity(UID, b"\x04\x00", 8), "A:1")
    fallback.clear_cache()
    assert fallback._cache() == {"tags": {}, "unknown": {}}


# --- the real decoders -------------------------------------------------------

IDENTITY = TagIdentity(UID, b"\x04\x00", 0x08)


def make_bambu(outcome):
    helper = box_rfid_bambu.BoxRfidBambu.__new__(box_rfid_bambu.BoxRfidBambu)
    helper.last_error = None
    helper.calls = 0

    def capture(slot, address=None):
        helper.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    helper._read_tag_stock_capture = capture
    return helper


def test_bambu_decoder_interface():
    helper = make_bambu("tag")

    assert box_rfid_fallback.is_decoder(helper)
    assert helper.rfid_decoder_candidate(IDENTITY, True)
    assert not helper.rfid_decoder_candidate(
        TagIdentity(UID, b"\x44\x00", 0x00), True)
    assert helper.rfid_decoder_cost(IDENTITY, True) == 1
    assert helper.rfid_decoder_read(0, IDENTITY, 1, True) == ("tag", 1)
    assert helper.rfid_decoder_read(0, IDENTITY, 0, True) == (None, 0)
    assert helper.calls == 1


def test_bambu_decoder_not_recognised_versus_error():
    helper = make_bambu(box_rfid_bambu.BambuTagNotRecognised("hit=0x00"))
    assert helper.rfid_decoder_read(0, IDENTITY, 1, True) == (None, 1)

    helper = make_bambu(box_rfid_bambu.BambuRfidError("STOCK_STATE timed out"))
    with pytest.raises(box_rfid_bambu.BambuRfidError):
        helper.rfid_decoder_read(0, IDENTITY, 1, True)


class KeyDecoder:
    def __init__(self, name, keys, parsed=None):
        self.name = name
        self.keys = keys
        self.parsed = parsed

    def key_candidates(self, _uid):
        return self.keys

    def parse(self, capture):
        return self.parsed


def make_mifare(monkeypatch, decoders, hint=None, outcomes=None):
    monkeypatch.setattr(box_rfid_mifare, "DECODERS", tuple(decoders))
    helper = box_rfid_mifare.BoxRfidMifare.__new__(box_rfid_mifare.BoxRfidMifare)
    helper.gcode = SimpleNamespace(respond_info=lambda msg: None)
    helper.last_error = None
    helper.last_reads = 0
    helper.keys_tried = []
    outcomes = list(outcomes or [])
    helper._inspect_candidate = lambda slot, address: (b"\x04\x00", UID, 0x08)
    helper._hint_for_uid = lambda uid: hint
    helper._remember_hint = lambda uid, name: None

    def capture(slot, address, key_a):
        helper.keys_tried.append(key_a)
        outcome = outcomes.pop(0) if outcomes else "incomplete"
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            complete=outcome == "complete", hitmask=7, okmask=0, failmask=7)

    helper._capture = capture
    return helper


KEY1, KEY2, KEY3 = (bytes([n]) * 6 for n in (1, 2, 3))


def test_mifare_decoder_automatic_only_for_hinted_uids(monkeypatch):
    helper = make_mifare(monkeypatch, [KeyDecoder("QIDI", [KEY1])])
    assert not helper.rfid_decoder_candidate(IDENTITY, True)
    assert helper.rfid_decoder_candidate(IDENTITY, False)

    helper = make_mifare(monkeypatch, [KeyDecoder("QIDI", [KEY1])], hint="QIDI")
    assert helper.rfid_decoder_candidate(IDENTITY, True)


def test_mifare_decoder_cost_counts_the_key_candidates(monkeypatch):
    decoders = [KeyDecoder("QIDI", [KEY1, KEY2]), KeyDecoder("OTHER", [KEY3])]
    helper = make_mifare(monkeypatch, decoders)
    assert helper.rfid_decoder_cost(IDENTITY, False) == 3

    helper = make_mifare(monkeypatch, decoders, hint="OTHER")
    assert helper.rfid_decoder_cost(IDENTITY, True) == 1


def test_mifare_read_stops_at_the_budget(monkeypatch):
    decoders = [KeyDecoder("QIDI", [KEY1, KEY2]), KeyDecoder("OTHER", [KEY3])]
    helper = make_mifare(monkeypatch, decoders)

    assert helper.rfid_decoder_read(0, IDENTITY, 2, False) == (None, 2)
    assert helper.keys_tried == [KEY1, KEY2]


def test_mifare_read_returns_the_decoded_tag(monkeypatch):
    parsed = SimpleNamespace(uid=UID)
    decoders = [KeyDecoder("QIDI", [KEY1, KEY2], parsed=parsed)]
    helper = make_mifare(
        monkeypatch, decoders, outcomes=["incomplete", "complete"])
    monkeypatch.setattr(box_rfid_mifare, "_tag_message", lambda tag: "tag")

    tag, used = helper.rfid_decoder_read(0, IDENTITY, 3, False)

    assert (tag, used) == (parsed, 2)


def test_manual_helper_read_is_cached_for_later_insertions():
    decoder = FakeDecoder("MIFARE", auto=False)
    box, fallback = make([decoder])
    fallback.run(0, automatic=True)
    assert "233A111D" in fallback._cache()["unknown"]

    tag = SimpleNamespace(
        uid=UID, atqa=b"\x04\x00", sak=0x08,
        as_dict=lambda: {"vendor": "QIDI", "material": "PLA"})
    printer = SimpleNamespace(
        lookup_object=lambda name, default=None: box if name == "box" else default)
    box.rfid_fallback = fallback
    box_rfid_fallback.remember_manual_read(printer, decoder, tag)

    assert fallback.run(0, automatic=True) == "MIFARE"
    assert decoder.reads == []
    assert box.applied[-1][2]["material"] == "PLA"


def test_mifare_read_raises_when_a_key_hit_a_bus_error(monkeypatch):
    decoders = [KeyDecoder("QIDI", [KEY1, KEY2])]
    helper = make_mifare(
        monkeypatch, decoders,
        outcomes=[box_rfid_mifare.ThirdPartyRfidError("busy"), "incomplete"])

    with pytest.raises(box_rfid_mifare.ThirdPartyRfidError, match="busy"):
        helper.rfid_decoder_read(0, IDENTITY, 3, False)
    assert helper.last_reads == 2


def test_mifare_requires_api7_reports_info_timeout_separately():
    with pytest.raises(box_rfid_mifare.ThirdPartyRfidInfoTimeout):
        box_rfid_mifare.BoxRfidMifare._require_api7(None)


# --- known-tag fast path (manual rereads) -----------------------------------

def test_known_cached_uid_is_read_directly():
    decoder = FakeDecoder("QIDI", tag={"vendor": "QIDI", "material": "PLA"})
    box, fallback = make([decoder])
    fallback.run(1, automatic=True)

    assert fallback.run_known(1) == "QIDI"

    assert decoder.reads == [1, 1]
    assert fallback.last_result == ("direct", "QIDI", "233A111D", 1)


def test_hinted_uid_is_read_directly():
    decoder = FakeDecoder("MIFARE", tag={"vendor": "QIDI"})
    decoder.rfid_decoder_known = lambda identity: identity.uid == UID
    box, fallback = make([decoder])

    assert fallback.run_known(0) == "MIFARE"
    assert "233A111D" in fallback._cache()["tags"]


def test_slot_profile_code_marks_a_bambu_tag_known():
    bambu = FakeDecoder("BAMBU", tag={"material": "PETG"})
    bambu.RFID_DECODER_KIND = "bambu"
    box, fallback = make([bambu])
    box.profiles[2] = {"rfid_code": "BAMBU:PETG HF"}

    assert fallback.run_known(2) == "BAMBU"
    assert box.applied[-1][0] == "bambu"


def test_unknown_tag_takes_the_stock_path():
    decoder = FakeDecoder("BAMBU", tag={"material": "PETG"})
    box, fallback = make([decoder])
    box.profiles[0] = {"rfid_code": "105628"}

    assert fallback.run_known(0) is None
    assert decoder.reads == []


def test_known_tag_no_longer_read_falls_back_without_forgetting():
    decoder = FakeDecoder("QIDI", tag={"vendor": "QIDI"})
    box, fallback = make([decoder])
    fallback.run(1, automatic=True)
    decoder.tag = None

    assert fallback.run_known(1) is None
    # The stock-first manual run decides; it rewrites the cache if needed.
    assert "233A111D" in fallback._cache()["tags"]


def test_known_read_error_falls_back():
    decoder = FakeDecoder("QIDI", tag={"vendor": "QIDI"})
    box, fallback = make([decoder])
    fallback.run(1, automatic=True)
    decoder.error = RuntimeError("busy")

    assert fallback.run_known(1) is None


def test_known_path_respects_a_zero_manual_budget():
    decoder = FakeDecoder("QIDI", tag={"vendor": "QIDI"})
    box, fallback = make([decoder], manual_budget=0)
    fallback.remember_tag(TagIdentity(UID, b"\x04\x00", 8), decoder, {"v": 1})

    assert fallback.run_known(1) is None
    assert decoder.reads == []
