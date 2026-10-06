"""k2_pa_analysis on SYNTHETIC captures.

These traces are generated here (first-order response plus Gaussian noise).
Passing tests prove the analysis code, not the accuracy on a real K2.
"""

import math
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import k2_pa_analysis as a  # noqa: E402

RATE = 1280.0


def synthetic(
    tau=0.04,
    step=4000.0,
    noise=20.0,
    rest=0.8,
    pulse=1.0,
    after=0.8,
    baseline=120000,
    seed=1,
    drift=0.0,
):
    rng = random.Random(seed)
    n = int((rest + pulse + after) * RATE)
    times, values, espds = [], [], []
    for i in range(n):
        t = i / RATE
        if t < rest:
            y, e = 0.0, 0
        elif t < rest + pulse:
            y, e = step * (1 - math.exp(-(t - rest) / tau)), -40000
        else:
            top = step * (1 - math.exp(-pulse / tau))
            y, e = top * math.exp(-(t - rest - pulse) / tau), 0
        v = int(baseline + drift * t + y + rng.gauss(0, noise))
        values.append(max(a.CS1237_MIN, min(a.CS1237_MAX, v)))
        times.append(t)
        espds.append(e)
    return {"times": times, "values": values, "espds": espds}


def test_cs1237_config_60():
    assert a.decode_cs1237_config(60) == {
        "rate_hz": 1280.0,
        "gain": 128,
        "channel": 0,
    }


def test_series_stats_on_rest():
    cap = synthetic(step=0.0, noise=10.0)
    s = a.series_stats(cap["times"], cap["values"])
    assert abs(s["rate_hz"] - RATE) < 1
    assert abs(s["baseline"] - 120000) < 5
    assert 7 < s["noise"] < 13
    assert not s["saturated"]


def test_missing_values_stay_none():
    s = a.series_stats([], [])
    assert s["noise"] is None and s["baseline"] is None and s["rate_hz"] is None


def test_extrusion_events():
    cap = synthetic()
    events = a.find_extrusion_events(cap["times"], cap["espds"])
    assert len(events) == 1
    start, stop = events[0]
    assert abs(start - 0.8) < 0.002 and abs(stop - 1.8) < 0.002


def test_recovers_tau_within_five_percent():
    for tau in (0.015, 0.04, 0.09):
        r = a.analyze_capture(synthetic(tau=tau))
        assert r["accepted"], r
        assert abs(r["tau"] - tau) / tau < 0.05


def test_small_signal_is_rejected():
    r = a.analyze_capture(synthetic(step=50.0, noise=40.0))
    assert not r["accepted"]
    assert any("SNR" in x for e in r["events"] for x in e["reasons"])


def test_saturation_is_rejected():
    cap = synthetic(baseline=a.CS1237_MAX - 10, step=0.0)
    r = a.analyze_capture(cap)
    assert "sensor saturated" in r["reasons"]


def test_capture_cut_before_the_decay_is_rejected():
    r = a.analyze_capture(synthetic(after=0.2))
    assert not r["accepted"]
    assert any("fit window" in x for e in r["events"] for x in e["reasons"])


def test_no_pulse_or_two_pulses_are_rejected():
    cap = synthetic()
    cap["espds"] = [0] * len(cap["espds"])
    assert not a.analyze_capture(cap)["accepted"]
    cap = synthetic()
    mid = len(cap["espds"]) // 2
    for i in range(mid, mid + 200):
        cap["espds"][i] = 0
    r = a.analyze_capture(cap)
    assert not r["accepted"] and any("found 2" in x for x in r["reasons"])


def test_replicates_must_agree():
    good = [a.analyze_capture(synthetic(tau=0.04, seed=s)) for s in (1, 2, 3)]
    assert a.combine(good)["ok"]
    mixed = good[:2] + [a.analyze_capture(synthetic(tau=0.08, seed=4))]
    c = a.combine(mixed)
    assert not c["ok"] and "disagree" in c["reasons"][0]
    c = a.combine(good[:2])
    assert not c["ok"] and "needed" in c["reasons"][0]


def test_candidate_needs_consistent_feed_rates():
    def group(tau):
        return a.combine(
            [a.analyze_capture(synthetic(tau=tau, seed=s)) for s in (1, 2, 3)]
        )

    ok = a.pa_candidate({2.0: group(0.04), 5.0: group(0.042)})
    assert ok["ok"] and abs(ok["candidate"] - 0.041) < 0.003
    bad = a.pa_candidate({2.0: group(0.03), 5.0: group(0.08)})
    assert not bad["ok"] and "feed rate" in bad["reasons"][0]
    out_of_range = a.pa_candidate({2.0: group(0.5)}, {"tau_max": 2.0})
    assert not out_of_range["ok"]


def test_candidate_without_valid_groups():
    r = a.pa_candidate({})
    assert not r["ok"] and r["candidate"] is None
    failed = a.combine([])
    assert not a.pa_candidate({2.0: failed})["ok"]


def _write_csv(path, cap, flow):
    with open(path, "w") as out:
        out.write("# label: flow=%g\n# session: 1\n" % flow)
        out.write(
            "tick,time_s,raw_counts,rel_counts,e_interval_ticks,"
            "e_velocity_mm_s_derived\n"
        )
        for i, (t, v, e) in enumerate(
            zip(cap["times"], cap["values"], cap["espds"])
        ):
            out.write("%d,%.6f,%d,,%d,\n" % (i, t, v, e))


def test_replay_script_on_csv(tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "k2_pa_replay", ROOT / "scripts" / "k2_pa_replay.py"
    )
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    files = []
    for flow in (2.0, 5.0):
        for seed in (1, 2, 3):
            path = tmp_path / ("f%g_%d.csv" % (flow, seed))
            _write_csv(path, synthetic(tau=0.04, seed=seed), flow)
            files.append(str(path))
    assert replay.main(files) == 0
    out = capsys.readouterr().out
    assert "candidate pressure_advance 0.04" in out
    assert "not validated" in out
    assert replay.main(files[:2]) == 1  # too few replicates


def test_pending_step_before_the_pulse_is_not_extrusion():
    # Seen on the K2 Pro: the first step of a queued move is pending (an
    # interval of seconds) during the whole rest before the pulse.
    cap = synthetic()
    cap["espds"] = [
        775808434 if t < 0.8 else e for t, e in zip(cap["times"], cap["espds"])
    ]
    cap["clock_freq"] = 120e6
    events = a.find_extrusion_events(
        cap["times"], cap["espds"], max_interval=0.05 * 120e6
    )
    assert len(events) == 1
    assert abs(events[0][0] - 0.8) < 0.002
    result = a.analyze_capture(cap)
    assert result["stats"]["noise"] is not None
    assert len(result["events"]) == 1
    assert abs(result["events"][0]["start"] - 0.8) < 0.002


def test_without_clock_any_interval_counts():
    cap = synthetic()
    cap["espds"] = [
        775808434 if t < 0.8 else e for t, e in zip(cap["times"], cap["espds"])
    ]
    events = a.find_extrusion_events(cap["times"], cap["espds"])
    assert abs(events[0][0] - 0.0) < 0.002
