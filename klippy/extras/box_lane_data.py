# Copyright (C) 2026 MzTechnology97 and contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Publish CFS slots to Moonraker's ``lane_data`` database namespace.

``lane_data`` is the convention AFC introduced and Happy Hare adopted for
filament changers on Moonraker. Official OrcaSlicer (2.3.2 and later) reads
it to fill its filament list with "Sync" on Moonraker printers, so the CFS
slots reach the slicer without a modified OrcaSlicer build.

One lane per occupied physical CFS slot: ``lane`` is the tool number (the
0-based physical slot, which Orca groups four by four like a CFS unit).
Writes run in a background thread and only send what changed, so the
reactor never waits for Moonraker.
"""

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

NAMESPACE = "lane_data"
MOONRAKER_URL = "http://127.0.0.1:7125"
TIMEOUT = 2.0
RETRY_INTERVAL = 30.0


def lane_key(slot_index):
    return "lane%d" % (int(slot_index) + 1)


def lanes_from_slots(slots):
    """lane_data entries for the occupied physical slots with a material."""
    lanes = {}
    for slot in slots:
        if slot.get("external") or not slot.get("present"):
            continue
        material = str(slot.get("material") or "").strip()
        if not material:
            continue
        index = int(slot["index"])
        color = str(slot.get("color") or "").strip().upper()
        entry = {
            "lane": str(index),
            "material": material,
            "color": color if color.startswith("#") else ("#" + color if color else ""),
            "nozzle_temp": slot.get("target_temp"),
            "bed_temp": None,
            "spool_id": slot.get("spoolman_id"),
            # Not read by OrcaSlicer yet; kept for preset matching later.
            "name": str(slot.get("name") or ""),
            "vendor": str(slot.get("brand") or ""),
            "filament_id": str(slot.get("filament_id") or ""),
            "scan_time": "",
        }
        lanes[lane_key(index)] = entry
    return lanes


class MoonrakerDatabase:
    """Minimal Moonraker database client (localhost, trusted client)."""

    def __init__(self, base_url=MOONRAKER_URL, timeout=TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method, query, body=None):
        url = "%s/server/database/item?%s" % (self.base_url, urllib.parse.urlencode(query))
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode() or "{}")

    def keys(self):
        try:
            result = self._request("GET", {"namespace": NAMESPACE})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return set()
            raise
        value = (result.get("result") or {}).get("value") or {}
        return set(value) if isinstance(value, dict) else set()

    def post(self, key, value):
        self._request("POST", {}, {"namespace": NAMESPACE, "key": key, "value": value})

    def delete(self, key):
        try:
            self._request("DELETE", {"namespace": NAMESPACE, "key": key})
        except urllib.error.HTTPError as exc:
            # Already gone: another client (HelixScreen keeps its slot
            # overrides in lane_data too) removed it. Nothing to undo.
            if exc.code != 404:
                raise


class LaneDataPublisher:
    """Keeps ``lane_data`` equal to the latest CFS slots, off the reactor."""

    def __init__(self, database=None, log=None):
        self.database = database or MoonrakerDatabase()
        self.log = log or logging.getLogger("box_lane_data")
        self._lock = threading.Lock()
        self._wanted = None
        self._published = None   # None until the namespace was read once
        self._event = threading.Event()
        self._failing = False
        self._thread = None
        self._stop = False

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="box-lane-data", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop = True
        self._event.set()

    def update(self, slots):
        """Called from the reactor with the current slot statuses."""
        lanes = lanes_from_slots(slots)
        with self._lock:
            if lanes == self._wanted:
                return
            self._wanted = lanes
        self._event.set()

    def sync_once(self):
        """One synchronisation pass; returns True when lane_data is current."""
        with self._lock:
            wanted = None if self._wanted is None else dict(self._wanted)
        if wanted is None:
            return True
        if self._published is None:
            # Remove lanes left by an earlier run (or another changer).
            self._published = {key: None for key in self.database.keys()}
        for key in sorted(set(self._published) - set(wanted)):
            self.database.delete(key)
            self._published.pop(key, None)
        for key, value in sorted(wanted.items()):
            if self._published.get(key) != value:
                self.database.post(key, value)
                self._published[key] = value
        with self._lock:
            return wanted == self._wanted

    def _run(self):
        while not self._stop:
            self._event.wait(RETRY_INTERVAL if self._failing else None)
            self._event.clear()
            if self._stop:
                return
            try:
                if not self.sync_once():
                    self._event.set()
                if self._failing:
                    self.log.info("box: lane_data publishing restored")
                self._failing = False
            except Exception as exc:
                if not self._failing:
                    self.log.warning("box: lane_data not published (%s); retrying", exc)
                self._failing = True
                self._published = None
