#!/usr/bin/env python3
# Fill the CFS filament library's max flow from OrcaSlicer filament presets.
#
# Copyright (C) 2026  MzTechnology97
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Copy filament_max_volumetric_speed from OrcaSlicer presets to the printer.

Runs on the PC where OrcaSlicer is installed. It reads the system and user
filament presets (resolving "inherits"), matches them by name to the custom
profiles of the printer's CFS filament library (box/filament-pa-maxflow) and
sends _BOX_FILAMENT_SET ... MAX_FLOW=<value> through Moonraker.

- Only custom profiles are changed; system (catalog) profiles are read only.
- A profile that already has a max flow is left alone (--overwrite changes it).
- Pressure advance is never copied: the printer keeps its own (calibrated) PA.
- Without --apply nothing is sent: the matches are only listed.

    python3 scripts/k2_orca_filament_import.py --printer http://10.10.1.97:7125
    python3 scripts/k2_orca_filament_import.py --printer http://10.10.1.97:7125 --apply
"""

import argparse
import glob
import json
import os
import re
import sys
import urllib.request

VENDOR_ALIASES = {
    "bambulab": "bambu",
    "bambu lab": "bambu",
    "bambu": "bambu",
    "creality": "cr",
}


def default_orca_dir():
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", ""), "OrcaSlicer")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/OrcaSlicer")
    return os.path.expanduser("~/.config/OrcaSlicer")


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def load_presets(orca_dir):
    """name -> (preset dict, is_user) for every filament preset found."""
    presets = {}
    for path in glob.glob(
        os.path.join(orca_dir, "system", "*", "filament", "**", "*.json"),
        recursive=True,
    ):
        _add(presets, path, False)
    for path in glob.glob(
        os.path.join(orca_dir, "user", "*", "filament", "*.json")
    ):
        _add(presets, path, True)
    return presets


def _add(presets, path, is_user):
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or not data.get("name"):
        return
    name = data["name"]
    if name in presets and presets[name][1] and not is_user:
        return  # a user preset wins over a system one with the same name
    presets[name] = (data, is_user)


def resolve(presets, name, key, depth=0):
    entry = presets.get(name)
    if entry is None or depth > 12:
        return None
    data = entry[0]
    if key in data:
        return _first(data[key])
    parent = data.get("inherits")
    return resolve(presets, parent, key, depth + 1) if parent else None


def normalize(text):
    """Comparable name: lower case, no printer suffix, common vendor names."""
    text = str(text or "").lower()
    text = text.split("@", 1)[0]
    text = re.sub(r"[^a-z0-9+]+", " ", text).strip()
    for alias, canonical in sorted(
        VENDOR_ALIASES.items(), key=lambda kv: -len(kv[0])
    ):
        if text.startswith(alias + " "):
            text = canonical + text[len(alias) :]
            break
    return re.sub(r"\s+", " ", text)


def is_for_printer(data, hint):
    compatible = " ".join(data.get("compatible_printers") or []).lower()
    return hint in str(data.get("name", "")).lower() or hint in compatible


def match(profile, presets, hint):
    """Preset with the same name as a library profile, or None.

    Names must be equal once normalised (printer suffix dropped, Bambu /
    Bambulab / Bambu Lab treated alike): a near match could copy another
    product's value. Among equal names: this printer's presets first, then
    user presets. The material must agree.
    """
    material = str(profile.get("material") or "").upper()
    wanted = {
        normalize(profile.get("name")),
        normalize(
            "%s %s" % (profile.get("brand", ""), profile.get("name", ""))
        ),
    }
    wanted.discard("")
    hint = hint.lower()
    best = None
    for name, (data, is_user) in presets.items():
        if normalize(name) not in wanted:
            continue
        kind = str(resolve(presets, name, "filament_type") or "").upper()
        if material and kind and kind != material:
            continue
        score = 2 * is_for_printer(data, hint) + int(is_user)
        if best is None or score > best[0]:
            best = (score, name)
    return None if best is None else best[1]


def moonraker(printer, path, payload=None, timeout=10):
    url = printer.rstrip("/") + path
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["result"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--printer",
        required=True,
        help="Moonraker URL, e.g. http://10.10.1.97:7125",
    )
    parser.add_argument("--orca-dir", default=default_orca_dir())
    parser.add_argument(
        "--printer-hint",
        default="K2",
        help="text in preset names/compatible printers (K2)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="also replace a max flow the profile already has",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="send the changes (default: only list them)",
    )
    args = parser.parse_args(argv)

    presets = load_presets(args.orca_dir)
    if not presets:
        print("No OrcaSlicer filament presets in %s" % args.orca_dir)
        return 1
    status = moonraker(args.printer, "/printer/objects/query?box=filaments")
    filaments = (status["status"].get("box") or {}).get("filaments") or {}
    custom = [f for f in filaments.values() if not f.get("system")]
    print(
        "%d OrcaSlicer presets, %d custom library profiles"
        % (len(presets), len(custom))
    )

    scripts, unmatched = [], []
    for profile in sorted(custom, key=lambda f: f.get("id", "")):
        label = "%s %s (%s)" % (
            profile.get("id"),
            profile.get("name") or "",
            profile.get("material"),
        )
        preset = match(profile, presets, args.printer_hint)
        if preset is None:
            unmatched.append(label)
            continue
        value = resolve(presets, preset, "filament_max_volumetric_speed")
        try:
            value = round(float(value), 2)
        except (TypeError, ValueError):
            print("  %-42s <- %s: no max volumetric speed" % (label, preset))
            continue
        current = profile.get("max_flow")
        if current is not None and not args.overwrite:
            print(
                "  %-42s <- %s: keeps %g (Orca %g)"
                % (label, preset, current, value)
            )
            continue
        print("  %-42s <- %s: max flow %g mm3/s" % (label, preset, value))
        scripts.append(
            '_BOX_FILAMENT_SET ID="%s" MATERIAL="%s" MAX_FLOW=%g'
            % (profile["id"], profile["material"], value)
        )
    for label in unmatched:
        print("  %-42s    no matching OrcaSlicer preset" % label)
    if not scripts:
        print("Nothing to change.")
        return 0
    if not args.apply:
        print(
            "%d change(s). Run again with --apply to send them." % len(scripts)
        )
        return 0
    for script in scripts:
        moonraker(
            args.printer,
            "/printer/gcode/script",
            {"script": script},
            timeout=30,
        )
    print("%d profile(s) updated." % len(scripts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
