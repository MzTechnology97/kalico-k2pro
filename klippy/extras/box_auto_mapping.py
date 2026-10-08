# Copyright (C) 2026 MzTechnology97 and contributors
# Derived in part from Jacob10383 OrcaSlicer Box mapping logic (GPLv3).
"""Pure helpers for matching slicer tools to CFS filament slots."""

import math
import re


def _norm(value):
    return re.sub(r"[\s_-]+", "", str(value or "").strip().upper())


def _family(value):
    match = re.match(r"[A-Z]+", str(value or "").strip().upper())
    return match.group(0) if match else ""


def _oklab(color):
    text = str(color or "").strip().lstrip("#")
    if len(text) not in (6, 8) or any(c not in "0123456789abcdefABCDEF" for c in text):
        return None
    rgb = []
    for offset in (0, 2, 4):
        value = int(text[offset:offset + 2], 16) / 255.0
        rgb.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    l = (0.4122214708 * rgb[0] + 0.5363325363 * rgb[1] + 0.0514459929 * rgb[2]) ** (1.0 / 3.0)
    m = (0.2119034982 * rgb[0] + 0.6806995451 * rgb[1] + 0.1073969566 * rgb[2]) ** (1.0 / 3.0)
    s = (0.0883024619 * rgb[0] + 0.2817188376 * rgb[1] + 0.6299787005 * rgb[2]) ** (1.0 / 3.0)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


def color_cost(first, second):
    a, b = _oklab(first), _oklab(second)
    if a is None or b is None:
        return None
    if math.hypot(a[1], a[2]) >= 0.05 and math.hypot(b[1], b[2]) >= 0.05:
        hue = abs(math.atan2(a[2], a[1]) - math.atan2(b[2], b[1])) * 180.0 / math.pi
        if min(hue, 360.0 - hue) > 10.0:
            return None
    distance = math.sqrt(((a[0] - b[0]) / 2.0) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)
    return distance if distance <= 0.12 else None


# Reinforcing fillers change how a filament prints (abrasive, different
# temperatures), so "PETG" and "PETG-CF" are not interchangeable.
FILLERS = ("CF", "GF", "KF", "AF")
# Filament needed beyond the slicer length: prime, purge and flush margin.
LENGTH_MARGIN = 1.10
LENGTH_RESERVE_M = 1.0


def _fillers(value):
    tokens = re.split(r"[^A-Z0-9]+", str(value or "").strip().upper())
    return frozenset(token for token in tokens[1:] if token in FILLERS)


def material_cost(first, second):
    a, b = _norm(first), _norm(second)
    if not a or not b:
        return 0.05
    if a == b:
        return 0.0
    family_a, family_b = _family(first), _family(second)
    if not family_a or family_a != family_b:
        return None
    if _fillers(first) != _fillers(second):
        # Never map a base material on its filled variant (or the reverse).
        return None
    return 0.05


def is_material_variant(first, second):
    """Same material family with different fillers, e.g. PETG and PETG-CF."""
    family = _family(first)
    return bool(family and family == _family(second)
                and _fillers(first) != _fillers(second))


def needed_m(tool):
    """Filament a tool needs in metres, with margin; None when unknown."""
    try:
        length = float(tool.get("length_mm"))
    except (TypeError, ValueError):
        return None
    if length <= 0:
        return None
    return length / 1000.0 * LENGTH_MARGIN + LENGTH_RESERVE_M


def _remaining_m(slot):
    try:
        value = float(slot.get("rfid_remaining_m"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def available_m(slot, slots, swap=False):
    """Known filament for a slot, plus the spools runout swap would continue on.

    None means unknown (no RFID estimate), which is treated as enough.
    """
    total = _remaining_m(slot)
    if total is None:
        return None
    if not swap or slot.get("external"):
        return total
    for other in slots:
        if other is slot or other.get("external") or not other.get("present", True):
            continue
        # Runout swap only continues on the same material and the same colour.
        if (_norm(other.get("material")) != _norm(slot.get("material"))
                or _norm(other.get("color")) != _norm(slot.get("color"))):
            continue
        remaining = _remaining_m(other)
        if remaining is None:
            return None
        total += remaining
    return total


def _slot_by_index(slots, index):
    for slot in slots:
        if int(slot.get("index", -1)) == int(index):
            return slot
    return None


def live_filament_check(tools, slots, mapping, swap, used_m, progress):
    """During a print: filament each mapped tool still needs vs what is left.

    With one tool the slicer length minus the filament already used is exact.
    With several tools the per-tool use is not known, so the need is scaled
    by the remaining file progress (``estimated``). ``available_m`` is None
    when no RFID estimate exists; such a slot is never reported short.
    """
    single = len(tools) == 1
    results = []
    for tool in tools:
        tool_id = int(tool["tool"])
        if tool_id not in mapping:
            continue
        slot = _slot_by_index(slots, mapping[tool_id])
        need = needed_m(tool)
        if slot is None or need is None:
            continue
        if single:
            left = max(0.0, need - max(0.0, float(used_m or 0.0)))
        else:
            left = need * max(0.0, 1.0 - min(1.0, float(progress or 0.0)))
        have = available_m(slot, slots, swap)
        results.append({
            "tool": tool_id,
            "slot": int(slot["index"]),
            "needed_m": round(left, 2),
            "available_m": None if have is None else round(have, 2),
            "includes_swap": bool(
                swap and have is not None and have != _remaining_m(slot)),
            "estimated": not single,
            "short": have is not None and have < left,
        })
    return results


def evaluate_mapping(tools, slots, mapping, swap=False):
    """Warnings for a tool -> slot map. They never block a print."""
    warnings = []
    for tool in tools:
        tool_id = int(tool["tool"])
        if tool_id not in mapping:
            continue
        slot = _slot_by_index(slots, mapping[tool_id])
        if slot is None:
            continue
        # Only a map chosen by hand (or the loaded-filament fallback) can
        # reach these: the automatic matcher never pairs such materials.
        if material_cost(tool.get("material"), slot.get("material")) is None:
            warnings.append({
                "kind": ("material_variant"
                         if is_material_variant(tool.get("material"), slot.get("material"))
                         else "material_mismatch"),
                "tool": tool_id, "slot": int(slot["index"]),
                "tool_material": tool.get("material", ""), "slot_material": slot.get("material", ""),
            })
        need = needed_m(tool)
        have = available_m(slot, slots, swap)
        if need is not None and have is not None and have < need:
            warnings.append({
                "kind": "low_filament", "tool": tool_id, "slot": int(slot["index"]),
                "needed_m": round(need, 2), "remaining_m": round(have, 2),
                "includes_swap": bool(swap and have != _remaining_m(slot)),
            })
        humidity = slot.get("humidity_pct")
        limit = slot.get("humidity_limit_pct")
        if (isinstance(humidity, (int, float)) and isinstance(limit, (int, float))
                and humidity > limit):
            warnings.append({
                "kind": "humidity", "tool": tool_id, "slot": int(slot["index"]),
                "humidity_pct": humidity, "limit_pct": limit,
                "slot_material": slot.get("material", ""),
            })
    return warnings


def suggest_mapping_report(tools, slots, swap=False):
    candidates = []
    for tool in tools:
        tool_id = int(tool["tool"])
        need = needed_m(tool)
        for slot in slots:
            material = material_cost(tool.get("material"), slot.get("material"))
            color = color_cost(tool.get("color"), slot.get("color"))
            name_exact = bool(tool.get("name") and slot.get("name")
                              and _norm(tool.get("name")) == _norm(slot.get("name")))
            slot_generic = (
                _norm(slot.get("brand")) == "GENERIC"
                or _norm(slot.get("name")).startswith("GENERIC"))
            if material is not None and color is not None:
                # Exact Orca preset names remain the strongest signal. If the
                # slicer preset cannot be matched by name, prefer a Generic
                # material profile over an unrelated vendor profile with the
                # same material/color. This mirrors Orca's safe generic
                # fallback without ever ignoring color on multicolor jobs.
                bucket = 0
                score = material + color
                if name_exact:
                    score -= 0.04
                elif slot_generic:
                    score += 0.01
                else:
                    score += 0.03
            elif name_exact and material is not None:
                bucket = 1
                score = 0.20 + material
            else:
                continue
            have = available_m(slot, slots, swap)
            short = need is not None and have is not None and have < need
            remaining = slot.get("rfid_percent")
            try:
                remaining = float(remaining)
            except (TypeError, ValueError):
                remaining = None
            candidates.append((
                bucket,
                # Among equivalent spools, one with enough filament wins;
                # otherwise the lowest remaining is used up first.
                short,
                score,
                # Then the user's runout order among equivalent spools.
                slot.get("runout_rank") is None,
                0 if slot.get("runout_rank") is None else int(slot["runout_rank"]),
                remaining is None,
                101.0 if remaining is None else max(0.0, min(100.0, remaining)),
                slot.get("index") != tool_id,
                bool(slot.get("external")),
                int(slot["index"]),
                tool_id,
            ))
    candidates.sort()
    mapping, used = {}, set()
    for shared in (False, True):
        for candidate in candidates:
            slot, tool = candidate[-2], candidate[-1]
            if tool in mapping or (not shared and slot in used):
                continue
            mapping[tool] = slot
            used.add(slot)
    loaded = [slot for slot in slots if slot.get("loaded")]
    if (len(tools) == 1 and int(tools[0]["tool"]) not in mapping and len(loaded) == 1
            and material_cost(tools[0].get("material"), loaded[0].get("material")) is not None):
        # The filament already in the printhead, unless it is another material.
        mapping[int(tools[0]["tool"])] = int(loaded[0]["index"])
    unresolved = sorted(int(item["tool"]) for item in tools if int(item["tool"]) not in mapping)
    return {
        "map": mapping,
        "unresolved": unresolved,
        "warnings": evaluate_mapping(tools, slots, mapping, swap),
    }


def suggest_mapping(tools, slots, swap=False):
    report = suggest_mapping_report(tools, slots, swap)
    return report["map"], report["unresolved"]
