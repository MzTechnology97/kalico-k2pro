# Offline analysis of K2 load-cell captures (statistics and an experimental
# pressure advance candidate).
#
# Copyright (C) 2026  MzTechnology97
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Pure Python, no Klipper imports: used by k2_load_cell and on a PC.

Method (see docs/K2_Load_Cell.md):
- each capture holds one E-only extrusion pulse at a known feed rate,
  with rest periods before and after;
- the load signal after the pulse stops is fitted with
  y(t) = c + a * exp(-(t - t_stop) / tau);
- under a first-order lag model of the melt flow, Klipper's pressure
  advance K (seconds) that cancels the lag is K = tau. This is a model
  candidate, not a measurement of pressure: it must be confirmed with a
  printed pressure advance test.
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
    "fit_window": 0.6,  # s after the pulse stop
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


def find_extrusion_events(times, espds, max_gap=0.02):
    """Intervals where the E stepper had steps pending (espd != 0).

    Short holes (<= max_gap s) inside a pulse are merged.
    """
    events = []
    start = last = None
    for t, e in zip(times, espds):
        if e:
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
    events = find_extrusion_events(times, capture["espds"])
    if len(events) != 1:
        out["reasons"].append(
            "expected one extrusion pulse, found %d" % len(events)
        )
    for start, stop in events:
        decay = fit_exponential(
            times, values, stop, o["fit_window"], o["tau_min"], o["tau_max"]
        )
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
        "model": "first_order_lag",
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
    return {
        "per_capture": by_flow,
        "groups": groups,
        "candidate": pa_candidate(groups, opts),
    }


def format_pa_report(result):
    lines = ["K2 PA analysis (experimental, model first_order_lag):"]
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
    """Read a k2_load_cell CSV: metadata from '# key: value' lines."""
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
    }


def capture_flow(meta):
    """Feed rate (mm/s) from a calibration capture label 'flow=<v>'."""
    label = str(meta.get("label", ""))
    if not label.startswith("flow="):
        return None
    try:
        return float(label.split("=", 1)[1])
    except ValueError:
        return None
