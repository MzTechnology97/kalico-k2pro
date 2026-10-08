# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""K2-OpenHost: remaining-filament estimates of CFS RFID spools.

BoxRfidEstimates is a mixin of Box (self is the Box): spool identity keys,
the estimate seeded from the tag, the CFS percentage and the profile or
material length, its update during printing (CFS encoder, else
print_stats), persistence by spool identity and restore at startup. The
methods moved here from box.py unchanged.
"""

import logging

from extras import box_protocol
from extras.box_addr import MAX_ADDRESSES
from extras.box_materials import (
    DEFAULT_SPOOL_LENGTH_M, FALLBACK_SPOOL_LENGTH_M, clean_nominal_length,
    default_spool_length_m)

SLOTS_PER_BOX = box_protocol.SLOTS_PER_BOX

# How often the CFS remaining percentage is polled and estimates saved (s).
RFID_REFRESH = 30.0
# K2-OpenHost: tags written with the generic serial (000000/000001) carry no
# spool identity, so a new spool identical to a used one inherits its saved
# estimate. Below this percentage the tag read prints how to declare it new.
RFID_LOW_ESTIMATE_HINT = 5.0


def _klog(msg, *args, level=logging.info):
    level("box: " + msg, *args)


class BoxRfidEstimates:
    """Remaining-filament estimates of RFID spools (mixin of Box)."""

    def cmd_rfid_remaining_diag(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX - 1)
        if slot is None or not self.is_physical_slot(slot):
            raise gcmd.error("[BOX]: SLOT must select a physical CFS slot")
        address, local = self._address_slot(slot)
        driver = self.drivers.get(address)
        if driver is None:
            raise gcmd.error("[BOX]: CFS Box driver is not ready")
        try:
            reply = driver.query_rfid_remaining(1 << local, timeout=1.0)
        except Exception as exc:
            raise gcmd.error(
                "[BOX]: %s remaining query failed: %s"
                % (self.slot_label(slot), exc))
        if reply is None:
            raise gcmd.error(
                "[BOX]: %s remaining query timed out"
                % self.slot_label(slot))
        values = getattr(reply, "values", {}) or {}
        value = values.get(box_protocol.RFID_SLOT_NAMES[local])
        self._info(
            gcmd,
            "%s: CFS RFID remaining status=%s payload=%s values=%s selected=%s"
            % (self.slot_label(slot), getattr(reply, "status_name", reply.status),
               reply.payload.hex().upper(), values, value))

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

    def _resume_rfid_polling(self, slot):
        """Keep polling the CFS remaining percentage of a restored RFID bay.

        Only rfid_live_slots are polled, and a restart empties it: without
        this the CFS percentage came back only after a reread or a new
        insertion. A bay with an RFID profile and a spool key is managed by
        its tag, as Mainsail already shows it. The query is passive; 255
        (no value) changes nothing.
        """
        if self.profile(slot).get("source") != "rfid":
            return False
        if not self._rfid_slot_keys().get(str(self._runtime_slot_key(slot))):
            return False
        self.rfid_live_slots.add(slot)
        return True

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
            "length_source": saved.get("length_source"),
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
            total_m = float(self._clean_rfid(fields.get("len")))
        except (TypeError, ValueError):
            total_m = 0.0
        total_mm = float(total_m * 1000.0) if total_m > 0.0 else None
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
            if spool.get("length_source"):
                persisted[key]["length_source"] = spool["length_source"]
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
        params = (
            gcmd.get_command_parameters()
            if hasattr(gcmd, "get_command_parameters")
            else getattr(gcmd, "params", {}))
        total_raw = params.get("TOTAL_M")
        total_m = None
        if total_raw not in (None, ""):
            try:
                total_m = float(total_raw)
            except (TypeError, ValueError):
                raise gcmd.error("[BOX]: TOTAL_M must be numeric")
            if not 1.0 <= total_m <= 10000.0:
                raise gcmd.error("[BOX]: TOTAL_M must be between 1 and 10000 metres")
        if slot is None or not self.is_physical_slot(slot):
            raise gcmd.error("[BOX]: SLOT must select a physical CFS slot")
        spool = self.rfid_spools.get(slot)
        if not spool or not spool.get("key"):
            raise gcmd.error(
                "[BOX]: T%d has no decoded RFID spool identity; read the "
                "tag first (_BOX_RFID_READ_SLOT SLOT=%d)" % (slot, slot))
        if total_m is not None:
            spool["total_mm"] = float(total_m) * 1000.0
        if not spool.get("total_mm"):
            raise gcmd.error(
                "[BOX]: T%d has no known nominal length. Set NOMINAL_LENGTH_M "
                "in its filament profile or use TOTAL_M=<metres> here."
                % slot)
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
        """Persist RFID spool consumption, preferring the CFS encoder.

        During BOX_STATE_PRINT the CFS exposes its own path encoder in mm.
        That is a better source for physical spool draw than slicer/extruder
        accounting, especially around buffer refills.  print_stats remains a
        fallback when the CFS encoder is not available.
        """
        stats = self.printer.lookup_object("print_stats", None)
        if stats is None:
            return
        status = stats.get_status(eventtime)
        state = status.get("state")
        try:
            used = float(status.get("filament_used", 0.0))
        except (TypeError, ValueError):
            used = 0.0
        try:
            encoder = (
                float(snap.encoder_mm)
                if snap.encoder_mm is not None else None)
        except (TypeError, ValueError):
            encoder = None

        if state != "printing":
            if self.rfid_last_print_state == "printing":
                self._persist_rfid_estimates(force=True)
            self.rfid_last_filament_used = None
            self.rfid_last_encoder_mm = None
            self.rfid_last_usage_source = None
            self.rfid_last_print_state = state
            self.rfid_last_usage_slot = None
            return

        slot = snap.loaded_slot
        physical_slot = (
            isinstance(slot, int) and slot >= 0
            and bool(getattr(self, "drivers", {}))
            and self.is_physical_slot(slot))
        source = (
            "cfs_encoder"
            if (physical_slot and snap.tracking and encoder is not None)
            else "print_stats")
        value = encoder if source == "cfs_encoder" else used

        first = (
            self.rfid_last_print_state != "printing"
            or slot != self.rfid_last_usage_slot
            or source != self.rfid_last_usage_source)
        if first:
            self.rfid_last_filament_used = used
            self.rfid_last_encoder_mm = encoder
            self.rfid_last_print_state = state
            self.rfid_last_usage_slot = slot
            self.rfid_last_usage_source = source
            return

        if source == "cfs_encoder":
            previous = self.rfid_last_encoder_mm
            self.rfid_last_encoder_mm = encoder
            self.rfid_last_filament_used = used
        else:
            previous = self.rfid_last_filament_used
            self.rfid_last_filament_used = used
            self.rfid_last_encoder_mm = encoder
        self.rfid_last_print_state = state
        if previous is None:
            return
        delta = value - previous

        # A negative CFS delta is reverse motion or a counter reset; neither
        # consumes new spool. Large jumps are treated as counter resets.
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
        spool["usage_source"] = source
        self.rfid_percent[slot] = 100.0 * remaining / spool["total_mm"]
        self.rfid_estimate_dirty = True
        if eventtime - self.last_rfid_estimate_save >= RFID_REFRESH:
            self._persist_rfid_estimates()
            self.last_rfid_estimate_save = eventtime

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

    def _third_party_length_m(self, data, filament):
        """(nominal spool length in metres, source) of a third-party tag.

        The tag's own length when the decoder has it, else the filament
        profile's nominal_length_m (editable in Mainsail), else the reference
        length of the profile's material (DEFAULT_SPOOL_LENGTH_M).
        """
        length = clean_nominal_length(data.get("filament_length_m"))
        if length:
            return length, "tag"
        return self._profile_length_m(filament, data.get("material"))

    @staticmethod
    def _profile_length_m(filament, material=None):
        """(length, "profile"|"material") of a filament profile."""
        length = clean_nominal_length(
            (filament or {}).get("nominal_length_m"))
        if length:
            return length, "profile"
        return default_spool_length_m(
            (filament or {}).get("material") or material), "material"

    def spool_length_defaults(self):
        """Reference length per known material, for the Mainsail editor."""
        materials = set(self.store.materials)
        materials.update(
            str(value.get("material") or "").strip().upper()
            for value in self.store.filaments_status.values())
        materials.update(DEFAULT_SPOOL_LENGTH_M)
        defaults = {name: default_spool_length_m(name)
                    for name in materials if name}
        defaults["*"] = FALLBACK_SPOOL_LENGTH_M
        return defaults

    def _refresh_spool_lengths(self, filament_id):
        """A profile's length changed: its loaded third-party spools follow.

        Spools whose length comes from the profile or its material take the
        new one at the same percentage; tag lengths are left alone.
        """
        key = str(filament_id or "").strip().upper()
        filament = self.store.filament(key)
        spools = getattr(self, "rfid_spools", None) or {}
        if filament is None or not spools:
            return 0
        changed = 0
        for slot, spool in spools.items():
            if spool.get("length_source") not in ("profile", "material"):
                continue
            if str(self.profile(slot).get("filament_id") or "").upper() != key:
                continue
            length, source = self._profile_length_m(filament)
            new_total = float(length) * 1000.0
            total = spool.get("total_mm")
            remaining = spool.get("remaining_mm")
            spool["length_source"] = source
            if total == new_total:
                continue
            if total and remaining is not None:
                spool["remaining_mm"] = new_total * float(remaining) / total
                self.rfid_percent[slot] = (
                    100.0 * spool["remaining_mm"] / new_total)
            spool["total_mm"] = new_total
            changed += 1
        if changed:
            self.rfid_estimate_dirty = True
            self._persist_rfid_estimates(force=True)
        return changed

    def _start_third_party_estimate(self, slot, length_source=None):
        """Seed a third-party spool's estimate like a Creality one.

        A saved estimate for this tag (by UID) is kept. The CFS percentage
        is read now, not at the next 30 s refresh, and caps the estimate. A
        spool with neither starts full, as a new generic Creality spool does.
        Without a length (none so far) the slot shows the CFS percentage.
        """
        spool = self.rfid_spools.get(slot)
        if not spool or not spool.get("total_mm"):
            self.rfid_percent.pop(slot, None)
            self._read_rfid_remaining(slot)
            return
        # Profile/material lengths follow later edits of the profile.
        spool["length_source"] = length_source
        self._read_rfid_remaining(slot)
        if spool.get("remaining_mm") is None:
            spool["remaining_mm"] = float(spool["total_mm"])
            self.rfid_percent[slot] = 100.0
            self.rfid_estimate_dirty = True
            self._persist_rfid_estimates(force=True)

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
