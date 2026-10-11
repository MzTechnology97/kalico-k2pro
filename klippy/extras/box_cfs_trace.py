# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Optional, passive CFS operation timeline from Kalico's already cached state.

No serial commands, firmware probes, event hooks in box.py, motor commands,
RFID locks, NVM writes, file logging, or MCU modifications. This is HOST
activity tracing, not raw stepper telemetry or proof of MCU motor state.

Enable explicitly with [box_cfs_trace]. Capture is OFF until G-code START.
The reactor may be occupied by synchronous BOX_LOAD/BOX_UNLOAD; long timer
gaps mean missing visibility and MUST be reported instead of interpolated.
"""

from collections import deque

# 5 Hz samples of LOCAL Python fields, NEVER 5 Hz RS-485 commands.
DEFAULT_INTERVAL_S = 0.20
MAX_RECORDS = 256
STATUS_TAIL = 8
MAX_REPORT_LINES = 50


class BoxCfsTrace:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        # 5..20 Hz cache-only observation. Actual Klipper scheduling may
        # be slower; gaps are surfaced, not treated as proof of no motion.
        self.interval = config.getfloat(
            "sample_interval", DEFAULT_INTERVAL_S, minval=0.05, maxval=1.0)
        self.history_max = config.getint(
            "max_events", MAX_RECORDS, minval=16, maxval=1024)
        self.history = deque(maxlen=self.history_max)
        self.running = False
        self.samples = 0
        self.changes = 0
        self.stage_events = 0
        self.operation_ends = 0
        self.host_stage_intervals = 0
        self.max_host_stage_interval_s = 0.0
        self.last_operation_host_span_s = None
        self._stage_started_at = None
        self._stage_name = None
        self._first_stage_at = None
        self.poll_gaps = 0
        self.max_gap_s = 0.0
        self.last_poll = None
        self.last_state = None
        self.started = None
        self.last_error = None
        self._timer = self.reactor.register_timer(self._poll)
        self.gcode.register_command("BOX_CFS_TRACE_START", self.cmd_start)
        self.gcode.register_command("BOX_CFS_TRACE_STOP", self.cmd_stop)
        self.gcode.register_command("BOX_CFS_TRACE_SHOW", self.cmd_show)
        self.gcode.register_command("BOX_CFS_TRACE_CLEAR", self.cmd_clear)
        # Events originate from the host's existing stage transitions.
        # Unlike timed polls, callbacks run BEFORE synchronous motor waits.
        self.printer.register_event_handler(
            "box:cfs_operation_stage", self._on_stage)
        self.printer.register_event_handler(
            "box:cfs_operation_end", self._on_operation_end)
        self.printer.register_event_handler("klippy:shutdown", self._shutdown)
        self.printer.register_event_handler("klippy:disconnect", self._shutdown)

    def _cached_state(self):
        """Read only already-populated fields; do not query the CFS or driver."""
        box = self.printer.lookup_object("box", None)
        if box is None:
            return None
        snap = getattr(box, "snapshot", None)
        progress = getattr(box, "operation_progress", None) or {}
        if not isinstance(progress, dict):
            progress = {}
        depth = getattr(box, "operation_depth", 0)
        return {
            "operation_active": bool(depth),
            "operation_kind": progress.get("kind"),
            "operation_slot": progress.get("slot"),
            "operation_stage": progress.get("stage"),
            "box_data_ready": getattr(snap, "data_ready", None),
            "box_state_code": getattr(snap, "state_code", None),
            "box_status_code": getattr(snap, "status_code", None),
            "loaded_slot": getattr(snap, "loaded_slot", None),
            "tracking": getattr(snap, "tracking", None),
            "filament_detected": getattr(snap, "filament_detected", None),
            "filament_sensor_error": getattr(
                snap, "filament_sensor_error", None),
        }

    def _append(self, eventtime, reason, cached):
        self.history.append({
            "elapsed_s": round(eventtime - self.started, 3),
            "reason": reason,
            "cached": None if cached is None else dict(cached),
        })

    def _sample(self, eventtime):
        state = self._cached_state()
        if self.last_poll is not None:
            gap = max(0., eventtime - self.last_poll)
            self.max_gap_s = max(self.max_gap_s, gap)
            if gap > max(0.5, self.interval * 3):
                self.poll_gaps += 1
                self._append(eventtime, "MISSED_WINDOW", {
                    "gap_s": round(gap, 3),
                    "not_proof_of_motor_idle": True,
                })
        self.last_poll = eventtime
        self.samples += 1
        if state != self.last_state:
            self.changes += 1
            self._append(eventtime, "CACHED_STATE_CHANGED", state)
            self.last_state = state

    def _host_stage_interval(self, now):
        # Elapsed HOST callback time, not proven motor-run duration.
        if self._stage_started_at is None:
            return None
        delta = now - self._stage_started_at
        if delta < 0:
            raise ValueError("non-monotonic host stage timestamps")
        self.host_stage_intervals += 1
        self.max_host_stage_interval_s = max(
            self.max_host_stage_interval_s, delta)
        return round(delta, 3)

    def _on_stage(self, kind, slot, stage):
        # Synchronous callback even while the reactor timer is blocked.
        # No serial requests, no motor or MCU time sources are used.
        if not self.running:
            return
        try:
            now = self.reactor.monotonic()
            previous_duration = self._host_stage_interval(now)
            self._append(now, "HOST_STAGE_EVENT", {
                "operation_active": True,
                "operation_kind": kind,
                "operation_slot": slot,
                "operation_stage": stage,
                "previous_host_stage": self._stage_name,
                "previous_host_stage_interval_s": previous_duration,
            })
            self.stage_events += 1
            self._stage_started_at = now
            self._stage_name = stage
            if self._first_stage_at is None:
                self._first_stage_at = now
        except Exception as exc:
            self._fail_passive_capture(exc)

    def _on_operation_end(self):
        # Context exit includes failures: result remains UNKNOWN.
        if not self.running:
            return
        try:
            now = self.reactor.monotonic()
            last_stage_duration = self._host_stage_interval(now)
            operation_span = None
            if self._first_stage_at is not None:
                span = now - self._first_stage_at
                if span < 0:
                    raise ValueError("non-monotonic operation timestamps")
                operation_span = round(span, 3)
            self._append(now, "HOST_OPERATION_END", {
                "operation_active": False,
                "operation_stage": None,
                "operation_result": "unknown",
                "last_host_stage": self._stage_name,
                "last_host_stage_interval_s": last_stage_duration,
                "host_first_stage_to_end_s": operation_span,
            })
            self.operation_ends += 1
            self.last_operation_host_span_s = operation_span
            self._stage_started_at = None
            self._stage_name = None
            self._first_stage_at = None
        except Exception as exc:
            self._fail_passive_capture(exc)

    def _fail_passive_capture(self, exc):
        # Diagnostics must not throw into Box's physical-operation path.
        self.running = False
        self.last_error = "%s: %s" % (type(exc).__name__, exc)
        try:
            self.reactor.update_timer(self._timer, self.reactor.NEVER)
        except Exception:
            pass

    def _poll(self, eventtime):
        if not self.running:
            return self.reactor.NEVER
        try:
            self._sample(eventtime)
        except Exception as exc:
            # Passive diagnostics must never propagate an error into an
            # active operation; fail closed and stop capturing.
            self._fail_passive_capture(exc)
            return self.reactor.NEVER
        return eventtime + self.interval

    def _shutdown(self, *args):
        self.running = False
        self.reactor.update_timer(self._timer, self.reactor.NEVER)

    def cmd_start(self, gcmd):
        if self.running:
            raise gcmd.error("CFS passive trace already running")
        if self.printer.lookup_object("box", None) is None:
            raise gcmd.error("CFS box is not available")
        self.history.clear()
        self.samples = self.changes = self.poll_gaps = 0
        self.stage_events = self.operation_ends = 0
        self.host_stage_intervals = 0
        self.max_host_stage_interval_s = 0.0
        self.last_operation_host_span_s = None
        self._stage_started_at = None
        self._stage_name = None
        self._first_stage_at = None
        self.max_gap_s = 0.0
        self.last_poll = self.last_state = self.last_error = None
        self.started = self.reactor.monotonic()
        self.running = True
        # START is a direct real-user action. First sample uses only cache.
        try:
            self._sample(self.started)
        except Exception as exc:
            self.running = False
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            raise gcmd.error(self.last_error)
        self.reactor.update_timer(self._timer, self.started + self.interval)
        gcmd.respond_info(
            "CFS passive HOST trace started. No RS-485 or motor reads. "
            "Reactor scheduling gaps can conceal entire CFS movements.")

    def cmd_stop(self, gcmd):
        if not self.running:
            gcmd.respond_info("CFS passive HOST trace already stopped")
            return
        now = self.reactor.monotonic()
        try:
            self._sample(now)
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            self.running = False
            self.reactor.update_timer(self._timer, self.reactor.NEVER)
        gcmd.respond_info(
            "CFS passive HOST trace stopped: %d samples, %d changes, "
            "%d gaps, longest gap %.3fs" %
            (self.samples, self.changes, self.poll_gaps, self.max_gap_s))

    def cmd_clear(self, gcmd):
        if self.running:
            raise gcmd.error("STOP capture before clearing trace")
        self.history.clear()
        self.started = self.last_poll = self.last_state = None
        self.samples = self.changes = self.poll_gaps = 0
        self.stage_events = self.operation_ends = 0
        self.host_stage_intervals = 0
        self.max_host_stage_interval_s = 0.0
        self.last_operation_host_span_s = None
        self._stage_started_at = None
        self._stage_name = None
        self._first_stage_at = None
        self.max_gap_s = 0.0
        self.last_error = None
        gcmd.respond_info("CFS passive HOST trace cleared")

    def cmd_show(self, gcmd):
        limit = gcmd.get_int(
            "LIMIT", 15, minval=1, maxval=MAX_REPORT_LINES)
        rows = list(self.history)[-limit:]
        lines = [
            "CFS passive HOST trace (NO MCU MOTOR TELEMETRY): "
            "active=%s samples=%d changes=%d gaps=%d max_gap=%.3fs "
            "stages=%d ends=%d host_intervals=%d max_host_interval=%.3fs "
            "last_host_span=%s events_kept=%d/%d" %
            (self.running, self.samples, self.changes, self.poll_gaps,
             self.max_gap_s, self.stage_events, self.operation_ends,
             self.host_stage_intervals, self.max_host_stage_interval_s,
             self.last_operation_host_span_s,
             len(self.history), self.history_max),
            "Sampling cannot observe internal motion while the "
            "Klipper reactor is blocked; NO missing-stage interpolation.",
        ]
        for row in rows:
            s = row["cached"] or {}
            lines.append(
                "t+%.3fs %s op=%s/%s/%s depth=%s box=%s status=%s "
                "loaded=%s ready=%s gap=%s previous_host_stage=%s "
                "previous_host_interval_s=%s end_last_host_interval_s=%s "
                "host_first_stage_to_end_s=%s" %
                (row["elapsed_s"], row["reason"],
                 s.get("operation_kind"), s.get("operation_slot"),
                 s.get("operation_stage"), s.get("operation_active"),
                 s.get("box_state_code"), s.get("box_status_code"),
                 s.get("loaded_slot"), s.get("box_data_ready"),
                 s.get("gap_s"), s.get("previous_host_stage"),
                 s.get("previous_host_stage_interval_s"),
                 s.get("last_host_stage_interval_s"),
                 s.get("host_first_stage_to_end_s")))
        gcmd.respond_info("\n".join(lines))

    def get_status(self, eventtime):
        return {
            "source": "cached_host_state_only",
            "not_mcu_motor_telemetry": True,
            "sampling_may_miss_motion": True,
            "active": self.running,
            "interval_s": self.interval,
            "samples": self.samples,
            "state_changes": self.changes,
            "host_stage_events": self.stage_events,
            "host_operation_end_events": self.operation_ends,
            "host_stage_intervals": self.host_stage_intervals,
            "max_host_stage_interval_s": self.max_host_stage_interval_s,
            "last_operation_host_span_s": self.last_operation_host_span_s,
            "host_intervals_are_not_motor_timings": True,
            "reactor_gaps": self.poll_gaps,
            "longest_gap_s": self.max_gap_s,
            "retained_events": len(self.history),
            "last_error": self.last_error,
            "recent_events": list(self.history)[-STATUS_TAIL:],
        }


def load_config(config):
    return BoxCfsTrace(config)
