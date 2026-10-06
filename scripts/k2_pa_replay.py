#!/usr/bin/env python3
# Replay k2_load_cell_pa captures offline and print the pressure advance
# analysis (experimental).
#
# Copyright (C) 2026  MzTechnology97
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Usage: k2_pa_replay.py [--json] [--opt key=value ...] capture.csv ...

Runs the same analysis as LOAD_CELL_PA_ANALYZE on CSV files written by
[k2_load_cell_pa]. Needs only the Python standard library. Captures are
grouped by their label (flow=<mm/s>).
"""

import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "klippy" / "extras" / "k2_pa_analysis.py"


def load_analysis():
    spec = importlib.util.spec_from_file_location("k2_pa_analysis", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", help="k2_load_cell_pa CSV files")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--opt",
        action="append",
        default=[],
        help="analysis option override, e.g. --opt min_snr=6",
    )
    args = parser.parse_args(argv)
    analysis = load_analysis()
    opts = {}
    for item in args.opt:
        key, _, value = item.partition("=")
        if key not in analysis.DEFAULTS:
            parser.error("unknown option %s" % key)
        opts[key] = type(analysis.DEFAULTS[key])(value)
    captures = [analysis.load_capture_csv(path) for path in args.files]
    missing = [p for p, c in zip(args.files, captures) if c["flow"] is None]
    if missing:
        parser.error("no flow=<mm/s> label in: %s" % ", ".join(missing))
    result = analysis.analyze_pa_captures(captures, opts)
    if args.json:
        json.dump(result, sys.stdout, indent=1, default=str)
        print()
    else:
        print(analysis.format_pa_report(result))
    return 0 if result["candidate"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
