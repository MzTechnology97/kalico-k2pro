"""link_monitor: percentiles, clock hook, probe and CSV rows (fakes only)."""

import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import link_monitor as lm  # noqa: E402


def test_percentiles_nearest_rank():
    values = sorted(range(1, 1001))
    assert lm.percentile(values, 0.5) == 500
    assert lm.percentile(values, 0.99) == 990
    assert lm.percentile(values, 0.999) == 999
    assert lm.percentile([], 0.5) is None
    s = lm.summarize([3, 1, 2])
    assert s == {
        "samples": 3,
        "p50": 2,
        "p95": 3,
        "p99": 3,
        "p999": 3,
        "max": 3,
    }


class FakeSerial:
    def __init__(self):
        self.handlers = {}

    def register_response(self, cb, name, oid=None):
        self.handlers[name] = cb


class FakeMcu:
    def __init__(self, name="mcu"):
        self._serial = FakeSerial()
        self.clock_params = []
        self._clocksync = SimpleNamespace(
            _handle_clock=lambda p: self.clock_params.append(p)
        )
        self.name = name
        self.stats = {
            "bytes_retransmit": 10,
            "bytes_invalid": 0,
            "srtt": 0.001,
            "rttvar": 0.0002,
            "mcu_awake": 0.005,
            "mcu_task_avg": 0.000009,
            "mcu_task_stddev": 0.000004,
        }

    def get_name(self):
        return self.name

    def get_status(self, eventtime):
        return {"last_stats": dict(self.stats)}

    def lookup_query_command(self, msg, resp):
        return SimpleNamespace(
            send=lambda args: {"#sent_time": 10.0, "#receive_time": 10.0015}
        )


def monitor(tmp_path, probe_hz=0.0):
    m = lm.LinkMonitor.__new__(lm.LinkMonitor)
    m.probe_hz = probe_hz
    m._raw_file = None
    m.channels = []
    m.cumulative = {}
    m.rs485 = None
    m._log = open(tmp_path / "lm.csv", "w")
    m._last_proc = None
    m._last_time = 0.0
    m.reactor = SimpleNamespace(monotonic=lambda: 1.0)
    return m


def test_clock_hook_records_rtt_and_forwards(tmp_path):
    m = monitor(tmp_path)
    mcu = FakeMcu()
    ch = lm.McuChannel(m, mcu)
    ch.hook()
    params = {"#sent_time": 5.0, "#receive_time": 5.002, "clock": 1}
    mcu._serial.handlers["clock"](params)
    assert mcu.clock_params == [params]
    assert ch.samples == [pytest.approx(0.002)]
    # a clock answer without a sent time is forwarded, not measured
    mcu._serial.handlers["clock"]({"#sent_time": 0, "#receive_time": 6.0})
    assert len(ch.samples) == 1 and len(mcu.clock_params) == 2


def test_probe_adds_samples(tmp_path):
    m = monitor(tmp_path, probe_hz=10.0)
    ch = lm.McuChannel(m, FakeMcu())
    ch.hook()
    m.channels.append(ch)
    assert m._probe(0.0) == pytest.approx(1.1)
    assert ch.samples == [pytest.approx(0.0015)]


def test_rows_report_interval_deltas(tmp_path):
    m = monitor(tmp_path)
    mcu = FakeMcu("nozzle_mcu")
    ch = lm.McuChannel(m, mcu)
    m.channels.append(ch)
    ch.samples = [0.001, 0.002, 0.003]
    m._write_rows(60.0)
    mcu.stats["bytes_retransmit"] = 25
    ch.samples = [0.004]
    m._write_rows(120.0)
    m._log.close()
    rows = (tmp_path / "lm.csv").read_text().strip().splitlines()
    first = rows[0].split(",")
    second = rows[1].split(",")
    assert first[1] == "nozzle_mcu" and first[2] == "3"
    assert first[3] == "2.000" and first[7] == "3.000"
    assert first[8] == "1.000" and first[10] == "0"
    assert second[10] == "15"
    assert len(first) == len(lm.CSV_HEADER.split(","))
    assert sorted(m.cumulative["nozzle_mcu"]) == [0.001, 0.002, 0.003, 0.004]


def test_rs485_rows(tmp_path):
    m = monitor(tmp_path)
    taken = [[0.004, 0.006]]
    m.rs485 = SimpleNamespace(
        take_rtt_samples=lambda: taken.pop() if taken else [],
        _status_fields=lambda: {"timeouts": 8, "crc_errors": 1},
    )
    m._write_rows(60.0)
    m._log.close()
    row = (tmp_path / "lm.csv").read_text().strip().split(",")
    assert row[1] == "rs485" and row[2] == "2" and row[7] == "6.000"
    assert row[15] == "0" and row[16] == "0"
