# Offline analysis of K2 load-cell captures (statistics and an experimental
# pressure advance candidate).
#
# Copyright (C) 2026  MzTechnology97
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Pure Python, no Klipper imports: used by k2_load_cell_pa and on a PC.

Method (see docs/K2_Load_Cell_PA.md):
- each capture holds one E-only extrusion pulse at a known feed rate,
  with rest periods before and after;
- the load signal after the pulse stops is fitted, by default (model
  "fast_component") with two exponentials:
  y(t) = c + a1 * exp(-(t - t_stop) / tau1) + a2 * exp(-(t - t_stop) / tau2)
  On the K2 Pro the decay has a fast part (tau1 ~30 ms) and a slow tail
  (0.2-0.6 s, filament path and mount); one exponential mixes them;
- model "first_order_lag" fits one exponential, y = c + a * exp(-t / tau);
- under a first-order lag model of the melt flow, Klipper's pressure
  advance K (seconds) that cancels the lag is K = tau (tau1 for the fast
  component). This is a model candidate, not a measurement of pressure: it
  must be confirmed with a printed pressure advance test.
"""

import csv
import math

CS1237_MIN = -(1 << 23)
CS1237_MAX = (1 << 23) - 1

# CS1237 configuration register (datasheet): bits 5-4 output rate,
# bits 3-2 PGA gain, bits 1-0 channel.
CS1237_RATES = {0: 10.0, 1: 40.0, 2: 640.0, 3: 1280.0}
CS1237_GAINS = {0: 1, 1: 2, 2: 64, 3: 128}

DEFAULTS = {
    "baseline_time": 0.25,  # s of rest used as baseline before the pulse
    # Longest E step interval that counts as extrusion. Klipper queues steps
    # up to ~2 s ahead, so the first step of a pulse is pending (interval of
    # seconds) long before the extruder turns.
    "max_step_interval": 0.05,  # s
    "model": "fast_component",  # or "first_order_lag"
    "fit_window": 0.6,  # s after the pulse stop (first_order_lag)
    # fast_component: fit up to fit_window_slow s after the stop, at least
    # fit_window_min s of data; tau2 >= min_tau_ratio * tau1
    "fit_window_slow": 1.5,
    "fit_window_min": 0.5,
    "tau_fast_max": 0.25,
    "tau_slow_max": 3.0,
    "min_tau_ratio": 3.0,
    # below this share of the decay amplitude a component is noise: the
    # decay is then a single exponential, and its tau is the other one
    "min_component_share": 0.15,
    "min_snr": 8.0,  # fitted amplitude / baseline noise
    "min_r2": 0.85,
    "tau_min": 0.003,
    "tau_max": 1.5,
    "min_replicates": 3,
    "max_rel_spread": 0.25,  # (max - min) / median of accepted taus
    "max_flow_ratio": 1.5,  # tau(high flow) / tau(low flow)
    "pa_min": 0.0,
    "pa_max": 0.2,
}


def decode_cs1237_config(cfg_regs):
    return {
        "rate_hz": CS1237_RATES[(cfg_regs >> 4) & 3],
        "gain": CS1237_GAINS[(cfg_regs >> 2) & 3],
        "channel": cfg_regs & 3,
    }


# --- basic statistics --------------------------------------------------------


def mean(values):
    return sum(values) / len(values) if values else None


def stdev(values):
    if len(values) < 2:
        return None
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def median(values):
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def linear_fit(xs, ys):
    """Least squares y = slope * x + intercept; returns (slope, intercept)."""
    n = len(xs)
    if n < 2:
        return None, None
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


def series_stats(times, values, baseline_time=DEFAULTS["baseline_time"]):
    """Baseline, noise, drift and range of a capture (None when unknown)."""
    out = {
        "samples": len(values),
        "rate_hz": None,
        "baseline": None,
        "noise": None,
        "drift_per_s": None,
        "min": None,
        "max": None,
        "range": None,
        "saturated": False,
    }
    if not values:
        return out
    out["min"], out["max"] = min(values), max(values)
    out["range"] = out["max"] - out["min"]
    out["saturated"] = out["min"] <= CS1237_MIN or out["max"] >= CS1237_MAX
    if len(times) > 1 and times[-1] > times[0]:
        out["rate_hz"] = (len(times) - 1) / (times[-1] - times[0])
    t0 = times[0]
    base = [v for t, v in zip(times, values) if t - t0 <= baseline_time]
    if len(base) >= 2:
        out["baseline"] = mean(base)
        out["noise"] = stdev(base)
    slope, _ = linear_fit(list(times), list(values))
    out["drift_per_s"] = slope
    return out


# --- extrusion events and fits -------------------------------------------------


def find_extrusion_events(times, espds, max_gap=0.02, max_interval=None):
    """Intervals where the E stepper was stepping.

    A sample counts when its E interval is non-zero and, with max_interval
    (MCU ticks), no longer than that: a longer one is a step still pending
    before the move starts. Short holes (<= max_gap s) are merged.
    """
    events = []
    start = last = None
    for t, e in zip(times, espds):
        if e and (max_interval is None or abs(e) <= max_interval):
            if start is None:
                start = t
            elif t - last > max_gap:
                events.append((start, last))
                start = t
            last = t
    if start is not None:
        events.append((start, last))
    return events


def _solve_linear(xs, ys):
    """Least squares y = c + a * x; returns (c, a, sse)."""
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    det = n * sxx - sx * sx
    if det == 0:
        return None
    a = (n * sxy - sx * sy) / det
    c = (sy - a * sx) / n
    sse = sum((y - c - a * x) ** 2 for x, y in zip(xs, ys))
    return c, a, sse


def fit_exponential(times, values, t0, window, tau_min, tau_max, steps=80):
    """Fit y = c + a * exp(-(t - t0) / tau) on [t0, t0 + window]."""
    pts = [(t - t0, v) for t, v in zip(times, values) if 0 <= t - t0 <= window]
    result = {"ok": False, "n": len(pts), "reason": None}
    if len(pts) < 10:
        result["reason"] = "not enough samples after the event"
        return result
    if times[-1] < t0 + window * 0.9:
        result["reason"] = "capture ends before the fit window"
        return result
    ys = [v for _, v in pts]
    my = mean(ys)
    sst = sum((y - my) ** 2 for y in ys)
    best = None
    ratio = (tau_max / tau_min) ** (1.0 / (steps - 1))
    for i in range(steps):
        tau = tau_min * ratio**i
        xs = [math.exp(-dt / tau) for dt, _ in pts]
        sol = _solve_linear(xs, ys)
        if sol is None:
            continue
        if best is None or sol[2] < best[3]:
            best = (tau, sol[0], sol[1], sol[2], i)
    if best is None or sst == 0:
        result["reason"] = "flat signal"
        return result
    tau, c, a, sse, index = best
    result.update(
        {
            "tau": tau,
            "offset": c,
            "amplitude": a,
            "r2": 1.0 - sse / sst,
            "rmse": math.sqrt(sse / len(pts)),
            "at_grid_edge": index in (0, steps - 1),
            "ok": True,
        }
    )
    return result


def _geometric(lo, hi, steps):
    if steps < 2 or hi <= lo:
        return [lo]
    ratio = (hi / lo) ** (1.0 / (steps - 1))
    return [lo * ratio**i for i in range(steps)]


def _solve3(m, v):
    """Solve the 3x3 system m x = v (Gaussian elimination); None if singular."""
    a = [list(row) + [rhs] for row, rhs in zip(m, v)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        for r in range(3):
            if r != col:
                f = a[r][col] / a[col][col]
                for k in range(col, 4):
                    a[r][k] -= f * a[col][k]
    return [a[i][3] / a[i][i] for i in range(3)]


def fit_two_exponential(
    times,
    values,
    t0,
    window,
    tau1_min,
    tau1_max,
    tau2_max,
    min_ratio=3.0,
    steps=28,
    bin_time=0.004,
    min_share=0.15,
):
    """Fit y = c + a1 exp(-(t-t0)/tau1) + a2 exp(-(t-t0)/tau2), tau2 >= ratio tau1.

    Samples are averaged in bin_time bins; a coarse geometric grid over
    (tau1, tau2) is refined once around the best pair. Amplitudes and offset
    are solved linearly for each pair.
    """
    raw = [(t - t0, v) for t, v in zip(times, values) if 0 <= t - t0 <= window]
    result = {"ok": False, "n": len(raw), "reason": None}
    if len(raw) < 20:
        result["reason"] = "not enough samples after the event"
        return result
    bins = {}
    for dt, v in raw:
        bins.setdefault(int(dt / bin_time), []).append((dt, v))
    pts = [
        (mean([p[0] for p in b]), mean([p[1] for p in b]))
        for _, b in sorted(bins.items())
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(pts)
    sy = sum(ys)
    syy = sum(y * y for y in ys)
    my = sy / n
    sst = syy - n * my * my
    if sst <= 0:
        result["reason"] = "flat signal"
        return result
    cache = {}

    def basis(tau):
        if tau not in cache:
            e = [math.exp(-x / tau) for x in xs]
            cache[tau] = (
                e,
                sum(e),
                sum(v * v for v in e),
                sum(v * y for v, y in zip(e, ys)),
            )
        return cache[tau]

    def evaluate(t1, t2):
        e1, s1, s11, s1y = basis(t1)
        e2, s2, s22, s2y = basis(t2)
        s12 = sum(p * q for p, q in zip(e1, e2))
        sol = _solve3(
            [[n, s1, s2], [s1, s11, s12], [s2, s12, s22]], [sy, s1y, s2y]
        )
        if sol is None:
            return None
        c, a1, a2 = sol
        return syy - (c * sy + a1 * s1y + a2 * s2y), c, a1, a2

    def search(grid1, grid2):
        best = None
        for t1 in grid1:
            for t2 in grid2:
                if t2 < min_ratio * t1:
                    continue
                r = evaluate(t1, t2)
                if r is not None and (best is None or r[0] < best[0]):
                    best = (r[0], t1, t2, r[1], r[2], r[3])
        return best

    grid1 = _geometric(tau1_min, tau1_max, steps)
    grid2 = _geometric(tau1_min * min_ratio, tau2_max, steps)
    best = search(grid1, grid2)
    if best is None:
        result["reason"] = "no fit"
        return result
    edge = best[1] in (grid1[0], grid1[-1])
    step1 = (tau1_max / tau1_min) ** (1.0 / (steps - 1))
    step2 = (tau2_max / (tau1_min * min_ratio)) ** (1.0 / (steps - 1))
    fine = search(
        [
            min(tau1_max, max(tau1_min, t))
            for t in _geometric(best[1] / step1, best[1] * step1, 9)
        ],
        _geometric(best[2] / step2, min(tau2_max, best[2] * step2), 9),
    )
    if fine is not None and fine[0] <= best[0]:
        best = fine
    sse, tau1, tau2, c, a1, a2 = best
    total = abs(a1) + abs(a2)
    share = abs(a1) / total if total else 0.0
    # "tau" is the fast component; when it is only noise the decay is a
    # single exponential and the other component is the one that counts
    single = share < min_share
    if single:
        tau, amplitude = tau2, a2
        edge = tau2 in (grid2[0], grid2[-1])
    else:
        tau, amplitude = tau1, a1
    result.update(
        {
            "tau": tau,
            "amplitude": amplitude,
            "tau1": tau1,
            "tau2": tau2,
            "amplitude1": a1,
            "amplitude2": a2,
            "fast_share": share,
            "single_component": single or share > 1.0 - min_share,
            "offset": c,
            "r2": 1.0 - max(sse, 0.0) / sst,
            "rmse": math.sqrt(max(sse, 0.0) / n),
            "at_grid_edge": edge,
            "ok": True,
        }
    )
    return result


def _fit_decay(times, values, stop, o):
    if o["model"] == "first_order_lag":
        return fit_exponential(
            times, values, stop, o["fit_window"], o["tau_min"], o["tau_max"]
        )
    available = (times[-1] - stop) if times else 0.0
    if available < o["fit_window_min"]:
        return {
            "ok": False,
            "n": 0,
            "reason": "capture ends before the fit window",
        }
    return fit_two_exponential(
        times,
        values,
        stop,
        min(o["fit_window_slow"], available),
        o["tau_min"],
        o["tau_fast_max"],
        o["tau_slow_max"],
        o["min_tau_ratio"],
        min_share=o["min_component_share"],
    )


# --- one capture, several captures, candidate ---------------------------------


def analyze_capture(capture, opts=None):
    """Analyze one capture dict: times (s), values (counts), espds."""
    o = dict(DEFAULTS)
    o.update(opts or {})
    times, values = capture["times"], capture["values"]
    stats = series_stats(times, values, o["baseline_time"])
    out = {"stats": stats, "events": [], "reasons": [], "accepted": False}
    if stats["saturated"]:
        out["reasons"].append("sensor saturated")
    if stats["noise"] is None:
        out["reasons"].append("no baseline before the pulse")
    clock = capture.get("clock_freq")
    max_interval = o["max_step_interval"] * clock if clock else None
    events = find_extrusion_events(
        times, capture["espds"], max_interval=max_interval
    )
    if len(events) != 1:
        out["reasons"].append(
            "expected one extrusion pulse, found %d" % len(events)
        )
    for start, stop in events:
        decay = _fit_decay(times, values, stop, o)
        rise = fit_exponential(
            times,
            values,
            start,
            min(o["fit_window"], stop - start),
            o["tau_min"],
            o["tau_max"],
        )
        ev = {"start": start, "stop": stop, "decay": decay, "rise": rise}
        reasons = []
        if not decay["ok"]:
            reasons.append("decay fit: %s" % decay["reason"])
        else:
            noise = stats["noise"] or 0.0
            snr = abs(decay["amplitude"]) / noise if noise else None
            ev["snr"] = snr
            if snr is None or snr < o["min_snr"]:
                reasons.append("signal too small (SNR %s)" % _fmt(snr))
            if decay["r2"] < o["min_r2"]:
                reasons.append("poor exponential fit (r2 %.3f)" % decay["r2"])
            if decay["at_grid_edge"]:
                reasons.append("tau at the edge of the search range")
        ev["reasons"] = reasons
        out["events"].append(ev)
    out["accepted"] = (
        not out["reasons"]
        and len(out["events"]) == 1
        and not out["events"][0]["reasons"]
    )
    if out["accepted"]:
        out["tau"] = out["events"][0]["decay"]["tau"]
    return out


def combine(results, opts=None):
    """Combine replicate analyses of the same condition."""
    o = dict(DEFAULTS)
    o.update(opts or {})
    taus = [r["tau"] for r in results if r.get("accepted")]
    out = {
        "replicates": len(results),
        "accepted": len(taus),
        "tau_median": median(taus),
        "rel_spread": None,
        "ok": False,
        "reasons": [],
    }
    if len(taus) < o["min_replicates"]:
        out["reasons"].append(
            "%d accepted replicates, %d needed"
            % (len(taus), o["min_replicates"])
        )
        return out
    out["rel_spread"] = (max(taus) - min(taus)) / out["tau_median"]
    if out["rel_spread"] > o["max_rel_spread"]:
        out["reasons"].append(
            "replicates disagree (spread %.0f%%)" % (100 * out["rel_spread"])
        )
        return out
    out["ok"] = True
    return out


def pa_candidate(groups, opts=None):
    """Experimental PA candidate from combined groups keyed by feed rate.

    Model "first_order_lag": K = tau. Returned only when every group
    passed and tau does not depend strongly on the feed rate.
    """
    o = dict(DEFAULTS)
    o.update(opts or {})
    out = {
        "model": o["model"],
        "candidate": None,
        "ok": False,
        "reasons": [],
        "groups": groups,
    }
    if not groups:
        out["reasons"].append("no data")
        return out
    bad = [k for k, g in groups.items() if not g["ok"]]
    if bad:
        out["reasons"].append(
            "feed rate(s) without a valid result: %s"
            % ", ".join(str(k) for k in sorted(bad))
        )
        return out
    taus = {k: g["tau_median"] for k, g in groups.items()}
    if len(taus) > 1:
        lo, hi = min(taus), max(taus)
        ratio = taus[hi] / taus[lo] if taus[lo] else None
        out["flow_ratio"] = ratio
        if ratio is None or not (
            1.0 / o["max_flow_ratio"] <= ratio <= o["max_flow_ratio"]
        ):
            out["reasons"].append(
                "tau changes with the feed rate (ratio %s): the linear model "
                "does not hold" % _fmt(ratio)
            )
            return out
    candidate = median(list(taus.values()))
    if not o["pa_min"] <= candidate <= o["pa_max"]:
        out["reasons"].append(
            "candidate %.4f outside %.3f-%.3f"
            % (candidate, o["pa_min"], o["pa_max"])
        )
        return out
    out["candidate"] = candidate
    out["ok"] = True
    return out


def analyze_pa_captures(captures, opts=None):
    by_flow = {}
    for cap in captures:
        by_flow.setdefault(cap.get("flow"), []).append(
            analyze_capture(cap, opts)
        )
    groups = {flow: combine(results, opts) for flow, results in by_flow.items()}
    model = dict(DEFAULTS, **(opts or {}))["model"]
    return {
        "model": model,
        "per_capture": by_flow,
        "groups": groups,
        "candidate": pa_candidate(groups, opts),
    }


def format_pa_report(result):
    lines = [
        "K2 PA analysis (experimental, model %s):"
        % result.get("model", "first_order_lag")
    ]
    for flow, results in result["per_capture"].items():
        group = result["groups"][flow]
        lines.append(
            "flow %s mm/s: %d/%d replicates accepted, tau median %s s%s"
            % (
                flow,
                group["accepted"],
                group["replicates"],
                _fmt4(group["tau_median"]),
                "" if group["ok"] else " - " + "; ".join(group["reasons"]),
            )
        )
        for index, r in enumerate(results):
            if r["accepted"]:
                fit = r["events"][0]["decay"]
                if "tau1" in fit:
                    lines.append(
                        "  replicate %d: tau1 %.4f s, tau2 %.3f s, "
                        "fast share %.0f%%, r2 %.3f"
                        % (
                            index + 1,
                            fit["tau1"],
                            fit["tau2"],
                            100.0 * fit["fast_share"],
                            fit["r2"],
                        )
                    )
                continue
            why = list(r["reasons"])
            for ev in r["events"]:
                why.extend(ev["reasons"])
            lines.append(
                "  replicate %d rejected: %s" % (index + 1, "; ".join(why))
            )
    cand = result["candidate"]
    if cand["ok"]:
        lines.append(
            "candidate pressure_advance %.4f (not validated: confirm with a "
            "printed test)" % cand["candidate"]
        )
    else:
        lines.append("no candidate: " + "; ".join(cand["reasons"]))
    return "\n".join(lines)


def _fmt4(value):
    return "n/a" if value is None else "%.4f" % value


def _fmt(value):
    return "n/a" if value is None else "%.2f" % value


# --- CSV replay ---------------------------------------------------------------


def load_capture_csv(path):
    """Read a k2_load_cell_pa CSV: metadata from '# key: value' lines."""
    meta = {}
    rows = []
    with open(path, newline="") as stream:
        lines = []
        for line in stream:
            if line.startswith("#"):
                key, _, value = line[1:].partition(":")
                meta[key.strip()] = value.strip()
            else:
                lines.append(line)
    reader = csv.DictReader(lines)
    for row in reader:
        rows.append(row)
    return {
        "meta": meta,
        "flow": capture_flow(meta),
        "times": [float(r["time_s"]) for r in rows],
        "values": [int(r["raw_counts"]) for r in rows],
        "espds": [int(r["e_interval_ticks"]) for r in rows],
        "clock_freq": _float_or_none(meta.get("clock_freq")),
    }


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def capture_flow(meta):
    """Feed rate (mm/s) from a calibration capture label 'flow=<v>'."""
    label = str(meta.get("label", ""))
    if not label.startswith("flow="):
        return None
    try:
        return float(label.split("=", 1)[1])
    except ValueError:
        return None
