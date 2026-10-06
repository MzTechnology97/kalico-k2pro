"""Host-side prototype of the closed-loop PA sweep (runs on the CM5)."""

import glob
import json
import os
import time
import urllib.error
import urllib.request

M = "http://127.0.0.1:7125"
LOGDIR = os.path.expanduser("~/printer_data/logs")


def get(path):
    return json.load(urllib.request.urlopen(M + path, timeout=10))["result"]


def gcode(script, timeout=1800):
    req = urllib.request.Request(
        M + "/printer/gcode/script",
        data=json.dumps({"script": script}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout)).get(
            "result"
        )
    except urllib.error.HTTPError as e:
        raise RuntimeError(json.load(e).get("error", {}).get("message", ""))


def csvs():
    return set(glob.glob(LOGDIR + "/k2_load_cell_pa_*.csv"))


def load(path):
    meta, rows = {}, []
    for line in open(path):
        if line.startswith("#"):
            k, _, v = line[1:].partition(":")
            meta[k.strip()] = v.strip()
        elif not line.startswith("tick"):
            r = line.strip().split(",")
            rows.append((float(r[1]), float(r[3]), int(r[4]), float(r[5] or 0)))
    return meta, rows


def capture(moves, duration, label, lead=0.3):
    """Start a capture, run the moves, return (meta, rows)."""
    before = csvs()
    gcode(
        "LOAD_CELL_CAPTURE DURATION=%.2f WAIT=0 LABEL=%s\nG4 P%d\n%s\nM400"
        % (duration, label, int(lead * 1000), moves)
    )
    for _ in range(100):
        new = sorted(csvs() - before)
        if new:
            time.sleep(0.5)
            return load(new[-1])
        time.sleep(0.2)
    raise RuntimeError("no capture file")
