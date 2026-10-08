#!/usr/bin/env python3
"""Generate config/k2/orca_k2pro_filament_ids.json from OrcaSlicer profiles.

For every filament of config/k2/cfs_system_filaments.json, find the
OrcaSlicer system preset of the same product for the K2 Pro and record its
``filament_id``, the ID OrcaSlicer's filament sync matches presets with.

Lookup order for a catalog filament named NAME:
  1. Creality vendor: "NAME @K2 Pro-all";
  2. OrcaFilamentLibrary: "NAME @System" (Generic profiles only).
A material without a match keeps no ID (OrcaSlicer then uses its generic
preset for the material type). The "materials" section gives the generic
preset per material, used for slots without a library profile. The "presets"
section lists every OrcaSlicer system filament usable on the K2 Pro (Creality
"@K2 Pro-all" and OrcaFilamentLibrary "@System" presets for all printers),
for the preset picker of Mainsail and for third-party spools (a Bambu tag
"PETG HF" is the preset "Bambu PETG HF").

The ``filament_id`` is resolved through the ``inherits`` chain, as OrcaSlicer
does. Profiles are read from raw.githubusercontent.com at a pinned OrcaSlicer
tag, so the output is reproducible.

usage: generate_orca_filament_ids.py [--ref v2.4.2] [--out PATH]
"""

import argparse
import concurrent.futures
import json
import os
import sys
import urllib.parse
import urllib.request

REPO = "OrcaSlicer/OrcaSlicer"
PRINTER_SUFFIX = " @K2 Pro-all"
LIBRARY_SUFFIX = " @System"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "config", "k2", "cfs_system_filaments.json")
OUTPUT = os.path.join(ROOT, "config", "k2", "orca_k2pro_filament_ids.json")


def list_dir(ref, path):
    url = "https://api.github.com/repos/%s/contents/%s?ref=%s" % (
        REPO,
        urllib.parse.quote(path),
        ref,
    )
    request = urllib.request.Request(url)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_json(ref, vendor, name):
    # vendor is a profile folder, or "Folder/sub" for a library vendor
    # (resources/profiles/OrcaFilamentLibrary/filament/Bambu/...).
    root, _, sub = vendor.partition("/")
    path = "resources/profiles/%s/filament/%s%s.json" % (
        root,
        sub + "/" if sub else "",
        name,
    )
    url = "https://raw.githubusercontent.com/%s/%s/%s" % (
        REPO,
        ref,
        urllib.parse.quote(path),
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def resolve(ref, vendor, name, cache):
    """(filament_id, filament_type, compatible_printers) of a preset,
    following ``inherits`` (a library vendor's base may sit in the folder of
    the library itself)."""
    key = (vendor, name)
    if key not in cache:
        cache[key] = fetch_json(ref, vendor, name)
        root = vendor.split("/")[0]
        for fallback in (root, root + "/base"):
            if cache[key] is not None or fallback == vendor:
                continue
            cache[key] = fetch_json(ref, fallback, name)
    profile = cache[key]
    if profile is None:
        return None, None, None
    filament_id = profile.get("filament_id")
    filament_type = profile.get("filament_type")
    if isinstance(filament_type, list):
        filament_type = filament_type[0] if filament_type else None
    compatible = profile.get("compatible_printers")
    parent = profile.get("inherits")
    if parent and (not filament_id or not filament_type or compatible is None):
        parent_id, parent_type, parent_compatible = resolve(
            ref, vendor, parent, cache
        )
        filament_id = filament_id or parent_id
        filament_type = filament_type or parent_type
        if compatible is None:
            compatible = parent_compatible
    return filament_id, filament_type, compatible


def k2_presets(ref, cache):
    """Every OrcaSlicer system filament usable on the K2 Pro."""
    jobs = []
    for item in list_dir(ref, "resources/profiles/Creality/filament"):
        name = item["name"][:-5]
        if item["type"] == "file" and name.endswith(PRINTER_SUFFIX):
            jobs.append(("Creality", name, "Creality"))
    library = "resources/profiles/OrcaFilamentLibrary/filament"
    for item in list_dir(ref, library):
        if item["type"] == "file" and item["name"].endswith(
            LIBRARY_SUFFIX + ".json"
        ):
            jobs.append(("OrcaFilamentLibrary", item["name"][:-5], ""))
        elif item["type"] == "dir" and item["name"] != "base":
            for sub in list_dir(ref, library + "/" + item["name"]):
                if sub["name"].endswith(LIBRARY_SUFFIX + ".json"):
                    jobs.append(
                        (
                            "OrcaFilamentLibrary/" + item["name"],
                            sub["name"][:-5],
                            item["name"],
                        )
                    )

    def run(job):
        vendor, preset, label = job
        return job, resolve(ref, vendor, preset, cache)

    presets = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for (vendor, preset, label), (fid, ftype, compatible) in pool.map(
            run, jobs
        ):
            if not fid:
                continue
            # Library presets: only those for every printer (empty list).
            if vendor != "Creality" and compatible:
                continue
            presets.append(
                {
                    "orca_filament_id": fid,
                    "orca_preset": preset,
                    "orca_filament_type": ftype or "",
                    "vendor": label or "Generic",
                }
            )
    # One entry per id: a Creality "Generic X @K2 Pro-all" inherits the
    # library's "Generic X @System" and is the same product (same id).
    presets.sort(
        key=lambda p: (p["vendor"] != "Creality", p["orca_preset"].lower())
    )
    unique = {}
    for preset in presets:
        unique.setdefault(preset["orca_filament_id"], preset)
    return sorted(unique.values(), key=lambda p: p["orca_preset"].lower())


def lookup(ref, entry, cache):
    name = str(entry.get("name") or "").strip()
    candidates = [("Creality", name + PRINTER_SUFFIX)]
    if str(entry.get("brand") or "").strip().lower() == "generic":
        candidates.append(("OrcaFilamentLibrary", name + LIBRARY_SUFFIX))
    for vendor, preset in candidates:
        filament_id, filament_type, _ = resolve(ref, vendor, preset, cache)
        if filament_id:
            return {
                "orca_filament_id": filament_id,
                "orca_preset": preset,
                "orca_filament_type": filament_type or "",
            }
    return None


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ref",
        default="v2.4.2",
        help="OrcaSlicer tag or commit (default v2.4.2)",
    )
    parser.add_argument("--out", default=OUTPUT)
    args = parser.parse_args(argv)

    with open(CATALOG, encoding="utf-8") as stream:
        catalog = json.load(stream)["materials"]
    cache = {}
    filaments, missing = {}, []
    for entry in catalog:
        found = lookup(args.ref, entry, cache)
        if found:
            filaments[str(entry["id"]).upper()] = dict(
                found, name=entry["name"]
            )
        else:
            missing.append("%s %s" % (entry["id"], entry["name"]))
    materials = {}
    for entry in catalog:
        if str(entry.get("brand") or "").lower() != "generic":
            continue
        material = str(entry.get("material") or "").upper()
        found = filaments.get(str(entry["id"]).upper())
        if (
            found
            and material not in materials
            and entry["name"].upper() == "GENERIC " + material
        ):
            materials[material] = {
                key: found[key]
                for key in (
                    "orca_filament_id",
                    "orca_preset",
                    "orca_filament_type",
                )
            }
    presets = k2_presets(args.ref, cache)
    payload = {
        "schema_version": 1,
        "source": "%s %s resources/profiles" % (REPO, args.ref),
        "printer": "Creality K2 Pro",
        "description": (
            "OrcaSlicer filament_id of the K2 Pro system preset matching each "
            "CFS system filament (by catalog ID) and the generic preset per "
            "material. Generated by scripts/generate_orca_filament_ids.py; "
            "user overrides live in the filament library."
        ),
        "filaments": dict(sorted(filaments.items())),
        "materials": dict(sorted(materials.items())),
        "presets": presets,
    }
    with open(args.out, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(
        "%d of %d filaments matched, %d materials, %d presets"
        % (len(filaments), len(catalog), len(materials), len(presets))
    )
    for line in missing:
        print("  no OrcaSlicer preset: %s" % line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
