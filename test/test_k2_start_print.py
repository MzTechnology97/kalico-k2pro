"""START_PRINT: hot nozzle clean before probing, saved axis twist switch.

START_PRINT cleans the nozzle hot, then cools it to the probing temperature;
START_PRINT_ATC keeps the axis twist calibration on or off across restarts.
The macros are rendered with the same Jinja settings Klipper uses; the test
checks the order of the generated commands in the three probe setups:
PRTouch as the probe, Cartographer alone, and PRTouch homing with a
Cartographer mesh (mixed).
"""

import ast
import configparser
import pathlib

import jinja2
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIGS = (
    ROOT / "config" / "k2" / "start_print.cfg",
    ROOT / "config" / "k2" / "reference" / "k2pro-cm5" / "start_print.cfg",
)
ENV = jinja2.Environment(
    "{%",
    "%}",
    "{",
    "}",
    extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"],
)


def load(path):
    parser = configparser.RawConfigParser(
        strict=False, inline_comment_prefixes=(";", "#")
    )
    parser.read(path, encoding="utf-8")
    variables = {
        key[len("variable_") :]: ast.literal_eval(value)
        for key, value in parser.items("gcode_macro _START_PRINT_VARS")
        if key.startswith("variable_")
    }
    return parser, variables


class MacroError(Exception):
    pass


def printer(variables, mode, fan_speed=0.0, saved=None, switch=None):
    config = {"heater_generic chamber_heater": {"max_delta": "10"}}
    status = {
        "gcode_macro _START_PRINT_VARS": variables,
        "configfile": {"config": config},
        "exclude_object": {"objects": []},
        "fan": {"speed": fan_speed},
    }
    if saved is not None:
        status["save_variables"] = {"variables": saved}
    if switch is not None:
        status["output_pin axis_twist_compensation"] = {"value": float(switch)}
    if mode in ("prtouch", "mixed"):
        status["prtouch"] = {}
        config["prtouch"] = {
            "register_as_probe": "True" if mode == "prtouch" else "False"
        }
    return status


def render(parser, macro, status, **params):
    template = ENV.from_string(parser.get("gcode_macro " + macro, "gcode"))

    def raise_error(msg):
        raise MacroError(msg)

    text = template.render(
        printer=status,
        params={k: str(v) for k, v in params.items()},
        rawparams="",
        action_raise_error=raise_error,
    )
    return [line.strip() for line in text.splitlines() if line.strip()]


def index(lines, prefix):
    found = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    assert found, "%r not in %r" % (prefix, lines)
    return found[0]


@pytest.fixture(params=CONFIGS, ids=["k2", "reference"])
def config(request):
    return load(request.param)


@pytest.mark.parametrize(
    "mode, home",
    [
        ("prtouch", "PRTOUCH_HOME"),
        ("cartographer", "CARTOGRAPHER_TOUCH_HOME"),
        ("mixed", "PRTOUCH_HOME"),
    ],
)
def test_hot_clean_runs_before_the_probing_clean(config, mode, home):
    parser, variables = config
    lines = render(
        parser,
        "START_PRINT",
        printer(variables, mode),
        EXTRUDER_TEMP=250,
        BED_TEMP=80,
        MATERIAL="PETG",
    )
    hot = index(lines, "_NOZZLE_HOT_CLEAN")
    assert lines[hot] == "_NOZZLE_HOT_CLEAN TEMP=240.0 PROBE_TEMP=140"
    assert [line for line in lines if line.startswith("_NOZZLE_HOT_CLEAN")] == [
        lines[hot]
    ]
    wait = index(lines, "TEMPERATURE_WAIT SENSOR=extruder")
    clean = index(lines, "NOZZLE_CLEAN")
    assert hot < wait < clean < index(lines, home)
    bed = index(lines, "TEMPERATURE_WAIT SENSOR=heater_bed")
    assert bed < hot
    if mode == "prtouch":
        # PRTouch probes the mesh with the nozzle: clean before the mesh.
        assert clean < index(lines, "BED_MESH_CALIBRATE")
    else:
        assert index(lines, "BED_MESH_CALIBRATE") < hot


def test_hot_clean_can_be_disabled(config):
    parser, variables = config
    variables = dict(variables, hot_clean=0)
    lines = render(
        parser,
        "START_PRINT",
        printer(variables, "prtouch"),
        EXTRUDER_TEMP=250,
        MATERIAL="PETG",
    )
    assert not any(line.startswith("_NOZZLE_HOT_CLEAN") for line in lines)
    assert any(line == "NOZZLE_CLEAN" for line in lines)


def test_no_hot_clean_close_to_the_probing_temperature(config):
    parser, variables = config
    lines = render(
        parser,
        "START_PRINT",
        printer(variables, "prtouch"),
        EXTRUDER_TEMP=165,
        MATERIAL="TPU",
    )
    assert not any(line.startswith("_NOZZLE_HOT_CLEAN") for line in lines)


def test_hot_clean_sequence_and_fan_restore(config):
    parser, variables = config
    lines = render(
        parser,
        "_NOZZLE_HOT_CLEAN",
        printer(variables, "prtouch", fan_speed=0.4),
        TEMP=240,
        PROBE_TEMP=140,
    )
    order = [
        "BOX_GO_TO_WASTEBIN",
        "M104 S240.0",
        "TEMPERATURE_WAIT SENSOR=extruder MINIMUM=238.0",
        "G1 E-2.0 F1800",
        "NOZZLE_CLEAN",
        "M104 S140.0",
        "M106 P0 S255",
        "TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=142.0",
        "M106 P0 S102",
    ]
    positions = [lines.index(line) for line in order]
    assert positions == sorted(positions), lines
    # The retract is relative and leaves the G-code state as it was.
    assert lines.index("M83") < lines.index("G1 E-2.0 F1800")
    assert lines.index("G1 E-2.0 F1800") < lines.index(
        "RESTORE_GCODE_STATE NAME=_NOZZLE_HOT_CLEAN"
    )


def test_hot_clean_without_retract(config):
    parser, variables = config
    variables = dict(variables, hot_clean_retract=0)
    lines = render(
        parser,
        "_NOZZLE_HOT_CLEAN",
        printer(variables, "prtouch"),
        TEMP=240,
        PROBE_TEMP=140,
    )
    assert not any(line.startswith("G1 E") for line in lines)
    assert lines[-1] == "M106 P0 S0"


# --- axis twist calibration switch -------------------------------------------

ATC_COMMANDS = (
    "PRTOUCH_AXIS_TWIST_COMPENSATION",
    "CARTOGRAPHER_AXIS_TWIST_COMPENSATION",
)


def atc_lines(lines):
    return [line for line in lines if line.startswith(ATC_COMMANDS)]


@pytest.mark.parametrize(
    "mode, command",
    [
        ("cartographer", "CARTOGRAPHER_AXIS_TWIST_COMPENSATION"),
        ("mixed", "PRTOUCH_AXIS_TWIST_COMPENSATION"),
    ],
)
def test_saved_switch_turns_the_calibration_on_and_off(config, mode, command):
    parser, variables = config
    variables = dict(variables, adaptive_axis_twist_comp=0)
    on = render(
        parser,
        "START_PRINT",
        printer(variables, mode, saved={"axis_twist_compensation": 1}),
        EXTRUDER_TEMP=250,
        MATERIAL="PETG",
    )
    assert atc_lines(on) and atc_lines(on)[0].startswith(command)
    # Cleaned hot, then calibrated, then meshed.
    assert (
        index(on, "_NOZZLE_HOT_CLEAN")
        < index(on, command)
        < index(on, "BED_MESH_CALIBRATE")
    )
    variables = dict(variables, adaptive_axis_twist_comp=1)
    off = render(
        parser,
        "START_PRINT",
        printer(variables, mode, saved={"axis_twist_compensation": 0}),
        EXTRUDER_TEMP=250,
        MATERIAL="PETG",
    )
    assert atc_lines(off) == []


def test_default_applies_until_a_value_is_saved(config):
    parser, variables = config
    variables = dict(variables, adaptive_axis_twist_comp=1)
    for saved in ({}, None):
        lines = render(
            parser,
            "START_PRINT",
            printer(variables, "cartographer", saved=saved),
            EXTRUDER_TEMP=250,
            MATERIAL="PETG",
        )
        assert atc_lines(lines)


def test_print_parameter_overrides_the_saved_switch(config):
    parser, variables = config
    lines = render(
        parser,
        "START_PRINT",
        printer(
            variables, "cartographer", saved={"axis_twist_compensation": 1}
        ),
        EXTRUDER_TEMP=250,
        MATERIAL="PETG",
        ATC=0,
    )
    assert atc_lines(lines) == []


def test_prtouch_probe_skips_the_calibration_and_says_so(config):
    parser, variables = config
    lines = render(
        parser,
        "START_PRINT",
        printer(variables, "prtouch", saved={"axis_twist_compensation": 1}),
        EXTRUDER_TEMP=250,
        MATERIAL="PETG",
    )
    assert atc_lines(lines) == []
    assert any("Axis twist calibration skipped" in line for line in lines)
    assert "AXIS_TWIST_COMPENSATION_CLEAR" in lines


@pytest.mark.parametrize(
    "mode, text",
    [
        ("cartographer", "on (Cartographer touch)"),
        ("mixed", "on (PRTouch reference with the Cartographer scan)"),
        ("prtouch", "on (not used: PRTouch probes with the nozzle"),
    ],
)
def test_switch_saves_and_reports(config, mode, text):
    parser, variables = config
    lines = render(
        parser, "START_PRINT_ATC", printer(variables, mode, saved={}), ENABLE=1
    )
    assert lines[0] == "SAVE_VARIABLE VARIABLE=axis_twist_compensation VALUE=1"
    assert text in lines[-1]


def test_switch_without_enable_shows_the_saved_state(config):
    parser, variables = config
    variables = dict(variables, adaptive_axis_twist_comp=1)
    lines = render(
        parser,
        "START_PRINT_ATC",
        printer(
            variables, "cartographer", saved={"axis_twist_compensation": 0}
        ),
    )
    assert not any(line.startswith("SAVE_VARIABLE") for line in lines)
    assert "calibration at print start: off" in lines[-1]
    lines = render(
        parser, "START_PRINT_ATC", printer(variables, "cartographer", saved={})
    )
    assert "calibration at print start: on" in lines[-1]


def test_switch_rejects_other_values(config):
    parser, variables = config
    with pytest.raises(MacroError, match="ENABLE must be 0 or 1"):
        render(
            parser,
            "START_PRINT_ATC",
            printer(variables, "cartographer", saved={}),
            ENABLE=2,
        )


def test_save_variables_file_is_declared(config):
    parser, _variables = config
    assert (
        parser.get("save_variables", "filename")
        == "~/printer_data/config/k2_start_print_variables.cfg"
    )


# --- Mainsail switch (virtual pin) -------------------------------------------


@pytest.fixture(params=[c.parent for c in CONFIGS], ids=["k2", "reference"])
def switch_config(request):
    parser = configparser.RawConfigParser(
        strict=False, inline_comment_prefixes=(";", "#")
    )
    parser.read(request.param / "openhost_controls.cfg", encoding="utf-8")
    start, variables = load(request.param / "start_print.cfg")
    return parser, start, variables


def test_switch_is_a_virtual_output_pin(switch_config):
    parser, _start, _variables = switch_config
    assert parser.has_section("virtual_pins")
    assert (
        parser.get("output_pin axis_twist_compensation", "pin")
        == "virtual_pin:axis_twist_compensation"
    )


@pytest.mark.parametrize("mode", ["cartographer", "mixed"])
def test_start_print_follows_the_switch(switch_config, mode):
    _parser, start, variables = switch_config
    # The switch wins over the saved value and the default.
    variables = dict(variables, adaptive_axis_twist_comp=0)
    on = render(
        start,
        "START_PRINT",
        printer(
            variables, mode, saved={"axis_twist_compensation": 0}, switch=1
        ),
        EXTRUDER_TEMP=250,
        MATERIAL="PETG",
    )
    assert atc_lines(on)
    variables = dict(variables, adaptive_axis_twist_comp=1)
    off = render(
        start,
        "START_PRINT",
        printer(
            variables, mode, saved={"axis_twist_compensation": 1}, switch=0
        ),
        EXTRUDER_TEMP=250,
        MATERIAL="PETG",
    )
    assert atc_lines(off) == []


def test_console_command_moves_the_switch(switch_config):
    _parser, start, variables = switch_config
    lines = render(
        start,
        "START_PRINT_ATC",
        printer(variables, "cartographer", saved={}, switch=0),
        ENABLE=1,
    )
    assert lines[0] == "SET_PIN PIN=axis_twist_compensation VALUE=1"
    assert not any(line.startswith("SAVE_VARIABLE") for line in lines)
    lines = render(
        start,
        "START_PRINT_ATC",
        printer(
            variables,
            "cartographer",
            saved={"axis_twist_compensation": 0},
            switch=1,
        ),
    )
    assert "calibration at print start: on" in lines[-1]


@pytest.mark.parametrize("value, saved", [("1.00", 1), ("0", 0), ("0.0", 0)])
def test_set_pin_saves_the_switch(switch_config, value, saved):
    parser, _start, variables = switch_config
    lines = render(
        parser,
        "SET_PIN",
        printer(variables, "cartographer"),
        PIN="axis_twist_compensation",
        VALUE=value,
    )
    assert lines == [
        "_SET_PIN_OPENHOST",
        "SAVE_VARIABLE VARIABLE=axis_twist_compensation VALUE=%d" % saved,
    ]


def test_set_pin_passes_other_pins_through(switch_config):
    parser, _start, variables = switch_config
    template = ENV.from_string(parser.get("gcode_macro SET_PIN", "gcode"))
    text = template.render(
        printer=printer(variables, "cartographer"),
        params={"PIN": "LED", "VALUE": "1"},
        rawparams="PIN=LED VALUE=1",
    )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert lines == ["_SET_PIN_OPENHOST PIN=LED VALUE=1"]
    assert (
        parser.get("gcode_macro SET_PIN", "rename_existing")
        == "_SET_PIN_OPENHOST"
    )


@pytest.mark.parametrize(
    "saved, default, expected",
    [
        ({"axis_twist_compensation": 1}, 0, 1),
        ({"axis_twist_compensation": 0}, 1, 0),
        ({}, 1, 1),
    ],
)
def test_switch_restored_after_start(switch_config, saved, default, expected):
    parser, _start, variables = switch_config
    variables = dict(variables, adaptive_axis_twist_comp=default)
    template = ENV.from_string(
        parser.get("delayed_gcode _AXIS_TWIST_SWITCH_RESTORE", "gcode")
    )
    text = template.render(
        printer=printer(variables, "cartographer", saved=saved), params={}
    )
    assert (
        text.strip()
        == "_SET_PIN_OPENHOST PIN=axis_twist_compensation VALUE=%d" % expected
    )
    assert (
        parser.get(
            "delayed_gcode _AXIS_TWIST_SWITCH_RESTORE", "initial_duration"
        )
        == "1"
    )


# --- wrapped command names ----------------------------------------------------


def test_wrapped_names_have_no_digits(switch_config):
    # Klipper's G-code parser splits a command name at its first digit.
    parser, _start, _variables = switch_config
    for section in parser.sections():
        if parser.has_option(section, "rename_existing"):
            name = parser.get(section, "rename_existing")
            assert not any(ch.isdigit() for ch in name), name
