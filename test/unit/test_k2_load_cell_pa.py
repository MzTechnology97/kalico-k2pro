"""k2_load_cell_pa: APAX transport, capture sessions, ownership and export.

Fakes only: no serial port and no printer. The fake MCU answers like the
published prtouch_v3 object: start acks with expar1=acq_tick, stop with 0,
blocks carry the pressure sensor oid. Sample data here is synthetic.
"""

import os
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import k2_load_cell_pa, prtouch_codec  # noqa: E402
from extras import k2_pa_analysis as analysis  # noqa: E402

FREQ = 120_000_000.0
MASK = 0xFFFFFFFF
PRES_OID = 5
E_OID = 7


# --- codec ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "values,signed",
    [
        ([], True),
        ([5], True),
        ([-3, -2, 200, -40000, 9_000_000, -9_000_000], True),
        ([4_000_000_000, 4_000_000_100, 4_000_000_050], False),
        (list(range(-60, 60, 7)), True),
    ],
)
def test_frame_round_trip(values, signed):
    frame = prtouch_codec.encode_frame(values)
    assert prtouch_codec.decode_frame(frame, signed) == values


def test_frame_widths_and_descriptor_order():
    # deltas 1, 300 (2 bytes), 70000 (3 bytes), 1 << 30 (4 bytes), 1
    values = [1, 301, 70301, 70301 + (1 << 30), 70302 + (1 << 30)]
    frame = prtouch_codec.encode_frame(values)
    assert frame[0] == 5
    # two descriptor bytes, the first value's codes live in the last one
    assert frame[2] == 0b11100100 and frame[1] == 0b00
    assert prtouch_codec.decode_frame(frame, True) == values


def test_first_value_signedness():
    frame = prtouch_codec.encode_frame([-1])
    assert prtouch_codec.decode_frame(frame, True) == [-1]
    assert prtouch_codec.decode_frame(frame, False) == [255]


@pytest.mark.parametrize("cut", [1, 2, 4])
def test_truncated_frames_raise(cut):
    frame = prtouch_codec.encode_frame([1, 2000, 300000])
    with pytest.raises(prtouch_codec.FrameError):
        prtouch_codec.decode_frame(frame[:-cut] if cut > 1 else frame[:1], True)


def test_prtouch_still_uses_the_shared_decoder():
    text = (ROOT / "klippy/extras/prtouch.py").read_text()
    assert "from .prtouch_codec import decode_frame" in text


def test_ticks_near_the_wrap_are_sign_extended():
    # firmware: delta from 0 in int32 -> 0xFFFFF894 is packed as 2 bytes
    ticks = [0xFFFFF894, 0xFFFFF894 + 93750]
    frame = prtouch_codec.encode_frame(ticks)
    assert frame[1] & 3 == 1  # the first value takes 2 bytes
    assert prtouch_codec.decode_frame(frame, False)[0] == 0xF894  # wrong way
    assert prtouch_codec.decode_ticks(frame) == [t & MASK for t in ticks]


def test_tick_unwrapper_rollover():
    u = prtouch_codec.TickUnwrapper()
    u.seed(MASK - 10)
    assert u.unwrap(MASK - 5) == MASK - 5
    assert u.unwrap(4) == MASK + 5
    assert u.unwrap(MASK - 1) == MASK - 1  # small step back is kept signed


# --- fakes ----------------------------------------------------------------------


class CommandError(Exception):
    pass


class Reactor:
    NOW = 0.0
    NEVER = 9e99

    def __init__(self):
        self.now = 1000.0
        self.timers = {}

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.now = max(self.now, until)
        return self.now

    def register_timer(self, callback, when=None):
        timer = SimpleNamespace(callback=callback, when=self.NEVER)
        self.timers[callback] = timer
        return timer

    def update_timer(self, timer, when):
        timer.when = when


class Command:
    def __init__(self, mcu, name):
        self.mcu, self.name = mcu, name

    def send(self, args, **kw):
        self.mcu.sent.append((self.name, list(args)))
        self.mcu.firmware(self.name, args)


class Serial:
    def __init__(self, formats):
        self.messages_by_name = {
            f.split()[0]: SimpleNamespace(msgformat=f) for f in formats
        }

    def get_msgparser(self):
        return self


ALL_FORMATS = (
    k2_load_cell_pa.CMD_CONFIG,
    k2_load_cell_pa.CMD_START,
    k2_load_cell_pa.CMD_STOP,
    k2_load_cell_pa.RSP_BLOCK,
    k2_load_cell_pa.RSP_ACK,
)


class MCU:
    def __init__(self, reactor, formats=ALL_FORMATS):
        self.reactor = reactor
        self._serial = Serial(formats)
        self.oids = 10
        self.config_callbacks = []
        self.responses = {}
        self.config_cmds = []
        self.sent = []
        self.ack_mode = "normal"

    def get_name(self):
        return "nozzle_mcu"

    def create_oid(self):
        self.oids += 1
        return self.oids - 1

    def register_config_callback(self, cb):
        self.config_callbacks.append(cb)

    def register_response(self, cb, name, oid=None):
        self.responses[(name, oid)] = cb

    def add_config_cmd(self, cmd):
        self.config_cmds.append(cmd)

    def lookup_command(self, fmt, cq=None):
        return Command(self, fmt.split()[0])

    def get_constant_float(self, name):
        assert name == "CLOCK_FREQ"
        return FREQ

    def estimated_print_time(self, eventtime):
        return eventtime

    def print_time_to_clock(self, print_time):
        return int(print_time * FREQ)

    def clock32_to_clock64(self, clock32):
        now = int(self.reactor.now * FREQ)
        diff = (clock32 - (now & MASK)) & MASK
        if diff & 0x80000000:
            diff -= 1 << 32
        return now + diff

    def get_status(self, eventtime):
        return {"mcu_version": "fake"}

    # firmware behaviour from the prtouch_v3 object
    def firmware(self, name, args):
        ack = self.responses.get(("ack_prtouch", self.apax_oid))
        if name == "start_prtouch_apax" and self.ack_mode != "silent":
            expar1 = 0 if self.ack_mode == "probe_style" else args[2]
            err = 3 if self.ack_mode == "error" else 0
            ack(
                {
                    "oid": self.apax_oid,
                    "err": err,
                    "expar0": 0,
                    "expar1": expar1,
                }
            )
        elif name == "stop_prtouch_apax" and self.ack_mode != "silent":
            ack({"oid": self.apax_oid, "err": 0, "expar0": 0, "expar1": 0})

    def block(self, ticks, datas, espds, ch=0):
        cb = self.responses[("resault_prtouch_apax", PRES_OID)]
        cb(
            {
                "oid": PRES_OID,
                "ch": ch,
                "len": len(ticks),
                "ticks": prtouch_codec.encode_frame([t & MASK for t in ticks]),
                "datas": prtouch_codec.encode_frame(datas),
                "espds": prtouch_codec.encode_frame(espds),
            }
        )


class Section:
    def __init__(self, values):
        self.values = values

    def _get(self, name, default):
        return self.values.get(name, default)

    def get(self, name, default=None, **kw):
        return self._get(name, default)

    def getint(self, name, default=None, **kw):
        return int(self._get(name, default))

    def getfloat(self, name, default=None, **kw):
        return float(self._get(name, default))

    def getboolean(self, name, default=None, **kw):
        return bool(self._get(name, default))

    def getchoice(self, name, choices, default=None, **kw):
        return choices[self._get(name, default)]

    def getfloatlist(self, name, default=None, **kw):
        return list(self._get(name, default))


class Config(Section):
    def __init__(self, printer, values):
        super().__init__(values)
        self.printer = printer
        self.error = CommandError

    def get_printer(self):
        return self.printer

    def has_section(self, name):
        return name == "prtouch"

    def getsection(self, name):
        return Section({"baud": 230400})


class Extruder:
    def __init__(self, mcu, temp=220.0, target=220.0):
        stepper = SimpleNamespace(
            get_mcu=lambda: mcu,
            get_oid=lambda: E_OID,
            get_step_dist=lambda: 0.0025,
            get_dir_inverted=lambda: (True, True),
        )
        self.extruder_stepper = SimpleNamespace(stepper=stepper)
        self.max_e_velocity = 30.0
        self.max_e_dist = 50.0
        self.status = {
            "temperature": temp,
            "target": target,
            "can_extrude": temp >= 170,
            "pressure_advance": 0.04,
            "smooth_time": 0.04,
        }

    def get_status(self, eventtime):
        return dict(self.status)


class Gcode:
    def __init__(self):
        self.commands = {}
        self.scripts = []

    def register_command(self, name, func, desc=None):
        # Same check as klippy/gcode.py: "K2_..." would parse as K with
        # argument 2, so Klipper refuses it at startup.
        if (
            name.upper() != name
            or not name.replace("_", "A").isalnum()
            or name[0].isdigit()
            or name[1:2].isdigit()
        ):
            raise ValueError(
                "Can't register '%s' as it is an invalid name" % name
            )
        self.commands[name] = func

    def run_script_from_command(self, script):
        self.scripts.append(script)


class Printer:
    command_error = CommandError
    config_error = CommandError

    def __init__(self, tmp_path):
        self.reactor = Reactor()
        self.mcu = MCU(self.reactor)
        self.gcode = Gcode()
        self.prtouch = SimpleNamespace(
            pres_mcu=self.mcu,
            pres_oid=PRES_OID,
            pres_cfg_regs=60,
            pres_acq_tkms=0.78125,
            _armed=False,
        )
        self.extruder = Extruder(self.mcu)
        self.print_stats = SimpleNamespace(state="standby")
        self.box = None
        self.events = {}
        self.shutdown = False
        self.log_file = str(tmp_path / "klippy.log")

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return {
            "gcode": self.gcode,
            "extruder": self.extruder,
            "print_stats": self.print_stats,
            "toolhead": SimpleNamespace(wait_moves=lambda: None),
            "box": self.box,
        }.get(name) or default

    def load_object(self, config, name):
        assert name == "prtouch"
        return self.prtouch

    def register_event_handler(self, name, cb):
        self.events[name] = cb

    def is_shutdown(self):
        return self.shutdown

    def get_start_args(self):
        return {"log_file": self.log_file, "software_version": "test"}


def make(tmp_path, formats=ALL_FORMATS, **cfg):
    printer = Printer(tmp_path)
    printer.mcu._serial = Serial(formats)
    values = {"export": False}
    values.update(cfg)
    lc = k2_load_cell_pa.K2LoadCell(Config(printer, values))
    printer.mcu.apax_oid = lc.apax_oid
    for cb in printer.mcu.config_callbacks:
        cb()
    return printer, lc


def clock_now(printer):
    return int(printer.reactor.now * FREQ)


def run_process(printer, lc):
    lc._process(printer.reactor.now)


# --- capability -----------------------------------------------------------------


def test_available_with_apax_and_config_command(tmp_path):
    printer, lc = make(tmp_path)
    assert lc.available
    assert printer.mcu.config_cmds == [
        "config_prtouch_apax oid=%d oid_estp=%d" % (lc.apax_oid, E_OID)
    ]
    assert lc.acq_tick == round(0.78125e-3 * FREQ)
    assert lc.apax_oid != PRES_OID


def test_missing_apax_is_reported_and_adds_nothing(tmp_path):
    formats = tuple(f for f in ALL_FORMATS if "apax" not in f)
    printer, lc = make(tmp_path, formats=formats)
    assert not lc.available
    assert "no APAX support" in lc.unavailable_reason
    assert printer.mcu.config_cmds == []
    with pytest.raises(CommandError, match="unavailable"):
        lc.start_capture(1.0)


def test_extruder_on_another_mcu_is_unavailable(tmp_path):
    printer = Printer(tmp_path)
    printer.extruder = Extruder(object())
    lc = k2_load_cell_pa.K2LoadCell(Config(printer, {"export": False}))
    printer.mcu.apax_oid = lc.apax_oid
    for cb in printer.mcu.config_callbacks:
        cb()
    assert not lc.available and "not on the sensor MCU" in lc.unavailable_reason


# --- start/stop and acks ----------------------------------------------------------


def test_start_requires_the_apax_ack(tmp_path):
    printer, lc = make(tmp_path)
    session = lc.start_capture(1.0)
    assert session.state == "running"
    assert printer.mcu.sent[0] == (
        "start_prtouch_apax",
        [lc.apax_oid, 60, lc.acq_tick],
    )


def test_probe_style_ack_is_not_accepted(tmp_path):
    printer, lc = make(tmp_path)
    printer.mcu.ack_mode = "probe_style"  # expar1 == 0 like start_prtouch_pres
    with pytest.raises(CommandError, match="ack timeout"):
        lc.start_capture(1.0)
    assert printer.mcu.sent[-1][0] == "stop_prtouch_apax"  # cleanup
    assert lc.session.state == "aborted"


def test_ack_error_aborts(tmp_path):
    printer, lc = make(tmp_path)
    printer.mcu.ack_mode = "error"
    with pytest.raises(CommandError, match="err=3"):
        lc.start_capture(1.0)


def test_capture_receives_blocks_and_completes(tmp_path):
    printer, lc = make(tmp_path)
    session = lc.start_capture(1.0)
    base = clock_now(printer) + int(0.1 * FREQ)
    period = int(FREQ / 1280)
    ticks = [base + i * period for i in range(12)]
    printer.mcu.block(ticks, [1000 + i for i in range(12)], [0] * 12)
    printer.mcu.block(
        [t + 12 * period for t in ticks],
        [1012 + i for i in range(12)],
        [0] * 12,
    )
    run_process(printer, lc)
    lc.stop_capture()
    printer.reactor.now += 1.0
    run_process(printer, lc)
    assert session.state == "complete"
    s = session.summary()
    assert s["samples"] == 24 and s["blocks"] == 2
    assert abs(s["rate_hz"] - 1280) < 1
    assert printer.mcu.sent[-1] == ("stop_prtouch_apax", [lc.apax_oid])


def test_settle_samples_are_dropped(tmp_path):
    printer, lc = make(tmp_path, settle_time=0.05)
    session = lc.start_capture(1.0)
    start = clock_now(printer)
    period = int(FREQ / 1280)
    ticks = [start + i * period for i in range(128)]  # first 100 ms
    printer.mcu.block(ticks, [5] * 128, [0] * 128)
    run_process(printer, lc)
    assert session.settle_dropped > 0
    assert session.ticks[0] >= session.settle_clock


def test_stale_and_wrong_channel_blocks(tmp_path):
    printer, lc = make(tmp_path)
    session = lc.start_capture(1.0)
    old = clock_now(printer) - int(0.5 * FREQ)
    printer.mcu.block([old, old + 100], [1, 2], [0, 0])
    printer.mcu.block([old + int(0.6 * FREQ)] * 1, [1], [0], ch=1)
    run_process(printer, lc)
    assert session.stale_blocks == 1 and session.wrong_channel == 1
    assert session.values == []


def test_duplicate_block_and_gap_are_counted(tmp_path):
    printer, lc = make(tmp_path, settle_time=0.0)
    session = lc.start_capture(1.0)
    base = clock_now(printer) + 1000
    period = int(FREQ / 1280)
    ticks = [base + i * period for i in range(10)]
    printer.mcu.block(ticks, [0] * 10, [0] * 10)
    printer.mcu.block(ticks, [0] * 10, [0] * 10)  # duplicate
    later = [ticks[-1] + 50 * period + i * period for i in range(5)]
    printer.mcu.block(later, [0] * 5, [0] * 5)
    run_process(printer, lc)
    assert session.duplicates == 1 and session.gaps == 1
    assert len(session.values) == 15


def test_length_mismatch_makes_the_session_invalid(tmp_path):
    printer, lc = make(tmp_path, settle_time=0.0)
    session = lc.start_capture(1.0)
    cb = printer.mcu.responses[("resault_prtouch_apax", PRES_OID)]
    t = clock_now(printer) + 1000
    cb(
        {
            "ch": 0,
            "len": 3,
            "ticks": prtouch_codec.encode_frame([t, t + 1, t + 2]),
            "datas": prtouch_codec.encode_frame([1, 2]),
            "espds": prtouch_codec.encode_frame([0, 0, 0]),
        }
    )
    run_process(printer, lc)
    lc.stop_capture()
    printer.reactor.now += 1.0
    run_process(printer, lc)
    assert session.length_mismatch == 1 and session.state == "invalid"


def test_tick_rollover_inside_a_capture(tmp_path):
    printer, lc = make(tmp_path, settle_time=0.0)
    # put the MCU clock just below a 32-bit wrap
    printer.reactor.now = ((1 << 34) - 2000) / FREQ
    session = lc.start_capture(1.0)
    base = clock_now(printer) + 100
    period = int(FREQ / 1280)
    ticks = [base + i * period for i in range(20)]
    printer.mcu.block(ticks, list(range(20)), [0] * 20)
    run_process(printer, lc)
    assert session.ticks == ticks  # monotonic 64-bit across the wrap
    assert session.backwards == 0


def test_sample_limit_overflow(tmp_path):
    printer, lc = make(tmp_path, settle_time=0.0, max_samples=64)
    session = lc.start_capture(1.0)
    base = clock_now(printer) + 1000
    for chunk in range(8):
        ticks = [base + (chunk * 12 + i) * 1000 for i in range(12)]
        printer.mcu.block(ticks, [0] * 12, [0] * 12)
    run_process(printer, lc)
    printer.reactor.now += 1.0
    run_process(printer, lc)
    assert session.overflow and len(session.values) == 64
    assert session.state == "invalid"


def test_blocks_after_the_session_are_late(tmp_path):
    printer, lc = make(tmp_path)
    printer.mcu.block([1, 2], [1, 2], [0, 0])
    assert lc._late_blocks == 1


# --- ownership --------------------------------------------------------------------


def test_second_session_and_armed_probe_are_refused(tmp_path):
    printer, lc = make(tmp_path)
    lc.start_capture(1.0)
    with pytest.raises(CommandError, match="already running"):
        lc.start_capture(1.0)
    lc.abort("test")
    printer.prtouch._armed = True
    with pytest.raises(CommandError, match="probe is armed"):
        lc.start_capture(1.0)


def test_homing_during_capture_aborts_and_raises(tmp_path):
    printer, lc = make(tmp_path)
    session = lc.start_capture(1.0)
    with pytest.raises(CommandError, match="aborted"):
        printer.events["homing:homing_move_begin"](object())
    assert session.state == "aborted"
    assert printer.mcu.sent[-1][0] == "stop_prtouch_apax"


def test_shutdown_aborts_without_sending(tmp_path):
    printer, lc = make(tmp_path)
    session = lc.start_capture(1.0)
    sent = len(printer.mcu.sent)
    printer.events["klippy:shutdown"]()
    assert session.state == "aborted" and len(printer.mcu.sent) == sent


def test_not_while_printing(tmp_path):
    printer, lc = make(tmp_path)
    printer.print_stats.state = "printing"
    with pytest.raises(CommandError, match="printing"):
        lc.start_capture(1.0)


# --- export -------------------------------------------------------------------------


def test_csv_round_trip_and_rotation(tmp_path):
    meta = {"session": 1, "label": "flow=2"}
    rows = [(100, 0.0, 10, 0), (200, 0.001, 12, -3000)]
    path = tmp_path / "k2_load_cell_pa_a.csv"
    k2_load_cell_pa.write_capture_csv(
        str(path), meta, rows, 10.0, 0.0025, 1, FREQ
    )
    cap = analysis.load_capture_csv(str(path))
    assert cap["meta"]["label"] == "flow=2"
    assert cap["values"] == [10, 12] and cap["espds"] == [0, -3000]
    text = path.read_text()
    assert "e_velocity_mm_s_derived" in text
    for name in ("k2_load_cell_pa_b.csv", "k2_load_cell_pa_c.csv", "other.csv"):
        (tmp_path / name).write_text("x")
    k2_load_cell_pa.rotate_files(str(tmp_path), "k2_load_cell_pa_", 2)
    left = sorted(os.listdir(tmp_path))
    assert left == [
        "k2_load_cell_pa_b.csv",
        "k2_load_cell_pa_c.csv",
        "other.csv",
    ]


# --- pressure advance gating ----------------------------------------------------------


class GCmd:
    def __init__(self, params):
        self.params = params
        self.replies = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, **kw):
        value = self.params.get(name, default)
        return None if value is None else int(value)

    def get_float(self, name, default=None, **kw):
        value = self.params.get(name, default)
        return None if value is None else float(value)

    def respond_info(self, text):
        self.replies.append(text)

    def error(self, text):
        return CommandError(text)


def test_pa_calibration_is_disabled_by_default(tmp_path):
    printer, lc = make(tmp_path)
    with pytest.raises(CommandError, match="disabled"):
        lc.cmd_PA_CALIBRATE(GCmd({}))
    assert printer.mcu.sent == []


def test_pa_calibration_never_heats(tmp_path):
    printer, lc = make(tmp_path, pa_calibration="experimental")
    printer.extruder = Extruder(printer.mcu, temp=25.0, target=0.0)
    with pytest.raises(CommandError, match="never heats"):
        lc.cmd_PA_CALIBRATE(GCmd({"POSITION_CONFIRMED": 1}))
    assert printer.gcode.scripts == []


def test_pa_calibration_needs_a_position(tmp_path):
    printer, lc = make(tmp_path, pa_calibration="experimental")
    with pytest.raises(CommandError, match="POSITION_CONFIRMED"):
        lc.cmd_PA_CALIBRATE(GCmd({}))
    assert printer.gcode.scripts == []


def test_pa_filament_limit(tmp_path):
    printer, lc = make(
        tmp_path, pa_calibration="experimental", pa_max_filament=5.0
    )
    with pytest.raises(CommandError, match="pa_max_filament"):
        lc.cmd_PA_CALIBRATE(GCmd({"POSITION_CONFIRMED": 1}))


def test_apply_only_with_a_valid_candidate(tmp_path, monkeypatch):
    printer, lc = make(tmp_path, pa_calibration="experimental")

    def fake_start(duration, label="", allow_printing=False):
        session = k2_load_cell_pa.CaptureSession(1, 0, FREQ, 100, label)
        session.state = "complete"
        return session

    monkeypatch.setattr(lc, "start_capture", fake_start)
    monkeypatch.setattr(lc, "stop_capture", lambda reason=None: None)
    result = {
        "per_capture": {},
        "groups": {},
        "candidate": {"ok": False, "reasons": ["no data"], "candidate": None},
    }
    monkeypatch.setattr(
        k2_load_cell_pa.analysis,
        "analyze_pa_captures",
        lambda c, o=None: result,
    )
    gcmd = GCmd({"POSITION_CONFIRMED": 1, "APPLY": 1, "REPLICATES": 1})
    lc.cmd_PA_CALIBRATE(gcmd)
    assert not any("SET_PRESSURE_ADVANCE" in s for s in printer.gcode.scripts)
    assert printer.gcode.scripts[0] == "SAVE_GCODE_STATE NAME=_K2_PA_CALIBRATE"
    assert (
        printer.gcode.scripts[-1] == "RESTORE_GCODE_STATE NAME=_K2_PA_CALIBRATE"
    )
    assert "unchanged" in gcmd.replies[-1]

    result["candidate"] = {"ok": True, "candidate": 0.031, "reasons": []}
    printer.gcode.scripts.clear()
    lc.cmd_PA_CALIBRATE(
        GCmd({"POSITION_CONFIRMED": 1, "APPLY": 1, "REPLICATES": 1})
    )
    assert "SET_PRESSURE_ADVANCE ADVANCE=0.0310" in printer.gcode.scripts
    assert not any("SAVE_CONFIG" in s for s in printer.gcode.scripts)

    printer.gcode.scripts.clear()
    lc.cmd_PA_CALIBRATE(GCmd({"POSITION_CONFIRMED": 1, "REPLICATES": 1}))
    assert not any("SET_PRESSURE_ADVANCE" in s for s in printer.gcode.scripts)


class Box:
    """Records the box calls in the G-code log, to check the order."""

    def __init__(self, gcode):
        self.gcode = gcode

    def move_to_wastebin(self):
        self.gcode.scripts.append("<wastebin>")

    def flush_clean_snap(self, fan_after=None):
        self.gcode.scripts.append("<clean>")


def fake_captures(lc, monkeypatch, printer):
    def fake_start(duration, label="", allow_printing=False):
        printer.gcode.scripts.append("<capture %s>" % label)
        session = k2_load_cell_pa.CaptureSession(1, 0, FREQ, 100, label)
        session.state = "complete"
        return session

    monkeypatch.setattr(lc, "start_capture", fake_start)
    monkeypatch.setattr(lc, "stop_capture", lambda reason=None: None)
    monkeypatch.setattr(
        k2_load_cell_pa.analysis,
        "analyze_pa_captures",
        lambda c, o=None: {
            "per_capture": {},
            "groups": {},
            "candidate": {"ok": False, "reasons": ["x"], "candidate": None},
        },
    )


def steps(printer):
    keep = ("<", "G1 E1.2000")
    return [s for s in printer.gcode.scripts if s.startswith(keep)]


def test_box_wastebin_and_clean_after_each_capture(tmp_path, monkeypatch):
    printer, lc = make(
        tmp_path, pa_calibration="experimental", pa_warmup=0, pa_prime=0
    )
    printer.box = Box(printer.gcode)
    fake_captures(lc, monkeypatch, printer)
    # no POSITION_CONFIRMED: the box provides the position
    lc.cmd_PA_CALIBRATE(GCmd({"FLOWS": "2,5", "REPLICATES": 1}))
    assert steps(printer) == [
        "<wastebin>",
        "<capture flow=2>",
        "<clean>",
        "G1 E1.2000 F120",
        "<capture flow=5>",
        "<clean>",
    ]
    assert (
        printer.gcode.scripts[-1] == "RESTORE_GCODE_STATE NAME=_K2_PA_CALIBRATE"
    )


def test_box_clean_only_at_the_end(tmp_path, monkeypatch):
    printer, lc = make(tmp_path, pa_calibration="experimental")
    printer.box = Box(printer.gcode)
    fake_captures(lc, monkeypatch, printer)
    lc.cmd_PA_CALIBRATE(GCmd({"FLOWS": "2,5", "REPLICATES": 1, "CLEAN": "end"}))
    assert steps(printer) == [
        "<wastebin>",
        "<capture flow=2>",
        "<capture flow=5>",
        "<clean>",
    ]


def test_clean_gcode_replaces_the_box_clean(tmp_path, monkeypatch):
    printer, lc = make(
        tmp_path,
        pa_calibration="experimental",
        pa_clean_gcode="MY_CLEAN",
        pa_reprime=0.0,
        pa_pulse_time=1.0,
        pa_warmup=0,
        pa_prime=0,
    )
    printer.box = Box(printer.gcode)
    fake_captures(lc, monkeypatch, printer)
    lc.cmd_PA_CALIBRATE(GCmd({"FLOWS": "2,5", "REPLICATES": 1}))
    seq = [
        s for s in printer.gcode.scripts if s.startswith(("<", "MY_", "G1 E1"))
    ]
    assert seq == [
        "<wastebin>",
        "<capture flow=2>",
        "MY_CLEAN",
        "<capture flow=5>",
        "MY_CLEAN",
    ]


def test_pa_box_no_needs_a_position(tmp_path, monkeypatch):
    printer, lc = make(tmp_path, pa_calibration="experimental", pa_box="no")
    printer.box = Box(printer.gcode)
    with pytest.raises(CommandError, match="POSITION_CONFIRMED"):
        lc.cmd_PA_CALIBRATE(GCmd({}))
    assert printer.gcode.scripts == []


def test_pa_box_yes_without_a_box(tmp_path):
    printer, lc = make(tmp_path, pa_calibration="experimental", pa_box="yes")
    with pytest.raises(CommandError, match="needs a"):
        lc.cmd_PA_CALIBRATE(GCmd({"POSITION_CONFIRMED": 1}))
    assert printer.gcode.scripts == []


def test_reprime_counts_in_the_filament_limit(tmp_path):
    # 2+5 mm of pulses, plus one 1.2 mm reprime between the two captures
    plan = k2_load_cell_pa.PaPlan([2.0, 5.0], 1, 1.0, 0.8, 1.2)
    assert plan.filament_mm == pytest.approx(8.2)
    printer, lc = make(
        tmp_path,
        pa_calibration="experimental",
        pa_max_filament=8.0,
        pa_pulse_time=1.0,
    )
    printer.box = Box(printer.gcode)
    with pytest.raises(CommandError, match="pa_max_filament"):
        lc.cmd_PA_CALIBRATE(GCmd({"FLOWS": "2,5", "REPLICATES": 1}))


def test_calibration_error_restores_state(tmp_path, monkeypatch):
    printer, lc = make(tmp_path, pa_calibration="experimental")

    def failing_start(duration, label="", allow_printing=False):
        raise CommandError("boom")

    monkeypatch.setattr(lc, "start_capture", failing_start)
    with pytest.raises(CommandError, match="boom"):
        lc.cmd_PA_CALIBRATE(GCmd({"POSITION_CONFIRMED": 1}))
    assert (
        printer.gcode.scripts[-1] == "RESTORE_GCODE_STATE NAME=_K2_PA_CALIBRATE"
    )
    assert not any("SET_PRESSURE_ADVANCE" in s for s in printer.gcode.scripts)


def test_handlers_do_not_replace_the_probe_handlers(tmp_path):
    printer, lc = make(tmp_path)
    keys = set(printer.mcu.responses)
    # the probe owns ("ack_prtouch", pres_oid) and its step oid; APAX acks
    # use their own oid and blocks use a message name the probe never uses
    assert ("ack_prtouch", PRES_OID) not in keys
    assert keys == {
        ("ack_prtouch", lc.apax_oid),
        ("resault_prtouch_apax", PRES_OID),
    }


def test_command_names_are_valid_klipper_names(tmp_path):
    printer, _ = make(tmp_path)
    assert sorted(printer.gcode.commands) == [
        "LOAD_CELL_CAPTURE",
        "LOAD_CELL_DIAGNOSTIC",
        "LOAD_CELL_PA_ANALYZE",
        "LOAD_CELL_PA_CALIBRATE",
        "LOAD_CELL_STOP",
    ]


def test_pause_inside_the_settle_window_is_not_a_gap(tmp_path):
    # Seen on the K2 Pro: the CS1237 pauses a few ms while it is
    # reconfigured after start (57 of ~64 settle samples arrived).
    printer, lc = make(tmp_path, settle_time=0.05)
    session = lc.start_capture(1.0)
    start = clock_now(printer)
    period = int(FREQ / 1280)
    early = [start + i * period for i in range(5)]
    rest = [early[-1] + 8 * period + i * period for i in range(120)]
    printer.mcu.block(early + rest, [5] * 125, [0] * 125)
    run_process(printer, lc)
    assert session.gaps == 0
    assert session.settle_dropped > 0


def test_analysis_runs_off_the_reactor_thread(tmp_path, monkeypatch):
    import threading

    printer, lc = make(tmp_path, pa_calibration="experimental")
    seen = {}

    def fake_analyze(captures, opts=None):
        seen["thread"] = threading.current_thread().name
        seen["opts"] = opts
        return {
            "per_capture": {},
            "groups": {},
            "candidate": {"ok": False, "reasons": ["x"], "candidate": None},
        }

    monkeypatch.setattr(
        k2_load_cell_pa.analysis, "analyze_pa_captures", fake_analyze
    )
    lc._last_pa_captures = [{"flow": 5.0}]
    lc.cmd_PA_ANALYZE(GCmd({}))
    assert seen["thread"] == "k2_load_cell_pa_analysis"
    assert seen["opts"] == {"model": "fast_component"}


def test_warmup_pulse_is_cleaned_and_not_captured(tmp_path, monkeypatch):
    # K2 Pro bench: the first capture of a run read 25-30 % higher than the
    # next ones; an uncaptured pulse first gives every replicate the same start
    printer, lc = make(tmp_path, pa_calibration="experimental", pa_prime=0)
    printer.box = Box(printer.gcode)
    fake_captures(lc, monkeypatch, printer)
    lc.cmd_PA_CALIBRATE(GCmd({"FLOWS": "5,8", "REPLICATES": 1}))
    seq = [
        s
        for s in printer.gcode.scripts
        if s.startswith(("<", "G1 E1.2000", "G1 E1.2500", "G1 E2.0000"))
    ]
    assert seq == [
        "<wastebin>",
        "G1 E1.2500 F300.0",
        "<clean>",
        "G1 E1.2000 F120",
        "<capture flow=5>",
        "G1 E1.2500 F300.0",
        "<clean>",
        "G1 E1.2000 F120",
        "<capture flow=8>",
        "G1 E2.0000 F480.0",
        "<clean>",
    ]


def test_warmup_counts_in_the_filament_plan():
    # pulses 1.25 + 2.0, warm-up 1.25, two reprimes of 1.2 mm
    plan = k2_load_cell_pa.PaPlan([5.0, 8.0], 1, 0.25, 1.5, 1.2, 1)
    assert plan.filament_mm == pytest.approx(6.9)
    assert k2_load_cell_pa.PaPlan(
        [5.0, 8.0], 1, 0.25, 1.5, 1.2
    ).filament_mm == (pytest.approx(4.45))


class ProfileBox(Box):
    """A [box] with filament profiles (box/filament-pa-maxflow)."""

    def __init__(self, gcode, loaded=1, max_flow=15.0, temp=250):
        super().__init__(gcode)
        self.snapshot = SimpleNamespace(loaded_slot=loaded)
        self.max_flow = max_flow
        self.temp = temp
        self.saved = []

    def is_valid_slot(self, slot):
        return 0 <= slot <= 4

    def slot_target_temp(self, slot):
        return self.temp

    def slot_filament_settings(self, slot):
        return {
            "max_flow": self.max_flow,
            "max_flow_source": "filament 90002",
            "pressure_advance": None,
        }

    def save_slot_pressure_advance(self, slot, value):
        self.saved.append((slot, value))
        return "filament 90002"


def calibrate_with(lc, monkeypatch, printer, params, candidate):
    fake_captures(lc, monkeypatch, printer)
    monkeypatch.setattr(
        k2_load_cell_pa.analysis,
        "analyze_pa_captures",
        lambda c, o=None: {
            "per_capture": {},
            "groups": {},
            "candidate": candidate,
        },
    )
    gcmd = GCmd(dict(params, REPLICATES=1))
    lc.cmd_PA_CALIBRATE(gcmd)
    return gcmd


def test_feed_rates_come_from_the_slot_max_flow_and_save(tmp_path, monkeypatch):
    printer, lc = make(tmp_path, pa_calibration="experimental", pa_warmup=0)
    printer.box = ProfileBox(printer.gcode)
    ok = {"ok": True, "candidate": 0.041, "reasons": []}
    gcmd = calibrate_with(lc, monkeypatch, printer, {"SAVE": 1}, ok)
    # 20/30/40 % of 15 mm3/s over a 1.75 mm filament
    captures = [s for s in printer.gcode.scripts if s.startswith("<capture")]
    assert captures == [
        "<capture flow=1.25>",
        "<capture flow=1.87>",
        "<capture flow=2.49>",
    ]
    assert printer.box.saved == [(1, 0.041)]
    assert any("max flow 15 mm3/s (filament 90002)" in r for r in gcmd.replies)
    assert "saved in filament 90002" in gcmd.replies[-1]
    # no SLOT: nothing loaded or heated
    assert not any(
        s.startswith(("BOX_SELECT_SLOT", "M109")) for s in printer.gcode.scripts
    )


def test_slot_mode_loads_and_heats_first(tmp_path, monkeypatch):
    printer, lc = make(tmp_path, pa_calibration="experimental", pa_warmup=0)
    printer.box = ProfileBox(printer.gcode, loaded=1, temp=250)
    calibrate_with(
        lc,
        monkeypatch,
        printer,
        {"SLOT": 2, "FLOWS": "2,3"},
        {"ok": False, "reasons": ["x"], "candidate": None},
    )
    first = printer.gcode.scripts[:2]
    assert first == ["BOX_SELECT_SLOT SLOT=2", "M109 S250"]
    captures = [s for s in printer.gcode.scripts if s.startswith("<capture")]
    assert captures == ["<capture flow=2>", "<capture flow=3>"]  # FLOWS wins


def test_invalid_candidate_is_not_saved(tmp_path, monkeypatch):
    printer, lc = make(tmp_path, pa_calibration="experimental", pa_warmup=0)
    printer.box = ProfileBox(printer.gcode)
    bad = {"ok": False, "reasons": ["replicates disagree"], "candidate": None}
    gcmd = calibrate_with(lc, monkeypatch, printer, {"SAVE": 1}, bad)
    assert printer.box.saved == []
    assert "nothing saved: replicates disagree" in gcmd.replies[-1]


def test_slot_and_save_need_box_profiles(tmp_path):
    printer, lc = make(tmp_path, pa_calibration="experimental")
    printer.box = Box(printer.gcode)  # no filament profiles
    with pytest.raises(CommandError, match="filament profiles"):
        lc.cmd_PA_CALIBRATE(GCmd({"SLOT": 1}))


def test_priming_purge_fills_the_nozzle_first(tmp_path, monkeypatch):
    # K2 Pro bench: a slot left loaded after a print gave a quarter of the
    # usual force, growing capture after capture: the nozzle was not full
    printer, lc = make(tmp_path, pa_calibration="experimental", pa_warmup=0)
    printer.box = Box(printer.gcode)
    fake_captures(lc, monkeypatch, printer)
    lc.cmd_PA_CALIBRATE(GCmd({"FLOWS": "5", "REPLICATES": 1}))
    seq = [
        s
        for s in printer.gcode.scripts
        if s.startswith(("<", "G1 E20", "G1 E1.2000"))
    ]
    assert seq == [
        "<wastebin>",
        "G1 E20.0000 F120.0",
        "<clean>",
        "G1 E1.2000 F120",
        "<capture flow=5>",
        "<clean>",
    ]
    plan = k2_load_cell_pa.PaPlan([5.0], 1, 0.25, 1.5, 1.2, 0, 20.0)
    assert plan.filament_mm == pytest.approx(1.25 + 20.0 + 1.2)
    # PRIME=0 skips it
    printer.gcode.scripts.clear()
    lc.cmd_PA_CALIBRATE(GCmd({"FLOWS": "5", "REPLICATES": 1, "PRIME": 0}))
    assert not any(s.startswith("G1 E20") for s in printer.gcode.scripts)
