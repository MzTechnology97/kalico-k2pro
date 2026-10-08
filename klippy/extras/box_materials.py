# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""K2-OpenHost per-material tables for the CFS Box.

Reference spool lengths and humidity limits by material, resolved by
material family. No Klipper or Box dependencies: box.py and its mixins
import from here.
"""


def clean_nominal_length(value):
    """Nominal spool filament length in metres, or None."""
    if value in (None, ""):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not 1.0 <= value <= 10000.0:
        return None
    return round(value, 3)


# K2-OpenHost: filament length (m) of a 1 kg, 1.75 mm spool per material,
# from typical densities: 1000 / (density g/cm3 * 2.405 cm3/m). It is the
# reference length of third-party RFID spools whose tag gives none (Bambu,
# QIDI through API7); a filament profile's nominal_length_m overrides it.
# Brands and composites vary by a few percent; the CFS percentage caps the
# estimate. Materials not listed use FALLBACK_SPOOL_LENGTH_M.
DEFAULT_SPOOL_LENGTH_M = {
    "ABS": 400.0, "ASA": 390.0, "HIPS": 400.0, "PA": 365.0, "PA-CF": 355.0,
    "PC": 345.0, "PCTG": 340.0, "PETG": 327.0, "PETG-CF": 320.0,
    "PLA": 335.0, "PLA-CF": 320.0, "PP": 460.0, "PVA": 340.0, "TPU": 345.0,
}


FALLBACK_SPOOL_LENGTH_M = 330.0


def material_table_value(table, material):
    """A per-material table entry for a material name, or None.

    The exact name, else its family: PA6-CF, PA12-CF and PAHT-CF are PA-CF,
    PA6 is PA, PLA-SILK is PLA, ASA-CF is ASA when there is no ASA-CF entry.
    """
    name = str(material or "").strip().upper()
    if name in table:
        return table[name]
    base, _sep, suffix = name.partition("-")
    if base.startswith("PA") and base not in table:
        base = "PA"
    for key in (("%s-%s" % (base, suffix)) if suffix else base, base):
        if key in table:
            return table[key]
    return None


def default_spool_length_m(material):
    """Reference spool length (m) for a material name."""
    length = material_table_value(DEFAULT_SPOOL_LENGTH_M, material)
    return FALLBACK_SPOOL_LENGTH_M if length is None else length


# K2-OpenHost: highest CFS relative humidity (%) for a material before a
# print start warns that the spool may be wet (typical storage guidance).
# humidity_limits in [box] overrides entries, e.g. "PA:15, PLA:45".
DEFAULT_HUMIDITY_LIMIT_PCT = {
    "ABS": 50, "ASA": 50, "BVOH": 20, "HIPS": 50, "PA": 25, "PA-CF": 25,
    "PC": 35, "PCTG": 50, "PETG": 50, "PETG-CF": 45, "PLA": 55,
    "PLA-CF": 50, "PP": 55, "PVA": 20, "TPU": 40,
}


def parse_humidity_limits(text):
    """"PA:15, PLA:45" -> {"PA": 15, "PLA": 45}; ValueError when malformed."""
    limits = {}
    for item in str(text or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        material, sep, value = item.partition(":")
        material = material.strip().upper()
        if not sep or not material:
            raise ValueError("expected MATERIAL:PERCENT, got %r" % item)
        limit = int(value)
        if not 1 <= limit <= 100:
            raise ValueError("humidity limit of %s must be 1..100" % material)
        limits[material] = limit
    return limits
