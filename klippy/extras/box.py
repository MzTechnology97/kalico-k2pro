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
    "_BOX_SLOT_PA_SET",
    "_BOX_FILAMENT_SET",
    "_BOX_FILAMENT_DELETE",
    "_BOX_SLOT_ASSIGN",
    "_BOX_RFID_READ_SLOT",
    "BOX_RFID_SCAN",
    "BOX_INFO_REFRESH",
    "_BOX_SET_RUNOUT_SWAP",
    "_BOX_SET_CLOG_DETECTION",
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

# Maximum volumetric flow (mm3/s) of the generic materials, from OrcaSlicer's
# "Generic <material> @K2 Pro-all" profiles. Used only when no filament, slot
# or material setting gives one. Their pressure advance is not imported.
DEFAULT_MAX_FLOW = {
    "ABS": 16.0, "ASA": 12.0, "BVOH": 6.0, "PA": 8.0, "PA-CF": 8.0,
    "PA6-CF": 8.0, "PA612-CF": 8.0, "PAHT-CF": 3.0, "PC": 16.0, "PET": 8.0,
    "PET-CF": 8.0, "PETG": 16.0, "PETG-CF": 10.0, "PETG-GF": 10.0,
    "PLA": 12.0, "PLA-CF": 18.0, "PLA-SILK": 10.0, "PP": 10.0, "PVA": 6.0,
    "TPU": 2.0,
}

# set_material(): leave a field as it is
_KEEP = object()

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

# The CFS refills the buffer in ~25-30mm chunks, so any single refill moves
# the encoder past this and proves filament is flowing.
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
# Toolhead moves between the wastebin visits of a filament retry.
DEFAULT_FILAMENT_RETRY_MOVES = "Y350, X300, Y50, X50"

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


def _parse_retry_moves(text, error):
    """Parse comma-separated XY moves such as "Y350, X300 Y50"."""
    moves = []
    for item in str(text).split(","):
        move = {}
        try:
            words = item.upper().split()
            if not words:
                raise ValueError(item)
            for word in words:
                axis, value = word[0].lower(), float(word[1:])
                if axis not in "xy" or axis in move or not math.isfinite(value):
                    raise ValueError(item)
                move[axis] = value
        except ValueError:
            raise error(
                "Invalid filament_retry_moves entry %r" % (item.strip(),))
        moves.append(move)
    return tuple(moves)


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
            for field, clean in (
                    ("pressure_advance", BoxStore._clean_pressure_advance),
                    ("max_flow", BoxStore._clean_max_flow)):
                cleaned = clean(value.get(field))
                if cleaned is not None:
                    result[name][field] = cleaned
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
    def _clean_max_flow(value):
        """Maximum volumetric flow in mm3/s, or None."""
        if value in (None, ""):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if not 0.1 <= value <= 200.0:
            return None
        return round(value, 2)

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
        max_flow = value.get("max_flow")
        if max_flow in (None, ""):
            max_flow = None
        else:
            max_flow = BoxStore._clean_max_flow(max_flow)
            if max_flow is None:
                raise BoxError(
                    "Invalid max_flow for filament %s" % filament_id)
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
            "max_flow": max_flow,
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
            profile["max_flow"] = clean.get("max_flow")
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

    def set_material(self, name, target=None, pressure_advance=_KEEP,
                     max_flow=_KEEP):
        """Create or update a generic material; fields left as _KEEP stay."""
        key = str(name).strip().upper()
        if not key:
            raise ValueError("material is required")
        entry = dict(self.data["materials"].get(key) or {})
        if target is not None:
            entry["target_temp"] = int(target)
        elif entry.get("target_temp") is None:
            raise ValueError("a new material needs a target temperature")
        for field, value, clean in (
                ("pressure_advance", pressure_advance,
                 self._clean_pressure_advance),
                ("max_flow", max_flow, self._clean_max_flow)):
            if value is _KEEP:
                continue
            value = clean(value)
            if value is None:
                entry.pop(field, None)
            else:
                entry[field] = value
        self.data["materials"][key] = entry
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
            "max_flow": self._clean_max_flow(value.get("max_flow")),
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
            "max_flow": self._clean_max_flow(profile.get("max_flow")),
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