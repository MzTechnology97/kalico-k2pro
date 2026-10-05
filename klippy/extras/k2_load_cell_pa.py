# K2 nozzle load cell: timed capture through the stock firmware's APAX
# stream, diagnostics, CSV export and an experimental pressure advance
# analysis.
#
# Copyright (C) 2026  MzTechnology97
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""[k2_load_cell_pa] - optional, needs [prtouch].

The stock K2 nozzle firmware (prtouch_v3) has an "APAX" mode: once
started it samples the CS1237 continuously and sends blocks of
(tick, sensor counts, E-step interval) triples on its own:

    config_prtouch_apax oid=%c oid_estp=%c
    start_prtouch_apax oid=%c cfg_regs=%c acq_tick=%u
    stop_prtouch_apax oid=%c
    resault_prtouch_apax oid=%c ch=%c len=%c ticks=%.*s datas=%.*s espds=%.*s

Facts from the published prtouch_v3 object (docs/K2_Load_Cell_PA.md):
- start acks with err=0 expar0=0 expar1=acq_tick, stop with 0/0/0;
- blocks carry the oid of the pressure sensor (config_prtouch_pres), not
  the APAX oid;
- start clears the probing sample buffer; the probe re-arms with
  start_prtouch_pres, which reconfigures the sensor;
- a block is sent when the three packed series exceed 41 bytes; the
  partial block at stop is not sent.
"""

import collections
import logging
import os
import threading
import time

from . import k2_pa_analysis as analysis
from .prtouch_codec import (
    FrameError,
    TickUnwrapper,
    decode_frame,
    decode_ticks,
)

CMD_CONFIG = "config_prtouch_apax oid=%c oid_estp=%c"
CMD_START = "start_prtouch_apax oid=%c cfg_regs=%c acq_tick=%u"
CMD_STOP = "stop_prtouch_apax oid=%c"
RSP_BLOCK = (
    "resault_prtouch_apax oid=%c ch=%c len=%c ticks=%.*s datas=%.*s espds=%.*s"
)
RSP_ACK = "ack_prtouch oid=%c err=%c expar0=%u expar1=%u"

ACTIVE_STATES = ("starting", "running", "stopping")
CSV_PREFIX = "k2_load_cell_pa_"
BLOCK_OVERHEAD = 9  # message framing + fixed fields, bytes (approximate)


def _klog(msg, *args, level=logging.info):
    level("k2_load_cell_pa: " + msg, *args)


class CaptureSession:
    """Samples and counters of one capture. Not thread safe: reactor only."""

    def __init__(self, sid, channel, clock_freq, max_samples, label):
        self.sid = sid
        self.channel = channel
        self.clock_freq = clock_freq
        self.max_samples = max_samples
        self.label = label
        self.state = "starting"
        self.reason = ""
        self.ticks = []
        self.values = []
        self.espds = []
        self.blocks = 0
        self.payload_bytes = 0
        self.decode_errors = 0
        self.length_mismatch = 0
        self.wrong_channel = 0
        self.duplicates = 0
        self.backwards = 0
        self.gaps = 0
        self.settle_dropped = 0
        self.stale_blocks = 0
        self.overflow = False
        self.start_clock = None
        self.settle_clock = None
        self.stop_clock = None
        self.started_at = time.time()
        self.notes = []
        self.unwrapper = TickUnwrapper()

    @property
    def active(self):
        return self.state in ACTIVE_STATES

    def add_block(self, params, clock32_to_clock64, gap_ticks):
        """Decode one resault_prtouch_apax block into the session."""
        self.blocks += 1
        ticks_raw = params.get("ticks", b"")
        datas_raw = params.get("datas", b"")
        espds_raw = params.get("espds", b"")
        self.payload_bytes += (
            len(ticks_raw) + len(datas_raw) + len(espds_raw) + BLOCK_OVERHEAD
        )
        if params.get("ch") != self.channel:
            self.wrong_channel += 1
            return
        try:
            # The first tick is a delta from 0 in int32 arithmetic: decode
            # it signed and keep 32 bits (see prtouch_codec.decode_ticks).
            ticks = decode_ticks(ticks_raw)
            datas = decode_frame(datas_raw, True)
            espds = decode_frame(espds_raw, True)
        except FrameError as exc:
            self.decode_errors += 1
            self._note("decode error: %s" % exc)
            return
        count = params.get("len")
        if not (len(ticks) == len(datas) == len(espds) == count):
            self.length_mismatch += 1
            self._note(
                "series lengths %d/%d/%d, len=%s"
                % (len(ticks), len(datas), len(espds), count)
            )
            return
        if not ticks:
            return
        first64 = clock32_to_clock64(ticks[0] & 0xFFFFFFFF)
        if self.start_clock is not None and first64 < self.start_clock:
            # Samples from before this session's start (a previous run).
            self.stale_blocks += 1
            return
        if self.ticks and first64 <= self.ticks[-1]:
            self.duplicates += 1
            return
        self.unwrapper.seed(first64)
        last = self.ticks[-1] if self.ticks else None
        for index, (tick, value, espd) in enumerate(zip(ticks, datas, espds)):
            tick64 = first64 if index == 0 else self.unwrapper.unwrap(tick)
            if last is not None:
                if tick64 <= last:
                    self.backwards += 1
                    continue
                if gap_ticks and tick64 - last > gap_ticks:
                    self.gaps += 1
            last = tick64
            if self.settle_clock is not None and tick64 < self.settle_clock:
                self.settle_dropped += 1
                continue
            if len(self.ticks) >= self.max_samples:
                self.overflow = True
                return
            self.ticks.append(tick64)
            self.values.append(value)
            self.espds.append(espd)

    def _note(self, text):
        if len(self.notes) < 20:
            self.notes.append(text)

    def times(self):
        if not self.ticks:
            return []
        t0 = self.ticks[0]
        return [(t - t0) / self.clock_freq for t in self.ticks]

    def finish(self, aborted_reason=None):
        if aborted_reason:
            self.state, self.reason = "aborted", aborted_reason
        elif not self.ticks:
            self.state, self.reason = "invalid", "no samples received"
        elif self.decode_errors or self.length_mismatch:
            self.state, self.reason = "invalid", "malformed blocks"
        elif self.overflow:
            self.state, self.reason = "invalid", "sample limit reached"
        else:
            self.state = "complete"

    def summary(self):
        times = self.times()
        stats = analysis.series_stats(times, self.values)
        duration = times[-1] if times else 0.0
        return {
            "session": self.sid,
            "state": self.state,
            "reason": self.reason,
            "label": self.label,
            "channel": self.channel,
            "samples": len(self.values),
            "blocks": self.blocks,
            "duration_s": duration,
            "rate_hz": stats["rate_hz"],
            "baseline": stats["baseline"],
            "noise": stats["noise"],
            "drift_per_s": stats["drift_per_s"],
            "range": stats["range"],
            "saturated": stats["saturated"],
            "payload_bytes_per_s": (
                self.payload_bytes / duration if duration else None
            ),
            "decode_errors": self.decode_errors,
            "length_mismatch": self.length_mismatch,
            "wrong_channel": self.wrong_channel,
            "duplicates": self.duplicates,
            "backwards": self.backwards,
            "gaps": self.gaps,
            "settle_dropped": self.settle_dropped,
            "stale_blocks": self.stale_blocks,
            "overflow": self.overflow,
            "notes": list(self.notes),
        }


class K2LoadCell:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        if not config.has_section("prtouch"):
            raise config.error("[k2_load_cell_pa] needs a [prtouch] section")
        self.prtouch = self.printer.load_object(config, "prtouch")
        self.mcu = self.prtouch.pres_mcu
        self.pres_oid = self.prtouch.pres_oid
        self.apax_oid = self.mcu.create_oid()

        self.channel = config.getint("channel", 0, minval=0, maxval=3)
        self.cfg_regs = config.getint(
            "cfg_regs", self.prtouch.pres_cfg_regs, minval=0, maxval=255
        )
        self.acq_tkms = config.getfloat(
            "acq_tkms", self.prtouch.pres_acq_tkms, minval=0.1, maxval=1000.0
        )
        self.max_duration = config.getfloat(
            "max_duration", 10.0, above=0.0, maxval=60.0
        )
        self.settle_time = config.getfloat(
            "settle_time", 0.05, minval=0.0, maxval=2.0
        )
        self.ack_timeout = config.getfloat("ack_timeout", 0.5, above=0.0)
        self.block_grace = config.getfloat(
            "block_grace", 0.25, minval=0.0, maxval=2.0
        )
        nominal = analysis.decode_cs1237_config(self.cfg_regs)["rate_hz"]
        self.max_samples = config.getint(
            "max_samples",
            int(self.max_duration * nominal * 1.25) + 64,
            minval=64,
            maxval=200000,
        )
        self.export = config.getboolean("export", True)
        self.output_dir = config.get("output_dir", None)
        self.max_files = config.getint("max_files", 20, minval=1)
        self.pa_mode = config.getchoice(
            "pa_calibration",
            {"disabled": "disabled", "experimental": "experimental"},
            "disabled",
        )
        self.pa_flows = config.getfloatlist("pa_flows", (2.0, 5.0))
        self.pa_pulse_time = config.getfloat(
            "pa_pulse_time", 1.0, above=0.0, maxval=5.0
        )
        self.pa_rest_time = config.getfloat(
            "pa_rest_time", 0.8, above=0.2, maxval=5.0
        )
        self.pa_replicates = config.getint(
            "pa_replicates", 3, minval=1, maxval=10
        )
        self.pa_max_filament = config.getfloat(
            "pa_max_filament", 150.0, above=0.0, maxval=1000.0
        )
        self.pa_purge_gcode = config.get("pa_purge_gcode", "")
        mcu_name = self.mcu.get_name()
        mcu_section = "mcu" if mcu_name == "mcu" else "mcu " + mcu_name
        self.mcu_baud = config.getsection(mcu_section).getint(
            "baud", 250000, note_valid=False
        )

        self.available = False
        self.unavailable_reason = "MCU not configured yet"
        self.start_cmd = self.stop_cmd = None
        self.clock_freq = None
        self.acq_tick = None
        self.e_stepper = None
        self.e_step_dist = None
        self.e_dir_sign = None

        self.session = None
        self.last_summary = None
        self.recent = collections.deque(maxlen=64)  # finished sessions
        self._sid = 0
        self._lock = threading.Lock()
        self._pending = collections.deque()
        self._accepting = None  # session id accepting blocks
        self._acks = collections.deque(maxlen=16)
        self._late_blocks = 0
        self._process_timer = self.reactor.register_timer(self._process)
        self._stop_timer = self.reactor.register_timer(self._auto_stop)
        self._finalize_at = None
        self._export_threads = []

        self.mcu.register_config_callback(self._build_config)
        self.mcu.register_response(
            self._handle_ack, "ack_prtouch", self.apax_oid
        )
        self.mcu.register_response(
            self._handle_block, "resault_prtouch_apax", self.pres_oid
        )
        self.printer.register_event_handler("klippy:shutdown", self._shutdown)
        self.printer.register_event_handler("klippy:disconnect", self._shutdown)
        self.printer.register_event_handler(
            "homing:homing_move_begin", self._guard_homing
        )
        for name, func, desc in (
            ("K2_LOAD_CELL_CAPTURE", self.cmd_CAPTURE, self.cmd_CAPTURE_help),
            ("K2_LOAD_CELL_STOP", self.cmd_STOP, self.cmd_STOP_help),
            (
                "K2_LOAD_CELL_DIAGNOSTIC",
                self.cmd_DIAGNOSTIC,
                self.cmd_DIAGNOSTIC_help,
            ),
            ("K2_PA_ANALYZE", self.cmd_PA_ANALYZE, self.cmd_PA_ANALYZE_help),
            (
                "K2_PA_CALIBRATE",
                self.cmd_PA_CALIBRATE,
                self.cmd_PA_CALIBRATE_help,
            ),
        ):
            self.gcode.register_command(name, func, desc=desc)

    # --- configuration ------------------------------------------------------

    def _msgparser_has(self, fmt):
        name = fmt.split()[0]
        try:
            parser = self.mcu._serial.get_msgparser()
        except Exception:
            return False
        msg = parser.messages_by_name.get(name)
        return msg is not None and msg.msgformat == fmt

    def _build_config(self):
        missing = [
            fmt.split()[0]
            for fmt in (CMD_CONFIG, CMD_START, CMD_STOP, RSP_BLOCK, RSP_ACK)
            if not self._msgparser_has(fmt)
        ]
        if missing:
            self.unavailable_reason = (
                "nozzle firmware has no APAX support (missing %s)"
                % ", ".join(missing)
            )
            _klog(self.unavailable_reason, level=logging.warning)
            return
        extruder = self.printer.lookup_object("extruder", None)
        e_stepper = getattr(
            getattr(extruder, "extruder_stepper", None), "stepper", None
        )
        if e_stepper is None:
            self.unavailable_reason = "no extruder stepper"
            return
        if e_stepper.get_mcu() is not self.mcu:
            self.unavailable_reason = (
                "the extruder stepper is not on the sensor MCU (%s)"
                % self.mcu.get_name()
            )
            return
        self.e_stepper = e_stepper
        self.mcu.add_config_cmd(
            "config_prtouch_apax oid=%d oid_estp=%d"
            % (self.apax_oid, e_stepper.get_oid())
        )
        self.start_cmd = self.mcu.lookup_command(CMD_START, cq=None)
        self.stop_cmd = self.mcu.lookup_command(CMD_STOP, cq=None)
        self.clock_freq = self.mcu.get_constant_float("CLOCK_FREQ")
        self.acq_tick = max(
            1, int(round(self.acq_tkms * 0.001 * self.clock_freq))
        )
        self.e_step_dist = e_stepper.get_step_dist()
        # The MCU reports -interval when the queued step direction bit is
        # set. Klipper sends that bit with the dir pin inversion applied, so
        # positive extrusion has a set bit unless the pin is inverted.
        invert_dir = e_stepper.get_dir_inverted()[0]
        self.e_dir_sign = 1 if invert_dir else -1
        self.available = True
        self.unavailable_reason = ""

    # --- MCU messages (serial thread: keep these short) ---------------------

    def _handle_ack(self, params):
        with self._lock:
            self._acks.append(params)

    def _handle_block(self, params):
        with self._lock:
            if self._accepting is None:
                self._late_blocks += 1
                return
            self._pending.append((self._accepting, params))

    def _wait_ack(self, expected_expar1, label):
        deadline = self.reactor.monotonic() + self.ack_timeout
        while True:
            with self._lock:
                acks = list(self._acks)
                self._acks.clear()
            for ack in acks:
                if ack.get("oid") != self.apax_oid:
                    continue
                if ack.get("err", 0):
                    raise self.printer.command_error(
                        "k2_load_cell_pa: %s failed (err=%s expar0=%s expar1=%s)"
                        % (
                            label,
                            ack.get("err"),
                            ack.get("expar0"),
                            ack.get("expar1"),
                        )
                    )
                if (
                    ack.get("expar0", 0) == 0
                    and ack.get("expar1", 0) == expected_expar1
                ):
                    return ack
            now = self.reactor.monotonic()
            if now >= deadline:
                raise self.printer.command_error(
                    "k2_load_cell_pa: %s ack timeout" % label
                )
            self.reactor.pause(min(deadline, now + 0.005))

    # --- session control ------------------------------------------------------

    def _check_can_start(self, allow_printing=False):
        if not self.available:
            raise self.printer.command_error(
                "k2_load_cell_pa: unavailable: %s" % self.unavailable_reason
            )
        if self.printer.is_shutdown():
            raise self.printer.command_error(
                "k2_load_cell_pa: printer is shut down"
            )
        if self.session is not None and self.session.active:
            raise self.printer.command_error(
                "k2_load_cell_pa: a capture is already running (session %d)"
                % self.session.sid
            )
        if getattr(self.prtouch, "_armed", False):
            raise self.printer.command_error(
                "k2_load_cell_pa: the probe is armed; finish probing first"
            )
        if not allow_printing:
            stats = self.printer.lookup_object("print_stats", None)
            state = getattr(stats, "state", "")
            if state in ("printing", "paused"):
                raise self.printer.command_error(
                    "k2_load_cell_pa: not available while a print is %s" % state
                )

    def start_capture(self, duration, label="", allow_printing=False):
        self._check_can_start(allow_printing)
        duration = min(duration, self.max_duration)
        self._sid += 1
        session = CaptureSession(
            self._sid,
            self.channel,
            self.clock_freq,
            self.max_samples,
            label,
        )
        now = self.reactor.monotonic()
        clock = self.mcu.print_time_to_clock(self.mcu.estimated_print_time(now))
        session.start_clock = clock
        session.settle_clock = clock + int(self.settle_time * self.clock_freq)
        with self._lock:
            self._pending.clear()
            self._acks.clear()
            self._accepting = session.sid
        self.session = session
        try:
            self.start_cmd.send([self.apax_oid, self.cfg_regs, self.acq_tick])
            self._wait_ack(self.acq_tick, "start_prtouch_apax")
        except Exception as exc:
            self._send_stop_quietly()
            with self._lock:
                self._accepting = None
            session.finish(aborted_reason="start failed: %s" % exc)
            self._remember(session)
            raise
        session.state = "running"
        self.reactor.update_timer(self._process_timer, now + 0.05)
        self.reactor.update_timer(self._stop_timer, now + duration)
        _klog("session %d started for %.2f s", session.sid, duration)
        return session

    def stop_capture(self, reason=None):
        session = self.session
        if session is None or session.state not in ("starting", "running"):
            return session
        self.reactor.update_timer(self._stop_timer, self.reactor.NEVER)
        session.state = "stopping"
        note = None
        try:
            self.stop_cmd.send([self.apax_oid])
            self._wait_ack(0, "stop_prtouch_apax")
        except Exception as exc:
            note = "stop: %s" % exc
        now = self.reactor.monotonic()
        session.stop_clock = self.mcu.print_time_to_clock(
            self.mcu.estimated_print_time(now)
        )
        if note:
            session.notes.append(note)
        session.notes.append(
            "the firmware does not send the last partial block at stop"
        )
        self._finalize_at = (now + self.block_grace, reason)
        self.reactor.update_timer(self._process_timer, self.reactor.NOW)
        return session

    def wait_session(self, session, timeout):
        deadline = self.reactor.monotonic() + timeout
        while session.active:
            now = self.reactor.monotonic()
            if now >= deadline:
                self.abort("wait timeout")
                break
            self.reactor.pause(min(deadline, now + 0.05))
        return session

    def abort(self, reason, send_stop=True):
        session = self.session
        if session is None or not session.active:
            return
        self.reactor.update_timer(self._stop_timer, self.reactor.NEVER)
        if send_stop:
            self._send_stop_quietly()
        with self._lock:
            self._accepting = None
            self._pending.clear()
        session.finish(aborted_reason=reason)
        self._remember(session)
        _klog(
            "session %d aborted: %s", session.sid, reason, level=logging.warning
        )

    def _send_stop_quietly(self):
        try:
            if self.stop_cmd is not None:
                self.stop_cmd.send([self.apax_oid])
        except Exception:
            logging.exception("k2_load_cell_pa: stop during cleanup")

    def _auto_stop(self, eventtime):
        self.stop_capture()
        return self.reactor.NEVER

    def _process(self, eventtime):
        session = self.session
        if session is None:
            return self.reactor.NEVER
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        gap_ticks = None
        if self.clock_freq:
            rate = analysis.decode_cs1237_config(self.cfg_regs)["rate_hz"]
            gap_ticks = int(3.0 * self.clock_freq / rate)
        for sid, params in pending:
            if sid != session.sid:
                session.stale_blocks += 1
                continue
            session.add_block(params, self.mcu.clock32_to_clock64, gap_ticks)
        if session.overflow and session.state == "running":
            self.stop_capture()
        if self._finalize_at is not None:
            when, reason = self._finalize_at
            if eventtime >= when:
                self._finalize_at = None
                with self._lock:
                    self._accepting = None
                    for sid, params in self._pending:
                        if sid == session.sid:
                            session.add_block(
                                params, self.mcu.clock32_to_clock64, gap_ticks
                            )
                    self._pending.clear()
                session.finish(aborted_reason=reason)
                self._remember(session)
                return self.reactor.NEVER
            return min(when, eventtime + 0.05)
        if not session.active:
            return self.reactor.NEVER
        return eventtime + 0.05

    def _remember(self, session):
        self.last_summary = session.summary()
        self.recent.append(session)
        if self.export and session.ticks:
            self._export_async(session)

    def _shutdown(self, *args):
        # Never send commands here: the MCU may be in shutdown.
        self.abort("printer shutdown or disconnect", send_stop=False)

    def _guard_homing(self, hmove):
        if self.session is not None and self.session.active:
            self.abort("homing started during a capture")
            raise self.printer.command_error(
                "k2_load_cell_pa: a capture was running; it was aborted. "
                "Repeat the homing/probing."
            )

    # --- export -----------------------------------------------------------------

    def _output_dir(self):
        if self.output_dir:
            return os.path.expanduser(self.output_dir)
        log_file = self.printer.get_start_args().get("log_file")
        if log_file:
            return os.path.dirname(os.path.abspath(log_file))
        return os.path.expanduser("~/printer_data/logs")

    def capture_metadata(self, session):
        eventtime = self.reactor.monotonic()
        meta = {
            "session": session.sid,
            "label": session.label,
            "date": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(session.started_at)
            ),
            "mcu": self.mcu.get_name(),
            "clock_freq": self.clock_freq,
            "cfg_regs": self.cfg_regs,
            "acq_tick": self.acq_tick,
            "e_step_dist": self.e_step_dist,
            "e_dir_sign": self.e_dir_sign,
            "e_velocity": "derived from the E step interval, not measured",
            "counts": "raw CS1237 counts, no force calibration",
        }
        meta.update(
            {
                "cs1237_" + k: v
                for k, v in analysis.decode_cs1237_config(self.cfg_regs).items()
            }
        )
        try:
            meta["mcu_version"] = self.mcu.get_status(eventtime).get(
                "mcu_version"
            )
        except Exception:
            meta["mcu_version"] = None
        meta["software"] = self.printer.get_start_args().get("software_version")
        extruder = self.printer.lookup_object("extruder", None)
        if extruder is not None:
            st = extruder.get_status(eventtime)
            meta["extruder_temp"] = st.get("temperature")
            meta["extruder_target"] = st.get("target")
            meta["pressure_advance"] = st.get("pressure_advance")
            meta["smooth_time"] = st.get("smooth_time")
        fan = self.printer.lookup_object("fan", None)
        if fan is not None:
            meta["part_fan"] = fan.get_status(eventtime).get("speed")
        for key, value in session.summary().items():
            if key != "notes":
                meta[key] = value
        meta["notes"] = "; ".join(session.notes)
        return meta

    def _export_async(self, session):
        path = os.path.join(
            self._output_dir(),
            "%s%s_%03d.csv"
            % (
                CSV_PREFIX,
                time.strftime(
                    "%Y%m%d-%H%M%S", time.localtime(session.started_at)
                ),
                session.sid,
            ),
        )
        meta = self.capture_metadata(session)
        rows = list(
            zip(session.ticks, session.times(), session.values, session.espds)
        )
        baseline = meta.get("baseline")
        session.csv_path = path

        def write():
            try:
                write_capture_csv(
                    path,
                    meta,
                    rows,
                    baseline,
                    self.e_step_dist,
                    self.e_dir_sign,
                    self.clock_freq,
                )
                rotate_files(os.path.dirname(path), CSV_PREFIX, self.max_files)
            except Exception:
                logging.exception("k2_load_cell_pa: export failed")

        thread = threading.Thread(target=write, name="k2_load_cell_pa_export")
        thread.daemon = True
        thread.start()
        self._export_threads = [
            t for t in self._export_threads if t.is_alive()
        ] + [thread]

    # --- status -------------------------------------------------------------------

    def get_status(self, eventtime):
        session = self.session
        return {
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "state": session.state if session else "idle",
            "session": session.sid if session else None,
            "last": self.last_summary,
            "pa_calibration": self.pa_mode,
        }

    # --- G-code -----------------------------------------------------------------

    cmd_CAPTURE_help = (
        "Capture the nozzle load cell: K2_LOAD_CELL_CAPTURE [DURATION=s] "
        "[LABEL=text] [WAIT=1]"
    )

    def cmd_CAPTURE(self, gcmd):
        duration = gcmd.get_float(
            "DURATION", 2.0, above=0.0, maxval=self.max_duration
        )
        label = gcmd.get("LABEL", "")
        session = self.start_capture(duration, label)
        if gcmd.get_int("WAIT", 1, minval=0, maxval=1):
            self.wait_session(session, duration + self.ack_timeout + 2.0)
            gcmd.respond_info(self._format_summary(session.summary()))
        else:
            gcmd.respond_info(
                "k2_load_cell_pa: session %d running for %.2f s"
                % (session.sid, duration)
            )

    cmd_STOP_help = "Stop the running load cell capture"

    def cmd_STOP(self, gcmd):
        session = self.stop_capture()
        if session is None:
            gcmd.respond_info("k2_load_cell_pa: no capture")
            return
        self.wait_session(session, self.block_grace + 2.0)
        gcmd.respond_info(self._format_summary(session.summary()))

    cmd_DIAGNOSTIC_help = "Load cell capture: availability and last session"

    def cmd_DIAGNOSTIC(self, gcmd):
        cfg = analysis.decode_cs1237_config(self.cfg_regs)
        lines = [
            "k2_load_cell_pa: %s"
            % (
                "available"
                if self.available
                else "unavailable: %s" % self.unavailable_reason
            ),
            "MCU %s, clock %s Hz, link %d baud"
            % (self.mcu.get_name(), self.clock_freq, self.mcu_baud),
            "CS1237 cfg_regs=%d: nominal %.0f Hz, gain %d, channel %d"
            % (self.cfg_regs, cfg["rate_hz"], cfg["gain"], cfg["channel"]),
            "late blocks outside a session: %d" % self._late_blocks,
        ]
        if self.last_summary:
            lines.append(self._format_summary(self.last_summary))
        gcmd.respond_info("\n".join(lines))

    def _format_summary(self, s):
        load = s.get("payload_bytes_per_s")
        capacity = self.mcu_baud / 10.0
        text = (
            "session %(session)d %(state)s %(reason)s: %(samples)d samples in "
            "%(blocks)d blocks over %(duration_s).3f s"
        ) % s
        text += "\nrate %s Hz, baseline %s, noise %s, drift %s/s, range %s" % (
            _fmt(s["rate_hz"], "%.1f"),
            _fmt(s["baseline"], "%.0f"),
            _fmt(s["noise"], "%.1f"),
            _fmt(s["drift_per_s"], "%.1f"),
            _fmt(s["range"], "%d"),
        )
        text += "\nlink load %s B/s (%s of %d baud)" % (
            _fmt(load, "%.0f"),
            "n/a" if load is None else "%.0f%%" % (100.0 * load / capacity),
            self.mcu_baud,
        )
        problems = [
            "%s=%d" % (k, s[k])
            for k in (
                "decode_errors",
                "length_mismatch",
                "wrong_channel",
                "duplicates",
                "backwards",
                "gaps",
                "stale_blocks",
            )
            if s[k]
        ]
        if s["saturated"]:
            problems.append("saturated")
        if problems:
            text += "\nproblems: " + ", ".join(problems)
        if s.get("notes"):
            text += "\nnotes: " + "; ".join(s["notes"])
        return text

    cmd_PA_ANALYZE_help = (
        "Analyze load cell captures: K2_PA_ANALYZE [FILES=a.csv,b.csv] "
        "(default: the last calibration run)"
    )

    def cmd_PA_ANALYZE(self, gcmd):
        files = gcmd.get("FILES", "")
        captures = []
        if files:
            base = self._output_dir()
            for name in files.split(","):
                name = os.path.basename(name.strip())
                path = os.path.join(base, name)
                try:
                    cap = analysis.load_capture_csv(path)
                except (OSError, ValueError, KeyError) as exc:
                    raise gcmd.error(
                        "k2_load_cell_pa: %s: %s" % (name, exc)
                    ) from exc
                if cap["flow"] is None:
                    raise gcmd.error(
                        "k2_load_cell_pa: %s has no flow=<mm/s> label" % name
                    )
                captures.append(cap)
        else:
            captures = list(getattr(self, "_last_pa_captures", []))
        if not captures:
            raise gcmd.error("k2_load_cell_pa: no captures to analyze")
        gcmd.respond_info(
            analysis.format_pa_report(analysis.analyze_pa_captures(captures))
        )

    cmd_PA_CALIBRATE_help = (
        "EXPERIMENTAL: E-only pulses with load cell capture and a pressure "
        "advance candidate. K2_PA_CALIBRATE [FLOWS=2,5] [REPLICATES=3] "
        "[POSITION_CONFIRMED=1] [APPLY=0]"
    )

    def cmd_PA_CALIBRATE(self, gcmd):
        if self.pa_mode != "experimental":
            raise gcmd.error(
                "k2_load_cell_pa: pressure advance calibration is disabled; set "
                "pa_calibration: experimental in [k2_load_cell_pa] to try it"
            )
        flows = [
            float(v)
            for v in gcmd.get(
                "FLOWS", ",".join(str(f) for f in self.pa_flows)
            ).split(",")
            if v.strip()
        ]
        replicates = gcmd.get_int(
            "REPLICATES", self.pa_replicates, minval=1, maxval=10
        )
        apply = gcmd.get_int("APPLY", 0, minval=0, maxval=1)
        plan = PaPlan(
            flows,
            replicates,
            self.pa_pulse_time,
            self.pa_rest_time,
        )
        self._check_pa_preconditions(gcmd, plan)
        toolhead = self.printer.lookup_object("toolhead")
        extruder = self.printer.lookup_object("extruder")
        before = extruder.get_status(self.reactor.monotonic())
        run = self.gcode.run_script_from_command
        if self.pa_purge_gcode:
            run(self.pa_purge_gcode)
        elif not gcmd.get_int("POSITION_CONFIRMED", 0, minval=0, maxval=1):
            raise gcmd.error(
                "k2_load_cell_pa: move the nozzle over the purge area first and "
                "pass POSITION_CONFIRMED=1 (or set pa_purge_gcode)"
            )
        captures = []
        run("SAVE_GCODE_STATE NAME=_K2_PA_CALIBRATE")
        try:
            run("M83")
            for flow in plan.flows:
                for rep in range(plan.replicates):
                    toolhead.wait_moves()
                    # The duration is only a safety cap: the capture is
                    # stopped after the pulse has really ended plus a rest.
                    session = self.start_capture(
                        self.max_duration, label="flow=%g" % flow
                    )
                    run("G4 P%d" % int(plan.rest_time * 1000))
                    run(
                        "G1 E%.4f F%.1f" % (flow * plan.pulse_time, flow * 60.0)
                    )
                    toolhead.wait_moves()
                    self.reactor.pause(
                        self.reactor.monotonic() + plan.rest_time + 0.1
                    )
                    self.stop_capture()
                    self.wait_session(session, self.block_grace + 3.0)
                    if session.state != "complete":
                        raise gcmd.error(
                            "k2_load_cell_pa: capture %d %s: %s"
                            % (session.sid, session.state, session.reason)
                        )
                    captures.append(
                        {
                            "flow": flow,
                            "times": session.times(),
                            "values": list(session.values),
                            "espds": list(session.espds),
                            "meta": {"session": session.sid},
                        }
                    )
                    gcmd.respond_info(
                        "k2_load_cell_pa: flow %g mm/s replicate %d/%d captured"
                        % (flow, rep + 1, plan.replicates)
                    )
        except Exception:
            self.abort("calibration error")
            raise
        finally:
            run("RESTORE_GCODE_STATE NAME=_K2_PA_CALIBRATE")
        self._last_pa_captures = captures
        result = analysis.analyze_pa_captures(captures)
        gcmd.respond_info(analysis.format_pa_report(result))
        candidate = result["candidate"]
        if apply and candidate["ok"]:
            run("SET_PRESSURE_ADVANCE ADVANCE=%.4f" % candidate["candidate"])
            gcmd.respond_info(
                "k2_load_cell_pa: pressure advance set to %.4f for this session "
                "(was %.4f); not saved"
                % (candidate["candidate"], before.get("pressure_advance", 0.0))
            )
        elif apply:
            gcmd.respond_info(
                "k2_load_cell_pa: no valid candidate; pressure advance unchanged "
                "(%.4f)" % before.get("pressure_advance", 0.0)
            )

    def _check_pa_preconditions(self, gcmd, plan):
        self._check_can_start()
        extruder = self.printer.lookup_object("extruder", None)
        if extruder is None:
            raise gcmd.error("k2_load_cell_pa: no extruder")
        eventtime = self.reactor.monotonic()
        st = extruder.get_status(eventtime)
        temp, target = st.get("temperature", 0.0), st.get("target", 0.0)
        if (
            target <= 0
            or not st.get("can_extrude", False)
            or abs(temp - target) > 5.0
        ):
            raise gcmd.error(
                "k2_load_cell_pa: heat the nozzle to printing temperature first "
                "(now %.1f/%.1f C); this command never heats" % (temp, target)
            )
        max_e_v = getattr(extruder, "max_e_velocity", None)
        max_e_dist = getattr(extruder, "max_e_dist", None)
        if not plan.flows:
            raise gcmd.error("k2_load_cell_pa: FLOWS is empty")
        for flow in plan.flows:
            if flow <= 0 or (max_e_v and flow > max_e_v):
                raise gcmd.error(
                    "k2_load_cell_pa: flow %g mm/s outside the extruder limit"
                    % flow
                )
            if max_e_dist and flow * plan.pulse_time > max_e_dist:
                raise gcmd.error(
                    "k2_load_cell_pa: a %.1f mm pulse exceeds max_extrude_only_"
                    "distance" % (flow * plan.pulse_time)
                )
        if plan.filament_mm > self.pa_max_filament:
            raise gcmd.error(
                "k2_load_cell_pa: plan uses %.0f mm of filament, limit %.0f "
                "(pa_max_filament)" % (plan.filament_mm, self.pa_max_filament)
            )
        if plan.capture_time > self.max_duration:
            raise gcmd.error(
                "k2_load_cell_pa: each capture needs %.1f s, max_duration is %.1f"
                % (plan.capture_time, self.max_duration)
            )


class PaPlan:
    def __init__(self, flows, replicates, pulse_time, rest_time):
        self.flows = list(flows)
        self.replicates = replicates
        self.pulse_time = pulse_time
        self.rest_time = rest_time
        # rest, pulse, rest again for the decay, plus margin
        # rest before, pulse, rest after for the decay, motion queue margin
        self.capture_time = 2.0 * rest_time + pulse_time + 0.5
        self.filament_mm = sum(f * pulse_time for f in self.flows) * replicates


def write_capture_csv(
    path, meta, rows, baseline, step_dist, dir_sign, clock_freq
):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as out:
        for key, value in meta.items():
            out.write("# %s: %s\n" % (key, value))
        out.write(
            "tick,time_s,raw_counts,rel_counts,e_interval_ticks,"
            "e_velocity_mm_s_derived\n"
        )
        for tick, t, value, espd in rows:
            rel = "" if baseline is None else "%.1f" % (value - baseline)
            if espd and step_dist and clock_freq and dir_sign:
                vel = "%.4f" % (dir_sign * step_dist * clock_freq / espd)
            else:
                vel = "0" if espd == 0 else ""
            out.write(
                "%d,%.6f,%d,%s,%d,%s\n" % (tick, t, value, rel, espd, vel)
            )
    os.replace(tmp, path)


def rotate_files(directory, prefix, keep):
    names = sorted(
        n
        for n in os.listdir(directory)
        if n.startswith(prefix) and n.endswith(".csv")
    )
    for name in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def _fmt(value, fmt):
    return "n/a" if value is None else fmt % value


def load_config(config):
    return K2LoadCell(config)
