"""Sweep metrics with a PA-independent time reference: the first E step."""

import json
import os
import sys

import numpy as np


def load(d):
    info = json.load(open(os.path.join(d, "info.json")))
    out = []
    for p in info["passes"]:
        r = np.array(json.load(open(os.path.join(d, p["file"])))["rows"])
        out.append((p["k"], r[:, 0], r[:, 1], np.abs(r[:, 3])))
    return info, out


def analyze(d, verbose=True):
    info, passes = load(d)
    vs = info["l_slow"] * info["ratio"]
    vf = info["ratio"] * info["v_fast"]
    mid = (vs + vf) / 2
    # offsets of the two transitions from the first step, from the K=0 pass
    k, t, f, ev = passes[0]
    t0 = t[np.argmax(ev > 0.05)]
    up = t[np.argmax(ev > mid)]
    hi = np.where(ev > mid)[0]
    dn = t[hi[-1]]
    off_up, off_dn = up - t0, dn - t0
    rows = []
    for k, t, f, ev in passes:
        t0 = t[np.argmax(ev > 0.05)]
        t_up, t_dn = t0 + off_up, t0 + off_dn
        fs = np.convolve(f, np.ones(10) / 10, mode="same")
        dt = np.median(np.diff(t))

        def m(a, b):
            s = (t >= a) & (t < b)
            return fs[s].mean()

        f_s0 = m(t_up - 0.15, t_up - 0.02)
        f_f = m(t_dn - 0.12, t_dn - 0.02)
        f_s1 = m(t_dn + 0.45, t_dn + 0.7)
        step = f_f - f_s1
        s = (t >= t_dn) & (t < t_dn + 0.4)
        under = (f_s1 - fs[s].min()) / step
        sa = (t >= t_dn - 0.05) & (t < t_dn + 0.45)
        y = (fs[sa] - f_s1) / step
        fall_area = y.sum() * dt - 0.05
        f_fm = m(t_up + 0.15, t_up + 0.3)
        sr = (t >= t_up - 0.05) & (t < t_up + 0.3)
        rise_area = ((f_fm - fs[sr]) / (f_fm - f_s0)).sum() * dt - 0.05
        s2 = (t >= t_up) & (t < t_up + 0.15)
        over = (fs[s2].max() - f_fm) / (f_fm - f_s0)
        rows.append((k, under, fall_area, rise_area, over, step))
    a = np.array(rows)
    if verbose:
        print(
            "offsets from first step: up %.3f s, down %.3f s" % (off_up, off_dn)
        )
        print(" K      undershoot fallArea riseArea overshoot  step")
        for r in rows:
            print("%.4f %8.3f %9.4f %8.4f %8.3f %8.0f" % r)
    # zero crossing of the fall area: linear fit over the passes around it
    K, A = a[:, 0], a[:, 2]
    i = np.argmin(np.abs(A))
    sel = slice(max(0, i - 3), i + 4)
    c = np.polyfit(K[sel], A[sel], 1)
    k_area = -c[1] / c[0]
    cr = np.polyfit(K[sel], a[sel, 3], 1)
    k_rise = -cr[1] / cr[0]
    # undershoot onset: hinge fit, flat ~0 below k_on, linear above
    U = a[:, 1]
    best = None
    for k_on in np.arange(K[0], K[-1], 0.0005):
        x = np.clip(K - k_on, 0, None)
        if not x.any():
            continue
        sl = (x * U).sum() / (x * x).sum()
        err = ((U - sl * x) ** 2).sum()
        if best is None or err < best[0]:
            best = (err, k_on, sl)
    print(
        "fall-area zero %.4f | rise-area zero %.4f | undershoot onset %.4f"
        % (k_area, k_rise, best[1])
    )
    return k_area, k_rise, best[1]


if __name__ == "__main__":
    for d in sys.argv[1:]:
        print("==", d)
        analyze(d)
