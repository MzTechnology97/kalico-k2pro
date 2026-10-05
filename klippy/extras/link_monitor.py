# Lightweight serial link monitor (round trip times and transport counters)
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Measure the host <-> MCU links during long prints.

    [link_monitor]
    interval: 60          # seconds between CSV rows
    log_path: ~/printer_data/logs/link_monitor.csv
    probe_hz: 0           # extra get_uptime round trips per second (benchmarks)
    raw_path:             # optional file for every RTT sample (benchmarks)

Only measurements the stack really provides are recorded:
- RTT of every "clock" answer the MCU already sends to clocksync (about one
  per second per MCU), from the serial layer's #sent_time/#receive_time;
- optional extra get_uptime round trips (probe_hz), same timestamps;
- serialqueue and MCU counters from mcu.get_status()["last_stats"]
  (bytes_retransmit, bytes_invalid, srtt, rttvar, rto, ready_bytes,
  upcoming_bytes, mcu_awake, mcu_task_avg, mcu_task_stddev);
- RS-485 request round trips measured by serial_485 (request write to
  matched answer) and its timeouts/CRC counters;
- the Klipper process CPU time, context switches and RSS from /proc.
Nothing is sent unless probe_hz > 0.
"""

from __future__ import annotations

import logging
import math
import os
import time

CSV_HEADER = (
    "time,channel,samples,p50_ms,p95_ms,p99_ms,p999_ms,max_ms,"
    "srtt_ms,rttvar_ms,retransmit_bytes,invalid_bytes,mcu_awake,"
    "mcu_task_avg_us,mcu_task_stddev_us,timeouts,crc_errors,"
    "proc_cpu_pct,ctx_voluntary,ctx_involuntary,rss_kb"
)


def percentile(sorted_values, fraction):
    """Nearest-rank percentile of an already sorted list (None if empty)."""
    if not sorted_values:
        return None
    rank = max(1, int(math.ceil(fraction * len(sorted_values))))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def summarize(samples):
    values = sorted(samples)
    return {
        "samples": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "p999": percentile(values, 0.999),
        "max": values[-1] if values else None,
    }


class RttHistogram:
    """Round trips since start in fixed log buckets (2% wide, 10 us .. 100 s).

    Memory and cost stay constant however long the print runs: adding a
    sample is one log(), a summary walks about 800 counters. Percentiles are
    the upper edge of their bucket (at most 2% high); the maximum is exact.
    The first version kept every sample and sorted them all on every
    get_status, which Moonraker calls several times a second: after three
    hours of 10 Hz probing klippy used half a CPU core."""

    LOW = 1e-5
    STEP = math.log(1.02)
    BUCKETS = int(math.log(100.0 / 1e-5) / math.log(1.02)) + 2

    def __init__(self):
        self.counts = [0] * self.BUCKETS
        self.total = 0
        self.max = None

    def add(self, value):
        if value <= self.LOW:
            bucket = 0
        else:
            bucket = min(
                self.BUCKETS - 1,
                1 + int(math.log(value / self.LOW) / self.STEP),
            )
        self.counts[bucket] += 1
        self.total += 1
        if self.max is None or value > self.max:
            self.max = value

    def extend(self, values):
        for value in values:
            self.add(value)

    def copy(self):
        other = RttHistogram()
        other.counts = list(self.counts)
        other.total = self.total
        other.max = self.max
        return other

    def _edge(self, bucket):
        return self.LOW * math.exp(self.STEP * bucket)

    def summary(self):
        result = {
            "samples": self.total,
            "p50": None,
            "p95": None,
            "p99": None,
            "p999": None,
            "max": self.max,
        }
        if not self.total:
            return result
        wanted = [
            (key, max(1, int(math.ceil(fraction * self.total))))
            for key, fraction in (
                ("p50", 0.50),
                ("p95", 0.95),
                ("p99", 0.99),
                ("p999", 0.999),
            )
        ]
        seen = 0
        for bucket, count in enumerate(self.counts):
            if not count:
                continue
            seen += count
            while wanted and seen >= wanted[0][1]:
                result[wanted.pop(0)[0]] = min(self._edge(bucket), self.max)
            if not wanted:
                break
        return result


def read_proc_self():
    """(cpu seconds, voluntary ctx, involuntary ctx, rss kB) of this process."""
    try:
        with open("/proc/self/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu = (int(fields[11]) + int(fields[12])) / float(ticks)
        vol = invol = rss = 0
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("voluntary_ctxt_switches:"):
                    vol = int(line.split()[1])
                elif line.startswith("nonvoluntary_ctxt_switches:"):
                    invol = int(line.split()[1])
                elif line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
        return cpu, vol, invol, rss
    except (OSError, ValueError, IndexError):
        return None


class McuChannel:
    def __init__(self, monitor, mcu):
        self.monitor = monitor
        self.mcu = mcu
        self.name = mcu.get_name()
        self.samples = []
        self.last_counters = None
        self.probe = None

    def hook(self):
        serial = self.mcu._serial
        clocksync = self.mcu._clocksync
        original = clocksync._handle_clock

        def handle_clock(params):
            self.note(params)
            original(params)

        serial.register_response(handle_clock, "clock")
        if self.monitor.probe_hz > 0:
            self.probe = self.mcu.lookup_query_command(
                "get_uptime", "uptime high=%u clock=%u"
            )

    def note(self, params):
        sent = params.get("#sent_time")
        received = params.get("#receive_time")
        if sent and received and received >= sent:
            rtt = received - sent
            self.samples.append(rtt)
            self.monitor.raw(self.name, rtt)

    def counters(self, eventtime):
        try:
            return dict(self.mcu.get_status(eventtime).get("last_stats", {}))
        except Exception:
            return {}


class LinkMonitor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.interval = config.getfloat(
            "interval", 60.0, minval=5.0, maxval=3600.0
        )
        self.probe_hz = config.getfloat(
            "probe_hz", 0.0, minval=0.0, maxval=50.0
        )
        self.log_path = os.path.expanduser(
            config.get("log_path", "~/printer_data/logs/link_monitor.csv")
        )
        raw = config.get("raw_path", "").strip()
        self.raw_path = os.path.expanduser(raw) if raw else None
        self.channels = []
        self.rs485 = None
        self._raw_file = None
        self._last_proc = None
        self._last_time = None
        self.cumulative = {}
        self._status = {"rtt_ms": {}}
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler(
            "klippy:disconnect", self._handle_disconnect
        )
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command(
            "LINK_MONITOR_REPORT",
            self.cmd_LINK_MONITOR_REPORT,
            desc="Round trip percentiles of the serial links since start",
        )

    # --- setup ----------------------------------------------------------------
    def _handle_ready(self):
        for name, obj in self.printer.lookup_objects("mcu"):
            serial = getattr(obj, "_serial", None)
            if serial is None or getattr(obj, "is_fileoutput", lambda: False)():
                continue
            channel = McuChannel(self, obj)
            try:
                channel.hook()
            except Exception:
                logging.exception("link_monitor: cannot hook %s", name)
                continue
            self.channels.append(channel)
        self.rs485 = self.printer.lookup_object("serial_485 serial485", None)
        if self.raw_path:
            self._raw_file = open(self.raw_path, "a", buffering=1)
        new_file = not os.path.exists(self.log_path)
        self._log = open(self.log_path, "a", buffering=1)
        if new_file:
            self._log.write(CSV_HEADER + "\n")
        now = self.reactor.monotonic()
        self._last_proc = read_proc_self()
        self._last_time = now
        self.reactor.register_timer(self._flush, now + self.interval)
        if self.probe_hz > 0:
            self.reactor.register_timer(self._probe, now + 1.0)

    def _handle_disconnect(self):
        for f in (getattr(self, "_log", None), self._raw_file):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass

    def raw(self, channel, rtt):
        if self._raw_file is not None:
            self._raw_file.write("%.6f,%s,%.6f\n" % (time.time(), channel, rtt))

    # --- probe -------------------------------------------------------------------
    def _probe(self, eventtime):
        for channel in self.channels:
            if channel.probe is None:
                continue
            try:
                params = channel.probe.send([])
            except Exception:
                continue
            channel.note(params)
        return self.reactor.monotonic() + 1.0 / self.probe_hz

    # --- output ----------------------------------------------------------------
    def _accumulate(self, name, samples):
        hist = self.cumulative.get(name)
        if hist is None:
            hist = self.cumulative[name] = RttHistogram()
        hist.extend(samples)

    def _flush(self, eventtime):
        try:
            self._write_rows(eventtime)
        except Exception:
            logging.exception("link_monitor: flush failed")
        return eventtime + self.interval

    def _write_rows(self, eventtime):
        proc = read_proc_self()
        cpu_pct = vol = invol = rss = ""
        if proc and self._last_proc:
            dt = max(1e-6, eventtime - self._last_time)
            cpu_pct = "%.2f" % (100.0 * (proc[0] - self._last_proc[0]) / dt)
            vol = proc[1] - self._last_proc[1]
            invol = proc[2] - self._last_proc[2]
            rss = proc[3]
        self._last_proc, self._last_time = proc, eventtime
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for channel in self.channels:
            samples, channel.samples = channel.samples, []
            self._accumulate(channel.name, samples)
            s = summarize(samples)
            c = channel.counters(eventtime)
            last = channel.last_counters or {}
            channel.last_counters = c

            def delta(key):
                if key not in c:
                    return ""
                return c[key] - last.get(key, c[key])

            self._log.write(
                ",".join(
                    str(v)
                    for v in (
                        stamp,
                        channel.name,
                        s["samples"],
                        fmt_ms(s["p50"]),
                        fmt_ms(s["p95"]),
                        fmt_ms(s["p99"]),
                        fmt_ms(s["p999"]),
                        fmt_ms(s["max"]),
                        fmt_ms(c.get("srtt")),
                        fmt_ms(c.get("rttvar")),
                        delta("bytes_retransmit"),
                        delta("bytes_invalid"),
                        c.get("mcu_awake", ""),
                        fmt_us(c.get("mcu_task_avg")),
                        fmt_us(c.get("mcu_task_stddev")),
                        "",
                        "",
                        cpu_pct,
                        vol,
                        invol,
                        rss,
                    )
                )
                + "\n"
            )
        if self.rs485 is not None and hasattr(self.rs485, "take_rtt_samples"):
            samples = self.rs485.take_rtt_samples()
            self._accumulate("rs485", samples)
            for rtt in samples:
                self.raw("rs485", rtt)
            s = summarize(samples)
            stats = self.rs485._status_fields()
            last = getattr(self, "_last_rs485", {})
            self._last_rs485 = stats
            self._log.write(
                ",".join(
                    str(v)
                    for v in (
                        stamp,
                        "rs485",
                        s["samples"],
                        fmt_ms(s["p50"]),
                        fmt_ms(s["p95"]),
                        fmt_ms(s["p99"]),
                        fmt_ms(s["p999"]),
                        fmt_ms(s["max"]),
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        stats["timeouts"]
                        - last.get("timeouts", stats["timeouts"]),
                        stats["crc_errors"]
                        - last.get("crc_errors", stats["crc_errors"]),
                        "",
                        "",
                        "",
                        "",
                    )
                )
                + "\n"
            )
        self._update_status()

    def _update_status(self):
        # Computed once per interval; get_status only returns it.
        result = {}
        for name, hist in self.cumulative.items():
            result[name] = {
                k: (None if v is None else round(v * 1000.0, 3))
                if k != "samples"
                else v
                for k, v in hist.summary().items()
            }
        self._status = {"rtt_ms": result}

    def cmd_LINK_MONITOR_REPORT(self, gcmd):
        lines = []
        for name, hist in sorted(self.cumulative.items()):
            hist = hist.copy()
            for channel in self.channels:
                if channel.name == name:
                    hist.extend(channel.samples)
            s = hist.summary()
            lines.append(
                "%s: n=%d p50=%s p95=%s p99=%s p99.9=%s max=%s ms"
                % (
                    name,
                    s["samples"],
                    fmt_ms(s["p50"]),
                    fmt_ms(s["p95"]),
                    fmt_ms(s["p99"]),
                    fmt_ms(s["p999"]),
                    fmt_ms(s["max"]),
                )
            )
        gcmd.respond_info("\n".join(lines) or "link_monitor: no samples yet")

    def get_status(self, eventtime):
        return self._status


def fmt_ms(value):
    return "" if value is None else "%.3f" % (float(value) * 1000.0)


def fmt_us(value):
    return "" if value is None else "%.1f" % (float(value) * 1e6)


def load_config(config):
    return LinkMonitor(config)
