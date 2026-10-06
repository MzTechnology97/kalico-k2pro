"""Pressure advance and maximum flow per filament profile (K2-OpenHost)."""

import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_gcode  # noqa: E402
from extras.box import DEFAULT_MAX_FLOW, Box, BoxError, BoxStore  # noqa: E402
from extras.box_k2rfid_catalog import K2RfidMaterialCatalog  # noqa: E402


class FakeGcmd:
    def __init__(self, params):
        self.params = dict(params)

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name, default)
        return None if value is None else int(value)

    def get_float(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name, default)
        return None if value is None else float(value)

    def get_command_parameters(self):
        return dict(self.params)

    def error(self, message):
        return BoxError(message)


class FakeStepper:
    def __init__(self, pressure_advance=0.038, smooth_time=0.038):
        self.pressure_advance = pressure_advance
        self.pressure_advance_smooth_time = smooth_time
        self.calls = []

    def _set_pressure_advance(self, pressure_advance, smooth_time):
        self.calls.append((pressure_advance, smooth_time))
        self.pressure_advance = pressure_advance
        self.pressure_advance_smooth_time = smooth_time


class FakeChangeEngine:
    default_temp = 220

    def __init__(self):
        self.metadata = None

    def metadata_filament(self, slot):
        return self.metadata


PETG_CF = {"material": "PETG-CF", "brand": "IEMAI", "name": "IEMAI PETG-CF",
           "target_temp": 250}


def make_box(tmp_path, stepper=None):
    box = Box.__new__(Box)
    box.store = BoxStore(
        str(tmp_path / "filament_box.json"),
        str(tmp_path / "config" / "cfs_filaments.json"))
    box.drivers = {1: object()}
    box.change_engine = FakeChangeEngine()
    box.apply_pressure_advance = True
    box.default_pressure_advance = 0.038
    box.messages = []
    box._info = lambda responder, msg: box.messages.append(msg)
    box._param = lambda gcmd, name: gcmd.params.get(name)
    box._normal_color = lambda value: value
    box._apply_new_filament = lambda filament: None
    box.gcode = object()
    box.stepper = stepper or FakeStepper()
    toolhead = types.SimpleNamespace(
        get_extruder=lambda: types.SimpleNamespace(
            extruder_stepper=box.stepper))
    box.printer = types.SimpleNamespace(
        lookup_object=lambda name, default=None: (
            toolhead if name == "toolhead" else default))
    return box


def assign(box, slot, filament_id, **profile):
    entry = box.store.filament(filament_id)
    base = {
        "material": entry["material"], "color": "#000000",
        "brand": entry["brand"], "name": entry["name"],
        "target_temp": entry["target_temp"],
        "pressure_advance": entry.get("pressure_advance"),
        "max_flow": entry.get("max_flow"),
        "spoolman_id": None, "filament_id": filament_id,
        "source": "library", "rfid_code": ""}
    base.update(profile)
    box.set_profile(slot, base)


# --- library fields ------------------------------------------------------------


def test_max_flow_is_saved_in_the_library_and_validated(tmp_path):
    box = make_box(tmp_path)
    box.store.set_filament("90002", dict(PETG_CF, max_flow=15))
    reloaded = BoxStore(box.store.path, box.store.library_path)
    assert reloaded.filament("90002")["max_flow"] == 15.0
    with pytest.raises(BoxError, match="max_flow"):
        box.store.set_filament("90003", dict(PETG_CF, max_flow=-1))


def test_filament_set_max_flow_and_pressure_advance_empty_clears(tmp_path):
    box = make_box(tmp_path)
    box.cmd_filament_set(FakeGcmd({
        "ID": "90002", "MATERIAL": "PETG-CF", "BRAND": "IEMAI",
        "NAME": "IEMAI PETG-CF", "TARGET_TEMP": 250,
        "MAX_FLOW": "15", "PRESSURE_ADVANCE": "0.04"}))
    saved = box.store.filament("90002")
    assert saved["max_flow"] == 15.0 and saved["pressure_advance"] == 0.04
    # absent keeps, empty clears
    box.cmd_filament_set(FakeGcmd({"ID": "90002", "MATERIAL": "PETG-CF",
                                   "PRESSURE_ADVANCE": ""}))
    saved = box.store.filament("90002")
    assert saved["pressure_advance"] is None and saved["max_flow"] == 15.0
    with pytest.raises(BoxError, match="MAX_FLOW"):
        box.cmd_filament_set(FakeGcmd({"ID": "90002", "MATERIAL": "PETG-CF",
                                       "MAX_FLOW": "500"}))


def test_material_set_updates_pa_and_max_flow_and_keeps_the_temperature(tmp_path):
    box = make_box(tmp_path)
    box.cmd_material_set(FakeGcmd({"MATERIAL": "PLA", "PRESSURE_ADVANCE": "0.032",
                                   "MAX_FLOW": "20"}))
    pla = box.store.materials["PLA"]
    assert pla == {"target_temp": 220, "pressure_advance": 0.032, "max_flow": 20.0}
    box.cmd_material_set(FakeGcmd({"MATERIAL": "PLA", "MAX_FLOW": ""}))
    assert "max_flow" not in box.store.materials["PLA"]
    assert box.store.materials["PLA"]["pressure_advance"] == 0.032
    with pytest.raises(BoxError, match="TARGET_TEMP"):
        box.cmd_material_set(FakeGcmd({"MATERIAL": "PP", "MAX_FLOW": "10"}))
    reloaded = BoxStore(box.store.path, box.store.library_path)
    assert reloaded.materials["PLA"]["pressure_advance"] == 0.032


def test_catalog_reads_max_flow_in_both_formats(tmp_path):
    path = tmp_path / "db.json"
    path.write_text(
        '{"materials": ['
        '{"id": "01001", "brand": "Creality", "name": "Hyper PLA", '
        '"material": "PLA", "target_temp": 220, "max_flow": 21},'
        '{"base": {"id": "02002", "brand": "B", "name": "B PETG", '
        '"meterialType": "PETG", "minTemp": 230, "maxTemp": 260}, '
        '"kvParam": {"filament_max_volumetric_speed": "14"}}]}')
    catalog = K2RfidMaterialCatalog(str(path))
    catalog.reload()
    flows = {e["id"]: e.get("max_flow") for e in catalog.entries}
    assert flows == {"01001": 21.0, "02002": 14.0}


# --- resolution ------------------------------------------------------------------


def test_settings_come_from_slot_then_filament_then_material_then_orca(tmp_path):
    box = make_box(tmp_path)
    box.store.set_filament("90002", dict(PETG_CF, max_flow=15,
                                         pressure_advance=0.04))
    assign(box, 0, "90002")
    s = box.slot_filament_settings(0)
    assert (s["pressure_advance"], s["max_flow"]) == (0.04, 15.0)
    assert s["pressure_advance_source"] == "slot"

    # a filament without values falls back to the material, then to Orca
    box.store.set_filament("90004", dict(PETG_CF, id="90004"))
    assign(box, 1, "90004")
    s = box.slot_filament_settings(1)
    assert s["pressure_advance"] is None
    assert s["max_flow"] == DEFAULT_MAX_FLOW["PETG-CF"] == 10.0
    assert s["max_flow_source"] == "OrcaSlicer generic PETG-CF"
    box.store.set_material("PETG-CF", 250, pressure_advance=0.05, max_flow=12)
    s = box.slot_filament_settings(1)
    assert (s["pressure_advance"], s["max_flow"]) == (0.05, 12.0)
    assert s["max_flow_source"] == "material PETG-CF"


# --- applying pressure advance ------------------------------------------------


def test_profile_pa_is_applied_on_load_and_printer_cfg_restored(tmp_path):
    box = make_box(tmp_path)
    box.store.set_filament("90002", dict(PETG_CF, pressure_advance=0.04))
    box.store.set_filament("90004", dict(PETG_CF, id="90004"))
    assign(box, 0, "90002")
    assign(box, 1, "90004")

    box.activate_spool(0)
    assert box.stepper.calls == [(0.04, 0.038)]  # smooth_time kept
    assert "Pressure advance 0.0400" in box.messages[-1]
    box.activate_spool(0)
    assert len(box.stepper.calls) == 1  # unchanged: no call, no message

    box.activate_spool(1)  # no PA anywhere: back to printer.cfg
    assert box.stepper.calls[-1] == (0.038, 0.038)
    assert "printer.cfg" in box.messages[-1]


def test_file_that_sets_pa_for_the_filament_wins(tmp_path):
    box = make_box(tmp_path)
    box.store.set_filament("90002", dict(PETG_CF, pressure_advance=0.04))
    assign(box, 0, "90002")
    box.change_engine.metadata = {"tool": 0, "pressure_advance_enabled": True,
                                  "pressure_advance": 0.049, "max_flow": None}
    box.activate_spool(0)
    assert box.stepper.calls == []
    # the library value is not replaced by the file's
    assert box.store.filament("90002")["pressure_advance"] == 0.04
    box.change_engine.metadata["pressure_advance_enabled"] = False
    box.activate_spool(0)
    assert box.stepper.calls == [(0.04, 0.038)]


def test_disabled_option_leaves_pa_alone(tmp_path):
    box = make_box(tmp_path)
    box.apply_pressure_advance = False
    box.store.set_filament("90002", dict(PETG_CF, pressure_advance=0.04))
    assign(box, 0, "90002")
    box.activate_spool(0)
    assert box.stepper.calls == []


# --- learning max flow, saving a calibration --------------------------------


def test_max_flow_is_learned_from_the_file_only_when_missing(tmp_path):
    box = make_box(tmp_path)
    box.store.set_filament("90002", dict(PETG_CF))
    assign(box, 0, "90002")
    box.change_engine.metadata = {"tool": 0, "pressure_advance_enabled": False,
                                  "pressure_advance": None, "max_flow": 15.0}
    box.activate_spool(0)
    assert box.store.filament("90002")["max_flow"] == 15.0
    assert box.profile(0)["max_flow"] == 15.0  # the slot follows its filament
    assert any("Max flow 15" in m for m in box.messages)

    box.change_engine.metadata["max_flow"] = 9.0
    box.activate_spool(0)
    assert box.store.filament("90002")["max_flow"] == 15.0  # own value kept


def test_calibrated_pa_is_saved_in_the_custom_filament(tmp_path):
    box = make_box(tmp_path)
    box.store.set_filament("90002", dict(PETG_CF, pressure_advance=0.05))
    assign(box, 0, "90002")
    where = box.save_slot_pressure_advance(0, 0.0412)
    assert where == "filament 90002"
    assert box.store.filament("90002")["pressure_advance"] == 0.0412
    assert box.profile(0)["pressure_advance"] == 0.0412
    assert box.stepper.calls[-1] == (0.0412, 0.038)


def test_calibrated_pa_for_a_read_only_or_manual_profile_stays_on_the_slot(tmp_path):
    box = make_box(tmp_path)
    box.store.set_system([{"id": "01001", "material": "PLA", "brand": "Creality",
                           "name": "Hyper PLA", "target_temp": 220,
                           "pressure_advance": 0.04, "system": True}])
    assign(box, 0, "01001", source="rfid")
    assert box.save_slot_pressure_advance(0, 0.031).endswith("profile")
    assert box.store.filament("01001")["pressure_advance"] == 0.04
    assert box.slot_filament_settings(0)["pressure_advance"] == 0.031

    box.set_profile(2, {"material": "PLA", "color": "#FFFFFF",
                        "source": "manual"})
    box.save_slot_pressure_advance(2, 0.033)
    assert box.profile(2)["pressure_advance"] == 0.033
    with pytest.raises(BoxError):
        box.save_slot_pressure_advance(2, 5.0)


# --- slicer metadata -------------------------------------------------------------


def test_read_metadata_finds_pa_and_max_flow_per_filament(tmp_path):
    path = tmp_path / "print.gcode"
    path.write_text(
        "G1 X1\n"
        "; adaptive_pressure_advance = 0\n"
        "; enable_pressure_advance = 1,0\n"
        "; filament_max_volumetric_speed = 15,33\n"
        "; pressure_advance = 0.049,0.04\n")
    meta = box_gcode.read_metadata(str(path))
    assert meta["pa_enabled"] == [True, False]
    assert meta["pressure_advance"] == [0.049, 0.04]
    assert meta["max_flow"] == [15.0, 33.0]
