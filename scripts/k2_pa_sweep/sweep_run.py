"""Closed-loop PA sweep prototype (archived 2026-10-06, see docs/K2_Load_Cell_PA.md).
Runs on the host through Moonraker: lines in the air over the bed, one capture per
pressure advance value. Usage:
  python3 sweep_run.py SLOT TEMP SLOW_MM3 FAST_MM3 K0 K1 KSTEP [ACCEL] [TAG]"""

import json
import os
import sys
import time

from sweep_lib import capture, gcode, get

slot, temp = int(sys.argv[1]), float(sys.argv[2])
slow_q, fast_q = float(sys.argv[3]), float(sys.argv[4])
k0, k1, kstep = float(sys.argv[5]), float(sys.argv[6]), float(sys.argv[7])
accel = float(sys.argv[8]) if len(sys.argv) > 8 else 5000.0
tag = sys.argv[9] if len(sys.argv) > 9 else "sweep"
AREA = 2.4053  # 1.75 mm filament
V_FAST = 100.0  # mm/s XY in the fast segment
ratio = fast_q / AREA / V_FAST  # mm of filament per mm of XY
v_slow = slow_q / AREA / ratio  # XY speed giving the slow flow
L_SLOW, T_FAST = v_slow * 1.0, 0.4  # 1 s slow before and after
L_FAST = V_FAST * T_FAST
X0, Y0, Z = (
    float(os.environ.get("XC", 150)) - (2 * L_SLOW + L_FAST) / 2,
    float(os.environ.get("Y0", 60)),
    50.0,
)
out = os.path.expanduser("~/pa_sweep/%s_%s" % (tag, time.strftime("%H%M%S")))
os.makedirs(out)

st = get(
    "/printer/objects/query?box=loaded_slot&extruder=pressure_advance,smooth_time&toolhead=homed_axes"
)["status"]
pa_before = st["extruder"]["pressure_advance"]
if st["toolhead"]["homed_axes"] != "xyz":
    gcode("G28")
if st["box"]["loaded_slot"] != slot:
    gcode("BOX_SELECT_SLOT SLOT=%d" % slot)
gcode("M109 S%d" % temp)
gcode("BOX_NOZZLE_CLEAN")
gcode(
    "G90\nG1 Z%.1f F1200\nG1 X%.2f Y%.2f F12000\nM83\nSET_VELOCITY_LIMIT ACCEL=%d\nM106 S0\nM400"
    % (Z, X0, Y0 - 6, accel)
)
# prime in the air at the slow flow, then let the pressure settle
gcode(
    "G1 X%.2f E%.3f F%.1f\nG1 X%.2f E%.3f F%.1f\nG1 Y%.2f F3000\nG4 P2000"
    % (
        X0 + 2 * L_SLOW + L_FAST,
        15.0,
        (2 * L_SLOW + L_FAST) / (15.0 / (slow_q / AREA)) * 60,
        X0,
        0.0,
        6000,
        Y0,
    )
)
info = dict(
    slot=slot,
    temp=temp,
    slow_q=slow_q,
    fast_q=fast_q,
    ratio=ratio,
    v_slow=v_slow,
    v_fast=V_FAST,
    l_slow=L_SLOW,
    l_fast=L_FAST,
    accel=accel,
    smooth_time=st["extruder"]["smooth_time"],
    passes=[],
)
n = int(round((k1 - k0) / kstep)) + 1
x, y = X0, Y0
try:
    for i in range(n):
        k = round(k0 + i * kstep, 4)
        d = 1 if i % 2 == 0 else -1
        xs = [
            x + d * L_SLOW,
            x + d * (L_SLOW + L_FAST),
            x + d * (2 * L_SLOW + L_FAST),
        ]
        moves = (
            "SET_PRESSURE_ADVANCE ADVANCE=%.4f\n"
            "G1 X%.2f E%.4f F%.1f\nG1 X%.2f E%.4f F%.1f\nG1 X%.2f E%.4f F%.1f"
            % (
                k,
                xs[0],
                L_SLOW * ratio,
                v_slow * 60,
                xs[1],
                L_FAST * ratio,
                V_FAST * 60,
                xs[2],
                L_SLOW * ratio,
                v_slow * 60,
            )
        )
        meta, rows = capture(moves, 5.5, "sweep_k%.4f" % k, lead=0.4)
        fn = "%s/k%.4f.json" % (out, k)
        json.dump({"k": k, "meta": meta, "rows": rows}, open(fn, "w"))
        info["passes"].append({"k": k, "file": os.path.basename(fn), "dir": d})
        print("K %.4f captured %d samples" % (k, len(rows)), flush=True)
        x = xs[2]
        y += 3.0
        gcode("G1 Y%.2f F3000\nG4 P1000" % y)
finally:
    gcode(
        "SET_PRESSURE_ADVANCE ADVANCE=%.4f\nSET_VELOCITY_LIMIT ACCEL=20000\nM400"
        % pa_before
    )
    json.dump(info, open(out + "/info.json", "w"), indent=1)
    print("saved", out)
