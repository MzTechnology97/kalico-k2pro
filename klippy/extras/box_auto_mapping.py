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


def material_cost(first, second):
    a, b = _norm(first), _norm(second)
    if not a or not b:
        return 0.05
    if a == b:
        return 0.0
    family_a, family_b = _family(first), _family(second)
    return 0.05 if family_a and family_a == family_b else None


def suggest_mapping(tools, slots):
    candidates = []
    for tool in tools:
        tool_id = int(tool["tool"])
        for slot in slots:
            material = material_cost(tool.get("material"), slot.get("material"))
            color = color_cost(tool.get("color"), slot.get("color"))
            name_exact = bool(tool.get("name") and slot.get("name")
                              and _norm(tool.get("name")) == _norm(slot.get("name")))
            if material is not None and color is not None:
                score = material + color - (0.02 if name_exact else 0.0)
            elif name_exact and material is not None:
                score = 0.20 + material
            else:
                continue
            candidates.append((score, slot.get("index") != tool_id,
                               bool(slot.get("external")), int(slot["index"]), tool_id))
    candidates.sort()
    mapping, used = {}, set()
    for shared in (False, True):
        for _score, _own, _external, slot, tool in candidates:
            if tool in mapping or (not shared and slot in used):
                continue
            mapping[tool] = slot
            used.add(slot)
    loaded = [slot for slot in slots if slot.get("loaded")]
    if len(tools) == 1 and int(tools[0]["tool"]) not in mapping and len(loaded) == 1:
        mapping[int(tools[0]["tool"])] = int(loaded[0]["index"])
    unresolved = sorted(int(item["tool"]) for item in tools if int(item["tool"]) not in mapping)
    return mapping, unresolved