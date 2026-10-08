import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras.box import (
    DEFAULT_HUMIDITY_LIMIT_PCT, Box, material_table_value,
    parse_humidity_limits)
from extras.box_auto_mapping import evaluate_mapping
from extras.box_print_mapping import BoxPrintMapping


@pytest.mark.parametrize("material, limit", [
    ("PLA", 55), ("PLA-SILK", 55), ("PETG", 50), ("PETG-CF", 45),
    ("PA6-CF", 25), ("PAHT-CF", 25), ("PA12", 25), ("TPU", 40),
    ("PVA", 20), ("PPS", None), ("", None),
])
def test_limit_by_material_family(material, limit):
    assert material_table_value(DEFAULT_HUMIDITY_LIMIT_PCT, material) == limit


def test_custom_limits_are_parsed():
    assert parse_humidity_limits("PA:15, pla:45; PETG:40") == {
        "PA": 15, "PLA": 45, "PETG": 40}
    assert parse_humidity_limits("") == {}


@pytest.mark.parametrize("text", ["PA", "PA:0", "PA:101", ":20", "PA:x"])
def test_bad_custom_limits_are_refused(text):
    with pytest.raises(ValueError):
        parse_humidity_limits(text)


def make_box(warnings=True, humidity=48):
    box = Box.__new__(Box)
    box.humidity_warnings = warnings
    box.humidity_limits = dict(DEFAULT_HUMIDITY_LIMIT_PCT, PA=15)
    box.box_replies = {1: SimpleNamespace(humidity_pct=humidity)}
    box._address_slot = lambda slot: (slot // 4 + 1, slot % 4)
    return box


def test_slot_humidity_and_limit():
    box = make_box()

    assert box._slot_humidity(2) == 48
    assert box._slot_humidity(5) is None
    assert box.humidity_limit_pct("PA6-CF") == 25
    assert box.humidity_limit_pct("PA") == 15
    assert box.humidity_limit_pct("") is None


def test_warnings_off_means_no_limit():
    assert make_box(warnings=False).humidity_limit_pct("PLA") is None


def tool(index, material):
    return {"tool": index, "material": material, "color": "#000000"}


def slot(index, material, humidity, limit):
    return {"index": index, "material": material, "color": "#000000",
            "present": True, "humidity_pct": humidity,
            "humidity_limit_pct": limit}


def test_humid_cfs_warns_for_the_mapped_spool():
    warnings = evaluate_mapping(
        [tool(0, "PA-CF"), tool(1, "PLA")],
        [slot(0, "PA-CF", 48, 25), slot(1, "PLA", 48, 55)],
        {0: 0, 1: 1})

    humid = [w for w in warnings if w["kind"] == "humidity"]
    assert humid == [{"kind": "humidity", "tool": 0, "slot": 0,
                      "humidity_pct": 48, "limit_pct": 25,
                      "slot_material": "PA-CF"}]


@pytest.mark.parametrize("humidity, limit", [(None, 25), (48, None), (25, 25)])
def test_no_warning_without_data_or_at_the_limit(humidity, limit):
    warnings = evaluate_mapping(
        [tool(0, "PA-CF")], [slot(0, "PA-CF", humidity, limit)], {0: 0})

    assert not [w for w in warnings if w["kind"] == "humidity"]


def test_humidity_warning_text_and_notification():
    mapping = BoxPrintMapping.__new__(BoxPrintMapping)
    responses, calls = [], []
    mapping.gcode = SimpleNamespace(respond_info=responses.append)
    mapping.box = SimpleNamespace(
        slot_label=lambda index: "Box 1, slot %d" % (index + 1),
        notify=lambda *args: calls.append(args))

    mapping._notify([{"kind": "humidity", "tool": 0, "slot": 2,
                      "humidity_pct": 48, "limit_pct": 25,
                      "slot_material": "PA-CF"}])

    assert "T0 uses Box 1, slot 3 (PA-CF) in a CFS at 48% humidity" in responses[0]
    assert "above 25% for PA-CF" in responses[0]
    assert [call[0] for call in calls] == ["humidity"]
