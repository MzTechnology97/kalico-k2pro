# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""K2-RFID / Creality material database compatibility helpers."""

import json
import os


def _text(value):
    return str(value or "").strip()


def _temperature(item, base):
    value = (item.get("kvParam") or {}).get("nozzle_temperature")
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        pass
    try:
        low = float(base.get("minTemp"))
        high = float(base.get("maxTemp"))
        return int(round((low + high) / 2.0))
    except (TypeError, ValueError):
        return None


def _codes(material_id, aliases=(), explicit=()):
    values = []
    for value in [material_id] + list(aliases) + list(explicit):
        code = _text(value).upper()
        if not code:
            continue
        if code not in values:
            values.append(code)
        if len(code) == 5 and code.isdigit():
            tagged = "1" + code
            if tagged not in values:
                values.append(tagged)
    return values


class K2RfidMaterialCatalog:
    """Load a K2-RFID material_database.json without making color an identity."""

    def __init__(self, path=None):
        self.path = os.path.expanduser(_text(path))
        self._mtime = None
        self._entries = []
        self._by_code = {}
        self.reload()

    @property
    def entries(self):
        self._refresh_if_changed()
        return [dict(item) for item in self._entries]

    def _refresh_if_changed(self):
        if not self.path:
            return
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if self._mtime != mtime:
            self.reload()

    def reload(self):
        self._entries = []
        self._by_code = {}
        if not self.path:
            return 0
        try:
            with open(self.path, "r", encoding="utf-8-sig") as stream:
                payload = json.load(stream)
            self._mtime = os.path.getmtime(self.path)
        except (OSError, ValueError):
            self._mtime = None
            return 0

        raw = payload
        if isinstance(payload, dict):
            raw = payload.get("materials")
            if raw is None:
                raw = (payload.get("result") or {}).get("list")
        if not isinstance(raw, list):
            return 0

        identities = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("base"), dict):
                base = item["base"]
                entry = {
                    "id": _text(base.get("id")).upper(),
                    "brand": _text(base.get("brand")),
                    "name": _text(base.get("name")),
                    "material": _text(base.get("meterialType")).upper(),
                    "target_temp": _temperature(item, base),
                    "aliases": [],
                    "rfid_codes": [],
                }
            else:
                entry = {
                    "id": _text(item.get("id")).upper(),
                    "brand": _text(item.get("brand")),
                    "name": _text(item.get("name")),
                    "material": _text(item.get("material")).upper(),
                    "target_temp": item.get("target_temp"),
                    "aliases": list(item.get("aliases") or []),
                    "rfid_codes": list(item.get("rfid_codes") or []),
                }
            if not entry["id"] or not entry["material"]:
                continue

            identity = (
                entry["brand"].casefold(),
                entry["name"].casefold(),
                entry["material"],
            )
            existing = identities.get(identity)
            if existing is None:
                entry["rfid_codes"] = _codes(
                    entry["id"], entry["aliases"], entry["rfid_codes"])
                identities[identity] = entry
                continue

            aliases = list(existing.get("aliases") or [])
            if entry["id"] != existing["id"] and entry["id"] not in aliases:
                aliases.append(entry["id"])
            for alias in entry.get("aliases") or []:
                alias = _text(alias).upper()
                if alias and alias != existing["id"] and alias not in aliases:
                    aliases.append(alias)
            existing["aliases"] = aliases
            existing["rfid_codes"] = _codes(
                existing["id"], aliases,
                list(existing.get("rfid_codes") or [])
                + list(entry.get("rfid_codes") or []))

        self._entries = list(identities.values())
        for entry in self._entries:
            for code in _codes(
                    entry["id"], entry.get("aliases") or [],
                    entry.get("rfid_codes") or []):
                self._by_code[code] = entry
        return len(self._entries)

    def lookup(self, raw_code):
        self._refresh_if_changed()
        code = _text(raw_code).upper()
        if not code:
            return None
        candidates = [code]
        if len(code) == 6 and code.startswith("1"):
            candidates.append(code[1:])
        for candidate in candidates:
            entry = self._by_code.get(candidate)
            if entry is not None:
                return dict(entry)
        return None
