# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Klipper integration for CFS boxes, filament state, and nozzle operations.

``Box`` owns discovery, coherent live state, persistence, RFID metadata, and
the public G-code surface, composing change sequencing through ``BoxChangeEngine``.
"""

import hashlib
import json
import logging
import math
import os
import threading
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, replace

from extras import box_protocol
from extras.box_addr import ADDRESS_WEDGE_WARNING, MAX_ADDRESSES, AutoAddressManager
from extras.box_change import BoxChangeEngine
from extras.box_gcode import read_metadata
from extras.box_catalog import resolve_material
from extras.box_k2rfid_catalog import K2RfidMaterialCatalog
from extras.box_lane_data import LaneDataPublisher
from extras.motion_limits import restore_motion_limits, save_motion_limits


SLOTS_PER_BOX = box_protocol.SLOTS_PER_BOX
EXTERNAL_PROFILE_KEY = "external"
# External compatibility contract: Jacob's OrcaSlicer and HelixScreen both
# identify the community flat CFS command dialect from box.api_version == 1.
# Additive K2-OpenHost features must use their own version fields instead of
# incrementing this value.
API_VERSION = 1
FILAMENT_INVENTORY_VERSION = 2
FILAMENT_LIBRARY_VERSION = 1
LIBRARY_REFRESH = 2.0
FILAMENT_SOURCES = ("user", "import", "rfid")
LEGACY_WIDGET_VERSION = 2
SAFE_WIDGET_COMMANDS = frozenset((
    "_BOX_SLOT_SET",
    "_BOX_SLOT_CLEAR",
    "_BOX_MATERIAL_SET",
    "_BOX_FILAMENT_SET",
    "_BOX_FILAMENT_DELETE",
    "_BOX_SLOT_ASSIGN",
    "_BOX_RFID_READ_SLOT",
    "BOX_RFID_SCAN",
    "BOX_INFO_REFRESH",
    "_BOX_SET_RUNOUT_SWAP",
    "_BOX_SET_RUNOUT_ORDER",
    "BOX_ENABLE_AUTO_REFILL",
    "_BOX_SET_UNLOAD_AFTER_PRINT",
    "_BOX_SET_RFID_INSERT_READING",
    "_BOX_SET_RFID_STARTUP_READING",
))

DEFAULT_MATERIALS = {
    "ABS": {"target_temp": 245},
    "ASA": {"target_temp": 245},
    "PLA": {"target_temp": 220},
    "PETG": {"target_temp": 245},
}

POLL_START_DELAY = 5.0
ACTIVE_POLL = 1.0
IDLE_POLL = 5.0
TOPOLOGY_POLL = 15.0
# K2-OpenHost: when no CFS answered the startup enumeration (RS-485 down
# behind the T113 bridge at boot), discovery is retried while idle, backing
# off from REDISCOVERY_MIN to REDISCOVERY_MAX seconds, and at once when
# serial_485 reports the link restored. The original code enumerated once,
# so the CFS stayed missing until a manual RESTART.
REDISCOVERY_MIN = 30.0
REDISCOVERY_MAX = 300.0
RFID_REFRESH = 30.0
# K2-OpenHost: tags written with the generic serial (000000/000001) carry no
# spool identity, so a new spool identical to a used one inherits its saved
# estimate. Below this percentage the tag read prints how to declare it new.
RFID_LOW_ESTIMATE_HINT = 5.0
ERROR_BACKOFF = 10.0
STATE_TIMEOUT = 5.0
STATE_POLL = 0.1
STATE_EVENT_DRAIN = 4
LOAD_TIMEOUT = 45.0
STAGE5_POLL = 0.1
PATH_RETRACT_TIMEOUT = 45.0
BUFFER_RETRACT_TIMEOUT = 7.0

UNLOAD_RETRACT_MM = 25.0
UNLOAD_CLEAR_MIN_MM = 50.0
UNLOAD_RETRY_MM = 25.0
UNLOAD_RETRIES = 3
ENCODER_CLEAR_MM = 20.0

CLOG_EXTRUDER_MM = 80.0
CLOG_ENCODER_RESET_MM = 18.0

CFS_COMMAND_FATAL_STATUSES = frozenset((
    box_protocol.STATUS_STAGE0_SENSOR_TIMEOUT,
    box_protocol.STATUS_SLOT_EMPTY,
    box_protocol.STATUS_STAGE0_ODOMETER_TIMEOUT,
    box_protocol.STATUS_FEED_TIMEOUT,
    box_protocol.STATUS_OVERTRAVEL,
    box_protocol.STATUS_ODOMETER_STALLED,
    box_protocol.STATUS_BUFFER_FILL_TIMEOUT,
    box_protocol.STATUS_BUFFER_NOT_FULL,
    box_protocol.STATUS_UNLOAD_BUFFER_TIMEOUT,
    box_protocol.STATUS_UNLOAD_HUB_PE_TIMEOUT,
    box_protocol.STATUS_UNLOAD_NO_FILAMENT,
    box_protocol.STATUS_UNLOAD_INLET_CLEAR,
    box_protocol.STATUS_UNLOAD_ALL_EMPTY,
    box_protocol.STATUS_UNLOAD_MOTOR_BLOCKED,
    box_protocol.STATUS_UNLOAD_ODOMETER_TIMEOUT,
    box_protocol.STATUS_BUFFER_REFILL_STALLED,
    box_protocol.STATUS_BUFFER_REFILL_NO_MOTION,
))
CFS_ADVISORY_STATUSES = frozenset((
    box_protocol.STATUS_INVALID_PARAM,
    box_protocol.STATUS_BAD_CRC,
    box_protocol.STATUS_BUSY,
    box_protocol.STATUS_STAGE7_NO_MOTION,
))

CLEAN_LIMIT_VELOCITY = 800
CLEAN_LIMIT_ACCEL = 10000
CLEAN_MINIMUM_CRUISE_RATIO = 0.5
CLEAN_LIMIT_SCV = 5
CLEAN_SERPENTINE_Y_STEP = 2.0
CLEAN_SCRAPER_PASSES = 3

SNAP_RETRACT_MM = 1.2

CUT_SAFE_Z = 2.0
CUT_LIMIT_VELOCITY = 800
CUT_LIMIT_ACCEL = 7500
CUT_LIMIT_CRUISE = 1.0 / 3.0
CUT_LIMIT_SCV = 10
CUT_RETURN_WAIT = 3.0
CUT_RETRY_SETTLE = 0.15
CUT_POST_RETRACT_MM = 3.0

SPOOLMAN_PROXY_URL = "http://127.0.0.1:7125/server/spoolman/proxy"
SPOOLMAN_TIMEOUT = 2.0
SPOOLMAN_MAX_BYTES = 8 * 1024 * 1024


def _klog(msg, *args, level=logging.info):
    level("box: " + msg, *args)


class _VirtualSDGCodeObserver:
    def __init__(self, delegate, owner):
        self.delegate = delegate
        self.owner = owner
        self.enabled = True

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def run_script(self, script):
        result = self.delegate.run_script(script)
        if self.enabled:
            try:
                self.owner._observe_sd_line(script)
            except Exception:
                self.enabled = False
                _klog('runout feature observer disabled', level=logging.exception)
        return result


def _spool_id_from_reserve(value):
    value = str(value or "").strip()
    # Some stock spools have 1 in this field, so it can't be used as a spool ID.
    return int(value) if value.isdigit() and int(value) > 1 else None


def _fetch_spoolman_spool(spool_id):
    body = json.dumps({
        "request_method": "GET", "path": "/v1/spool/%d" % spool_id,
        "use_v2_response": True,
    }).encode()
    request = urllib.request.Request(
        SPOOLMAN_PROXY_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=SPOOLMAN_TIMEOUT) as response:
        raw = response.read(SPOOLMAN_MAX_BYTES + 1)
    if len(raw) > SPOOLMAN_MAX_BYTES:
        return None
    result = json.loads(raw.decode()).get("result", {})
    spool = result.get("response") if result.get("error") is None else None
    if not isinstance(spool, dict) or spool.get("archived"):
        return None
    try:
        return spool if int(spool["id"]) == spool_id else None
    except (KeyError, TypeError, ValueError):
        return None


class BoxError(RuntimeError):
    pass


# K2-OpenHost observation mode
class _ReadOnlyCFSProxy:
    """Block mutating CFS functions before they reach serial_485."""

    ALLOWED_FUNCTIONS = frozenset((
        0x02,  # RFID/material records
        0x03,  # remaining
        0x05,  # buffer
        0x08,  # slot/hub mask
        0x0A,  # box state
        0x0E,  # encoder
        0x14,  # version/SN
        0x15,  # hardware status (read-only diagnostics)
        0xF0,  # firmware version
        0xA1,  # discovery
        0xA2,  # online check
        0xA3,  # address table
    ))

    def __init__(self, transport):
        self.transport = transport
        self.allowed_requests = 0
        self.blocked_requests = 0
        self.blocked_functions = []

    def cmd_send_data_with_response(
            self, data, timeout=1.0, attempts=1):
        body = bytes(data)

        if len(body) < 4:
            self.blocked_requests += 1
            raise PermissionError(
                "CFS observation guard: malformed request blocked"
            )

        address = body[0]
        function = body[3]

        if function not in self.ALLOWED_FUNCTIONS:
            self.blocked_requests += 1
            self.blocked_functions.append((address, function))
            raise PermissionError(
                "CFS observation guard: "
                "blocked addr=0x%02X func=0x%02X"
                % (address, function)
            )

        self.allowed_requests += 1
        return self.transport.cmd_send_data_with_response(
            data, timeout, attempts=attempts,
        )


@dataclass(frozen=True)
class BoxSnapshot:
    data_ready: bool = False
    status_code: object = None
    state_code: object = None
    temp_c: object = None
    humidity_pct: object = None
    loaded_slot: object = None
    loaded_mask: int = 0
    slot_mask: int = 0
    tracking: bool = False
    filament_detected: object = None
    filament_sensor_error: object = None
    path_box: object = None
    encoder_mm: object = None
    buffer_status: object = None
    buffer_state: object = None


@dataclass(frozen=True)
class TrackingOwner:
    address: int
    slot: int
    epoch: int


class BoxStore:
    """Small atomic JSON store for profiles, settings, and runtime identity.

    Filament profiles live in three layers merged into one view:

    * system: the shipped read-only catalog, kept in memory only so catalog
      updates apply on the next start;
    * library: the user's custom profiles. With ``library_path`` they are kept
      in their own JSON file (the same compact format as the system catalog),
      which Mainsail, the Moonraker file API or a companion app can read and
      replace; it is reloaded when it changes on disk;
    * runtime state (``path``): slot assignments, RFID estimates, settings and
      box identities, written often and never mixed with the library.

    Without ``library_path`` the custom profiles stay in the state file, as in
    earlier releases.
    """

    def __init__(self, path, library_path=None):
        self.path = path
        self.library_path = library_path or None
        self.system = {}
        self.library_meta = {"imports": {}}
        self.library_error = ""
        self._library_mtime = None
        self._merged = None
        self.data = self._load()
        if self.library_path:
            self._open_library()

    def _load(self):
        if not os.path.exists(self.path):
            return {
                "schema_version": FILAMENT_INVENTORY_VERSION,
                "materials": {name: dict(value) for name, value in DEFAULT_MATERIALS.items()},
                "filaments": {},
                "slots": {},
                "rfid_mappings": {},
                "runtime": {},
                "addresses": {},
            }
        try:
            with open(self.path, "r") as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise BoxError("Unable to read %s: %s" % (self.path, exc))
        if not isinstance(data, dict):
            raise BoxError("%s must contain a JSON object" % self.path)
        sections = {
            name: data.get(name, {})
            for name in ("materials", "filaments", "slots", "rfid_mappings", "runtime", "addresses")
        }
        for name, value in sections.items():
            if not isinstance(value, dict):
                raise BoxError("%s.%s must be an object" % (self.path, name))
        sections["materials"] = self._materials(sections["materials"])
        sections["filaments"] = self._filaments(
            sections["filaments"], strict=False)
        sections["rfid_mappings"] = self._rfid_mappings(
            sections["rfid_mappings"])
        sections["addresses"] = self._addresses(sections["addresses"])
        sections["schema_version"] = FILAMENT_INVENTORY_VERSION
        return sections

    @staticmethod
    def _materials(values):
        result = {}
        for key, value in values.items():
            name = str(key).strip().upper()
            if not name or not isinstance(value, dict):
                raise BoxError("Invalid material entry %r" % key)
            target = value.get("target_temp")
            if target is not None and (
                    isinstance(target, bool) or not isinstance(target, int)):
                raise BoxError("Invalid target temperature for %s" % name)
            result[name] = {"target_temp": target}
        return result

    @staticmethod
    def _clean_pressure_advance(value):
        if value in (None, ""):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if not 0.0 <= value <= 2.0:
            return None
        return round(value, 6)

    @staticmethod
    def _filaments(values, strict=True):
        result = {}
        for key, value in values.items():
            try:
                filament_id, clean = BoxStore._filament_entry(key, value)
            except BoxError as exc:
                if strict:
                    raise
                # A single damaged library entry must not stop Klipper from
                # starting; skip it and keep the rest of the persisted state.
                logging.warning("box: skipping invalid filament %r: %s", key, exc)
                continue
            result[filament_id] = clean
        return result

    @staticmethod
    def _filament_entry(key, value):
        if not isinstance(value, dict):
            raise BoxError("Invalid filament entry %r" % key)
        filament_id = str(value.get("id", key)).strip().upper()
        if not filament_id or len(filament_id) > 64:
            raise BoxError("Invalid filament ID %r" % key)
        material = str(value.get("material", "")).strip().upper()
        if not material:
            raise BoxError("Filament %s has no material" % filament_id)
        color = str(value.get("color", "")).strip().upper()
        if color and (len(color) != 7 or color[0] != "#"
                      or any(c not in "0123456789ABCDEF" for c in color[1:])):
            raise BoxError("Invalid color for filament %s" % filament_id)
        target = value.get("target_temp")
        if target is not None and (isinstance(target, bool)
                                   or not isinstance(target, int)
                                   or not 170 <= target <= 350):
            raise BoxError("Invalid target temperature for filament %s" % filament_id)
        ranges = {}
        for field in ("min_temp", "max_temp"):
            raw = value.get(field)
            if raw in (None, ""):
                ranges[field] = None
                continue
            try:
                raw = int(round(float(raw)))
            except (TypeError, ValueError):
                raise BoxError(
                    "Invalid %s for filament %s" % (field, filament_id))
            if not 0 <= raw <= 500:
                raise BoxError(
                    "Invalid %s for filament %s" % (field, filament_id))
            ranges[field] = raw
        pressure_advance = value.get("pressure_advance")
        if pressure_advance in (None, ""):
            pressure_advance = None
        else:
            try:
                pressure_advance = float(pressure_advance)
            except (TypeError, ValueError):
                raise BoxError(
                    "Invalid pressure_advance for filament %s" % filament_id)
            if not 0.0 <= pressure_advance <= 2.0:
                raise BoxError(
                    "Invalid pressure_advance for filament %s" % filament_id)
            pressure_advance = round(pressure_advance, 6)
        spoolman_id = value.get("spoolman_id")
        if spoolman_id is not None:
            try:
                spoolman_id = int(spoolman_id)
            except (TypeError, ValueError):
                raise BoxError("Invalid Spoolman ID for filament %s" % filament_id)
            if spoolman_id < 0:
                spoolman_id = None
        codes = []
        for raw_code in (
                list(value.get("rfid_codes") or [])
                + [value.get("rfid_code", "")]):
            code = str(raw_code or "").strip().upper()
            if code and code not in codes:
                codes.append(code)
        aliases = []
        for raw_alias in value.get("aliases") or []:
            alias = str(raw_alias or "").strip().upper()
            if alias and alias != filament_id and alias not in aliases:
                aliases.append(alias)
        return filament_id, {
            "id": filament_id,
            "material": material,
            "color": color,
            "brand": str(value.get("brand", "")).strip(),
            "name": str(value.get("name", "")).strip(),
            "target_temp": target,
            "min_temp": ranges["min_temp"],
            "max_temp": ranges["max_temp"],
            "pressure_advance": pressure_advance,
            "rfid_code": codes[0] if codes else "",
            "rfid_codes": codes,
            "aliases": aliases,
            "spoolman_id": spoolman_id,
            "system": bool(value.get("system", False)),
            "source": (
                str(value.get("source", "")).strip().lower()
                if str(value.get("source", "")).strip().lower()
                in FILAMENT_SOURCES else "user"),
        }

    @staticmethod
    def _rfid_mappings(values):
        result = {}
        for key, value in values.items():
            code, _product = resolve_material(key)
            if not code or not isinstance(value, dict):
                raise BoxError("Invalid RFID mapping %r" % key)
            material = str(value.get("material", "")).strip().upper()
            if not material:
                raise BoxError("RFID mapping %s has no material" % code)
            target = value.get("target_temp")
            if target is not None and (
                    isinstance(target, bool) or not isinstance(target, int)):
                raise BoxError("Invalid target temperature for RFID %s" % code)
            result[code] = {
                "material": material,
                "brand": str(value.get("brand", "")).strip(),
                "name": str(value.get("name", "")).strip(),
                "target_temp": target,
            }
        return result

    @staticmethod
    def _addresses(values):
        result = {}
        for key, value in values.items():
            try:
                address = int(key)
                uid = bytes.fromhex(value)
            except (TypeError, ValueError):
                raise BoxError("Invalid box address entry %r" % key)
            if not 1 <= address <= 4 or len(uid) != 12 or not any(uid):
                raise BoxError("Invalid box identity at address %s" % key)
            result[str(address)] = uid.hex()
        return result

    @staticmethod
    def _write_json(path, payload):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)

    def save(self):
        payload = self.data
        if self.library_path:
            payload = {
                key: value for key, value in self.data.items()
                if key != "filaments"}
        self._write_json(self.path, payload)

    # --- filament library file ---------------------------------------------
    def _library_stat(self):
        try:
            return os.path.getmtime(self.library_path)
        except OSError:
            return None

    def _read_library(self):
        """Return (filaments, meta) from the library file; raise BoxError."""
        try:
            with open(self.library_path, "r", encoding="utf-8-sig") as stream:
                text = stream.read()
        except OSError as exc:
            raise BoxError("Unable to read %s: %s" % (self.library_path, exc))
        if not text.strip():
            return {}, {"imports": {}}
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise BoxError("%s is not valid JSON: %s" % (self.library_path, exc))
        if isinstance(payload, list):
            payload = {"materials": payload}
        if not isinstance(payload, dict):
            raise BoxError("%s must contain a JSON object" % self.library_path)
        items = payload.get("materials", [])
        if isinstance(items, dict):
            items = [dict(value, id=value.get("id", key))
                     for key, value in items.items() if isinstance(value, dict)]
        if not isinstance(items, list):
            raise BoxError("%s.materials must be a list" % self.library_path)
        values = {}
        for item in items:
            if isinstance(item, dict) and str(item.get("id", "")).strip():
                values[str(item["id"]).strip().upper()] = dict(
                    item, system=False)
        imports = payload.get("imports")
        meta = {"imports": dict(imports) if isinstance(imports, dict) else {}}
        return self._filaments(values, strict=False), meta

    def _open_library(self):
        """Load the library file, moving custom profiles out of the state file."""
        legacy = {
            key: value for key, value in self.data["filaments"].items()
            if not value.get("system")}
        exists = os.path.exists(self.library_path)
        entries, meta = {}, {"imports": {}}
        if exists:
            try:
                entries, meta = self._read_library()
            except BoxError as exc:
                # Keep Klipper running on a damaged upload; the file is left
                # untouched and every library write is refused until fixed.
                self.library_error = str(exc)
                logging.warning("box: %s", exc)
        migrated = [key for key in legacy if key not in entries]
        for key in migrated:
            entries[key] = legacy[key]
        self.data["filaments"] = entries
        self.library_meta = meta
        self._library_mtime = self._library_stat()
        state_has_profiles = bool(self._state_file_filaments())
        if not self.library_error and (migrated or not exists):
            self.save_library()
        if state_has_profiles and not self.library_error:
            backup = self.path + ".pre-library"
            if not os.path.exists(backup):
                try:
                    with open(self.path, "rb") as source:
                        content = source.read()
                    with open(backup, "wb") as target:
                        target.write(content)
                except OSError:
                    logging.warning("box: unable to back up %s", self.path)
            self.save()

    def _state_file_filaments(self):
        try:
            with open(self.path, "r") as stream:
                return (json.load(stream) or {}).get("filaments") or {}
        except (OSError, ValueError, AttributeError):
            return {}

    def save_library(self):
        if not self.library_path:
            self.save()
            return
        if self.library_error:
            raise BoxError(
                "Filament library %s is damaged (%s); fix or remove it first"
                % (self.library_path, self.library_error))
        materials = []
        for key in sorted(self.data["filaments"]):
            entry = dict(self.data["filaments"][key])
            entry.pop("system", None)
            materials.append(entry)
        self._write_json(self.library_path, {
            "schema_version": FILAMENT_LIBRARY_VERSION,
            "description": (
                "K2-OpenHost CFS filament library: custom profiles only. "
                "IDs are K2-RFID material IDs (tags carry 1 + ID); name matches "
                "the OrcaSlicer preset; spoolman_id links a Spoolman filament. "
                "Edit from Mainsail or replace this file, then run "
                "_BOX_FILAMENT_RELOAD."),
            "imports": dict(self.library_meta.get("imports") or {}),
            "materials": materials,
        })
        self._library_mtime = self._library_stat()
        self._merged = None

    def refresh_library(self, force=False):
        """Reload the library file when it changed on disk. Returns True on reload."""
        if not self.library_path:
            return False
        mtime = self._library_stat()
        if not force and mtime == self._library_mtime:
            return False
        self._library_mtime = mtime
        if mtime is None:
            # Removed on disk: keep the profiles in memory and rewrite it.
            self.library_error = ""
            self.save_library()
            return False
        try:
            entries, meta = self._read_library()
        except BoxError as exc:
            self.library_error = str(exc)
            logging.warning("box: %s", exc)
            return False
        self.library_error = ""
        previous = self.data["filaments"]
        self.data["filaments"] = entries
        self.library_meta = meta
        self._merged = None
        slots_changed = False
        for key, clean in entries.items():
            if previous.get(key) != clean:
                slots_changed |= self._sync_slots(
                    key, clean, (previous.get(key) or {}).get("color", ""))
        for key in previous:
            if key not in entries and key not in self.system:
                slots_changed |= self._detach_slots(key)
        if slots_changed:
            self.save()
        return True

    @property
    def library_status(self):
        return {
            "path": self.library_path or self.path,
            "separate_file": bool(self.library_path),
            "custom_count": len(self.data["filaments"]),
            "system_count": len(self.system),
            "error": self.library_error,
        }

    # --- merged filament view ----------------------------------------------
    def set_system(self, entries):
        """Replace the in-memory system catalog (never persisted)."""
        values = {}
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            key = str(entry["id"]).strip().upper()
            values[key] = dict(entry, system=True)
        self.system = self._filaments(values, strict=False)
        for clean in self.system.values():
            clean["source"] = "system"
        self._merged = None

    def _merged_view(self):
        if self._merged is None:
            merged = dict(self.system)
            for key, value in self.data["filaments"].items():
                if key in self.system and not value.get("system"):
                    # The editor refuses system IDs; keep the catalog entry.
                    continue
                merged[key] = value
            self._merged = merged
        return self._merged

    def _ordered_filaments(self):
        """Custom profiles first, so a user profile wins an RFID/identity match."""
        return [
            value for key, value in self.data["filaments"].items()
            if key not in self.system] + list(self.system.values())

    @property
    def materials(self):
        return {name: dict(value) for name, value in self.data["materials"].items()}

    @property
    def filaments(self):
        return {key: dict(value) for key, value in self._merged_view().items()}

    @property
    def filaments_status(self):
        """Merged view for get_status; rebuilt only when a layer changes."""
        return self._merged_view()

    def filament(self, filament_id):
        key = str(filament_id or "").strip().upper()
        value = self.system.get(key) or self.data["filaments"].get(key)
        return None if value is None else dict(value)

    def _sync_slots(self, key, clean, previous_color=""):
        previous_color = str(previous_color or "").strip().upper()
        changed = False
        for profile in self.data["slots"].values():
            if str(profile.get("filament_id", "")).strip().upper() != key:
                continue
            source = str(profile.get("source", "manual")).strip().lower()
            if source not in ("library", "rfid"):
                continue
            before = dict(profile)
            profile["material"] = clean["material"]
            profile["brand"] = clean["brand"]
            profile["name"] = clean["name"]
            profile["target_temp"] = clean["target_temp"]
            profile["pressure_advance"] = clean.get("pressure_advance")
            if source == "library":
                profile["spoolman_id"] = clean["spoolman_id"]
                profile["rfid_code"] = clean["rfid_code"]
                current_color = str(profile.get("color", "")).strip().upper()
                if not current_color or current_color == previous_color:
                    profile["color"] = clean["color"]
            changed |= profile != before
        return changed

    def _detach_slots(self, key):
        changed = False
        for profile in self.data["slots"].values():
            if str(profile.get("filament_id", "")).strip().upper() == key:
                profile.pop("filament_id", None)
                if profile.get("source") == "library":
                    profile["source"] = "manual"
                changed = True
        return changed

    def set_filament(self, filament_id, value, save=True):
        key = str(filament_id or "").strip().upper()
        clean = self._filaments({key: dict(value, id=key)})[key]
        if clean["system"]:
            clean["source"] = "system"
            previous = self.system.get(key)
            self.system[key] = clean
        else:
            if save:
                self.refresh_library()
            previous = self.data["filaments"].get(key)
            self.data["filaments"][key] = clean
        self._merged = None
        slots_changed = self._sync_slots(
            key, clean, (previous or {}).get("color", ""))
        if save:
            if not clean["system"]:
                # Without a library file this writes the state file itself.
                self.save_library()
                if self.library_path and slots_changed:
                    self.save()
            elif slots_changed:
                self.save()
        return dict(clean)

    def delete_filament(self, filament_id):
        key = str(filament_id or "").strip().upper()
        if key in self.system:
            return False
        self.refresh_library()
        current = self.data["filaments"].get(key)
        if current is None or current.get("system"):
            return False
        self.data["filaments"].pop(key, None)
        self._merged = None
        slots_changed = self._detach_slots(key)
        self.save_library()
        if self.library_path and slots_changed:
            self.save()
        return True

    def filament_for_rfid(self, raw_code):
        raw = str(raw_code or "").strip().upper()
        normalized, _product = resolve_material(raw)
        candidates = {value for value in (raw, normalized) if value}
        if len(raw) == 6 and raw.startswith("1"):
            candidates.add(raw[1:])
        for filament in self._ordered_filaments():
            codes = set(str(value or "").strip().upper() for value in (
                list(filament.get("rfid_codes") or [])
                + [filament.get("rfid_code", ""), filament.get("id", "")]
                + list(filament.get("aliases") or [])))
            if candidates.intersection(code for code in codes if code):
                return dict(filament)
        return None

    def filament_by_identity(self, brand, name, material):
        identity = (
            str(brand or "").strip().casefold(),
            str(name or "").strip().casefold(),
            str(material or "").strip().upper(),
        )
        for filament in self._ordered_filaments():
            candidate = (
                str(filament.get("brand", "")).strip().casefold(),
                str(filament.get("name", "")).strip().casefold(),
                str(filament.get("material", "")).strip().upper(),
            )
            if candidate == identity:
                return dict(filament)
        return None

    def set_material(self, name, target):
        key = str(name).strip().upper()
        if not key:
            raise ValueError("material is required")
        self.data["materials"][key] = {"target_temp": int(target)}
        self.save()
        return key

    def profile(self, slot):
        value = self.data["slots"].get(str(slot), {})
        target_temp = value.get("target_temp")
        try:
            target_temp = None if target_temp is None else int(target_temp)
        except (TypeError, ValueError):
            target_temp = None
        return {
            "material": str(value.get("material", "")).strip().upper(),
            "color": str(value.get("color", "")).strip().upper(),
            "brand": str(value.get("brand", "")).strip(),
            "name": str(value.get("name", "")).strip(),
            "target_temp": target_temp,
            "pressure_advance": self._clean_pressure_advance(
                value.get("pressure_advance")),
            "spoolman_id": value.get("spoolman_id"),
            "filament_id": str(value.get("filament_id", "")).strip().upper(),
            "source": str(value.get("source", "manual")).strip().lower() or "manual",
            "rfid_code": str(value.get("rfid_code", "")).strip().upper(),
            "rfid_reserve": str(value.get(
                "rfid_reserve", "")).strip().strip("\x00").strip(),
        }

    def set_profile(self, slot, profile):
        target_temp = profile.get("target_temp")
        if target_temp is not None:
            target_temp = int(target_temp)
            if not 170 <= target_temp <= 350:
                raise ValueError("target_temp must be 170..350")
        clean = {
            "material": str(profile.get("material", "")).strip().upper(),
            "color": str(profile.get("color", "")).strip().upper(),
            "brand": str(profile.get("brand", "")).strip(),
            "name": str(profile.get("name", "")).strip(),
            "target_temp": target_temp,
            "pressure_advance": self._clean_pressure_advance(
                profile.get("pressure_advance")),
            "spoolman_id": profile.get("spoolman_id"),
            "filament_id": str(profile.get("filament_id", "")).strip().upper(),
            "source": str(profile.get("source", "manual")).strip().lower() or "manual",
            "rfid_code": str(profile.get("rfid_code", "")).strip().upper(),
        }
        reserve = str(profile.get(
            "rfid_reserve", "")).strip().strip("\x00").strip()
        if reserve:
            clean["rfid_reserve"] = reserve
        self.data["slots"][str(slot)] = clean
        self.save()

    def clear_profile(self, slot):
        if self.data["slots"].pop(str(slot), None) is not None:
            self.save()

    def setting(self, name, default=None):
        return self.data["runtime"].get(name, default)

    def set_setting(self, name, value):
        self.data["runtime"][name] = value
        self.save()

    def clear_settings(self, *names):
        changed = False
        for name in names:
            changed |= self.data["runtime"].pop(name, None) is not None
        if changed:
            self.save()

    def rfid_mapping(self, code):
        normalized, _product = resolve_material(code)
        value = self.data["rfid_mappings"].get(normalized)
        return None if value is None else dict(value)

    def set_rfid_mapping(self, code, value):
        normalized, _product = resolve_material(code)
        if not normalized:
            raise ValueError("RFID code is required")
        clean = self._rfid_mappings({normalized: value})
        self.data["rfid_mappings"][normalized] = clean[normalized]
        self.save()
        return normalized

    def delete_rfid_mapping(self, code):
        normalized, _product = resolve_material(code)
        if self.data["rfid_mappings"].pop(normalized, None) is not None:
            self.save()
        return normalized

    @property
    def known_addresses(self):
        return {int(address): bytes.fromhex(uid) for address, uid in self.data["addresses"].items()}

    def set_known_addresses(self, mapping):
        value = {
            str(int(address)): bytes(uid).hex() for address, uid in mapping.items()
        }
        if value != self.data["addresses"]:
            self.data["addresses"] = value
            self.save()


class Box:
    CONSOLE_PREFIX = "[BOX]: "

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")

        # K2-OpenHost observation mode
        self.observation_mode = config.getboolean(
            "observation_mode", False)

        self.pause_resume = self.printer.load_object(
            config, "pause_resume")

        self.box_count = config.getint(
            "box_count", MAX_ADDRESSES,
            minval=1, maxval=MAX_ADDRESSES)

        persistent_root = (
            "/mnt/UDISK/printer_data"
            if os.path.isdir("/mnt/UDISK/printer_data")
            else os.path.expanduser("~/printer_data")
        )
        default_state_path = (
            "/dev/shm/k2-openhost-filament_box.json"
            if self.observation_mode
            else os.path.join(persistent_root, "filament_box.json")
        )

        default_library_path = (
            "" if self.observation_mode
            else os.path.join(persistent_root, "config", "cfs_filaments.json"))
        self.store = BoxStore(
            os.path.expanduser(config.get("state_path", default_state_path)),
            os.path.expanduser(
                config.get("library_path", default_library_path) or ""),
        )
        self.last_library_refresh = 0.0
        repo_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.realpath(__file__))))
        default_system_catalog = os.path.join(
            repo_root, "config", "k2", "cfs_system_filaments.json")
        self.system_material_database_path = os.path.expanduser(
            config.get(
                "system_material_database_path",
                default_system_catalog))
        self.material_database_path = os.path.expanduser(
            config.get("material_database_path", ""))
        self.auto_register_rfid_filaments = config.getboolean(
            "auto_register_rfid_filaments", True)
        self.auto_seed_material_database = config.getboolean(
            "auto_seed_material_database", True)
        # CFS slots in Moonraker's lane_data namespace for OrcaSlicer "Sync".
        self.lane_data = (
            LaneDataPublisher()
            if config.getboolean("publish_lane_data", True) else None)
        self.system_material_catalog = K2RfidMaterialCatalog(
            self.system_material_database_path)
        self.material_catalog = K2RfidMaterialCatalog(
            self.material_database_path)

        self.clean_pad_left_x = config.getfloat("clean_pad_left_x", 154.0)
        self.clean_pad_right_x = config.getfloat("clean_pad_right_x", 166.0)
        self.clean_pad_front_y = config.getfloat("clean_pad_front_y", 367.0)
        self.clean_pad_back_y = config.getfloat("clean_pad_back_y", 378.0)
        self.clean_pad_passes = config.getint(
            "clean_pad_passes", 1, minval=1)
        for legacy_name in (
                "clean_left_pos_x", "clean_right_pos_x", "clean_right_pos_y"):
            config.get(legacy_name, None)
        if (self.clean_pad_left_x >= self.clean_pad_right_x
                or self.clean_pad_front_y >= self.clean_pad_back_y):
            raise config.error("Invalid clean pad boundaries")
        self.wastebin_x = config.getfloat("wastebin_pos_x", 133.0)
        self.wastebin_y = config.getfloat("wastebin_pos_y", 378.0)
        self.travel_velocity = config.getfloat(
            "travel_velocity", 18000.0, above=0.0)
        self.z_velocity = config.getfloat(
            "z_velocity", 600.0, above=0.0)
        self.clean_velocity = config.getfloat(
            "clean_velocity", 12000.0, above=0.0)
        self.snap_fan_speed = config.getfloat(
            "snap_fan_speed", 1.0, minval=0.0, maxval=1.0)
        self.snap_fan_dwell_ms = config.getint(
            "snap_fan_dwell_ms", 2000, minval=0)
        self.pre_cut_x = config.getfloat("pre_cut_pos_x", 10.0)
        self.cut_x = config.getfloat("cut_pos_x", None)
        self.cut_y = config.getfloat("cut_pos_y", 200.0)
        self.cut_velocity = config.getfloat(
            "cut_velocity", 30000.0, above=0.0)
        self.retract_velocity = config.getfloat(
            "retract_velocity", 3000.0, above=0.0)
        self.external_feed_velocity = config.getfloat(
            "external_feed_velocity", 600.0, above=0.0)
        self.pre_cut_cal_x = config.getfloat("pre_cut_cal_pos_x", -5.0)
        self.cut_check_max_x = config.getfloat("check_cut_pos_x_max", -5.5)
        self.cut_check_min_x = config.getfloat("check_cut_pos_x_min", -9.5)

        self.serial = None
        self.drivers = {}
        self.address_errors = ()
        self.drivers_ready = False
        self.snapshot = BoxSnapshot(loaded_slot=None)
        self.operation_depth = 0
        # K2-OpenHost: live load/unload stage for the UI while the poll
        # timer is paused by an operation.
        self.operation_progress = None
        self.tracking_epoch = 0
        self.tracking_owner = None
        self.path_owner = None
        self._clear_runout_state()
        self.runout_defer_active = False
        self._kalico_runout_taken = False
        self.runout_feature = None
        self.fault_generation = 0
        self.last_fatal_reason = None
        self.fault_episodes = {}
        self.rfid_percent = {}
        self.rfid_reported_percent = {}
        self.rfid_spools = {}
        self.rfid_last_filament_used = None
        self.rfid_last_print_state = None
        self.rfid_last_usage_slot = None
        self.rfid_estimate_dirty = False
        self.last_rfid_estimate_save = 0.0
        self.unknown_rfid = {}
        self.rfid_presence = {}
        self.rfid_absent_confirm = {}
        self.rfid_pending = set()
        self.rfid_snapshot = {}
        self.rfid_seen_invalid = set()
        self.rfid_live_slots = set()
        self.box_replies = {}
        self.last_rfid_refresh = 0.0
        self.last_topology_refresh = 0.0
        self.spoolman_generation = 0
        self.spoolman_tokens = {}
        self.clog_event_count = 0
        self.clog_baseline = None
        self.last_clog = {"extruder_mm": None, "encoder_mm": None}

        self.cut_sensor_state = False

        if not self.observation_mode:
            pins = self.printer.lookup_object("pins")
            pins.allow_multi_use_pin("nozzle_mcu:PB9")
            buttons = self.printer.load_object(config, "buttons")
            buttons.register_buttons(
                ["!nozzle_mcu:PB9"],
                self._cut_sensor_callback
            )

        self.address_manager = AutoAddressManager(
            self.box_count,
            self.store.known_addresses
        )

        self.change_engine = BoxChangeEngine(self, config)
        self._seed_material_catalog(
            import_database=self.auto_seed_material_database)

        self.poll_timer = self.reactor.register_timer(self._poll)
        self.rediscovery_timer = self.reactor.register_timer(self._rediscover)
        self.rediscovery_delay = REDISCOVERY_MIN
        self.rediscovery_attempts = 0
        self.enumeration_started = False
        self.klippy_ready = False
        self.registered_tools = set()
        self.print_info = None
        self._register_commands()
        self.printer.register_event_handler("serial_485:ready", self._serial_ready)
        self.printer.register_event_handler(
            "serial_485:link_restored", self._link_restored)
        self.printer.register_event_handler("klippy:ready", self._klippy_ready)
        self.printer.register_event_handler("klippy:disconnect", self._disconnect)
        self.printer.register_event_handler("klippy:shutdown", self._disconnect)
        self.printer.register_event_handler(
            "filament:runout", self._handle_filament_runout)
        self.printer.register_event_handler(
            "external_rfid_reader:record", self._external_rfid_record)
        for event in (
                "print_stats:complete_printing",
                "print_stats:error_printing",
                "print_stats:cancelled_printing",
                "print_stats:reset",
                "virtual_sdcard:load_file",
                "virtual_sdcard:reset_file"):
            self.printer.register_event_handler(
                event, self.change_engine.reset_print_recovery)
            self.printer.register_event_handler(
                event, self._reset_runout_defer)

    # ------------------------------------------------------------------
    # Lifecycle and public status
    # ------------------------------------------------------------------

    def _register_commands(self):
        # K2-OpenHost observation mode:
        # no user-facing operational commands are registered.
        if self.observation_mode:
            return

        commands = (
            ("BOX_PRINT_INFO", self.cmd_print_info, "Inspect tools used by a print"),
            ("BOX_PRINT_START", self.cmd_print_start, "Start a print with a tool map"),
            ("BOX_SELECT_SLOT", self.cmd_select_slot, "Select a physical filament slot"),
            ("BOX_LOAD", self.cmd_load, "Load filament from a CFS slot"),
            ("BOX_UNLOAD", self.cmd_unload, "Fully unload the active filament"),
            ("BOX_DEBUG", self.cmd_debug, "Show complete box diagnostics"),
            ("BOX_BUFFER_RETRACT", self.cmd_buffer_retract,
             "Run the CFS buffer retract phase"),
            ("BOX_CUT", self.cmd_cut, "Cut the active filament"),
            ("NOZZLE_CLEAN", self.cmd_nozzle_clean, "Clean the nozzle"),
            ("BOX_NOZZLE_CLEAN", self.cmd_nozzle_clean,
             "HelixScreen-compatible nozzle clean alias"),
            ("BOX_SAVE_FAN", self.cmd_helix_noop,
             "HelixScreen K2 compatibility envelope"),
            ("BOX_RESTORE_FAN", self.cmd_helix_noop,
             "HelixScreen K2 compatibility envelope"),
            ("BOX_GO_TO_EXTRUDE_POS", self.cmd_helix_noop,
             "HelixScreen K2 compatibility envelope"),
            ("BOX_MOVE_TO_SAFE_POS", self.cmd_helix_noop,
             "HelixScreen K2 compatibility envelope"),
            ("BOX_MODE_WAIT", self.cmd_helix_noop,
             "HelixScreen K2 compatibility envelope"),
            ("CR_BOX_PRE_OPT", self.cmd_helix_noop,
             "HelixScreen K2 compatibility pre-operation"),
            ("CR_BOX_CUT", self.cmd_helix_noop,
             "HelixScreen K2 compatibility cut stage"),
            ("CR_BOX_RETRUDE", self.cmd_helix_retrude,
             "HelixScreen K2 compatibility unload stage"),
            ("CR_BOX_EXTRUDE", self.cmd_helix_extrude,
             "HelixScreen K2 compatibility load stage"),
            ("CR_BOX_WASTE", self.cmd_helix_noop,
             "HelixScreen K2 compatibility purge stage"),
            ("CR_BOX_FLUSH", self.cmd_helix_noop,
             "HelixScreen K2 compatibility flush stage"),
            ("CR_BOX_END_OPT", self.cmd_helix_noop,
             "HelixScreen K2 compatibility end-operation"),
            ("BOX_GO_TO_WASTEBIN", self.cmd_wastebin, "Move to the wastebin"),
            ("PARSE_FLUSH_VOLUMES", self.change_engine.parse_flush_volumes,
             "Parse slicer flush metadata"),
            ("BOX_RUNOUT_CHECK", self.cmd_runout,
             "Handle CFS runout"),
            ("_BOX_PAUSE_CAPTURE", self.change_engine.capture_pause,
             "Capture the temperature to resume this pause at"),
            ("_BOX_RESUME_PREPARE", self.change_engine.prepare_resume,
             "Recover Box state, then heat and prime at the wastebin"),
            ("_BOX_RESUME_COMMIT",
             self.change_engine.complete_pause_resume,
             "Clear completed pause state"),
            ("_BOX_SLOT_SET", self.cmd_slot_set, "Save slot metadata"),
            ("_BOX_SLOT_CLEAR", self.cmd_slot_clear, "Clear slot metadata"),
            ("_BOX_MATERIAL_SET", self.cmd_material_set, "Save material metadata"),
            ("_BOX_FILAMENT_SET", self.cmd_filament_set, "Save a reusable filament profile"),
            ("_BOX_FILAMENT_DELETE", self.cmd_filament_delete, "Delete a reusable filament profile"),
            ("_BOX_FILAMENT_RELOAD", self.cmd_filament_reload,
             "Reload the filament library file and the K2-RFID import"),
            ("_BOX_SLOT_ASSIGN", self.cmd_slot_assign, "Assign a saved filament profile to a slot"),
            ("_BOX_RFID_READ_SLOT", self.cmd_rfid_read_slot, "Force an RFID reread for one CFS slot"),
            ("_BOX_RFID_SPOOL_NEW", self.cmd_rfid_spool_new,
             "Declare the RFID spool in a CFS slot new (resets its remaining estimate)"),
            ("BOX_RFID_SCAN", self.cmd_info_refresh,
             "Scan RFID records in all populated CFS slots"),
            ("BOX_INFO_REFRESH", self.cmd_info_refresh,
             "HelixScreen/Creality-compatible RFID refresh"),
            ("BOX_MODIFY_TN", self.cmd_modify_tn,
             "HelixScreen/Creality-compatible tool-to-slot mapping"),
            ("BOX_MODIFY_TN_DATA", self.cmd_modify_tn_data,
             "HelixScreen/Creality-compatible slot metadata update"),
            ("_BOX_SET_RUNOUT_SWAP", self.cmd_runout_swap,
             "Set automatic runout swapping"),
            ("_BOX_SET_RUNOUT_ORDER", self.cmd_runout_order,
             "Set the manual order of identical spools for runout swap"),
            ("BOX_ENABLE_AUTO_REFILL", self.cmd_runout_swap,
             "HelixScreen/Creality-compatible runout swap setter"),
            ("_BOX_SET_UNLOAD_AFTER_PRINT", self.cmd_unload_after_print,
             "Set automatic unload after printing"),
            ("_BOX_SET_RFID_INSERT_READING", self.cmd_rfid_insert,
             "Set RFID insertion reads"),
            ("_BOX_SET_RFID_STARTUP_READING", self.cmd_rfid_startup,
             "Set RFID startup reads"),
            ("_BOX_RFID_MAP_SET", self.cmd_rfid_map_set, "Save an RFID mapping"),
            ("_BOX_RFID_MAP_DELETE", self.cmd_rfid_map_delete,
             "Delete an RFID mapping"),
        )
        for name, handler, description in commands:
            if name in SAFE_WIDGET_COMMANDS:
                handler = self._guard_widget_command(name, handler)
            self.gcode.register_command(name, handler, desc=description)

    def _guard_widget_command(self, name, handler):
        def guarded(gcmd):
            try:
                return handler(gcmd)
            except self.gcode.error:
                raise
            except Exception as exc:
                raise gcmd.error(
                    "[BOX]: " + box_protocol.format_failed(name, exc))
        return guarded

    def cmd_runout(self, gcmd):
        self.cancel_runout_defer()
        return self.change_engine.runout(gcmd)

    def _serial_ready(self, *args):
        if self.enumeration_started:
            return
        self.enumeration_started = True
        self.reactor.register_callback(self._enumerate)

    def _enumerate(self, eventtime, retry=False):
        self._invalidate_tracking_session()
        base_serial = self.printer.lookup_object(
            "serial_485 serial485"
        )

        self.serial = (
            _ReadOnlyCFSProxy(base_serial)
            if self.observation_mode
            else base_serial
        )

        client = box_protocol.AutoAddressClient(self.serial)
        reactor = self.reactor
        result = self.address_manager.enumerate(
            client,
            pause=lambda delay: reactor.pause(reactor.monotonic() + delay))
        self.address_errors = tuple(result.errors)
        if ADDRESS_WEDGE_WARNING in self.address_errors:
            self._warn(ADDRESS_WEDGE_WARNING)
        self.store.set_known_addresses(result.known)
        self.drivers = {
            address: box_protocol.BoxDriver(self.serial, address)
            for address in sorted(result.online)
        }
        self.drivers_ready = True
        self._initialize_rfid()
        self._register_t_commands()
        self.printer.send_event("box:ready")
        if self.klippy_ready:
            self.reactor.update_timer(
                self.poll_timer, self.reactor.monotonic() + POLL_START_DELAY)
        if not self.drivers and not retry:
            _klog("no CFS answered the startup enumeration; retrying discovery "
                  "while idle", level=logging.warning)
            self.reactor.update_timer(
                self.rediscovery_timer,
                self.reactor.monotonic() + self.rediscovery_delay)

    # K2-OpenHost: bounded rediscovery when the bus was down at startup.
    def _discovery_idle(self):
        if not self.klippy_ready or self.operation_depth:
            return False
        if self.printer.is_shutdown():
            return False
        print_stats = self.printer.lookup_object("print_stats", None)
        state = getattr(print_stats, "state", None)
        return state not in ("printing", "paused")

    def _rediscover(self, eventtime):
        if self.drivers:
            return self.reactor.NEVER
        if not self._discovery_idle():
            return eventtime + REDISCOVERY_MIN
        self.rediscovery_attempts += 1
        _klog("CFS rediscovery attempt %d", self.rediscovery_attempts)
        try:
            self._enumerate(eventtime, retry=True)
        except Exception:
            _klog("CFS rediscovery failed", level=logging.exception)
        if self.drivers:
            self.rediscovery_delay = REDISCOVERY_MIN
            self._info(
                self.gcode,
                "CFS found after the RS-485 link came back: box %s"
                % ", ".join(str(a) for a in sorted(self.drivers)))
            return self.reactor.NEVER
        self.rediscovery_delay = min(REDISCOVERY_MAX, self.rediscovery_delay * 2)
        return self.reactor.monotonic() + self.rediscovery_delay

    def _link_restored(self, *args):
        if self.drivers_ready and not self.drivers:
            self.rediscovery_delay = REDISCOVERY_MIN
            self.reactor.update_timer(
                self.rediscovery_timer, self.reactor.monotonic() + 1.0)

    def _register_t_commands(self):
        if self.observation_mode:
            return
        self._register_tools(self.physical_slots + (self.external_slot,))

    def _register_tools(self, tools):
        if self.observation_mode:
            return
        for tool in tools:
            if tool in self.registered_tools:
                continue
            self.gcode.register_command(
                "T%d" % tool,
                lambda gcmd, tool=tool: self.change_engine.select_tool(gcmd, tool),
                desc="Select tool T%d" % tool,
            )
            self.registered_tools.add(tool)

    def _klippy_ready(self, *args):
        self.klippy_ready = True
        lane_data = getattr(self, "lane_data", None)
        if lane_data is not None:
            lane_data.start()

        if not self.observation_mode:
            self.reactor.register_callback(
                self._install_runout_source_observer
            )

        if self.drivers_ready:
            self.reactor.update_timer(
                self.poll_timer, self.reactor.monotonic() + POLL_START_DELAY)

    def _install_runout_source_observer(self, eventtime):
        helper = getattr(self._filament_sensor(), "runout_helper", None)
        original_handler = getattr(helper, "_runout_event_handler", None)
        if original_handler is not None:
            def handler(eventtime):
                if not self._kalico_runout_taken:
                    return original_handler(eventtime)

            helper._runout_event_handler = handler
        sd = self.printer.lookup_object("virtual_sdcard", None)
        if sd is None or isinstance(sd.gcode, _VirtualSDGCodeObserver):
            return
        sd.gcode = _VirtualSDGCodeObserver(sd.gcode, self)

    def _handle_filament_runout(self, _eventtime, sensor):
        if sensor != "filament_sensor":
            return
        sd = self.printer.lookup_object("virtual_sdcard", None)
        try:
            armed = (sd is not None and sd.is_active()
                     and self.filament_sensor_enabled())
            self._kalico_runout_taken = False
            if armed:
                self.runout_defer_active = True
            self._info(
                self.gcode,
                "Filament sensor runout event received; %s" % (
                    "watching for infill" if armed
                    else "infill watch not armed"))
        except Exception:
            _klog('runout defer arm failed', level=logging.exception)

    def _observe_sd_line(self, line):
        marker = line.lstrip()
        if marker.upper().startswith(";TYPE:"):
            self.runout_feature = marker.split(":", 1)[1].strip().lower()
        if (self.runout_defer_active
                and "infill" in (self.runout_feature or "")):
            _klog(
                "runout defer dispatching BOX_RUNOUT_CHECK at feature=%s",
                self.runout_feature)
            self.gcode.run_script("BOX_RUNOUT_CHECK")

    def cancel_runout_defer(self):
        self.runout_defer_active = False
        self._kalico_runout_taken = True
        try:
            helper = self._filament_sensor().runout_helper
            helper.reset_runout_distance_info()
            if helper.min_event_systime == self.reactor.NEVER:
                helper.min_event_systime = (
                    self.reactor.monotonic() + helper.event_delay)
        except Exception:
            _klog('runout distance cleanup failed', level=logging.exception)

    def _reset_runout_defer(self, *args):
        self.cancel_runout_defer()
        self.runout_feature = None

    def _disconnect(self, *args):
        self.change_engine.reset_print_mapping()
        self._invalidate_tracking_session()
        self.spoolman_generation += 1
        self.spoolman_tokens.clear()
        self.serial = None
        self.reactor.update_timer(self.poll_timer, self.reactor.NEVER)
        self.reactor.update_timer(self.rediscovery_timer, self.reactor.NEVER)

    def get_status(self, eventtime):
        snap = self.snapshot
        physical = self._slot_statuses(snap)
        slots = physical + [self._external_status(snap)]
        status = {
            "api_version": API_VERSION,
            "filament_inventory_version": FILAMENT_INVENTORY_VERSION,
            "print_mapping_version": 1,
            "print_info": self.print_info,
            "print_mapping": self.change_engine.mapping_status(),
            "fluidd_widget_version": LEGACY_WIDGET_VERSION,
            "data_ready": snap.data_ready,
            "status": box_protocol.status_name(snap.status_code),
            "status_code": snap.status_code,
            "state": box_protocol.state_name(snap.state_code),
            "state_code": snap.state_code,
            "temp_c": snap.temp_c,
            "humidity_pct": snap.humidity_pct,
            "loaded_slot": snap.loaded_slot,
            "loaded_mask": snap.loaded_mask,
            "slot_filament_mask": snap.slot_mask,
            "slots": slots,
            "boxes": self._box_unit_statuses(),
            "operation": self._operation_status(),
            "materials": self.store.materials,
            "filaments": self.store.filaments_status,
            "filament_library": self.store.library_status,
            "runout": self._runout_status(physical, snap),
            "runout_groups": self._runout_groups(physical),
            "runout_swap_enabled": self.runout_swap_enabled,
            "runout_order": self.runout_order,
            "unload_after_print_enabled": self.unload_after_print_enabled,
            "rfid_insert_reading_enabled": self.rfid_insert_reading_enabled,
            "rfid_startup_reading_enabled": self.rfid_startup_reading_enabled,
            "tracking_active": snap.tracking,
            "filament_detected": snap.filament_detected,
            "filament_sensor_error": snap.filament_sensor_error,
            "load_path": self._load_path_status(snap),
            "recovery": self.change_engine.recovery_status(),
            "driver_ready": self.drivers_ready,
        }
        status.update(self._helix_compat_status(physical, snap))
        return status

    def _helix_compat_status(self, physical, snap):
        """Expose stock K2-style box fields consumed by upstream HelixScreen.

        K2-OpenHost keeps its richer flat slots API, but HelixScreen currently
        parses the stock nested T1..T4 unit objects. Publishing both shapes is
        additive and avoids maintaining a separate HelixScreen fork.
        """
        by_slot = {item["index"]: item for item in physical}
        mapping = self._helix_tool_map()
        compat_map = {}
        same_groups = {}
        units = {}

        for slot in self.physical_slots:
            source = self._slot_to_tnn(slot)
            target = self._slot_to_tnn(mapping.get(slot, slot))
            if source and target:
                compat_map[source] = target

        for address in sorted(self.drivers):
            colors = []
            materials = []
            remain = []
            vendors = []
            for local in range(SLOTS_PER_BOX):
                slot = self._global_slot(address, local)
                item = by_slot.get(slot, {})
                present = bool(item.get("present"))

                color = str(item.get("color") or "").strip().upper()
                if present and len(color) == 7 and color.startswith("#"):
                    legacy_color = "0" + color[1:]
                elif present:
                    legacy_color = "unknown"
                else:
                    legacy_color = "-1"
                colors.append(legacy_color)

                raw_code = str(item.get("rfid_code") or "").strip().upper()
                filament_id = str(item.get("filament_id") or "").strip().upper()
                material = str(item.get("material") or "").strip().upper()
                if not present:
                    legacy_material = "-1"
                elif raw_code:
                    legacy_material = raw_code
                elif filament_id:
                    legacy_material = ("1" + filament_id
                                       if len(filament_id) == 5 else filament_id)
                elif material:
                    legacy_material = "K2O%02d" % slot
                else:
                    legacy_material = "unknown"
                materials.append(legacy_material)

                if present and material and legacy_material not in ("-1", "unknown"):
                    tnn = self._slot_to_tnn(slot)
                    group = same_groups.setdefault(
                        legacy_material,
                        {"material": material, "slots": [], "color": legacy_color})
                    if tnn:
                        group["slots"].append(tnn)

                remaining = item.get("rfid_remaining_m")
                remain.append(
                    "-1" if remaining is None
                    else ("%.3f" % float(remaining)).rstrip("0").rstrip("."))
                vendor = str(item.get("brand") or "").strip()
                vendors.append(vendor if vendor else "unknown")

            active = "None"
            if self.is_physical_slot(snap.loaded_slot):
                active_address, active_local = self._address_slot(snap.loaded_slot)
                if active_address == address:
                    active = chr(ord("A") + active_local)

            known_uid = self.store.known_addresses.get(address)
            units["T%d" % address] = {
                "state": str(snap.state_code if snap.state_code is not None else 0),
                "version": "K2-OpenHost",
                "sn": self._public_unit_id(known_uid),
                "temperature": ("None" if snap.temp_c is None else str(snap.temp_c)),
                "dry_and_humidity": (
                    "None" if snap.humidity_pct is None else str(snap.humidity_pct)),
                "color_value": colors,
                "material_type": materials,
                "remain_len": remain,
                "vender": vendors,
                "filament": active,
            }

        same_material = [
            [code, group["color"], group["slots"], group["material"]]
            for code, group in sorted(same_groups.items())
        ]
        result = {
            "helix_compat_version": 1,
            "auto_refill": 1 if self.runout_swap_enabled else 0,
            "filament_useup": 1 if self.runout_active else 0,
            "filament": snap.loaded_slot,
            "map": compat_map,
            "same_material": same_material,
        }
        result.update(units)
        return result

    @staticmethod
    def _public_unit_id(uid):
        """Stable per-unit identifier that does not expose the CFS UniID."""
        if not uid:
            return "-1"
        return hashlib.sha256(bytes(uid)).hexdigest()[:12]

    def _slot_statuses(self, snap):
        return [self._slot_status(slot, snap) for slot in self.physical_slots]

    def _box_unit_statuses(self):
        """K2-OpenHost: one entry per online CFS for multi-unit frontends.

        The top-level temp_c/humidity_pct follow the box that owns the load
        path; this list carries every unit's own environment and slot range.
        """
        units = []
        for address in sorted(self.drivers):
            reply = self.box_replies.get(address)
            units.append({
                "address": address,
                "online": reply is not None,
                "status_code": None if reply is None else reply.status,
                "state_code": None if reply is None else reply.box_state,
                "temp_c": None if reply is None else reply.temp_c,
                "humidity_pct": None if reply is None else reply.humidity_pct,
                "slots": [self._global_slot(address, local)
                          for local in range(SLOTS_PER_BOX)],
            })
        return units

    def _external_status(self, snap):
        return self._slot_status(self.external_slot, snap, external=True)

    def _slot_status(self, slot, snap, external=False):
        profile = self.profile(slot)
        slot_key = self._runtime_slot_key(slot)
        unknown = self.unknown_rfid.get(slot_key)
        unknown_fields = unknown.get("fields", {}) if unknown else {}
        unknown_color = self._normal_color(unknown_fields.get("color"))
        spool = {} if external else self.rfid_spools.get(slot, {})
        total_mm = spool.get("total_mm")
        remaining_mm = spool.get("remaining_mm")
        rfid_managed = (
            not external
            and (slot in self.rfid_live_slots
                 or (bool(snap.slot_mask & (1 << slot))
                     and profile.get("source") == "rfid"))
        )
        return {
            "index": slot,
            "present": external or bool(snap.slot_mask & (1 << slot)),
            "loaded": snap.loaded_slot == slot or (
                not external and bool(snap.loaded_mask & (1 << slot))),
            "profile_clearable": external or not rfid_managed,
            "material": profile["material"],
            "color": profile["color"],
            "brand": profile["brand"],
            "name": profile["name"],
            "target_temp": profile.get("target_temp"),
            "pressure_advance": profile.get("pressure_advance"),
            "spoolman_id": profile["spoolman_id"],
            "filament_id": profile.get("filament_id", ""),
            "source": profile.get("source", "manual"),
            "rfid_code": profile.get("rfid_code", ""),
            "rfid_active": False if external else slot in self.rfid_live_slots,
            "rfid_unknown_code": "" if unknown is None else str(
                unknown.get("raw_code") or unknown.get("code") or "").strip().upper(),
            "rfid_unknown_color": unknown_color or "",
            "rfid_percent": None if external else self.rfid_percent.get(slot),
            "rfid_reported_percent": None if external else self.rfid_reported_percent.get(slot),
            "rfid_estimated_percent": None if external else self.rfid_percent.get(slot),
            "rfid_total_m": None if total_mm is None else round(total_mm / 1000.0, 3),
            "rfid_remaining_m": None if remaining_mm is None else round(remaining_mm / 1000.0, 3),
            "rfid_reserve": profile["rfid_reserve"],
            "runout_rank": None if external else self._runout_rank(slot),
            "external": external,
        }

    def _load_path_status(self, snap):
        clog = self._clog_status()
        return {
            "source_slot": snap.loaded_slot if self.is_valid_slot(snap.loaded_slot) else None,
            "loaded_slot": snap.loaded_slot,
            "loaded_mask": snap.loaded_mask,
            "slot_filament_mask": snap.slot_mask,
            "box_addr": snap.path_box,
            "tracking_active": snap.tracking,
            "encoder": {
                "position_mm": snap.encoder_mm,
                "active": snap.path_box is not None and snap.tracking,
            },
            "buffer": {
                "status_code": snap.buffer_status,
                "state_code": snap.buffer_state,
                "active": snap.buffer_state not in (None, 0),
            },
            "printhead_sensor": {
                "detected": snap.filament_detected,
                "error": snap.filament_sensor_error,
            },
            "clog_detection": clog,
        }

    # ------------------------------------------------------------------
    # Profiles, settings, and public integration seam
    # ------------------------------------------------------------------

    @property
    def physical_slots(self):
        return tuple(
            self._global_slot(address, local)
            for address in sorted(self.drivers)
            for local in range(SLOTS_PER_BOX)
        )

    @property
    def max_physical_slot(self):
        return self.physical_slots[-1] if self.physical_slots else -1

    @property
    def external_slot(self):
        return self.max_physical_slot + 1

    def is_physical_slot(self, slot):
        if not isinstance(slot, int) or slot < 0:
            return False
        address, _local = self._address_slot(slot)
        return address in self.drivers

    def is_valid_slot(self, slot):
        return self.is_physical_slot(slot) or slot == self.external_slot

    def slot_label(self, slot):
        return box_protocol.slot_label(slot, self.external_slot)

    def profile(self, slot):
        return self.store.profile(self._runtime_slot_key(slot))

    def set_profile(self, slot, profile):
        self.store.set_profile(self._runtime_slot_key(slot), profile)

    def clear_profile(self, slot):
        self.store.clear_profile(self._runtime_slot_key(slot))

    def clear_slot_assignment(self, slot):
        """Clear one slot assignment without treating it as a spool removal.

        Physical CFS RFID bays are protected by cmd_slot_clear() while a live
        tag owns the slot. The external spool is different: the standalone
        reader has no removal notification, so replacing an RFID spool with a
        plain spool must be explicitly resettable. Invalidate any asynchronous
        Spoolman lookup and stale unknown-RFID state before clearing the saved
        external profile so an earlier scan cannot repopulate it.
        """
        slot_key = self._runtime_slot_key(slot)
        if slot == self.external_slot:
            self.unknown_rfid.pop(slot_key, None)
            self._invalidate_spoolman(slot)
        self.clear_profile(slot)

    def mark_slot_depleted(self, slot):
        """Persist a confirmed empty spool and clear its live slot assignment.

        RFID remaining estimates are kept by spool identity at 0 mm, so a
        later explicit reread cannot restore already-consumed filament.  The
        bay itself is left unassigned until a new insertion event, explicit
        RFID reread, or manual profile assignment repopulates it.
        """
        if not self.is_physical_slot(slot):
            return
        spool = self.rfid_spools.get(slot)
        if spool and spool.get("total_mm"):
            spool["remaining_mm"] = 0.0
            self.rfid_percent[slot] = 0.0
            self.rfid_estimate_dirty = True
            self._persist_rfid_estimates(force=True)
        self.rfid_live_slots.discard(slot)
        self.rfid_reported_percent.pop(slot, None)
        self.unknown_rfid.pop(self._runtime_slot_key(slot), None)
        self._clear_rfid_slot_key(slot)
        self.clear_profile(slot)

    def _runtime_slot_key(self, slot):
        if slot == EXTERNAL_PROFILE_KEY or slot == self.external_slot:
            return EXTERNAL_PROFILE_KEY
        return int(slot)

    def _runtime_slot(self, value):
        return self.external_slot if value == EXTERNAL_PROFILE_KEY else value

    @property
    def last_loaded_slot(self):
        return self._runtime_slot(self.store.setting("last_loaded_slot"))

    @last_loaded_slot.setter
    def last_loaded_slot(self, slot):
        self.store.set_setting("last_loaded_slot", self._runtime_slot_key(slot))

    def filament_identity(self, slot):
        profile = self.profile(slot)
        return profile["material"], profile["color"]

    def hotend_filament(self):
        value = self.store.setting("hotend_filament")
        if not isinstance(value, dict):
            return None
        temperature = value.get("temperature")
        return {
            "slot": self._runtime_slot(value.get("slot")),
            "material": str(value.get("material", "")).strip().upper(),
            "color": str(value.get("color", "")).strip().upper(),
            "temperature": int(temperature) if temperature is not None else None,
        }

    def set_hotend_filament(self, slot, temperature):
        material, color = self.filament_identity(slot)
        runtime = self.store.data["runtime"]
        runtime["hotend_filament"] = {
            "slot": self._runtime_slot_key(slot),
            "material": material,
            "color": color,
            "temperature": int(temperature),
        }
        runtime.pop("hotend_feed_pending", None)
        self.store.save()

    def mark_hotend_feed_pending(self, slot):
        self.store.set_setting(
            "hotend_feed_pending", self._runtime_slot_key(slot))

    def hotend_feed_pending(self, slot):
        return self.store.setting("hotend_feed_pending") == self._runtime_slot_key(slot)

    def clear_hotend_feed_pending(self, slot=None):
        if slot is None or self.hotend_feed_pending(slot):
            self.store.clear_settings("hotend_feed_pending")

    def _helix_tool_map(self):
        raw = self.store.setting("helix_tool_map", {}) or {}
        if not isinstance(raw, dict):
            return {}
        result = {}
        for tool, slot in raw.items():
            try:
                tool = int(tool)
                slot = int(slot)
            except (TypeError, ValueError):
                continue
            if self.is_physical_slot(tool) and self.is_physical_slot(slot):
                result[tool] = slot
        return result

    @staticmethod
    def _slot_to_tnn(slot):
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return None
        if slot < 0 or slot >= MAX_ADDRESSES * SLOTS_PER_BOX:
            return None
        address = slot // SLOTS_PER_BOX + 1
        local = slot % SLOTS_PER_BOX
        return "T%d%s" % (address, chr(ord("A") + local))

    @staticmethod
    def _tnn_to_slot(value):
        text = str(value or "").strip().upper()
        if len(text) != 3 or text[0] != "T":
            return None
        try:
            address = int(text[1])
        except ValueError:
            return None
        local = ord(text[2]) - ord("A")
        if not 1 <= address <= MAX_ADDRESSES or not 0 <= local < SLOTS_PER_BOX:
            return None
        return (address - 1) * SLOTS_PER_BOX + local

    def _mapped_slot(self, tool):
        return self._helix_tool_map().get(int(tool), int(tool))

    @property
    def runout_swap_enabled(self):
        return bool(self.store.setting("runout_swap_enabled", True))

    @property
    def runout_order(self):
        """Physical slots in the user's runout order (empty: automatic)."""
        store = getattr(self, "store", None)
        order = []
        for value in (store.setting("runout_order", []) if store else []) or []:
            try:
                slot = int(value)
            except (TypeError, ValueError):
                continue
            if (0 <= slot < MAX_ADDRESSES * box_protocol.SLOTS_PER_BOX
                    and slot not in order):
                order.append(slot)
        return order

    def _runout_rank(self, slot):
        order = self.runout_order
        return order.index(slot) if slot in order else None

    def _runout_sort_key(self, item):
        # A manual order wins; otherwise the lowest known RFID remaining is
        # used up first, then slot order.
        rank = self._runout_rank(item["index"])
        return (
            rank is None,
            0 if rank is None else rank,
            item.get("rfid_percent") is None,
            101.0 if item.get("rfid_percent") is None
            else float(item["rfid_percent"]),
            item["index"],
        )

    def _runout_strategy(self, items):
        if any(self._runout_rank(item["index"]) is not None for item in items):
            return "manual_order"
        if any(item.get("rfid_percent") is not None for item in items):
            return "lowest_remaining_first"
        return "slot_order"

    @property
    def unload_after_print_enabled(self):
        return bool(self.store.setting("unload_after_print_enabled", False))

    @property
    def rfid_insert_reading_enabled(self):
        return bool(self.store.setting("rfid_insert_reading_enabled", True))

    @property
    def rfid_startup_reading_enabled(self):
        # Presence is established with one lightweight slot-mask query per CFS.
        # Full tag scans at every boot are deliberately opt-in: persisted slot
        # metadata and remaining estimates are restored from filament_box.json.
        return bool(self.store.setting("rfid_startup_reading_enabled", False))

    def slot_target_temp(self, slot):
        profile = self.profile(slot)
        if profile.get("target_temp") is not None:
            return profile["target_temp"]
        value = self.store.materials.get(profile["material"])
        return value.get("target_temp") if value else None

    def activate_spool(self, slot):
        spoolman_id = self.profile(slot)["spoolman_id"]
        if spoolman_id is not None:
            self._set_active_spool(int(spoolman_id))

    def clear_active_spool(self, slot):
        if (self.is_valid_slot(slot)
                and self.profile(slot)["spoolman_id"] is not None):
            self._set_active_spool(None)

    def _set_active_spool(self, spool_id):
        try:
            webhooks = self.printer.lookup_object("webhooks", None)
            if webhooks is not None:
                webhooks.call_remote_method(
                    "spoolman_set_active_spool", spool_id=spool_id)
        except Exception:
            _klog('active Spoolman update failed', level=logging.exception)

    def get_filament_sensor_state(self):
        try:
            status = self._filament_sensor().get_status(
                self.reactor.monotonic())
            return bool(status["filament_detected"]), None
        except Exception as exc:
            return None, str(exc)

    def _filament_sensor(self):
        return self.printer.lookup_object(
            "filament_switch_sensor filament_sensor", None)

    def filament_detected(self):
        detected, error = self.get_filament_sensor_state()
        if error:
            raise BoxError("Printhead filament sensor is unavailable: %s" % error)
        return detected

    def filament_sensor_enabled(self):
        return bool(self._filament_sensor().runout_helper.sensor_enabled)

    def enable_filament_sensor(self):
        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=1")

    def disable_filament_sensor(self):
        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0")

    def get_cut_calibration_config(self):
        return {
            "pre_cut_pos_x": self.pre_cut_x,
            "cut_pos_x": self.cut_x,
            "cut_pos_y": self.cut_y,
            "pre_cut_cal_pos_x": self.pre_cut_cal_x,
            "check_cut_pos_x_max": self.cut_check_max_x,
            "check_cut_pos_x_min": self.cut_check_min_x,
        }

    def set_cut_position(self, value):
        self.cut_x = float(value)

    def get_cut_sensor_state(self):
        return bool(self.cut_sensor_state)

    def _runout_groups(self, physical_slots):
        groups = {}
        for item in physical_slots:
            material = str(item.get("material") or "").strip().upper()
            color = str(item.get("color") or "").strip().upper()
            if not item.get("present") or not material or not color:
                continue
            groups.setdefault((material, color), []).append(item)
        result = []
        for (material, color), items in groups.items():
            if len(items) < 2:
                continue
            items.sort(key=self._runout_sort_key)
            result.append({
                "material": material,
                "color": color,
                "slots": [item["index"] for item in items],
                "detail": [{
                    "slot": item["index"],
                    "percent": item.get("rfid_percent"),
                    "rfid": bool(item.get("rfid_active")),
                } for item in items],
                "strategy": self._runout_strategy(items),
            })
        result.sort(key=lambda item: (item["material"], item["color"]))
        return result

    def _runout_status(self, physical_slots, snap):
        source = self.runout_origin if self.runout_active else snap.loaded_slot
        if not self.is_physical_slot(source):
            return None
        profile = next(
            (item for item in physical_slots if item["index"] == source), None)
        if profile is None:
            return None
        material, color = profile["material"], profile["color"]
        candidates = []
        if material and color:
            candidates = [
                item for item in physical_slots
                if item["index"] != source and item["present"]
                and item["material"] == material and item["color"] == color
            ]
        candidates.sort(key=self._runout_sort_key)
        chain = [item["index"] for item in candidates]
        detail = [{
            "slot": item["index"],
            "percent": item.get("rfid_percent"),
            "rfid": bool(item.get("rfid_active")),
        } for item in candidates]
        return {
            "loaded_slot": source,
            "chain": chain,
            "chain_detail": detail,
            "sequence": [source] + chain,
            "strategy": self._runout_strategy(candidates),
        }

    def runout_recovery(self):
        snap = self.read_live_state()
        owner = self.tracking_owner
        if (snap.status_code == box_protocol.STATUS_RUNOUT
                and owner is not None
                and snap.path_box == owner.address):
            self.runout_active = True
            self.runout_origin = owner.slot
            self.runout_key = (owner.address, owner.epoch)
        physical = self._slot_statuses(snap)
        runout = self._runout_status(physical, snap)
        if not self.runout_active:
            return {
                "recoverable": False, "reason": "no active CFS runout",
                "loaded_slot": snap.loaded_slot, "target_slot": None,
            }
        if not runout:
            return {
                "recoverable": False, "reason": "no loaded CFS slot",
                "loaded_slot": snap.loaded_slot, "target_slot": None,
            }
        if not runout["chain"]:
            return {
                "recoverable": False,
                "reason": "no matching present replacement slot",
                "loaded_slot": runout["loaded_slot"], "target_slot": None,
            }
        return {
            "recoverable": True,
            "reason": "matching replacement slot found",
            "loaded_slot": runout["loaded_slot"],
            "target_slot": runout["chain"][0],
        }

    def _clog_status(self):
        if self.clog_baseline is None:
            extruder_delta = encoder_delta = None
        else:
            extruder_delta = self.clog_baseline.get("last_extruder", 0.0) - self.clog_baseline["extruder"]
            encoder_delta = abs(
                self.clog_baseline.get("last_encoder", 0.0) - self.clog_baseline["encoder"])
        triggered = (
            extruder_delta is not None and extruder_delta > CLOG_EXTRUDER_MM
            and encoder_delta <= CLOG_ENCODER_RESET_MM)
        state = "disabled" if self.runout_active else (
            "inactive" if not self.snapshot.tracking else (
                "triggered" if triggered else "active"))
        return {
            "state": state,
            "baseline_ready": self.clog_baseline is not None,
            "extruder_delta_mm": extruder_delta,
            "encoder_delta_mm": encoder_delta,
            "extruder_threshold_mm": CLOG_EXTRUDER_MM,
            "encoder_reset_mm": CLOG_ENCODER_RESET_MM,
            "triggered": triggered,
            "event_count": self.clog_event_count,
            "last_event": dict(self.last_clog),
        }

    # ------------------------------------------------------------------
    # G-code command wrappers
    # ------------------------------------------------------------------

    def cmd_helix_noop(self, gcmd):
        # Upstream HelixScreen emits the stock K2 CR_BOX_* envelope.  The
        # OpenHost change engine already owns fan, parking, purge and cleanup,
        # so the stock envelope stages that would duplicate those operations
        # are deliberately accepted as no-ops.
        return

    def cmd_helix_extrude(self, gcmd):
        tnn = self._param(gcmd, "TNN")
        slot = self._tnn_to_slot(tnn)
        if slot is None or not self.is_physical_slot(slot):
            raise gcmd.error("[BOX]: CR_BOX_EXTRUDE requires an online TNN slot")
        # A HelixScreen load/swap arrives as several stock CR_BOX_* commands.
        # Run the complete validated OpenHost change here; following WASTE /
        # FLUSH / END stages are compatibility no-ops to avoid double purge.
        return self.change_engine.change(gcmd, slot, True)

    def cmd_helix_retrude(self, gcmd):
        # For stock K2 unload/swap sequences, RETRUDE is the point at which
        # OpenHost performs the complete validated unload.  A later EXTRUDE
        # in the same HelixScreen script will then load the requested slot.
        return self.change_engine.unload(gcmd, manual=False)

    def _print_idle(self, gcmd):
        if self.change_engine._is_print_active() or self.change_engine._is_print_paused():
            raise gcmd.error("[BOX]: Finish or cancel the current print first")
        if self.operation_depth:
            raise gcmd.error("[BOX]: Wait for the current Box operation to finish")
        return self.printer.lookup_object("virtual_sdcard")

    def _print_path(self, gcmd, sd):
        # Match SDCARD_PRINT_FILE, which accepts a leading slash.
        filename = gcmd.get("FILENAME")
        if filename.startswith("/"):
            filename = filename[1:]
        root = os.path.realpath(sd.sdcard_dirname)
        path = os.path.realpath(os.path.join(root, filename))
        if (not filename or os.path.isabs(filename)
                or os.path.commonpath((root, path)) != root
                or not filename.lower().endswith((".gcode", ".gco", ".g"))):
            raise gcmd.error("[BOX]: Select a text G-code file inside Virtual SD")
        return filename, path

    def _inspect_print(self, gcmd, sd):
        filename, path = self._print_path(gcmd, sd)
        try:
            metadata = read_metadata(path)
        except OSError as exc:
            raise gcmd.error("[BOX]: Unable to inspect print: %s" % exc)
        self.print_info = {"filename": filename, "tools": metadata["tools"]}
        return self.print_info

    def cmd_print_info(self, gcmd):
        self._inspect_print(gcmd, self._print_idle(gcmd))

    def cmd_print_start(self, gcmd):
        sd = self._print_idle(gcmd)
        info = self._inspect_print(gcmd, sd)
        if not info["tools"]:
            raise gcmd.error("[BOX]: No filament usage metadata; start this file normally")
        try:
            mapping = {}
            for entry in gcmd.get("MAP", "").split(","):
                if not entry.strip():
                    continue
                tool, slot = entry.split(":")
                tool, slot = int(tool), int(slot)
                if tool in mapping or not 0 <= tool <= 255 or slot < 0:
                    raise ValueError()
                mapping[tool] = slot
        except ValueError:
            raise gcmd.error("[BOX]: MAP must contain unique tool:slot pairs, e.g. 2:0,3:1")
        used = {item["tool"] for item in info["tools"]}
        if set(mapping) != used:
            raise gcmd.error("[BOX]: Map every tool used by this file: %s" %
                             ", ".join("T%d" % tool for tool in sorted(used)))
        if not self.drivers_ready:
            raise gcmd.error("[BOX]: Filament slots are not ready")
        try:
            live = self.read_live_state()
        except Exception as exc:
            raise gcmd.error("[BOX]: Unable to read CFS state: %s" % exc)
        for slot in mapping.values():
            if not self.is_valid_slot(slot):
                raise gcmd.error("[BOX]: %s is offline" % self.slot_label(slot))
            if self.is_physical_slot(slot) and not live.slot_mask & (1 << slot):
                raise gcmd.error("[BOX]: %s has no filament" % self.slot_label(slot))
        try:
            self._register_tools(used)
        except Exception as exc:
            raise gcmd.error("[BOX]: Unable to register print tools: %s" % exc)
        # Load/reset events clear the old map. Install the new job's map only
        # after those events and before Virtual SD schedules its first command.
        sd._reset_file()
        try:
            sd._load_file(gcmd, info["filename"], check_subdirs=True)
            self.change_engine.tool_map = mapping
            self.change_engine.mapping_filename = info["filename"]
            sd.do_resume()
        except Exception as exc:
            sd._reset_file()
            if isinstance(exc, self.gcode.error):
                raise
            # Any other exception from a G-code handler shuts Klipper down.
            raise gcmd.error("[BOX]: Unable to start %s: %s"
                             % (info["filename"], exc))

    def cmd_select_slot(self, gcmd):
        self.change_engine.change(
            gcmd, gcmd.get_int("SLOT", minval=0),
            bool(gcmd.get_int("FLUSH", 1)))

    def cmd_load(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", 0, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX - 1)
        try:
            already_loaded = self.physical_load(slot)
        except Exception as exc:
            raise gcmd.error(
                "[BOX]: " + box_protocol.format_failed("BOX_LOAD", exc))
        if already_loaded:
            self._info(gcmd, "%s already loaded; tracking active" % self.slot_label(slot))
        else:
            self._info(gcmd, "%s loaded" % self.slot_label(slot))
        self.last_loaded_slot = slot
        self.activate_spool(slot)

    def cmd_unload(self, gcmd):
        if "SLOT" in gcmd.get_command_parameters():
            raise gcmd.error("[BOX]: BOX_UNLOAD no longer accepts SLOT")
        manual = bool(gcmd.get_int("MANUAL", 0, minval=0, maxval=1))
        self.change_engine.unload(gcmd, manual=manual)

    def cmd_buffer_retract(self, gcmd):
        try:
            with self._operation():
                live = self.read_live_state(include_topology=False)
                if self.is_physical_slot(live.loaded_slot):
                    driver, address, _local = self._driver_for_slot(live.loaded_slot)
                else:
                    if not self.drivers:
                        raise BoxError("No CFS box is online")
                    address = min(self.drivers)
                    driver = self.drivers[address]
                self._set_tracking(
                    driver, address, None, "disable CFS tracking")
                self._require_reply(
                    driver.unload_buffer(timeout=BUFFER_RETRACT_TIMEOUT),
                    "buffer retract")
        except Exception as exc:
            raise gcmd.error("[BOX]: BOX_BUFFER_RETRACT failed: %s" % exc)
        self._info(gcmd, "Box %d buffer retract complete" % address)

    def cmd_cut(self, gcmd):
        try:
            self.cut_filament(force=bool(gcmd.get_int("FORCE", 0)))
        except Exception as exc:
            raise gcmd.error("[BOX]: BOX_CUT failed: %s" % exc)

    def cmd_nozzle_clean(self, gcmd):
        self.nozzle_clean()

    def cmd_wastebin(self, gcmd):
        self.move_to_wastebin()
        self._info(gcmd, "Moved to wastebin")

    def cmd_slot_set(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX)
        if slot is None:
            raise gcmd.error("[BOX]: SLOT is required")
        if not self.is_valid_slot(slot):
            raise gcmd.error("[BOX]: %s is not online" % self.slot_label(slot))
        if self.is_physical_slot(slot) and slot in self.rfid_live_slots:
            raise gcmd.error(
                "[BOX]: %s is managed by a live RFID tag; remove the tagged spool before editing it manually" % self.slot_label(slot))
        material = self._param(gcmd, "MATERIAL")
        if not material:
            raise gcmd.error("[BOX]: MATERIAL is required")
        color = self._normal_color(self._param(gcmd, "COLOR"))
        if color is None:
            raise gcmd.error("[BOX]: COLOR must be #RRGGBB")
        profile = self.profile(slot)
        params = gcmd.get_command_parameters()
        profile["material"] = str(material).strip().upper()
        profile["color"] = color
        if "TARGET_TEMP" in params:
            profile["target_temp"] = gcmd.get_int(
                "TARGET_TEMP", minval=170, maxval=350)
        else:
            material_info = self.store.materials.get(profile["material"], {})
            profile["target_temp"] = material_info.get("target_temp")
        profile["filament_id"] = ""
        profile["source"] = "manual"
        profile["rfid_code"] = ""
        for field in ("BRAND", "NAME"):
            if field in params:
                profile[field.lower()] = str(self._param(gcmd, field) or "").strip()
        if "SPOOLMAN_ID" in params:
            try:
                spool = int(self._param(gcmd, "SPOOLMAN_ID"))
            except (TypeError, ValueError):
                raise gcmd.error("[BOX]: SPOOLMAN_ID must be an integer")
            profile["spoolman_id"] = None if spool < 0 else spool
        self.set_profile(slot, profile)
        self._info(gcmd, "Saved %s profile" % self.slot_label(slot))

    def _catalog_target(self, entry):
        target = entry.get("target_temp")
        try:
            target = int(target)
        except (TypeError, ValueError):
            target = None
        if target is None:
            material = str(entry.get("material", "")).strip().upper()
            target = (self.store.materials.get(material) or {}).get("target_temp")
        if target is None:
            target = self.change_engine.default_temp
        return max(170, min(350, int(target)))

    def _ensure_catalog_filament(self, entry, raw_code=None, save=True):
        if not isinstance(entry, dict):
            return None
        material = str(entry.get("material", "")).strip().upper()
        brand = str(entry.get("brand", "")).strip()
        name = str(entry.get("name", "")).strip()
        filament_id = str(entry.get("id", "")).strip().upper()
        if not material or not filament_id:
            return None

        system_entry = bool(entry.get("system", False))
        if system_entry:
            return self.store.filament(filament_id)
        existing = self.store.filament_by_identity(brand, name, material)
        if existing is not None and system_entry and not existing.get("system"):
            # Never turn a user's custom profile into a read-only system one
            # just because brand/name/material match a shipped catalog entry.
            existing = None
        if existing is None:
            existing = self.store.filament(filament_id)
            if (existing is not None and system_entry
                    and not existing.get("system")):
                _klog("system filament %s skipped: a custom profile uses that ID",
                      filament_id, level=logging.warning)
                return None
        key = filament_id if existing is None else existing["id"]
        codes = list((existing or {}).get("rfid_codes") or [])
        for code in (
                list(entry.get("rfid_codes") or [])
                + ([raw_code] if raw_code else [])):
            code = str(code or "").strip().upper()
            if code and code not in codes:
                codes.append(code)
        aliases = list((existing or {}).get("aliases") or [])
        for alias in entry.get("aliases") or []:
            alias = str(alias or "").strip().upper()
            if alias and alias != key and alias not in aliases:
                aliases.append(alias)

        target_temp = (
            (existing or {}).get("target_temp")
            if (existing or {}).get("target_temp") is not None
            else self._catalog_target(entry)
        )
        value = {
            "material": material,
            "color": (existing or {}).get("color", ""),
            "brand": brand,
            "name": name,
            "target_temp": target_temp,
            "min_temp": (
                (existing or {}).get("min_temp")
                if (existing or {}).get("min_temp") is not None
                else entry.get("min_temp")),
            "max_temp": (
                (existing or {}).get("max_temp")
                if (existing or {}).get("max_temp") is not None
                else entry.get("max_temp")),
            "pressure_advance": (
                (existing or {}).get("pressure_advance")
                if (existing or {}).get("pressure_advance") is not None
                else entry.get("pressure_advance")),
            "rfid_codes": codes,
            "aliases": aliases,
            "spoolman_id": (existing or {}).get("spoolman_id"),
            "system": bool((existing or {}).get("system", False)),
            "source": entry.get("source") or (existing or {}).get("source") or "user",
        }
        if value["system"]:
            return existing
        saved = self.store.set_filament(key, value, save=save)
        if material not in self.store.materials:
            self.store.set_material(material, saved["target_temp"])
        return saved

    def _seed_material_catalog(self, import_database=True):
        """Load the shipped catalog in memory and merge the K2-RFID import."""
        entries = []
        for entry in self.system_material_catalog.entries:
            entry["system"] = True
            entries.append(entry)
        self.store.set_system(entries)
        return self._import_material_database() if import_database else 0

    def _import_material_database(self, force=False):
        """Merge the K2-RFID database at material_database_path into the library.

        The import runs again only when that file's content changes, so a
        profile deleted from the library stays deleted. Profiles already in
        the library keep their temperatures, pressure advance and Spoolman link.
        """
        entries = self.material_catalog.entries
        if not entries:
            return 0
        # Hash the parsed profiles, not the file bytes: reformatting the file
        # or re-exporting the same database does not re-import it.
        digest = hashlib.sha1(json.dumps(
            entries, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        path = getattr(self, "material_database_path", "") or "material_database"
        imports = self.store.library_meta.setdefault("imports", {})
        if not force and imports.get(path) == digest:
            return 0
        self.store.refresh_library()
        count = 0
        for entry in entries:
            entry["system"] = False
            entry["source"] = "import"
            try:
                if self._ensure_catalog_filament(entry, save=False):
                    count += 1
            except (BoxError, TypeError, ValueError) as exc:
                _klog("skipping invalid catalog filament %r: %s",
                      entry.get("id"), exc, level=logging.warning)
        imports[path] = digest
        try:
            self.store.save_library()
            self.store.save()
        except BoxError as exc:
            _klog("filament import not saved: %s", exc, level=logging.warning)
            return 0
        _klog("imported %d filament profiles from %s", count, path)
        return count

    def cmd_filament_reload(self, gcmd):
        reloaded = self.store.refresh_library(force=True)
        if self.store.library_error:
            raise gcmd.error("[BOX]: %s" % self.store.library_error)
        imported = self._import_material_database()
        status = self.store.library_status
        self._info(gcmd, "Filament library %s: %d custom profiles%s%s" % (
            status["path"], status["custom_count"],
            " (reloaded)" if reloaded else "",
            ", %d imported" % imported if imported else ""))

    def cmd_filament_set(self, gcmd):
        filament_id = self._param(gcmd, "ID")
        material = self._param(gcmd, "MATERIAL")
        if not filament_id or not material:
            raise gcmd.error("[BOX]: ID and MATERIAL are required")
        color_raw = self._param(gcmd, "COLOR")
        color = "" if color_raw in (None, "") else self._normal_color(color_raw)
        if color is None:
            raise gcmd.error("[BOX]: COLOR must be #RRGGBB")
        target = gcmd.get_int("TARGET_TEMP", None, minval=170, maxval=350)
        existing = self.store.filament(filament_id) or {}
        if existing.get("system"):
            raise gcmd.error(
                "[BOX]: System filament %s is read only; create a custom profile instead"
                % str(filament_id).strip().upper())
        if target is None:
            target = existing.get("target_temp")
        if target is None:
            material_info = self.store.materials.get(str(material).strip().upper(), {})
            target = material_info.get("target_temp")
        if target is None:
            target = self.change_engine.default_temp
        spoolman = self._param(gcmd, "SPOOLMAN_ID")
        if spoolman not in (None, ""):
            try:
                spoolman = int(spoolman)
            except (TypeError, ValueError):
                raise gcmd.error("[BOX]: SPOOLMAN_ID must be an integer")
            if spoolman < 0:
                spoolman = None
        else:
            spoolman = existing.get("spoolman_id")
        params = gcmd.get_command_parameters()
        min_temp = (
            gcmd.get_int("MIN_TEMP", minval=0, maxval=500)
            if "MIN_TEMP" in params else existing.get("min_temp"))
        max_temp = (
            gcmd.get_int("MAX_TEMP", minval=0, maxval=500)
            if "MAX_TEMP" in params else existing.get("max_temp"))
        if (min_temp is not None and max_temp is not None
                and min_temp > max_temp):
            raise gcmd.error("[BOX]: MIN_TEMP cannot be greater than MAX_TEMP")
        pressure_advance = (
            gcmd.get_float("PRESSURE_ADVANCE", minval=0.0, maxval=2.0)
            if "PRESSURE_ADVANCE" in params
            else existing.get("pressure_advance"))
        rfid_code = str(
            self._param(gcmd, "RFID_CODE")
            if "RFID_CODE" in params
            else existing.get("rfid_code", "") or "").strip().upper()
        rfid_codes = list(existing.get("rfid_codes") or [])
        if rfid_code and rfid_code not in rfid_codes:
            rfid_codes.append(rfid_code)
        value = {
            "material": str(material).strip().upper(),
            "color": color if color_raw not in (None, "") else existing.get("color", ""),
            "brand": str(self._param(gcmd, "BRAND") if "BRAND" in gcmd.get_command_parameters() else existing.get("brand", "") or "").strip(),
            "name": str(self._param(gcmd, "NAME") if "NAME" in params else existing.get("name", "") or "").strip(),
            "target_temp": int(target),
            "min_temp": min_temp,
            "max_temp": max_temp,
            "pressure_advance": pressure_advance,
            "rfid_codes": rfid_codes,
            "aliases": list(existing.get("aliases") or []),
            "spoolman_id": spoolman,
            "system": False,
            "source": existing.get("source") or "user",
        }
        try:
            saved = self.store.set_filament(filament_id, value)
        except BoxError as exc:
            raise gcmd.error("[BOX]: %s" % exc)
        if saved["material"] not in self.store.materials:
            self.store.set_material(saved["material"], saved["target_temp"])
        self._apply_new_filament(saved)
        self._info(gcmd, "Saved filament %s (%s)" % (saved["id"], saved["material"]))

    def cmd_filament_delete(self, gcmd):
        filament_id = self._param(gcmd, "ID")
        if not filament_id:
            raise gcmd.error("[BOX]: ID is required")
        existing = self.store.filament(filament_id)
        if existing and existing.get("system"):
            raise gcmd.error(
                "[BOX]: System filament %s cannot be deleted" % filament_id)
        try:
            deleted = self.store.delete_filament(filament_id)
        except BoxError as exc:
            raise gcmd.error("[BOX]: %s" % exc)
        if not deleted:
            raise gcmd.error("[BOX]: Filament %s does not exist" % filament_id)
        self._info(gcmd, "Deleted filament %s" % str(filament_id).strip().upper())

    def cmd_slot_assign(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX)
        filament_id = self._param(gcmd, "FILAMENT_ID")
        if slot is None or not filament_id:
            raise gcmd.error("[BOX]: SLOT and FILAMENT_ID are required")
        if not self.is_valid_slot(slot):
            raise gcmd.error("[BOX]: T%d is not an online box slot" % slot)
        if self.is_physical_slot(slot) and slot in self.rfid_live_slots:
            raise gcmd.error("[BOX]: T%d is managed by a live RFID tag; remove the tag or disable RFID assignment first" % slot)
        filament = self.store.filament(filament_id)
        if filament is None:
            raise gcmd.error("[BOX]: Unknown filament %s" % filament_id)
        color_override = self._param(gcmd, "COLOR")
        color = filament.get("color", "")
        if color_override not in (None, ""):
            color = self._normal_color(color_override)
            if color is None:
                raise gcmd.error("[BOX]: COLOR must be #RRGGBB")
        if not color:
            color = "#808080"
        profile = {
            "material": filament["material"],
            "color": color,
            "brand": filament.get("brand", ""),
            "name": filament.get("name", ""),
            "target_temp": filament.get("target_temp"),
            "pressure_advance": filament.get("pressure_advance"),
            "spoolman_id": filament.get("spoolman_id"),
            "filament_id": filament["id"],
            "source": "library",
            "rfid_code": filament.get("rfid_code", ""),
        }
        self.set_profile(slot, profile)
        self._ensure_material(filament["material"], filament.get("target_temp"))
        self._info(gcmd, "Assigned filament %s to T%d" % (filament["id"], slot))

    def cmd_slot_clear(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX)
        if slot is None:
            raise gcmd.error("[BOX]: SLOT is required")
        if not self.is_valid_slot(slot):
            raise gcmd.error("[BOX]: %s is not online" % self.slot_label(slot))
        if self.is_physical_slot(slot) and slot in self.rfid_live_slots:
            raise gcmd.error(
                "[BOX]: %s is managed by a live RFID tag; remove the tagged spool before clearing it" % self.slot_label(slot))
        # The external spool is intentionally clearable even when its current
        # profile came from RFID. Unlike a CFS bay, the standalone reader has
        # no removal event to tell us that the spool was replaced.
        self.clear_slot_assignment(slot)
        self._info(gcmd, "Cleared %s profile" % self.slot_label(slot))

    def cmd_material_set(self, gcmd):
        material = self._param(gcmd, "MATERIAL")
        target = gcmd.get_int("TARGET_TEMP", None, minval=170, maxval=350)
        if not material or target is None:
            raise gcmd.error("[BOX]: MATERIAL and TARGET_TEMP are required")
        key = self.store.set_material(material, target)
        self._info(gcmd, "Saved material %s: %dC" % (key, target))

    def cmd_rfid_read_slot(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX - 1)
        if slot is None or not self.is_physical_slot(slot):
            raise gcmd.error("[BOX]: SLOT must select a physical CFS slot")
        driver, address, local = self._driver_for_slot(slot)
        presence = self._require_reply(
            driver.query_slot_mask(timeout=0.5),
            "box %d RFID slot mask" % address)
        if not (presence.value & (1 << local)):
            raise gcmd.error("[BOX]: T%d has no spool present" % slot)
        applied = self._force_rfid_results(
            address, driver, 1 << local, "manual T%d reread" % slot)
        if slot not in applied:
            raise gcmd.error(
                "[BOX]: RFID reread for T%d did not return a valid tag record; retry the slot read" % slot)
        self._read_rfid_remaining(slot)
        self._info(gcmd, "RFID reread complete for T%d" % slot)

    def cmd_info_refresh(self, gcmd):
        address = gcmd.get_int(
            "ADDR", None, minval=1, maxval=MAX_ADDRESSES)
        mask = gcmd.get_int(
            "NUM", 0x0F, minval=1, maxval=(1 << SLOTS_PER_BOX) - 1)
        addresses = [address] if address is not None else sorted(self.drivers)
        if not addresses:
            raise gcmd.error("[BOX]: No CFS box is online")
        total_applied = 0
        total_selected = 0
        for current in addresses:
            driver = self.drivers.get(current)
            if driver is None:
                if address is not None:
                    raise gcmd.error("[BOX]: CFS box %d is not online" % current)
                continue
            presence = self._require_reply(
                driver.query_slot_mask(timeout=0.5),
                "box %d RFID refresh slot mask" % current)
            selected = mask & int(presence.value) & ((1 << SLOTS_PER_BOX) - 1)
            if not selected:
                continue
            applied = self._force_rfid_results(
                current, driver, selected, "BOX_INFO_REFRESH")
            total_applied += len(applied)
            total_selected += bin(selected).count("1")
        self._info(
            gcmd, "CFS RFID refresh complete: %d/%d populated slots"
            % (total_applied, total_selected))

    def cmd_modify_tn(self, gcmd):
        params = gcmd.get_command_parameters()
        if not params:
            raise gcmd.error("[BOX]: BOX_MODIFY_TN requires T1A=T1A style mappings")
        mapping = self._helix_tool_map()
        changed = []
        for source, target in params.items():
            source_slot = self._tnn_to_slot(source)
            target_slot = self._tnn_to_slot(target)
            if source_slot is None or target_slot is None:
                raise gcmd.error(
                    "[BOX]: invalid CFS mapping %s=%s" % (source, target))
            mapping[source_slot] = target_slot
            changed.append("%s=%s" % (
                self._slot_to_tnn(source_slot), self._slot_to_tnn(target_slot)))
        self.store.set_setting(
            "helix_tool_map",
            {str(tool): slot for tool, slot in sorted(mapping.items())})
        self._info(gcmd, "Tool mapping updated: %s" % ", ".join(changed))

    def cmd_modify_tn_data(self, gcmd):
        address = gcmd.get_int("ADDR", None, minval=1, maxval=MAX_ADDRESSES)
        num = str(self._param(gcmd, "NUM") or "").strip().upper()
        part = str(self._param(gcmd, "PART") or "").strip().lower()
        data = str(self._param(gcmd, "DATA") or "").strip()
        if address is None or num not in ("A", "B", "C", "D"):
            raise gcmd.error("[BOX]: ADDR=1..4 and NUM=A..D are required")
        slot = self._global_slot(address, ord(num) - ord("A"))
        if not self.is_physical_slot(slot):
            raise gcmd.error("[BOX]: selected CFS slot is offline")
        if part != "color_value":
            raise gcmd.error(
                "[BOX]: only PART=color_value is supported by K2-OpenHost")
        color = self._normal_color(data)
        if color is None:
            raise gcmd.error("[BOX]: DATA must be 0RRGGBB or #RRGGBB")
        profile = self.profile(slot)
        profile["color"] = color
        self.set_profile(slot, profile)
        self._info(gcmd, "Updated %s color to %s" % (
            self._slot_to_tnn(slot), color))

    def cmd_runout_swap(self, gcmd):
        enabled = bool(gcmd.get_int("ENABLE", 1, minval=0, maxval=1))
        self.store.set_setting("runout_swap_enabled", enabled)
        self._info(gcmd, "Runout swap %s" % ("enabled" if enabled else "disabled"))

    def cmd_runout_order(self, gcmd):
        raw = str(gcmd.get("ORDER", "") or "").strip()
        if not raw or raw.upper() == "AUTO":
            self.store.set_setting("runout_order", [])
            self._info(gcmd, "Runout order: automatic")
            return
        order = []
        for part in raw.replace(" ", "").split(","):
            if not part:
                continue
            try:
                slot = int(part)
            except ValueError:
                raise gcmd.error("[BOX]: ORDER must list slot indexes, e.g. ORDER=2,1,0")
            if not self.is_physical_slot(slot):
                raise gcmd.error("[BOX]: %d is not a CFS slot" % slot)
            if slot not in order:
                order.append(slot)
        self.store.set_setting("runout_order", order)
        self._info(gcmd, "Runout order: %s" % ", ".join(
            self.slot_label(slot) for slot in order))

    def cmd_unload_after_print(self, gcmd):
        enabled = bool(gcmd.get_int("ENABLE", 0, minval=0, maxval=1))
        self.store.set_setting("unload_after_print_enabled", enabled)
        self._info(gcmd, "Unload after print %s" % (
            "enabled" if enabled else "disabled"))

    def cmd_rfid_insert(self, gcmd):
        enabled = bool(gcmd.get_int("ENABLE", 0, minval=0, maxval=1))
        self.store.set_setting("rfid_insert_reading_enabled", enabled)
        if not enabled:
            self.rfid_pending.clear()
            self.rfid_snapshot.clear()
            self.rfid_seen_invalid.clear()
        for driver in self.drivers.values():
            driver.set_rfid_insert_reading(enabled)
        self._info(gcmd, "RFID insertion reading %s" % (
            "enabled" if enabled else "disabled"))

    def cmd_rfid_startup(self, gcmd):
        enabled = bool(gcmd.get_int("ENABLE", 0, minval=0, maxval=1))
        self.store.set_setting("rfid_startup_reading_enabled", enabled)
        self._info(gcmd, "RFID startup reading %s" % (
            "enabled" if enabled else "disabled"))

    def cmd_rfid_map_set(self, gcmd):
        code = self._param(gcmd, "CODE")
        material = self._param(gcmd, "MATERIAL")
        brand = self._param(gcmd, "BRAND")
        name = self._param(gcmd, "NAME")
        if not all((code, material, brand, name)):
            raise gcmd.error("[BOX]: CODE, MATERIAL, BRAND, and NAME are required")
        target = self._param(gcmd, "TARGET_TEMP")
        if target not in (None, ""):
            try:
                target = int(target)
            except (TypeError, ValueError):
                raise gcmd.error("[BOX]: TARGET_TEMP must be an integer")
            if not 170 <= target <= 350:
                raise gcmd.error("[BOX]: TARGET_TEMP must be 170..350")
        else:
            target = None
        normalized = self.store.set_rfid_mapping(code, {
            "material": material,
            "brand": brand,
            "name": name,
            "target_temp": target,
        })
        if target is not None and str(material).strip().upper() not in self.store.materials:
            self.store.set_material(material, target)
        self._apply_new_mapping(normalized)
        self._info(gcmd, "Saved RFID mapping %s" % normalized)

    def cmd_rfid_map_delete(self, gcmd):
        code = self._param(gcmd, "CODE")
        if not code:
            raise gcmd.error("[BOX]: CODE is required")
        normalized = self.store.delete_rfid_mapping(code)
        self._info(gcmd, "Deleted RFID mapping %s" % normalized)

    @staticmethod
    def _param(gcmd, name):
        value = gcmd.get_command_parameters().get(name)
        if isinstance(value, str) and value.startswith("="):
            value = value[1:]
        if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                pass
        return value

    @staticmethod
    def _normal_color(value):
        if value is None:
            return None
        text = str(value).strip().upper()
        if text.startswith("#"):
            text = text[1:]
        elif len(text) == 7:
            # Creality/K2-RFID stores 0RRGGBB; RGB starts at the second nibble.
            text = text[1:]
        if len(text) != 6:
            return None
        try:
            int(text, 16)
        except ValueError:
            return None
        return "#" + text

    # ------------------------------------------------------------------
    # RFID metadata and optional Spoolman association
    # ------------------------------------------------------------------

    def _initialize_rfid(self):
        """Apply persisted reader policy and establish presence baselines."""

        if self.observation_mode:
            for address, driver in sorted(self.drivers.items()):
                try:
                    slots = self._require_reply(
                        driver.query_slot_mask(timeout=0.5),
                        "box %d RFID presence baseline"
                        % address
                    )

                    self.rfid_presence[address] = (
                        slots.value & 0x0F
                    )

                except Exception:
                    _klog(
                        "observation RFID baseline failed "
                        "for box %d",
                        address,
                        level=logging.exception
                    )

            return

        for address, driver in sorted(self.drivers.items()):
            try:
                reply = driver.set_rfid_insert_reading(
                    self.rfid_insert_reading_enabled, timeout=1.0)
                self._require_reply(reply, "box %d RFID insertion policy" % address)
                slots = self._require_reply(
                    driver.query_slot_mask(timeout=0.5),
                    "box %d RFID presence baseline" % address)
                local_mask = slots.value & 0x0F
                self.rfid_presence[address] = local_mask

                # Restore persisted slot state without scanning every tag.
                # One slot-mask query is enough to know which bays are still
                # occupied. Occupied RFID bays reuse their saved profile and
                # remaining estimate until an insertion event or explicit
                # reread supplies fresh tag data. Empty bays are only cleaned
                # after repeated topology confirmation below, never from one
                # potentially transient startup sample.
                for local in range(SLOTS_PER_BOX):
                    bit = 1 << local
                    slot = self._global_slot(address, local)
                    if local_mask & bit:
                        self._restore_cached_rfid_slot(slot)

                if self.rfid_startup_reading_enabled and local_mask:
                    cached = self._require_reply(
                        driver.query_rfid_records(
                            local_mask, timeout=0.5),
                        "box %d cached RFID query" % address)
                    pending = []
                    for local, name in enumerate(
                            box_protocol.RFID_SLOT_NAMES):
                        bit = 1 << local
                        if not local_mask & bit:
                            continue
                        slot = self._global_slot(address, local)
                        record = cached.records.get(
                            name, "").strip("\x00")
                        fields = cached.fields.get(name)
                        sample = (record, fields)
                        if self._rfid_record_ready(sample):
                            if self._apply_rfid_record(slot, record, fields):
                                self.rfid_live_slots.add(slot)
                                self._read_rfid_remaining(slot)
                            continue
                        # Do not make printer startup wait tens of seconds for
                        # marginal/self-programmed tags. Keep watching the
                        # CFS cache and expose the per-slot manual reread action.
                        self.rfid_pending.add(slot)
                        self.rfid_snapshot[slot] = self._rfid_cache_key(sample)
                        if record.lower() != "busy":
                            self.rfid_seen_invalid.add(slot)
                        pending.append(slot)
                    if pending:
                        self._warn(
                            "Startup RFID cache is not ready for %s; "
                            "use the per-slot reread action if it does not recover"
                            % ",".join("T%d" % slot for slot in pending))
            except Exception as exc:
                self._warn("RFID initialization failed on box %d: %s" % (
                    address, exc))
        self.snapshot = replace(
            self.snapshot, slot_mask=self._presence_mask())

    def _presence_mask(self):
        mask = 0
        for address, local_mask in self.rfid_presence.items():
            mask |= (local_mask & 0x0F) << ((address - 1) * SLOTS_PER_BOX)
        return mask

    def _clear_rfid_watch(self, slot):
        self.rfid_pending.discard(slot)
        self.rfid_snapshot.pop(slot, None)
        self.rfid_seen_invalid.discard(slot)

    def _snapshot_rfid_cache(self, slot):
        try:
            sample = self._query_rfid_sample(slot)
        except Exception:
            return
        if sample is None:
            return
        self.rfid_snapshot[slot] = self._rfid_cache_key(sample)
        if not self._rfid_record_ready(sample):
            self.rfid_seen_invalid.add(slot)

    def _rfid_inserted(self, slot):
        self.rfid_live_slots.discard(slot)
        self.rfid_percent.pop(slot, None)
        self.rfid_reported_percent.pop(slot, None)
        self.rfid_spools.pop(slot, None)
        self.unknown_rfid.pop(self._runtime_slot_key(slot), None)
        self._invalidate_spoolman(slot)
        self.rfid_snapshot.pop(slot, None)
        self.rfid_seen_invalid.discard(slot)
        if self.rfid_insert_reading_enabled:
            self.rfid_pending.add(slot)
            self._snapshot_rfid_cache(slot)

    def _defer_active_slot_clear(self, slot):
        """Return True while ``slot`` is the active print or runout source.

        When a spool runs out, the CFS reports its bay empty well before the
        printhead sensor triggers BOX_RUNOUT_CHECK. runout_recovery() finds the
        replacement chain from the source slot's material/colour, and
        BoxChangeEngine calls mark_slot_depleted() once the swap or pause has
        been decided, so the source profile must survive until then.
        """
        if (getattr(self, "runout_active", False)
                and slot == getattr(self, "runout_origin", None)):
            return True
        owner = getattr(self, "tracking_owner", None)
        return owner is not None and owner.slot == slot

    def _rfid_removed(self, slot):
        self._persist_rfid_estimates(force=True)
        self._clear_rfid_watch(slot)
        if self._defer_active_slot_clear(slot):
            # Keep profile, spool estimate and RFID state for runout recovery;
            # mark_slot_depleted() finalizes the bay. If the print ends without
            # a runout, the absent-confirmation pass clears it once idle.
            return
        self.rfid_live_slots.discard(slot)
        self.rfid_percent.pop(slot, None)
        self.rfid_reported_percent.pop(slot, None)
        self.rfid_spools.pop(slot, None)
        self.unknown_rfid.pop(self._runtime_slot_key(slot), None)
        self._clear_rfid_slot_key(slot)
        # Slot assignments describe the spool currently occupying that bay.
        # When the bay becomes empty (manual or RFID, including runout), clear
        # only the slot assignment. The reusable filament library and the RFID
        # estimate keyed by spool identity remain persisted for later reuse.
        self.clear_profile(slot)
        self._invalidate_spoolman(slot)

    def _reconcile_presence(self, address, current):
        current &= 0x0F
        previous = self.rfid_presence.get(address, current)
        self.rfid_presence[address] = current
        changed = previous ^ current
        for local in range(SLOTS_PER_BOX):
            bit = 1 << local
            slot = self._global_slot(address, local)

            if current & bit:
                self.rfid_absent_confirm.pop(slot, None)
            elif changed & bit:
                # A live present->absent transition is authoritative and may
                # be cleared immediately.
                self.rfid_absent_confirm.pop(slot, None)
            elif self._defer_active_slot_clear(slot):
                # Active print/runout source: see _defer_active_slot_clear().
                self.rfid_absent_confirm.pop(slot, None)
            else:
                # A printer can occasionally report an empty mask while the
                # CFS/RS485 bus is still settling after boot. Never erase a
                # persisted manual/RFID slot from one such sample. Require
                # three consecutive topology observations (~30 s idle) before
                # cleaning a slot that was already absent at startup.
                count = self.rfid_absent_confirm.get(slot, 0) + 1
                self.rfid_absent_confirm[slot] = count
                if count >= 3:
                    self._clear_rfid_slot_key(slot)
                    self.clear_profile(slot)
                    self.rfid_absent_confirm.pop(slot, None)

            if not changed & bit:
                continue
            if current & bit:
                self._rfid_inserted(slot)
            else:
                self._rfid_removed(slot)

    def _query_presence(self, address, driver):
        reply = driver.query_slot_mask(timeout=0.5)
        if reply is not None and reply.status == box_protocol.STATUS_OK:
            self._reconcile_presence(address, reply.value)
        return reply

    def _handle_slot_events(self, address, events):
        for local, event in enumerate(events):
            if event == 0:
                continue
            slot = self._global_slot(address, local)
            if event == 1:
                self._rfid_inserted(slot)
            elif event == 2:
                self._rfid_removed(slot)
            elif event == 3 and self.rfid_insert_reading_enabled:
                self._read_rfid_result(slot)

    def _pending_rfid_slots(self, address):
        first = (address - 1) * SLOTS_PER_BOX
        return tuple(
            slot for slot in self.rfid_pending
            if first <= slot < first + SLOTS_PER_BOX)

    @staticmethod
    def _rfid_cache_key(sample):
        if not sample:
            return None
        return (sample[0] or "").strip("\x00")

    @staticmethod
    def _rfid_record_ready(sample):
        if not sample:
            return False
        record, fields = sample
        return len((record or "").strip("\x00")) == 40 and bool(fields)

    def _rfid_should_apply(self, slot, sample):
        if not self._rfid_record_ready(sample):
            return False
        if slot in self.rfid_seen_invalid:
            return True
        if slot not in self.rfid_snapshot:
            return False
        return self._rfid_cache_key(sample) != self.rfid_snapshot[slot]

    def _watch_pending_rfid(self, address, driver):
        slots = self._pending_rfid_slots(address)
        if not slots:
            return
        try:
            reply = driver.query_rfid_records(timeout=0.5)
        except Exception:
            return
        if reply is None or reply.status != box_protocol.STATUS_OK:
            return
        for slot in slots:
            _, local = self._address_slot(slot)
            name = box_protocol.RFID_SLOT_NAMES[local]
            sample = (
                reply.records.get(name, "").strip("\x00"),
                reply.fields.get(name),
            )
            if not self._rfid_record_ready(sample):
                self.rfid_seen_invalid.add(slot)

    def _finish_pending_rfid(self, address, state_reply):
        if state_reply.box_state == box_protocol.BOX_STATE_PRELOAD:
            return
        for slot in self._pending_rfid_slots(address):
            self._read_rfid_result(slot)

    def _query_box_snapshot(self, address, driver, include_topology):
        if include_topology:
            self._query_presence(address, driver)
        for _attempt in range(STATE_EVENT_DRAIN):
            self._watch_pending_rfid(address, driver)
            reply = driver.query_box_state(timeout=0.5)
            if reply is None:
                return None
            if reply.slot_events is None:
                self._finish_pending_rfid(address, reply)
                return reply
            self._query_presence(address, driver)
            self._handle_slot_events(address, reply.slot_events)
        _klog("box %d slot events did not drain after %d reads",
             address, STATE_EVENT_DRAIN, level=logging.warning)
        return None

    @staticmethod
    def _clean_rfid(value):
        return str(value or "").strip().strip("\x00")

    def _rfid_spool_fingerprint(self, fields):
        supplier = self._clean_rfid(fields.get("supplier")).upper()
        material = self._clean_rfid(fields.get("mat_id")).upper()
        number = self._clean_rfid(fields.get("number")).upper()
        if not material:
            return None
        if number not in ("", "000000", "000001"):
            return "tag:%s:%s:%s" % (supplier or "?", material, number)
        # Windows/Android K2-RFID commonly use serial 000001. Build a portable
        # fingerprint from stable tag payload fields so a spool keeps its local
        # estimate when it is moved to another CFS slot. If two active tags are
        # byte-for-byte equivalent we split them by slot below, because there is
        # no unique identity available in the CFS record for that case.
        color = self._normal_color(fields.get("color")) or "?"
        length = self._clean_rfid(fields.get("len")).upper() or "?"
        reserve = self._clean_rfid(fields.get("reserve")).upper() or "?"
        return "fingerprint:%s:%s:%s:%s:%s" % (
            supplier or "?", material, color, length, reserve)

    def _rfid_spool_key(self, fields, slot=None):
        fingerprint = self._rfid_spool_fingerprint(fields)
        if fingerprint is None:
            return None
        number = self._clean_rfid(fields.get("number")).upper()
        if number not in ("", "000000", "000001") or slot is None:
            return fingerprint
        duplicate = any(
            other_slot != slot
            and spool.get("fingerprint") == fingerprint
            for other_slot, spool in self.rfid_spools.items())
        return (
            "%s:slot:%s" % (fingerprint, slot)
            if duplicate else fingerprint)

    def _rfid_slot_keys(self):
        value = self.store.setting("rfid_slot_keys", {}) or {}
        return dict(value) if isinstance(value, dict) else {}

    def _set_rfid_slot_key(self, slot, key):
        keys = self._rfid_slot_keys()
        slot_key = str(self._runtime_slot_key(slot))
        if keys.get(slot_key) == key:
            return
        keys[slot_key] = key
        self.store.set_setting("rfid_slot_keys", keys)

    def _clear_rfid_slot_key(self, slot):
        keys = self._rfid_slot_keys()
        slot_key = str(self._runtime_slot_key(slot))
        if keys.pop(slot_key, None) is not None:
            self.store.set_setting("rfid_slot_keys", keys)

    def _restore_cached_rfid_slot(self, slot):
        profile = self.profile(slot)
        if profile.get("source") != "rfid":
            return False
        key = self._rfid_slot_keys().get(str(self._runtime_slot_key(slot)))
        if not key:
            return False
        estimates = self.store.setting("rfid_estimates", {}) or {}
        saved = estimates.get(key, {}) if isinstance(estimates, dict) else {}
        try:
            total_mm = float(saved.get("total_mm"))
            remaining_mm = float(saved.get("remaining_mm"))
        except (TypeError, ValueError):
            return False
        if total_mm <= 0.0:
            return False
        remaining_mm = max(0.0, min(total_mm, remaining_mm))
        self.rfid_spools[slot] = {
            "key": key,
            "fingerprint": None,
            "total_mm": total_mm,
            "remaining_mm": remaining_mm,
        }
        self.rfid_percent[slot] = 100.0 * remaining_mm / total_mm
        return True

    def _remember_rfid_spool(self, slot, fields):
        fingerprint = self._rfid_spool_fingerprint(fields)
        key = self._rfid_spool_key(fields, slot)
        if key is None:
            return
        # K2-OpenHost: a generic-serial tag (000000/000001) does not identify
        # the spool, so its estimate belongs to the bay occupancy, not to the
        # tag. While the spool stays in the bay (startup restore, manual
        # reread) the slot keeps its key; once it was removed the slot key is
        # gone and the next read starts a new spool instance at 100% (or what
        # the CFS reports). Tags with a real serial keep following the spool.
        fresh = False
        # The external reader has no removal event, so it keeps the
        # tag-based key as before.
        if (fingerprint and self.is_physical_slot(slot)
                and self._generic_rfid_serial(fields)):
            current = self._rfid_slot_keys().get(
                str(self._runtime_slot_key(slot)))
            if current and self._rfid_key_base(current) == fingerprint:
                key = current
            else:
                key = "%s:spool:%d" % (fingerprint, self._next_rfid_spool_serial())
                fresh = True
        self._set_rfid_slot_key(slot, key)
        try:
            total_m = int(self._clean_rfid(fields.get("len")))
        except (TypeError, ValueError):
            total_m = 0
        total_mm = float(total_m * 1000) if total_m > 0 else None
        persisted = self.store.setting("rfid_estimates", {}) or {}
        saved = persisted.get(key, {}) if isinstance(persisted, dict) else {}
        if fresh:
            saved = {"remaining_mm": total_mm} if total_mm else {}
        # Migration from the earlier slot-scoped K2-RFID estimate format.
        # This keeps already tracked spools useful after upgrading.
        if not saved and not fresh and isinstance(persisted, dict):
            supplier = self._clean_rfid(fields.get("supplier")).upper() or "?"
            material = self._clean_rfid(fields.get("mat_id")).upper()
            number = self._clean_rfid(fields.get("number")).upper() or "?"
            color = self._normal_color(fields.get("color")) or "?"
            legacy = "slot:%s:%s:%s:%s:%s" % (
                supplier, material, number, color, slot)
            saved = persisted.get(legacy, {})
            if not saved and fingerprint and key != fingerprint:
                saved = persisted.get(fingerprint, {})
        remaining_mm = saved.get("remaining_mm")
        try:
            remaining_mm = float(remaining_mm)
        except (TypeError, ValueError):
            remaining_mm = None
        if total_mm is not None and remaining_mm is not None:
            remaining_mm = max(0.0, min(total_mm, remaining_mm))
        self.rfid_spools[slot] = {
            "key": key,
            "fingerprint": fingerprint,
            "total_mm": total_mm,
            "remaining_mm": remaining_mm,
            "generic_serial": self._generic_rfid_serial(fields),
        }
        if total_mm and remaining_mm is not None:
            self.rfid_percent[slot] = 100.0 * remaining_mm / total_mm
            percent = self.rfid_percent[slot]
            if (not fresh and percent < RFID_LOW_ESTIMATE_HINT
                    and self._generic_rfid_serial(fields)):
                self._info(
                    self.gcode,
                    "%s: estimate %.1f%%. If this is a new spool, remove and "
                    "reinsert it, or run _BOX_RFID_SPOOL_NEW SLOT=%d"
                    % (self.slot_label(self._runtime_slot(slot)), percent, slot))
        if fresh:
            self.rfid_estimate_dirty = True

    def _generic_rfid_serial(self, fields):
        number = self._clean_rfid(fields.get("number")).upper()
        return number in ("", "000000", "000001")

    @staticmethod
    def _rfid_key_base(key):
        return str(key).split(":slot:", 1)[0].split(":spool:", 1)[0]

    def _next_rfid_spool_serial(self):
        serial = int(self.store.setting("rfid_spool_serial", 0) or 0) + 1
        self.store.set_setting("rfid_spool_serial", serial)
        return serial

    def _apply_reported_remaining(self, slot, value):
        if not isinstance(value, int) or not 0 <= value <= 100:
            return
        self.rfid_reported_percent[slot] = value
        spool = self.rfid_spools.get(slot)
        if spool and spool.get("generic_serial"):
            # K2-OpenHost: on spools whose tag was applied by hand (no serial)
            # the CFS value does not track the filament: measured on the
            # development K2 Pro, a nearly empty spool reported 99 and a new
            # one 1 (the CFS likely derives it from the tag's rotation, which
            # depends on where the tag sits). It stays visible as the raw
            # rfid_reported_percent; the estimate follows the filament used.
            return
        if not spool or not spool.get("total_mm"):
            self.rfid_percent[slot] = float(value)
            return
        hardware_mm = spool["total_mm"] * value / 100.0
        remaining = spool.get("remaining_mm")
        if remaining is None:
            remaining = hardware_mm
        else:
            remaining = min(float(remaining), hardware_mm)
        spool["remaining_mm"] = max(0.0, remaining)
        self.rfid_percent[slot] = (
            100.0 * spool["remaining_mm"] / spool["total_mm"])
        self.rfid_estimate_dirty = True

    def _persist_rfid_estimates(self, force=False):
        if not self.rfid_estimate_dirty and not force:
            return
        persisted = self.store.setting("rfid_estimates", {}) or {}
        if not isinstance(persisted, dict):
            persisted = {}
        persisted = dict(persisted)
        for spool in self.rfid_spools.values():
            key = spool.get("key")
            if not key or not spool.get("total_mm"):
                continue
            remaining = spool.get("remaining_mm")
            if remaining is None:
                continue
            persisted[key] = {
                "total_mm": round(float(spool["total_mm"]), 3),
                "remaining_mm": round(float(remaining), 3),
            }
        # Per-insertion estimates (generic tags) end with their bay
        # occupancy: drop those no slot or live spool refers to any more.
        live = set(self._rfid_slot_keys().values())
        live.update(spool.get("key") for spool in self.rfid_spools.values())
        for key in [k for k in persisted if ":spool:" in k and k not in live]:
            del persisted[key]
        self.store.data["runtime"]["rfid_estimates"] = persisted
        self.store.save()
        self.rfid_estimate_dirty = False

    def cmd_rfid_spool_new(self, gcmd):
        """K2-OpenHost: declare the spool in a slot new.

        Tags with the generic serial cannot tell a fresh spool from a used
        one with the same brand, material, color and length, so the fresh
        one inherits the used one's estimate (down to 0%). This sets the
        estimate to REMAINING percent (default 100) for this slot only: when
        another slot holds a spool with the same identity, this slot gets
        its own key so the other estimate is left as it is.
        """
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX - 1)
        percent = gcmd.get_float("REMAINING", 100.0, minval=0.0, maxval=100.0)
        if slot is None or not self.is_physical_slot(slot):
            raise gcmd.error("[BOX]: SLOT must select a physical CFS slot")
        spool = self.rfid_spools.get(slot)
        if not spool or not spool.get("total_mm") or not spool.get("key"):
            raise gcmd.error(
                "[BOX]: T%d has no RFID spool with a known length; read the "
                "tag first (_BOX_RFID_READ_SLOT SLOT=%d)" % (slot, slot))
        stats = self.printer.lookup_object("print_stats", None)
        if (getattr(stats, "state", None) in ("printing", "paused")
                and self.snapshot.loaded_slot == slot):
            raise gcmd.error(
                "[BOX]: T%d is feeding the current print" % slot)
        key = spool["key"]
        if any(other is not spool and other.get("key") == key
               for other in self.rfid_spools.values()):
            base = spool.get("fingerprint") or self._rfid_key_base(key)
            key = "%s:spool:%d" % (base, self._next_rfid_spool_serial())
            spool["key"] = key
            self._set_rfid_slot_key(slot, key)
        spool["remaining_mm"] = float(spool["total_mm"]) * percent / 100.0
        self.rfid_percent[slot] = percent
        self.rfid_reported_percent.pop(slot, None)
        self.rfid_estimate_dirty = True
        self._persist_rfid_estimates(force=True)
        self._info(
            gcmd, "%s: spool declared new, estimate %.0f%%"
            % (self.slot_label(self._runtime_slot(slot)), percent))

    def _track_rfid_usage(self, eventtime, snap):
        stats = self.printer.lookup_object("print_stats", None)
        if stats is None:
            return
        status = stats.get_status(eventtime)
        state = status.get("state")
        try:
            used = float(status.get("filament_used", 0.0))
        except (TypeError, ValueError):
            used = 0.0

        if state != "printing":
            if self.rfid_last_print_state == "printing":
                self._persist_rfid_estimates(force=True)
            self.rfid_last_filament_used = None
            self.rfid_last_print_state = state
            self.rfid_last_usage_slot = None
            return

        slot = snap.loaded_slot
        if self.rfid_last_print_state != "printing" or self.rfid_last_filament_used is None:
            self.rfid_last_filament_used = used
            self.rfid_last_print_state = state
            self.rfid_last_usage_slot = slot
            return

        # Tool changes briefly move loaded_slot through -1 and then onto the
        # destination spool. Do not charge the extrusion accumulated across
        # that transition to either spool; start a fresh usage baseline once
        # the active source is stable. This slightly under-counts the handoff
        # window instead of corrupting one spool's remaining estimate.
        if slot != self.rfid_last_usage_slot:
            self.rfid_last_filament_used = used
            self.rfid_last_usage_slot = slot
            self.rfid_last_print_state = state
            return

        delta = used - self.rfid_last_filament_used
        self.rfid_last_filament_used = used
        self.rfid_last_print_state = state
        # print_stats can move backwards briefly during retract/reset paths.
        # Never let a negative delta increase the estimated spool remaining.
        if delta <= 0.0 or delta > 5000.0:
            return
        spool = self.rfid_spools.get(slot)
        if spool is None or not spool.get("total_mm"):
            return
        remaining = spool.get("remaining_mm")
        if remaining is None:
            return
        remaining = max(
            0.0, min(float(spool["total_mm"]), float(remaining) - delta))
        spool["remaining_mm"] = remaining
        self.rfid_percent[slot] = 100.0 * remaining / spool["total_mm"]
        self.rfid_estimate_dirty = True
        if eventtime - self.last_rfid_estimate_save >= RFID_REFRESH:
            self._persist_rfid_estimates()
            self.last_rfid_estimate_save = eventtime

    @staticmethod
    def _rfid_map_command(code):
        return (
            '_BOX_RFID_MAP_SET CODE=%s MATERIAL=PLA BRAND="Brand" '
            'NAME="Name" TARGET_TEMP=220' % code)

    def _resolve_rfid(self, raw_code):
        code, product = resolve_material(raw_code)
        filament = self.store.filament_for_rfid(raw_code)
        if filament:
            return code, {
                "filament_id": filament["id"],
                "material": filament["material"],
                "brand": filament.get("brand", ""),
                "name": filament.get("name", ""),
                "target_temp": filament.get("target_temp"),
                "pressure_advance": filament.get("pressure_advance"),
            }

        mapping = self.store.rfid_mapping(code)
        if mapping:
            resolved = dict(mapping)
            if self.auto_register_rfid_filaments:
                saved = self._ensure_catalog_filament({
                    "id": code,
                    "source": "rfid",
                    "material": mapping["material"],
                    "brand": mapping.get("brand", ""),
                    "name": mapping.get("name", ""),
                    "target_temp": mapping.get("target_temp"),
                    "rfid_codes": [raw_code, code],
                }, raw_code)
                if saved:
                    resolved["filament_id"] = saved["id"]
            return code, resolved

        catalog = self.material_catalog.lookup(raw_code)
        if catalog:
            catalog["source"] = "import"
            saved = (
                self._ensure_catalog_filament(catalog, raw_code)
                if self.auto_register_rfid_filaments else None)
            resolved = {
                "material": catalog["material"],
                "brand": catalog.get("brand", ""),
                "name": catalog.get("name", ""),
                "target_temp": self._catalog_target(catalog),
                "pressure_advance": catalog.get("pressure_advance"),
            }
            if saved:
                resolved["filament_id"] = saved["id"]
            return code, resolved

        if product:
            resolved = {
                "material": product["material"],
                "brand": product["brand"],
                "name": product["name"],
                "target_temp": product["default_temp"],
            }
            if self.auto_register_rfid_filaments:
                saved = self._ensure_catalog_filament({
                    "id": code,
                    "source": "rfid",
                    "material": product["material"],
                    "brand": product["brand"],
                    "name": product["name"],
                    "target_temp": product["default_temp"],
                    "rfid_codes": [raw_code, code],
                }, raw_code)
                if saved:
                    resolved["filament_id"] = saved["id"]
            return code, resolved
        return code, None

    def _record_unknown_rfid(self, slot, code, raw_code, record, fields):
        if not code:
            return
        slot_key = self._runtime_slot_key(slot)
        previous = self.unknown_rfid.get(slot_key)
        self.unknown_rfid[slot_key] = {
            "code": code,
            "raw_code": raw_code,
            "record": record,
            "fields": dict(fields),
        }
        if previous and previous.get("code") == code:
            return
        self._warn("Unknown RFID tag in %s: CODE=%s" % (
            self.slot_label(self._runtime_slot(slot_key)), code))
        self._warn("Map it with: %s" % self._rfid_map_command(code))

    def _ensure_material(self, material, target=None):
        material = str(material or "").strip().upper()
        if material and material not in self.store.materials:
            fallback = target is None
            target = self.change_engine.default_temp if fallback else target
            self.store.set_material(material, target)
            if fallback:
                self._info(
                    self.gcode,
                    "New material %s: flush temperature defaulted to %dC. "
                    "Update it in the slot UI or run _BOX_MATERIAL_SET "
                    'MATERIAL="%s" TARGET_TEMP=230'
                    % (material, target, material))

    def _rfid_profile(self, fields):
        raw_code = self._clean_rfid(fields["mat_id"]).upper()
        code, resolved = self._resolve_rfid(raw_code)
        reserve = self._clean_rfid(fields["reserve"]).upper()
        color = self._normal_color(fields["color"]) or ""
        if resolved:
            material = str(resolved["material"]).strip().upper()
            brand = str(resolved.get("brand", "")).strip()
            name = str(resolved.get("name", "")).strip()
            target = resolved.get("target_temp")
            pressure_advance = resolved.get("pressure_advance")
        else:
            material = raw_code
            brand = self._clean_rfid(fields["supplier"])
            name = ""
            target = None
            pressure_advance = None
        return ({
            "material": material, "color": color, "brand": brand,
            "name": name, "target_temp": target,
            "pressure_advance": pressure_advance, "spoolman_id": None,
            "filament_id": str(resolved.get("filament_id", "")).strip().upper() if resolved else "",
            "source": "rfid",
            "rfid_code": code,
            "rfid_reserve": reserve,
        }, code, raw_code, target, resolved is not None)

    def _apply_rfid_profile(self, slot, fields):
        profile, _code, _raw_code, target, resolved = self._rfid_profile(fields)
        if not resolved:
            return False
        self._ensure_material(profile["material"], target)
        self.unknown_rfid.pop(self._runtime_slot_key(slot), None)
        self.set_profile(slot, profile)
        return True

    def _invalidate_spoolman(self, slot):
        slot_key = self._runtime_slot_key(slot)
        self.spoolman_tokens[slot_key] = (
            self.spoolman_tokens.get(slot_key, 0) + 1)

    def _apply_rfid_record(self, slot, record, fields):
        record = record.rstrip("\x00")
        if len(record) != 40 or not record.strip():
            return False
        display_slot = self._runtime_slot(slot)
        code = self._clean_rfid(fields.get("mat_id")) or "unknown"
        reserve = self._clean_rfid(fields.get("reserve")) or "none"
        self._info(
            self.gcode,
            "RFID tag read for %s: code=%s reserve=%s"
            % (self.slot_label(display_slot), code, reserve))
        requested_id = _spool_id_from_reserve(fields.get("reserve"))
        if requested_id is not None:
            self._request_spoolman_profile(
                slot, requested_id, record, dict(fields))
            return True
        self._invalidate_spoolman(slot)
        raw_code = self._clean_rfid(fields.get("mat_id")).upper()
        self._remember_rfid_spool(slot, fields)
        normalized, resolved = self._resolve_rfid(raw_code)
        if not resolved:
            self._record_unknown_rfid(
                slot, normalized, raw_code, record, fields)
            return True
        if self._apply_rfid_profile(slot, fields):
            self._info(
                self.gcode, "%s: RFID profile applied" % self.slot_label(display_slot))
        return True

    def _apply_new_mapping(self, code):
        mapping = self.store.rfid_mapping(code)
        if not mapping:
            return
        self._ensure_material(mapping["material"], mapping.get("target_temp"))
        for slot, unknown in list(self.unknown_rfid.items()):
            if unknown.get("code") != code:
                continue
            fields = unknown.get("fields")
            if not isinstance(fields, dict):
                continue
            self._invalidate_spoolman(slot)
            self._apply_rfid_profile(slot, fields)

    def _apply_new_filament(self, filament):
        if not isinstance(filament, dict):
            return
        for slot, unknown in list(self.unknown_rfid.items()):
            fields = unknown.get("fields")
            if not isinstance(fields, dict):
                continue
            raw_code = self._clean_rfid(fields.get("mat_id")).upper()
            matched = self.store.filament_for_rfid(raw_code)
            if not matched or matched.get("id") != filament.get("id"):
                continue
            self._invalidate_spoolman(slot)
            if self._apply_rfid_profile(slot, fields):
                self._info(
                    self.gcode,
                    "RFID T%d: custom filament %s applied"
                    % (self._runtime_slot(slot), filament.get("id")))

    def _read_rfid_remaining(self, slot):
        address, local = self._address_slot(slot)
        driver = self.drivers.get(address)
        if driver is None or slot not in self.rfid_live_slots:
            return
        reply = driver.query_rfid_remaining(1 << local, timeout=0.5)
        if reply is None or reply.status != box_protocol.STATUS_OK:
            return
        value = reply.values.get(box_protocol.RFID_SLOT_NAMES[local])
        self._apply_reported_remaining(slot, value)
        # A tag read establishes the spool identity for this bay. Persist the
        # first remaining estimate immediately so a reboot does not require a
        # second RFID scan just to restore the gauge.
        self._persist_rfid_estimates(force=True)

    def _query_rfid_sample(self, slot):
        address, local = self._address_slot(slot)
        driver = self.drivers.get(address)
        if driver is None:
            return None
        name = box_protocol.RFID_SLOT_NAMES[local]
        reply = driver.query_rfid_records(1 << local, timeout=0.5)
        if reply is None or reply.status != box_protocol.STATUS_OK:
            return None
        return (
            reply.records.get(name, "").strip("\x00"),
            reply.fields.get(name),
        )

    def _read_rfid_result(self, slot):
        sample = self._query_rfid_sample(slot)
        if sample is None:
            return "error"
        record, fields = sample
        if record.lower() == "busy":
            self.rfid_seen_invalid.add(slot)
            return "busy"
        if record.lower() == "none":
            self._rfid_removed(slot)
            return "none"
        if not self._rfid_record_ready(sample):
            self.rfid_seen_invalid.add(slot)
            self.rfid_live_slots.discard(slot)
            self.rfid_percent.pop(slot, None)
            return record.lower() or "unknown"
        if not self._rfid_should_apply(slot, sample):
            return "stale"
        self._clear_rfid_watch(slot)
        if not self._apply_rfid_record(slot, record, fields):
            self.rfid_live_slots.discard(slot)
            self.rfid_percent.pop(slot, None)
            return "invalid"
        self.rfid_live_slots.add(slot)
        self._read_rfid_remaining(slot)
        return "record"

    def _force_rfid_results(self, address, driver, mask, reason):
        selected = tuple(
            local for local in range(SLOTS_PER_BOX) if mask & (1 << local))
        tools = tuple(self._global_slot(address, local) for local in selected)
        self._info(
            self.gcode, "Reading RFID for Box %d slot %s (%s)" % (
                address, ", ".join(str(local + 1) for local in selected), reason))
        self._require_reply(
            driver.force_rfid_read(mask),
            "box %d forced RFID read" % address)
        applied = set()
        pending = set(tools)
        # Self-programmed tags can need a short settling interval after the
        # force-read command. Poll the record cache a few times before
        # reporting failure; this is still one physical RFID read operation.
        for attempt in range(3):
            after = self._require_reply(
                driver.query_rfid_records(mask, timeout=0.75),
                "box %d post-RFID query" % address)
            for local, slot in zip(selected, tools):
                if slot not in pending:
                    continue
                name = box_protocol.RFID_SLOT_NAMES[local]
                record = after.records.get(name, "").strip("\x00")
                fields = after.fields.get(name)
                if len(record) != 40 or not fields:
                    continue
                if self._apply_rfid_record(slot, record, fields):
                    applied.add(slot)
                    pending.discard(slot)
                    self.rfid_live_slots.add(slot)
                    self._read_rfid_remaining(slot)
                self._clear_rfid_watch(slot)
            if not pending:
                break
            if attempt < 2:
                self.reactor.pause(self.reactor.monotonic() + 0.35)
        for slot in sorted(pending):
            _klog("%s forced RFID result was invalid after retries",
                 self.slot_label(slot))
        return applied

    def _refresh_rfid_remaining(self):
        if self.operation_depth:
            return
        by_address = {}
        for slot in self.rfid_live_slots:
            if self.snapshot.slot_mask & (1 << slot):
                address, local = self._address_slot(slot)
                by_address[address] = by_address.get(address, 0) | (1 << local)
        for address, mask in sorted(by_address.items()):
            driver = self.drivers.get(address)
            if driver is None:
                continue
            try:
                reply = driver.query_rfid_remaining(mask, timeout=0.5)
            except Exception as exc:
                _klog("RFID remaining query failed on box %d: %s", address, exc)
                continue
            if reply is None or reply.status != box_protocol.STATUS_OK:
                continue
            for local, name in enumerate(box_protocol.RFID_SLOT_NAMES):
                slot = self._global_slot(address, local)
                if slot not in self.rfid_live_slots:
                    continue
                value = reply.values.get(name)
                self._apply_reported_remaining(slot, value)
        # The periodic CFS remaining query is also the idle-time persistence
        # path. This avoids losing a hardware-reported decrease when no print
        # is currently active.
        self._persist_rfid_estimates()

    def _external_rfid_record(self, event):
        self._apply_rfid_record(
            EXTERNAL_PROFILE_KEY, event["record_ascii"], event["fields"])

    def _request_spoolman_profile(self, slot, requested_id, record, fields):
        slot_key = self._runtime_slot_key(slot)
        display_slot = self._runtime_slot(slot_key)
        token = self.spoolman_tokens.get(slot_key, 0) + 1
        self.spoolman_tokens[slot_key] = token
        generation = self.spoolman_generation
        self._info(
            self.gcode, "%s: fetching Spoolman ID %d"
            % (self.slot_label(display_slot), requested_id))

        def worker():
            try:
                spool = _fetch_spoolman_spool(requested_id)
            except Exception:
                spool = None

            def complete(eventtime):
                if (generation != self.spoolman_generation
                        or self.spoolman_tokens.get(slot_key) != token):
                    self._info(
                        self.gcode,
                        "%s: Spoolman ID %d result discarded; slot changed"
                        % (self.slot_label(display_slot), requested_id))
                    return

                try:
                    spool_id = int(spool["id"])
                except (KeyError, TypeError, ValueError):
                    spool_id = None
                filament = spool.get("filament") if isinstance(spool, dict) else None
                filament = filament if isinstance(filament, dict) else {}
                material = str(filament.get("material") or "").strip().upper()
                color = self._normal_color(filament.get("color_hex"))
                if spool_id != requested_id or not material or color is None:
                    applied = self._apply_rfid_profile(slot_key, fields)
                    if not applied:
                        raw_code = self._clean_rfid(
                            fields.get("mat_id")).upper()
                        code, _resolved = self._resolve_rfid(raw_code)
                        self._record_unknown_rfid(
                            slot_key, code, raw_code, record, fields)
                    self._info(
                        self.gcode,
                        "%s: Spoolman ID %d unavailable or incomplete; %s"
                        % (self.slot_label(display_slot), requested_id, "RFID profile applied"
                           if applied else "RFID mapping required"))
                    return
                vendor = filament.get("vendor")
                vendor = vendor if isinstance(vendor, dict) else {}
                reserve = self._clean_rfid(fields.get("reserve")).upper()
                profile = {
                    "spoolman_id": spool_id,
                    "material": material,
                    "color": color,
                    "brand": str(vendor.get("name") or "").strip(),
                    "name": str(filament.get("name") or "").strip(),
                    "target_temp": None,
                    "filament_id": "",
                    "source": "spoolman",
                    "rfid_code": self._clean_rfid(fields.get("mat_id")).upper(),
                    "rfid_reserve": reserve,
                }
                self._ensure_material(material)
                self.unknown_rfid.pop(slot_key, None)
                self.set_profile(slot_key, profile)
                raw_code = self._clean_rfid(fields.get("mat_id")).upper()
                code, resolved = self._resolve_rfid(raw_code)
                if resolved:
                    message = "Spoolman ID %d profile applied" % spool_id
                else:
                    message = (
                        "unknown RFID code %s resolved via Spoolman ID %d"
                        % (code, spool_id))
                self._info(self.gcode, "%s: %s" % (
                    self.slot_label(display_slot), message))
                if self.snapshot.loaded_slot == display_slot:
                    self.activate_spool(display_slot)

            self.reactor.register_async_callback(complete)

        threading.Thread(
            target=worker, name="box-spoolman-rfid", daemon=True).start()

    # ------------------------------------------------------------------
    # User-facing diagnostics
    # ------------------------------------------------------------------

    def cmd_debug(self, gcmd):
        raw = bool(gcmd.get_int("RAW", 0, minval=0, maxval=1))
        self._info(gcmd, "=== BOX DEBUG DUMP ===")
        self._info(gcmd, "drivers=%s known=%s errors=%s" % (
            sorted(self.drivers), sorted(self.store.known_addresses),
            list(self.address_errors) or "none"))
        self._info(
            gcmd, "operation_depth=%d tracking=%s path=%s runout=%s origin=%s faults=%d" % (
                self.operation_depth, self.tracking_owner, self.path_owner,
                self.runout_active, self.runout_origin,
                self.fault_generation))
        try:
            live = self.read_live_state(include_topology=True)
            self._info(gcmd, "live loaded=%s present=0x%04x tracking=%s" % (
                live.loaded_slot, live.slot_mask, live.tracking))
            self._info(gcmd, "sensor=%s sensor_error=%s cut_sensor=%s" % (
                live.filament_detected, live.filament_sensor_error,
                self.get_cut_sensor_state()))
            self._info(gcmd, "status=%s state=%s temp=%sC humidity=%s%%" % (
                box_protocol.status_name(live.status_code),
                box_protocol.state_name(live.state_code), live.temp_c,
                live.humidity_pct))
        except Exception as exc:
            self._info(gcmd, "live state FAILED: %s" % exc)

        self._info(gcmd, "hotend=%s feed_pending=%s" % (
            self.hotend_filament(),
            self._runtime_slot(self.store.setting("hotend_feed_pending"))))
        self._info(gcmd, "change=%s" % self.change_engine.debug_status())
        self._info(gcmd, "clog=%s" % self._clog_status())
        for address, driver in sorted(self.drivers.items()):
            self._info(gcmd, "--- Box %d (slot indices %d-%d) ---" % (
                address, self._global_slot(address, 0),
                self._global_slot(address, 3)))
            queries = (
                ("slots", driver.query_slot_mask),
                ("hub", driver.query_hub_mask),
                ("buffer", driver.query_buffer),
                ("encoder", driver.query_encoder),
                ("state", driver.query_box_state),
                ("rfid", driver.query_rfid_records),
                ("remaining", driver.query_rfid_remaining),
            )
            for label, query in queries:
                try:
                    reply = query(timeout=0.5)
                    if reply is None:
                        detail = "NO RESPONSE"
                    elif label == "state" and reply.slot_events is not None:
                        detail = "status=%s events=%s" % (
                            box_protocol.status_name(reply.status),
                            reply.slot_events)
                    elif label == "state":
                        detail = "status=%s state=%s temp=%s humidity=%s" % (
                            box_protocol.status_name(reply.status),
                            box_protocol.state_name(reply.box_state),
                            reply.temp_c, reply.humidity_pct)
                    elif label == "rfid":
                        detail = "status=%s records=%s" % (
                            box_protocol.status_name(reply.status), reply.records)
                    elif label == "remaining":
                        detail = "status=%s values=%s" % (
                            box_protocol.status_name(reply.status), reply.values)
                    else:
                        detail = "status=%s value=%s" % (
                            box_protocol.status_name(reply.status), reply.value)
                    if raw and reply is not None:
                        detail += " raw=" + reply.raw.hex()
                except Exception as exc:
                    detail = "FAILED: %s" % exc
                self._info(gcmd, "%s: %s" % (label, detail))
        if self.unknown_rfid:
            for slot, item in sorted(
                    self.unknown_rfid.items(),
                    key=lambda entry: self._runtime_slot(entry[0])):
                self._info(gcmd, "unknown RFID in %s CODE=%s" % (
                    self.slot_label(self._runtime_slot(slot)), item["code"]))
                self._info(gcmd, "map: %s" % self._rfid_map_command(item["code"]))
        else:
            self._info(gcmd, "unknown RFID: none")
        self._info(gcmd, "=== END DEBUG DUMP ===")

    def _info(self, responder, message):
        responder.respond_info(self.CONSOLE_PREFIX + str(message))

    def _warn(self, message):
        self.gcode.respond_raw("!! " + self.CONSOLE_PREFIX + str(message))

    # ------------------------------------------------------------------
    # Box-owned cutter, cleaning, and wastebin motion
    # ------------------------------------------------------------------

    def _cut_sensor_callback(self, eventtime, state):
        self.cut_sensor_state = bool(state)

    @contextmanager
    def part_fan_override(self, speed, restore=None):
        fan = self.printer.lookup_object("fan")
        saved = fan.get_status(self.reactor.monotonic())["value"]
        restore = saved if restore is None else restore
        setter = fan.fan.set_speed_from_command
        try:
            setter(max(0.0, min(float(speed), 1.0)))
            yield
        finally:
            setter(max(0.0, min(float(restore), 1.0)))

    def nozzle_clean(self):
        toolhead = self.printer.lookup_object("toolhead")
        save_motion_limits(
            self.printer, self.gcode, "_box_clean_limits", include_gcode=True)
        try:
            self.move_to_wastebin()
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%d ACCEL=%d "
                "MINIMUM_CRUISE_RATIO=%g SQUARE_CORNER_VELOCITY=%d"
                % (CLEAN_LIMIT_VELOCITY, CLEAN_LIMIT_ACCEL,
                   CLEAN_MINIMUM_CRUISE_RATIO, CLEAN_LIMIT_SCV))
            left = self.clean_pad_left_x
            right = self.clean_pad_right_x
            front = self.clean_pad_front_y
            back = self.clean_pad_back_y
            y_steps = max(
                1, int(round((back - front) / CLEAN_SERPENTINE_Y_STEP)))
            center_x = (left + right) / 2.0
            amplitude_x = (right - left) / 2.0
            segments = y_steps * 8
            self.gcode.run_script_from_command(
                "G0 Y%g F%.0f" % (back, self.travel_velocity))
            self.gcode.run_script_from_command(
                "G0 X%g F%.0f" % (left, self.clean_velocity))
            for _index in range(CLEAN_SCRAPER_PASSES):
                self.gcode.run_script_from_command(
                    "G0 X%g F%.0f" % (
                        self.wastebin_x, self.clean_velocity))
                self.gcode.run_script_from_command(
                    "G0 X%g F%.0f" % (left, self.clean_velocity))
            self.gcode.run_script_from_command(
                "G0 X%g F%.0f" % (right, self.clean_velocity))
            for pass_index in range(self.clean_pad_passes):
                direction = -1.0 if (pass_index * y_steps) % 2 else 1.0
                for index in range(1, segments + 1):
                    progress = index / float(segments)
                    x = center_x + direction * amplitude_x * math.cos(
                        math.pi * y_steps * progress)
                    y = back + (front - back) * progress
                    self.gcode.run_script_from_command(
                        "G0 X%.3f Y%.3f F%.0f" % (
                            x, y, self.clean_velocity))
                self.gcode.run_script_from_command(
                    "G0 X%g F%.0f" % (center_x, self.clean_velocity))
                self.gcode.run_script_from_command(
                    "G0 Y%g F%.0f" % (back, self.clean_velocity))
            self.gcode.run_script_from_command(
                "G0 X%g F%.0f" % (self.wastebin_x, self.clean_velocity))
            self.gcode.run_script_from_command(
                "G0 Y%g F%.0f" % (self.wastebin_y, self.travel_velocity))
            toolhead.wait_moves()
        finally:
            restore_motion_limits(
                self.gcode, "_box_clean_limits", include_gcode=True, move=0)

    def flush_clean_snap(self, fan_after=None):
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=_box_snap_clean")
        try:
            with self.part_fan_override(self.snap_fan_speed, restore=fan_after):
                self.gcode.run_script_from_command(
                    "G4 P%d" % self.snap_fan_dwell_ms)
                self.gcode.run_script_from_command("M83")
                self.gcode.run_script_from_command(
                    "G1 E-%.1f F%.0f" % (
                        SNAP_RETRACT_MM, self.retract_velocity))
                toolhead.wait_moves()
                self.nozzle_clean()
                toolhead.wait_moves()
        finally:
            self.gcode.run_script_from_command(
                "RESTORE_GCODE_STATE NAME=_box_snap_clean MOVE=0")

    def move_to_wastebin(self):
        toolhead = self.printer.lookup_object("toolhead")
        homed = toolhead.get_status(
            self.reactor.monotonic()).get("homed_axes", "")
        # Cleaning may run inside HOME_IF_NEEDED during Z homing.
        if "x" not in homed or "y" not in homed:
            self.gcode.run_script_from_command("HOME_IF_NEEDED AXIS=XY")
        # Compare in G-code coordinates, matching the absolute moves below.
        gcode_move = self.printer.lookup_object("gcode_move")
        position = gcode_move.get_status(
            self.reactor.monotonic())["gcode_position"]
        if (abs(position[0] - self.wastebin_x) < 1.0e-6
                and abs(position[1] - self.wastebin_y) < 1.0e-6):
            return
        save_motion_limits(
            self.printer, self.gcode, "_box_wastebin_limits", include_gcode=True)
        try:
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%d ACCEL=%d "
                "MINIMUM_CRUISE_RATIO=%g SQUARE_CORNER_VELOCITY=%d"
                % (CLEAN_LIMIT_VELOCITY, CLEAN_LIMIT_ACCEL,
                   CLEAN_MINIMUM_CRUISE_RATIO, CLEAN_LIMIT_SCV))
            self.gcode.run_script_from_command(
                "G0 X%g Y%g F%.0f" % (
                    self.wastebin_x + 10.0, self.wastebin_y,
                    self.travel_velocity))
            self.gcode.run_script_from_command(
                "G0 X%g Y%g F%.0f" % (
                    self.wastebin_x, self.wastebin_y, self.travel_velocity))
        finally:
            restore_motion_limits(
                self.gcode, "_box_wastebin_limits", include_gcode=True, move=0)

    def filament_retry_motion(self, message):
        toolhead = self.printer.lookup_object("toolhead")
        homed = toolhead.get_status(
            self.reactor.monotonic()).get("homed_axes", "")
        if "x" not in homed or "y" not in homed:
            return False
        self._info(self.gcode, message)
        save_motion_limits(
            self.printer, self.gcode, "_box_filament_retry",
            include_gcode=True)
        try:
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%d ACCEL=%d "
                "MINIMUM_CRUISE_RATIO=%g SQUARE_CORNER_VELOCITY=%d"
                % (CLEAN_LIMIT_VELOCITY, CLEAN_LIMIT_ACCEL,
                   CLEAN_MINIMUM_CRUISE_RATIO, CLEAN_LIMIT_SCV))
            wastebin = "X%g Y%g" % (self.wastebin_x, self.wastebin_y)
            for move in (
                    wastebin, "Y350", "X300", "Y50", "X50", wastebin):
                self.gcode.run_script_from_command(
                    "G0 %s F%.0f" % (move, self.travel_velocity))
            toolhead.wait_moves()
        finally:
            restore_motion_limits(
                self.gcode, "_box_filament_retry",
                include_gcode=True, move=0)
        return True

    def cut_filament(self, force=False):
        detected, sensor_error = self.get_filament_sensor_state()
        if not force and sensor_error is None and not detected:
            self._info(self.gcode, "No filament loaded; skipping cut")
            return
        if self.cut_x is None:
            raise BoxError("cut_pos_x is not defined in [box]")

        toolhead = self.printer.lookup_object("toolhead")
        rail = toolhead.kin.rails[0]
        old_position_min = rail.position_min
        old_limit = toolhead.kin.limits[0]
        old_axes_min = toolhead.kin.axes_min
        lifted_z = None
        limits_saved = False
        try:
            save_motion_limits(
                self.printer, self.gcode, "_box_cut_limits", include_gcode=True)
            limits_saved = True
            lifted_z = self._lift_for_cut(toolhead)
            self.gcode.run_script_from_command("HOME_IF_NEEDED AXIS=XY")
            self._set_cut_x_limit(toolhead, min(old_position_min, self.cut_x - 5.0))
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%d ACCEL=%d "
                "MINIMUM_CRUISE_RATIO=%g SQUARE_CORNER_VELOCITY=%d"
                % (CUT_LIMIT_VELOCITY, CUT_LIMIT_ACCEL,
                   CUT_LIMIT_CRUISE, CUT_LIMIT_SCV))
            self.gcode.run_script_from_command(
                "G0 X%.2f Y%.2f F%.0f" % (
                    self.pre_cut_x, self.cut_y, self.travel_velocity))
            toolhead.wait_moves()
            if not self.get_cut_sensor_state():
                raise BoxError("Cut sensor is not in standby")

            self._info(self.gcode, "Cutting filament")
            returned = False
            for attempt in range(3):
                if attempt:
                    self._info(self.gcode, "Cut retry %d/3" % (attempt + 1))
                self.gcode.run_script_from_command(
                    "G0 X%.2f F%.0f" % (self.cut_x, self.cut_velocity))
                toolhead.wait_moves()
                self.gcode.run_script_from_command(
                    "G0 X%.2f F%.0f" % (self.pre_cut_x, self.travel_velocity))
                toolhead.wait_moves()
                if self._wait_cut_return(1.0):
                    returned = True
                self._cut_extruder_jog(
                    toolhead, -CUT_POST_RETRACT_MM, self.retract_velocity)
                if self._wait_cut_return(CUT_RETURN_WAIT):
                    returned = True
                    break
                if attempt < 2:
                    self._cut_extruder_jog(
                        toolhead, CUT_POST_RETRACT_MM, self.retract_velocity)
                    self.reactor.pause(
                        self.reactor.monotonic() + CUT_RETRY_SETTLE)

            if returned:
                return

            warning = (
                "Cut attempts exhausted and the cutter sensor did not return to "
                "standby; proceeding as configured")
            self._warn(warning)
        finally:
            rail.position_min = old_position_min
            toolhead.kin.limits[0] = old_limit
            toolhead.kin.axes_min = old_axes_min
            if lifted_z is not None:
                try:
                    self.gcode.run_script_from_command("G90")
                    self.gcode.run_script_from_command(
                        "G0 Z%.3f F%.0f" % (lifted_z, self.z_velocity))
                    toolhead.wait_moves()
                except Exception:
                    _klog("failed restoring cut Z", level=logging.exception)
            if limits_saved:
                try:
                    restore_motion_limits(
                        self.gcode, "_box_cut_limits", include_gcode=True, move=0)
                except Exception:
                    _klog("failed restoring cut motion limits", level=logging.exception)

    def _lift_for_cut(self, toolhead):
        status = toolhead.get_status(self.reactor.monotonic())
        if "z" not in status.get("homed_axes", ""):
            return None
        current = toolhead.get_position()[2]
        if current >= CUT_SAFE_Z:
            return None
        self.gcode.run_script_from_command("G90")
        self.gcode.run_script_from_command(
            "G0 Z%.3f F%.0f" % (CUT_SAFE_Z, self.z_velocity))
        toolhead.wait_moves()
        return current

    @staticmethod
    def _set_cut_x_limit(toolhead, minimum):
        kin = toolhead.kin
        kin.rails[0].position_min = minimum
        kin.limits[0] = (minimum, kin.limits[0][1])
        kin.axes_min = toolhead.Coord(
            minimum, kin.axes_min.y, kin.axes_min.z, kin.axes_min.e)

    def _wait_cut_return(self, timeout):
        deadline = self.reactor.monotonic() + timeout
        while self.reactor.monotonic() < deadline:
            if self.get_cut_sensor_state():
                return True
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        return False

    def _cut_extruder_jog(self, toolhead, distance, feed):
        extruder = self.printer.lookup_object("extruder")
        temperature = extruder.get_heater().get_status(0)["temperature"]
        if temperature <= 170:
            return False
        self.gcode.run_script_from_command("G92 E0")
        self.gcode.run_script_from_command(
            "G1 E%.4f F%.0f" % (distance, feed))
        toolhead.wait_moves()
        return True

    def pause_print(self, synchronous=False, skip_retract_wipe=False):
        if synchronous:
            if not self.pause_resume.is_paused:
                command = (
                    "PAUSE SKIP_RETRACT_WIPE=1"
                    if skip_retract_wipe else "PAUSE")
                self.gcode.run_script_from_command(command)
            return self.pause_resume.is_paused
        if (self.pause_resume.is_paused
                or self.pause_resume.pause_command_sent):
            return False
        self.pause_resume.send_pause_command()
        self.reactor.register_async_callback(
            lambda _eventtime: self.gcode.run_script("PAUSE"))
        return True

    def _clear_runout_state(self):
        self.runout_active = False
        self.runout_origin = None
        self.runout_key = None

    def _set_tracking_owner(self, address, slot, renew=False):
        owner = self.tracking_owner
        if (not renew and owner is not None and owner.address == address
                and owner.slot == slot):
            return owner
        self.tracking_epoch += 1
        owner = TrackingOwner(int(address), int(slot), self.tracking_epoch)
        self.tracking_owner = owner
        self.path_owner = owner.slot
        self.fault_episodes.pop(owner.address, None)
        self._clear_runout_state()
        return owner

    def _clear_tracking_owner(self, address=None, clear_runout=False):
        owner = self.tracking_owner
        if owner is not None and (address is None or owner.address == address):
            self.tracking_epoch += 1
            self.tracking_owner = None
            self.fault_episodes.pop(owner.address, None)
        if clear_runout:
            self._clear_runout_state()

    def _invalidate_tracking_session(self):
        self.tracking_epoch += 1
        self.tracking_owner = None
        self.path_owner = None
        self.fault_episodes.clear()
        self._clear_runout_state()

    def _set_tracking(self, driver, address, local, context):
        reply = self._require_reply(
            driver.set_tracking(local, timeout=1.0), context)
        if local is None:
            self._clear_tracking_owner(address)
        else:
            self._set_tracking_owner(
                address, self._global_slot(address, local), renew=True)
        return reply

    def note_external_source(self):
        if self.tracking_owner is None:
            self.tracking_epoch += 1
            self.fault_episodes.clear()
        self._clear_tracking_owner(clear_runout=True)
        self.path_owner = None

    def _reconcile_tracking_owner(self, replies):
        candidates = []
        for address, reply in replies.items():
            if (reply.status != box_protocol.STATUS_OK
                    or reply.box_state != box_protocol.BOX_STATE_PRINT):
                continue
            local = self._local_from_mask(reply.downstream_mask or 0)
            if local >= 0:
                candidates.append((address, self._global_slot(address, local)))

        owner = self.tracking_owner
        if owner is not None:
            reply = replies.get(owner.address)
            if ((reply is None and candidates) or (
                    reply is not None
                    and reply.status == box_protocol.STATUS_OK
                    and reply.box_state in (
                        box_protocol.BOX_STATE_IDLE,
                        box_protocol.BOX_STATE_PRELOAD,
                        box_protocol.BOX_STATE_RELOAD,
                        box_protocol.BOX_STATE_TEST))):
                self._clear_tracking_owner(owner.address)
                owner = None

        if len(candidates) != 1:
            return
        address, slot = candidates[0]
        if owner is not None and (owner.address, owner.slot) != (address, slot):
            raise BoxError(
                "CFS tracking owner %s conflicts with loaded path %s"
                % (self.slot_label(owner.slot), self.slot_label(slot)))
        self._set_tracking_owner(address, slot)

    def _fault_key(self, status, category):
        print_epoch = self.printer.lookup_object("print_stats").print_start_time
        return int(status), self.tracking_epoch, print_epoch, category

    def _fatal_episode(self, address, status=None):
        episode = self.fault_episodes.get(address)
        return bool(
            episode is not None
            and episode[-1] == "fatal"
            and (status is None or episode[0] == status))

    def _latch_fatal_fault(self, address, status, reason):
        key = self._fault_key(status, "fatal")
        if self.fault_episodes.get(address) == key:
            return False
        self.fault_episodes[address] = key
        self.fault_generation += 1
        self.last_fatal_reason = str(reason)
        stats = self.printer.lookup_object("print_stats")
        if stats.state in ("printing", "paused"):
            pending = self.change_engine.pending
            self.change_engine.block_resume(
                reason,
                target=self.path_owner,
                automatic=(None if pending is not None
                           else self.is_physical_slot(self.path_owner)))
        if stats.state == "printing":
            self.pause_print()
        return True

    def _record_command_fault(self, reply, context):
        if reply is None or reply.status not in CFS_COMMAND_FATAL_STATUSES:
            return False
        reason = box_protocol.status_detail(reply.status)
        latched = self._latch_fatal_fault(
            reply.address, reply.status, reason)
        if latched:
            _klog(
                "CFS command fault address=%d command=0x%02x status=%s "
                "context=%s raw=%s",
                reply.address, reply.command,
                box_protocol.status_name(reply.status), context,
                reply.raw.hex() if reply.raw else "none",
                level=logging.warning)
        return latched

    def check_operation_abort(self, generation):
        if generation != self.fault_generation:
            raise BoxError(
                self.last_fatal_reason or "CFS fault detected during operation")
        if (self.pause_resume.pause_command_sent
                and not self.pause_resume.is_paused):
            raise BoxError("Pause requested during box operation")

    # ------------------------------------------------------------------
    # Coherent live state and budgeted polling
    # ------------------------------------------------------------------

    @contextmanager
    def _operation(self):
        self.operation_depth += 1
        try:
            yield
        finally:
            self.operation_depth -= 1
            if self.operation_depth == 0:
                self.clog_baseline = None
                self.operation_progress = None

    def _set_operation_progress(self, kind, slot, stage):
        self.operation_progress = {"kind": kind, "slot": slot, "stage": stage}

    def _refresh_sensor_snapshot(self):
        detected, error = self.get_filament_sensor_state()
        snap = self.snapshot
        if (detected, error) != (snap.filament_detected, snap.filament_sensor_error):
            self.snapshot = replace(
                snap, filament_detected=detected, filament_sensor_error=error)

    def _operation_status(self):
        progress = self.operation_progress or {}
        request = self.change_engine.pending
        return {
            "active": bool(self.operation_depth),
            "kind": progress.get("kind"),
            "slot": progress.get("slot"),
            "stage": progress.get("stage"),
            "change_step": None if request is None else request.last_step,
            "change_target": None if request is None else request.target,
        }

    def _require_reply(self, reply, context, allowed=(0x00,)):
        if reply is None:
            raise BoxError("CFS did not respond during %s" % context)
        if reply.status not in allowed:
            self._record_command_fault(reply, context)
            raise BoxError(box_protocol.status_detail(reply.status))
        return reply

    @staticmethod
    def _local_from_mask(mask):
        if mask == 0:
            return -1
        if mask & (mask - 1):
            raise BoxError("CFS reports multiple loaded paths (mask=0x%02x)" % mask)
        return mask.bit_length() - 1

    @staticmethod
    def _global_slot(address, local_slot):
        return (address - 1) * SLOTS_PER_BOX + local_slot

    @staticmethod
    def _address_slot(global_slot):
        return global_slot // SLOTS_PER_BOX + 1, global_slot % SLOTS_PER_BOX

    def read_live_state(self, include_topology=True):
        """Read one coherent safety snapshot; callers never mix cache epochs."""
        detected, sensor_error = self.get_filament_sensor_state()
        loaded = -1
        loaded_mask = 0
        replies = {}
        for address, driver in sorted(self.drivers.items()):
            try:
                reply = self._query_box_snapshot(
                    address, driver, include_topology)
            except Exception as exc:
                _klog("box %d status query failed: %s", address, exc,
                     level=logging.warning)
                continue
            if reply is None:
                continue
            replies[address] = reply
            local = self._local_from_mask(reply.downstream_mask or 0)
            if local >= 0:
                candidate = self._global_slot(address, local)
                if loaded >= 0:
                    raise BoxError(
                        "Multiple CFS boxes report loaded paths: %s and %s"
                        % (self.slot_label(loaded), self.slot_label(candidate)))
                loaded = candidate
                loaded_mask |= 1 << candidate
        self.box_replies = replies
        if loaded >= 0:
            self.path_owner = loaded
        elif detected is False:
            self.path_owner = None
        self._reconcile_tracking_owner(replies)
        for address, reply in replies.items():
            self._observe_fault(address, reply)

        path_owner = self.path_owner
        owner_reply_missing = False
        if loaded < 0 and detected is not False and self.is_physical_slot(path_owner):
            path_address, _local = self._address_slot(path_owner)
            loaded = path_owner
            loaded_mask = 1 << path_owner
            owner_reply_missing = path_address not in replies

        if loaded < 0 and detected is True:
            loaded = self.external_slot

        topology = self._presence_mask()

        path_address = None
        if self.is_physical_slot(loaded):
            path_address, _local = self._address_slot(loaded)
        elif replies:
            path_address = min(replies)
        driver = self.drivers.get(path_address)
        state_reply = replies.get(path_address)
        encoder_reply = None
        buffer_reply = None
        if driver:
            buffer_reply = driver.query_buffer(timeout=0.5)
            if (self.is_physical_slot(loaded) and state_reply is not None
                    and state_reply.box_state == box_protocol.BOX_STATE_PRINT):
                encoder_reply = driver.query_encoder(timeout=0.5)

        status_code = state_reply.status if state_reply else None
        state_code = state_reply.box_state if state_reply else None
        owner = self.tracking_owner
        tracking = (
            owner is not None and owner.address == path_address
            and state_code == box_protocol.BOX_STATE_PRINT
            and not self._fatal_episode(path_address, status_code))
        snap = BoxSnapshot(
            data_ready=self.drivers_ready and not owner_reply_missing,
            status_code=status_code,
            state_code=state_code,
            temp_c=state_reply.temp_c if state_reply else None,
            humidity_pct=state_reply.humidity_pct if state_reply else None,
            loaded_slot=loaded,
            loaded_mask=loaded_mask,
            slot_mask=topology,
            tracking=tracking,
            filament_detected=detected,
            filament_sensor_error=sensor_error,
            path_box=path_address if self.is_physical_slot(loaded) else None,
            encoder_mm=encoder_reply.value if encoder_reply else None,
            buffer_status=buffer_reply.status if buffer_reply else None,
            buffer_state=buffer_reply.value if buffer_reply else None,
        )
        self.snapshot = snap
        return snap

    def _poll(self, eventtime):
        if self.operation_depth:
            # The CFS bus belongs to the running operation; only refresh the
            # local printhead sensor so the UI follows the filament live.
            self._refresh_sensor_snapshot()
            return eventtime + 0.25
        if eventtime - self.last_library_refresh >= LIBRARY_REFRESH:
            self.last_library_refresh = eventtime
            try:
                if self.store.refresh_library():
                    _klog("filament library reloaded from %s",
                          self.store.library_path)
            except Exception:
                _klog("filament library refresh failed", level=logging.exception)
        if not self.drivers_ready:
            return eventtime + IDLE_POLL
        include_topology = eventtime - self.last_topology_refresh >= TOPOLOGY_POLL
        try:
            snap = self.read_live_state(include_topology=include_topology)
            self._track_rfid_usage(eventtime, snap)
            lane_data = getattr(self, "lane_data", None)
            if lane_data is not None:
                lane_data.update(self._slot_statuses(snap))
            if include_topology:
                self.last_topology_refresh = eventtime
            if snap.tracking and not self.runout_active:
                self._check_clog(eventtime, snap)
            else:
                self.clog_baseline = None
            if eventtime - self.last_rfid_refresh >= RFID_REFRESH:
                self._refresh_rfid_remaining()
                self.last_rfid_refresh = eventtime
        except Exception:
            _klog("status poll failed", level=logging.exception)
            return eventtime + ERROR_BACKOFF
        return eventtime + (ACTIVE_POLL if self.snapshot.tracking else IDLE_POLL)

    def _observe_fault(self, address, reply):
        status = reply.status
        if status is None:
            return
        if status == box_protocol.STATUS_OK:
            self.fault_episodes.pop(address, None)
            return

        tracking_owner = self.tracking_owner
        tracking_owned = (
            tracking_owner is not None
            and tracking_owner.address == address)
        path_address = (
            None if self.path_owner is None
            else self._address_slot(self.path_owner)[0])
        path_owned = path_address == address

        if status == box_protocol.STATUS_RUNOUT:
            valid_runout = (
                tracking_owned
                and reply.box_state == box_protocol.BOX_STATE_PRINT)
            key = self._fault_key(
                status, "runout" if valid_runout else "advisory")
            is_new = self.fault_episodes.get(address) != key
            if valid_runout:
                self.runout_active = True
                self.runout_origin = tracking_owner.slot
                self.runout_key = (address, tracking_owner.epoch)
                if is_new:
                    self._info(
                        self.gcode,
                        "CFS box %d spool runout %s"
                        % (address, box_protocol.status_detail(status)))
            elif is_new:
                self._warn(
                    "CFS box %d reported %s without active tracking; advisory only"
                    % (address, box_protocol.status_name(status)))
            self.fault_episodes[address] = key
            return

        if (status == box_protocol.STATUS_BUFFER_REFILL_STALLED
                and self.runout_key is not None
                and self.runout_key[0] == address):
            key = self._fault_key(status, "runout")
            is_new = self.fault_episodes.get(address) != key
            if is_new:
                _klog(
                    "CFS box %d BUFFER_REFILL_STALLED during runout %s",
                    address, box_protocol.status_detail(status))
            self.fault_episodes[address] = key
            return

        fatal = path_owned and status not in CFS_ADVISORY_STATUSES
        key = self._fault_key(
            status, "fatal" if fatal else "advisory")
        if self.fault_episodes.get(address) == key:
            return
        detail = "CFS box %d %s %s state=%s" % (
            address,
            "fault" if fatal else "status",
            box_protocol.status_detail(status),
            box_protocol.state_name(reply.box_state))
        if not fatal:
            self.fault_episodes[address] = key
            self._warn(detail + "; advisory only")
            return

        if self._latch_fatal_fault(address, status, detail):
            if self.change_engine.resume_recovery is not None:
                detail = self.change_engine.recovery_notice()
            self._warn(detail)

    def _check_clog(self, eventtime, snap):
        if snap.encoder_mm is None:
            return
        toolhead = self.printer.lookup_object("toolhead")
        mcu = self.printer.lookup_object("mcu")
        extruder = toolhead.get_extruder()
        position = extruder.find_past_position(mcu.estimated_print_time(eventtime))
        if self.clog_baseline is None:
            self.clog_baseline = {
                "extruder": position, "encoder": snap.encoder_mm,
                "last_extruder": position, "last_encoder": snap.encoder_mm,
            }
            return
        self.clog_baseline["last_extruder"] = position
        self.clog_baseline["last_encoder"] = snap.encoder_mm
        extruder_delta = position - self.clog_baseline["extruder"]
        encoder_delta = abs(snap.encoder_mm - self.clog_baseline["encoder"])
        if encoder_delta > CLOG_ENCODER_RESET_MM:
            self.clog_baseline = {
                "extruder": position, "encoder": snap.encoder_mm,
                "last_extruder": position, "last_encoder": snap.encoder_mm,
            }
            return
        if extruder_delta <= CLOG_EXTRUDER_MM:
            return
        self.clog_event_count += 1
        self.last_clog = {
            "extruder_mm": extruder_delta, "encoder_mm": encoder_delta}
        detail = (
            "Likely clog: extruder moved %.1fmm while CFS encoder moved %.1fmm"
            % (extruder_delta, encoder_delta))
        self.clog_baseline = {
            "extruder": position, "encoder": snap.encoder_mm,
            "last_extruder": position, "last_encoder": snap.encoder_mm,
        }
        stats = self.printer.lookup_object("print_stats")
        if stats.state == "printing":
            target = snap.loaded_slot
            automatic = self.is_valid_slot(target)
            self.change_engine.block_resume(
                detail,
                target=target if automatic else None,
                automatic=automatic)
            self._warn(self.change_engine.recovery_notice())
            self.pause_print()
        else:
            self._warn(detail)

    # ------------------------------------------------------------------
    # Physical CFS operations
    # ------------------------------------------------------------------

    def _driver_for_slot(self, slot):
        address, local = self._address_slot(slot)
        driver = self.drivers.get(address)
        if driver is None:
            raise BoxError("Box %d is offline (%s)" % (address, self.slot_label(slot)))
        return driver, address, local

    def physical_load(self, slot, fault_generation=None):
        if not self.is_physical_slot(slot):
            raise BoxError("Physical load requires an online CFS slot")
        if fault_generation is None:
            fault_generation = self.fault_generation
        self.check_operation_abort(fault_generation)
        driver, address, local = self._driver_for_slot(slot)
        warning = None
        with self._operation():
            live = self.read_live_state(include_topology=False)
            self.check_operation_abort(fault_generation)
            if live.loaded_slot == self.external_slot:
                raise BoxError(
                    "External filament is loaded; unload it before loading %s"
                    % self.slot_label(slot))
            if (self.is_physical_slot(live.loaded_slot)
                    and live.loaded_slot != slot):
                raise BoxError(
                    "%s is already loaded; unload it before loading %s"
                    % (self.slot_label(live.loaded_slot), self.slot_label(slot)))
            if (live.loaded_slot == slot and live.filament_detected
                    and self._fatal_episode(address, live.status_code)):
                return self._recover_loaded_path(
                    slot, driver, address, local, fault_generation)
            if live.loaded_slot == slot and live.filament_detected:
                self.activate_tracking(slot)
                return True

            self._set_tracking(
                driver, address, None, "disable CFS tracking")
            self.check_operation_abort(fault_generation)
            slots = self._require_reply(
                self._query_presence(address, driver), "slot-presence query")
            self.check_operation_abort(fault_generation)
            self._info(self.gcode, "Loading %s" % self.slot_label(slot))
            self._set_operation_progress("load", slot, "preparing")
            if slot in self.rfid_pending:
                state = self.box_replies.get(address)
                if (live.loaded_slot == -1
                        and slots.value & (1 << local)
                        and state is not None
                        and state.box_state == box_protocol.BOX_STATE_IDLE
                        and not state.downstream_mask):
                    try:
                        sample = self._query_rfid_sample(slot)
                        if (sample is not None
                                and sample[0].lower() != "busy"):
                            self._force_rfid_results(
                                address, driver, 1 << local,
                                "deferred insertion")
                    except Exception as exc:
                        self._warn(
                            "%s deferred RFID read failed: %s; loading without metadata"
                            % (self.slot_label(slot), exc))
                self._clear_rfid_watch(slot)
            load_encoder_start = self._optional_encoder(driver)
            self._set_operation_progress("load", slot, "feeding_to_buffer")
            self._require_reply(
                driver.load_stage(local, 0, timeout=45.0), "load stage 0")
            self._set_operation_progress("load", slot, "feeding_to_printhead")
            self.check_operation_abort(fault_generation)

            with driver.load_session() as load_driver:
                stage4 = load_driver.load_stage(local, 4, timeout=1.0)
                if stage4 is not None and stage4.status != 0x00:
                    raise BoxError(box_protocol.status_detail(stage4.status))
                self.check_operation_abort(fault_generation)

                deadline = self.reactor.monotonic() + LOAD_TIMEOUT
                sensor_confirmed = False
                stall_retried = False
                while self.reactor.monotonic() < deadline:
                    self.check_operation_abort(fault_generation)
                    detected, error = self.get_filament_sensor_state()
                    if error:
                        raise BoxError(
                            "Printhead filament sensor is unavailable during load: %s"
                            % error)
                    if detected:
                        sensor_confirmed = True
                        break

                    stage5 = load_driver.load_stage(local, 5, timeout=1.0)
                    if stage5 is None:
                        self.reactor.pause(
                            self.reactor.monotonic() + STAGE5_POLL)
                        continue
                    if stage5.status in (0x0A, 0x0B):
                        self._record_command_fault(stage5, "load stage 5")
                        raise BoxError(box_protocol.status_detail(stage5.status))
                    if stage5.status == box_protocol.STATUS_ODOMETER_STALLED:
                        detected, error = self.get_filament_sensor_state()
                        if not error and detected:
                            sensor_confirmed = True
                            break
                        if (not stall_retried
                                and self.filament_retry_motion(
                                    "CFS odometer stalled; moving toolhead and retrying load")):
                            stall_retried = True
                            self.check_operation_abort(fault_generation)
                            stage4 = load_driver.load_stage(
                                local, 4, timeout=1.0)
                            if stage4 is not None and stage4.status != 0x00:
                                raise BoxError(
                                    box_protocol.status_detail(stage4.status))
                            continue
                        self._record_command_fault(stage5, "load stage 5")
                        raise BoxError(box_protocol.status_detail(stage5.status))
                    if stage5.status != 0x00:
                        self._record_command_fault(stage5, "load stage 5")
                        raise BoxError(box_protocol.status_detail(stage5.status))
                    self.reactor.pause(
                        self.reactor.monotonic() + STAGE5_POLL)

                if not sensor_confirmed:
                    try:
                        timeout_reply = load_driver.load_stage(
                            local, 6, timeout=5.0)
                        self._record_command_fault(
                            timeout_reply, "load timeout stop")
                    finally:
                        raise BoxError(
                            "Load timed out before printhead sensor arrival")

                self.check_operation_abort(fault_generation)
                self._set_operation_progress("load", slot, "seating")
                self._require_reply(
                    load_driver.load_stage(local, 6, timeout=5.0),
                    "load stage 6")
                self.check_operation_abort(fault_generation)
            stage7 = driver.load_stage(local, 7, timeout=2.0)
            if stage7 is None:
                warning = (
                    "CFS did not respond to the final load nudge; printhead "
                    "sensor was already confirmed")
            elif stage7.status == 0x0E:
                warning = (
                    "CFS final nudge did not reach 3mm; printhead sensor was "
                    "already confirmed")
            elif stage7.status != 0x00:
                raise BoxError(box_protocol.status_detail(stage7.status))

            self.check_operation_abort(fault_generation)
            self._set_operation_progress("load", slot, "verifying")
            self.activate_tracking(slot)
            final = self._wait_for_state(
                slot, True, True, fault_generation=fault_generation)
            if not (final.loaded_slot == slot and final.filament_detected and final.tracking):
                raise BoxError(
                    "%s did not reach verified loaded state" % self.slot_label(slot))
            self.runout_active = False
            self.runout_origin = None
            self.mark_hotend_feed_pending(slot)
        if warning:
            self._info(self.gcode, warning)
        self._report_encoder_delta(driver, load_encoder_start, "fed")
        return False

    def _recover_loaded_path(
            self, slot, driver, address, local, fault_generation):
        self._set_tracking(
            driver, address, None, "disable CFS tracking for recovery")
        self.check_operation_abort(fault_generation)
        self._require_reply(
            driver.load_stage(local, 6, timeout=5.0),
            "loaded-path recovery buffer validation")
        self.check_operation_abort(fault_generation)
        self.activate_tracking(slot)
        final = self._wait_for_state(
            slot, True, True, fault_generation=fault_generation)
        if (final.status_code != box_protocol.STATUS_OK
                or final.loaded_slot != slot
                or not final.filament_detected
                or not final.tracking):
            raise BoxError(
                "%s recovery did not reach verified loaded state" % self.slot_label(slot))
        return True

    def physical_unload(self, allow_extruder_retract=True,
                        fault_generation=None):
        if fault_generation is None:
            fault_generation = self.fault_generation
        self.check_operation_abort(fault_generation)
        previous_sensor = self.filament_sensor_enabled()
        success = False
        saved_gcode = False
        retract_toolhead = None
        with self._operation():
            try:
                live = self.read_live_state(include_topology=False)
                self.check_operation_abort(fault_generation)
                if live.loaded_slot in (-1, self.external_slot):
                    success = True
                    return
                if not self.is_physical_slot(live.loaded_slot):
                    raise BoxError("CFS loaded-slot state is unavailable")
                slot = live.loaded_slot
                driver, address, local = self._driver_for_slot(slot)
                self._info(self.gcode, "Unloading %s" % self.slot_label(slot))
                self._set_operation_progress("unload", slot, "preparing")
                self.disable_filament_sensor()
                self._set_tracking(
                    driver, address, None, "disable CFS tracking")
                self.check_operation_abort(fault_generation)
                unload_encoder_start = self._optional_encoder(driver)
                self._require_reply(
                    self._query_presence(address, driver), "unload slot query")
                self.check_operation_abort(fault_generation)

                if (allow_extruder_retract
                        and live.filament_detected is not False
                        and self._extruder_can_move()):
                    self.gcode.run_script_from_command(
                        "SAVE_GCODE_STATE NAME=_box_unload_retract")
                    saved_gcode = True
                    self.gcode.run_script_from_command("M83")
                    retract_toolhead = self.printer.lookup_object("toolhead")
                    self._set_operation_progress(
                        "unload", slot, "retracting_from_printhead")
                    self._buffer_retract(driver, "before extruder retract")
                    total = 0.0
                    previous_encoder = self._encoder(driver)
                    for attempt in range(UNLOAD_RETRIES + 1):
                        self.check_operation_abort(fault_generation)
                        amount = UNLOAD_RETRACT_MM if attempt == 0 else UNLOAD_RETRY_MM
                        self.gcode.run_script_from_command(
                            "G1 E-%.0f F%.0f" % (
                                amount, self.retract_velocity))
                        retract_toolhead.wait_moves()
                        self.check_operation_abort(fault_generation)
                        total += amount
                        self._buffer_retract(driver, "after extruder retract")
                        current = self._encoder(driver)
                        detected, error = self.get_filament_sensor_state()
                        if error is None and detected is False:
                            break
                        if previous_encoder is not None and current is not None:
                            delta = abs(current - previous_encoder)
                            threshold = min(ENCODER_CLEAR_MM, amount * 0.8)
                            if total >= UNLOAD_CLEAR_MIN_MM and delta < threshold:
                                break
                        previous_encoder = current
                    self.check_operation_abort(fault_generation)
                    self.gcode.run_script_from_command(
                        "G1 E-%.0f F%.0f"
                        % (UNLOAD_RETRACT_MM, self.retract_velocity))
                    retract_toolhead.flush_step_generation()
                    self.gcode.run_script_from_command(
                        "RESTORE_GCODE_STATE NAME=_box_unload_retract MOVE=0")
                    saved_gcode = False
                self._set_operation_progress("unload", slot, "retracting_to_cfs")
                for attempt in range(2):
                    try:
                        path_reply = driver.unload_path(
                            local, timeout=PATH_RETRACT_TIMEOUT)
                    finally:
                        if retract_toolhead is not None:
                            retract_toolhead.wait_moves()
                    if (path_reply is None
                            or path_reply.status
                            != box_protocol.STATUS_UNLOAD_MOTOR_BLOCKED
                            or attempt):
                        break
                    if not self.filament_retry_motion(
                            "CFS unload motor blocked; moving toolhead and retrying unload"):
                        break
                    self.check_operation_abort(fault_generation)
                    self._set_tracking(
                        driver, address, None,
                        "disable CFS tracking for unload retry")
                    self.check_operation_abort(fault_generation)
                    self._require_reply(
                        self._query_presence(address, driver),
                        "unload retry slot query")
                    self.check_operation_abort(fault_generation)
                self._require_reply(path_reply, "loaded-path retract")
                self.check_operation_abort(fault_generation)
                self._set_operation_progress("unload", slot, "verifying")
                final = self._wait_for_state(
                    -1, False, False, fault_generation=fault_generation)
                if final.filament_sensor_error:
                    raise BoxError(
                        "Unable to verify unload: %s" % final.filament_sensor_error)
                if final.filament_detected:
                    raise BoxError(
                        "CFS retracted the path but the printhead sensor still detects filament")
                if final.loaded_slot != -1:
                    raise BoxError(
                        "CFS retracted the path but still reports loaded slot %s"
                        % final.loaded_slot)
                self.check_operation_abort(fault_generation)
                self.snapshot = replace(
                    final, loaded_slot=-1, loaded_mask=0, tracking=False)
                self.path_owner = None
                self._clear_runout_state()
                self.clear_hotend_feed_pending(slot)
                success = True
            finally:
                try:
                    if saved_gcode:
                        self.gcode.run_script_from_command(
                            "RESTORE_GCODE_STATE NAME=_box_unload_retract MOVE=0")
                finally:
                    if success or previous_sensor:
                        self.enable_filament_sensor()
                    else:
                        self.disable_filament_sensor()
        self._report_encoder_delta(driver, unload_encoder_start, "retracted")

    def retract_for_cut(self, distance, fault_generation=None):
        if fault_generation is None:
            fault_generation = self.fault_generation
        self.check_operation_abort(fault_generation)
        live = self.read_live_state(include_topology=False)
        self.check_operation_abort(fault_generation)
        if not self.is_physical_slot(live.loaded_slot):
            raise BoxError("No physical CFS slot is loaded")
        driver, address, _local = self._driver_for_slot(live.loaded_slot)
        remaining = float(distance)
        with self._operation():
            self.gcode.run_script_from_command(
                "SAVE_GCODE_STATE NAME=_box_retract_cut")
            try:
                self.gcode.run_script_from_command("M83")
                self._set_tracking(
                    driver, address, None, "disable CFS tracking")
                while remaining > 0:
                    self.check_operation_abort(fault_generation)
                    self._buffer_retract(driver, "cut retract")
                    amount = min(15.0, remaining)
                    self.gcode.run_script_from_command(
                        "G1 E-%.4f F%.0f" % (amount, self.retract_velocity))
                    self.printer.lookup_object("toolhead").wait_moves()
                    self.check_operation_abort(fault_generation)
                    remaining -= amount
            finally:
                self.gcode.run_script_from_command(
                    "RESTORE_GCODE_STATE NAME=_box_retract_cut MOVE=0")

    def _buffer_retract(self, driver, context):
        self._require_reply(
            driver.unload_buffer(timeout=BUFFER_RETRACT_TIMEOUT),
            "buffer retract (%s)" % context)

    def activate_tracking(self, slot):
        driver, address, local = self._driver_for_slot(slot)
        self._set_tracking(
            driver, address, local, "enable CFS tracking")
        self.enable_filament_sensor()
        self.snapshot = replace(
            self.snapshot, loaded_slot=slot, loaded_mask=1 << slot,
            tracking=True, filament_detected=True,
            filament_sensor_error=None)

    def _wait_for_state(self, loaded_slot, detected, tracking,
                        fault_generation=None):
        if fault_generation is None:
            fault_generation = self.fault_generation
        deadline = self.reactor.monotonic() + STATE_TIMEOUT
        last = self.read_live_state(include_topology=False)
        while True:
            self.check_operation_abort(fault_generation)
            episode = self.fault_episodes.get(last.path_box)
            if episode is not None and episode[-1] == "fatal":
                raise BoxError(
                    "CFS box %d remains in fatal state %s"
                    % (last.path_box, box_protocol.status_detail(episode[0])))
            if (last.loaded_slot == loaded_slot
                    and last.filament_detected == detected
                    and last.tracking == tracking):
                return last
            if self.reactor.monotonic() >= deadline:
                return last
            self.reactor.pause(self.reactor.monotonic() + STATE_POLL)
            last = self.read_live_state(include_topology=False)

    def _encoder(self, driver):
        reply = driver.query_encoder(timeout=0.5)
        return reply.value if reply is not None and reply.status == 0x00 else None

    def _optional_encoder(self, driver):
        try:
            return self._encoder(driver)
        except Exception:
            return None

    def _report_encoder_delta(self, driver, start, action):
        try:
            end = self._encoder(driver)
            if start is not None and end is not None:
                self._info(self.gcode, "CFS %s %.2f m of filament." % (
                    action, abs(end - start) / 1000.0))
        except Exception:
            pass

    def _extruder_can_move(self):
        try:
            return self.printer.lookup_object(
                "extruder").get_heater().can_extrude
        except Exception:
            return False


def load_config(config):
    return Box(config)
