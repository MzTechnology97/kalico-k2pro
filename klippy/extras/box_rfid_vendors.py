# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""K2-OpenHost: third-party RFID tags in the CFS (Bambu, QIDI, ...).

BoxRfidVendors is a mixin of Box (self is the Box): the profile built from
a Bambu or generic third-party tag and its library match, the automatic and
manual runs of the vendor decoders (box_rfid_fallback), the known-tag fast
path, _BOX_RFID_ASSOCIATE and _BOX_RFID_FALLBACK_CACHE. The methods moved
here from box.py unchanged; the read claim (acquire_rfid_read) stays in
box.py with the read cycle.
"""

import logging

from extras import box_protocol
from extras.box_addr import MAX_ADDRESSES

SLOTS_PER_BOX = box_protocol.SLOTS_PER_BOX


def _klog(msg, *args, level=logging.info):
    level("box: " + msg, *args)


class BoxRfidVendors:
    """Third-party RFID tags: Bambu, MIFARE vendors (mixin of Box)."""

    def cmd_rfid_associate(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX - 1)
        filament_id = self._param(gcmd, "FILAMENT_ID")
        if slot is None or not self.is_physical_slot(slot):
            raise gcmd.error("[BOX]: SLOT must select a physical CFS slot")
        if not filament_id:
            raise gcmd.error("[BOX]: FILAMENT_ID is required")
        filament = self.store.filament(filament_id)
        if filament is None:
            raise gcmd.error("[BOX]: Unknown filament %s" % filament_id)

        slot_key = self._runtime_slot_key(slot)
        unknown = self.unknown_rfid.get(slot_key) or {}
        current = self.profile(slot)
        code = str(
            unknown.get("raw_code") or unknown.get("code")
            or current.get("rfid_code") or "").strip().upper()
        if not code:
            raise gcmd.error(
                "[BOX]: %s has no decoded RFID identity to associate; read the tag first"
                % self.slot_label(slot))

        normalized = self.store.set_rfid_mapping(code, {
            "material": filament["material"],
            "brand": filament.get("brand", ""),
            "name": filament.get("name", ""),
            "target_temp": filament.get("target_temp"),
            "filament_id": filament["id"],
        })

        unknown_fields = unknown.get("fields", {}) if isinstance(unknown, dict) else {}
        color = (
            self._normal_color(unknown_fields.get("color"))
            or self._normal_color(current.get("color"))
            or self._normal_color(filament.get("color"))
            or "#808080")
        profile = {
            "material": filament["material"],
            "color": color,
            "brand": filament.get("brand", ""),
            "name": filament.get("name", ""),
            "target_temp": filament.get("target_temp"),
            "pressure_advance": filament.get("pressure_advance"),
            "max_flow": filament.get("max_flow"),
            "spoolman_id": filament.get("spoolman_id"),
            "filament_id": filament["id"],
            "source": "rfid",
            "rfid_code": normalized,
            "rfid_reserve": current.get("rfid_reserve", ""),
        }
        self.set_profile(slot, profile)
        self._ensure_material(filament["material"], filament.get("target_temp"))
        self.unknown_rfid.pop(slot_key, None)
        self.rfid_live_slots.add(slot)
        self._info(
            gcmd,
            "%s: RFID %s associated with filament %s (%s)"
            % (self.slot_label(slot), normalized, filament["id"],
               filament.get("name") or filament["material"]))
        _klog(
            "RFID association slot=%s code=%s filament_id=%s brand=%r name=%r material=%s",
            self.slot_label(slot), normalized, filament["id"],
            filament.get("brand", ""), filament.get("name", ""),
            filament["material"])

    def cmd_rfid_fallback_cache(self, gcmd):
        if gcmd.get_int("CLEAR", 0, minval=0, maxval=1):
            uid = str(gcmd.get("UID", "") or "").strip().upper()
            if uid:
                if not self.rfid_fallback.forget(uid):
                    raise gcmd.error("[BOX]: UID %s is not cached" % uid)
                self._info(gcmd, "Third-party RFID cache: UID %s cleared" % uid)
            else:
                self.rfid_fallback.clear_cache()
                self._info(gcmd, "Third-party RFID cache cleared")
            return
        status = self.rfid_fallback.get_status()
        decoders = self.rfid_fallback.decoders()
        self._info(
            gcmd,
            "Third-party RFID: decoders=%s budget=%d (manual %d) cached "
            "tags=%d unknown=%d auto_disabled=%s last=%s"
            % (self.rfid_fallback.signature(decoders) or "none",
               status["auto_budget"], status["manual_budget"],
               status["cached_tags"], status["cached_unknown"],
               status["auto_disabled"], status["last_result"]))

    @staticmethod
    def _bambu_profile_id(data):
        material_id = str(data.get("material_id") or "").strip().upper()
        variant_id = str(data.get("variant_id") or "").strip().upper()
        safe = lambda value: "".join(
            c if c.isalnum() or c in ("-", "_") else "-" for c in value)
        if material_id:
            return ("BAMBU-%s-%s" % (
                safe(material_id), safe(variant_id or "DEFAULT")))[:64]
        # API7 stock capture intentionally reads only block4 detail + block5
        # RGBA. Use the stable Bambu preset identity rather than inventing IDs.
        fallback = (str(data.get("profile_name") or "").strip()
                    or str(data.get("material") or "UNKNOWN").strip())
        return ("BAMBU-%s" % safe(fallback).upper())[:64]

    @staticmethod
    def _bambu_identity_key(value):
        # Orca/imported presets are not consistent about separators
        # (for example "PETG Basic" vs "PETG-BASIC"). Keep matching strict
        # about the words, but ignore punctuation, separators and case.
        return "".join(
            ch for ch in str(value or "").casefold() if ch.isalnum())

    def _bambu_library_match(self, data):
        material = str(data.get("material") or "").strip().upper()
        brand = str(data.get("brand") or "Bambulab").strip() or "Bambulab"
        name = str(data.get("profile_name") or "").strip()
        code = "BAMBU:%s" % str(
            data.get("material_id")
            or data.get("detailed_filament_type")
            or material).strip().upper()
        mapping = self.store.rfid_mapping(code)
        if mapping:
            mapped = self.store.filament(mapping.get("filament_id"))
            if mapped:
                return mapped
        exact = self.store.filament_by_identity(brand, name, material)
        if exact:
            return exact
        # Imported Orca/user profiles may spell the same preset with spaces,
        # dashes or underscores, or use "Bambu Lab" vs "Bambulab". Prefer the
        # existing preset over creating a duplicate and use its settings as
        # authoritative.
        brand_key = self._bambu_identity_key(brand)
        name_key = self._bambu_identity_key(name)
        for filament in self.store.filaments.values():
            if (self._bambu_identity_key(filament.get("brand")) == brand_key
                    and self._bambu_identity_key(filament.get("name"))
                    == name_key):
                return dict(filament)
        return None

    def _apply_bambu_rfid_tag(self, slot, tagdata, note=None):
        if tagdata is None:
            return False
        data = tagdata.as_dict() if hasattr(tagdata, "as_dict") else dict(tagdata)
        material = str(data.get("material") or "").strip().upper()
        blocks = data.get("blocks") if isinstance(data.get("blocks"), dict) else {}
        self._info(
            self.gcode,
            "Bambu RFID decoded %s: UID=%s ATQA=%s SAK=%02X detail=%r "
            "material=%s expected_profile=%r color=%s RGBA=%s block4=%s block5=%s"
            % (self.slot_label(self._runtime_slot(slot)),
               data.get("uid", ""), data.get("atqa", ""), int(data.get("sak") or 0),
               data.get("detailed_filament_type", ""), material,
               data.get("profile_name", ""), data.get("color", ""),
               data.get("color_rgba", ""), blocks.get(4, ""), blocks.get(5, "")))
        color = self._normal_color(data.get("color")) or ""
        name = str(data.get("profile_name") or "").strip()
        brand = str(data.get("brand") or "Bambulab").strip() or "Bambulab"
        if not material or not name or not color:
            return False

        filament = self._bambu_library_match(data)
        if filament is not None:
            self._info(
                self.gcode,
                "Bambu RFID library match %s: FILAMENT_ID=%s brand=%r name=%r"
                % (self.slot_label(self._runtime_slot(slot)),
                   filament.get("id", ""), filament.get("brand", ""),
                   filament.get("name", "")))
        if filament is None:
            # Persist an identity profile so future spools of the same Bambu
            # material/variant can reuse it. Do not invent PA/max-flow. The tag
            # temperature range is stored, while target temp remains the generic
            # material/library decision when available.
            filament_id = self._bambu_profile_id(data)
            generic = self.store.materials.get(material) or {}
            value = {
                "material": material,
                "color": "",
                "brand": brand,
                "name": name,
                "target_temp": generic.get("target_temp"),
                "min_temp": data.get("min_hotend_c"),
                "max_temp": data.get("max_hotend_c"),
                "pressure_advance": None,
                "max_flow": None,
                "nominal_length_m": None,
                "rfid_codes": [],
                "aliases": [],
                "spoolman_id": None,
                "source": "rfid",
            }
            self._info(
                self.gcode,
                "Bambu RFID no library match %s: CODE=%s; creating fallback "
                "profile %s. To bind it to an existing library profile use "
                "_BOX_RFID_ASSOCIATE SLOT=%d FILAMENT_ID=<id>"
                % (self.slot_label(self._runtime_slot(slot)),
                   "BAMBU:%s" % str(
                       data.get("material_id")
                       or data.get("detailed_filament_type")
                       or material).strip().upper(),
                   filament_id, slot))
            try:
                filament = self.store.set_filament(filament_id, value)
            except Exception as exc:
                _klog("Bambu RFID library profile %s could not be saved: %s",
                      filament_id, exc, level=logging.warning)
                filament = dict(value, id=filament_id)

        profile = {
            "material": str(filament.get("material", material)).strip().upper(),
            "color": color,
            "brand": str(filament.get("brand", brand)).strip(),
            "name": str(filament.get("name", name)).strip(),
            "target_temp": filament.get("target_temp"),
            "pressure_advance": filament.get("pressure_advance"),
            "max_flow": filament.get("max_flow"),
            "nominal_length_m": filament.get("nominal_length_m"),
            "spoolman_id": filament.get("spoolman_id"),
            "filament_id": str(filament.get("id", "")).strip().upper(),
            "source": "rfid",
            "rfid_code": "BAMBU:%s" % (
                str(data.get("material_id")
                    or data.get("detailed_filament_type")
                    or material).strip().upper()),
            "rfid_reserve": "",
        }
        self.set_profile(slot, profile)
        self._ensure_material(profile["material"], profile.get("target_temp"))
        self.unknown_rfid.pop(self._runtime_slot_key(slot), None)

        # Feed the existing spool-usage estimator with a stable Bambu identity.
        uid = str(data.get("uid") or "").strip().upper()
        length_m, length_source = self._third_party_length_m(data, filament)
        fields = {
            "supplier": "BAMBU",
            "mat_id": str(
                data.get("material_id")
                or data.get("detailed_filament_type")
                or material).strip().upper(),
            "number": uid,
            "color": color,
            "len": str(length_m),
            "reserve": "",
        }
        self._remember_rfid_spool(slot, fields)
        self.rfid_live_slots.add(slot)
        self._clear_rfid_watch(slot)
        self._start_third_party_estimate(slot, length_source)
        # note: how the tag was identified when the stock read did not run
        # first (RFID cache, known-tag reread); None after a stock "unknown".
        self._info(
            self.gcode,
            "%s: %s %s / %s %s"
            % (self.slot_label(self._runtime_slot(slot)),
               ("%s; applied" % note) if note
               else "Creality RFID unknown; Bambu fallback applied",
               profile["name"], color,
               ("[%s]" % profile["filament_id"]) if profile["filament_id"] else ""))
        return True

    @staticmethod
    def _third_party_profile_id(data):
        vendor = str(data.get("vendor") or "RFID").strip().upper()
        fallback = (
            str(data.get("profile_name") or "").strip()
            or str(data.get("detailed_filament_type") or "").strip()
            or str(data.get("material") or "UNKNOWN").strip())
        safe = lambda value: "".join(
            c if c.isalnum() or c in ("-", "_") else "-" for c in value)
        return ("%s-%s" % (safe(vendor), safe(fallback).upper()))[:64]

    def _third_party_library_match(self, data):
        material = str(data.get("material") or "").strip().upper()
        brand = str(data.get("brand") or data.get("vendor") or "").strip()
        name = str(data.get("profile_name") or "").strip()
        code = str(data.get("identity_code") or "").strip().upper()
        if code:
            mapping = self.store.rfid_mapping(code)
            if mapping:
                mapped = self.store.filament(mapping.get("filament_id"))
                if mapped:
                    return mapped
        exact = self.store.filament_by_identity(brand, name, material)
        if exact:
            return exact
        # Reuse the same punctuation/case-insensitive identity matching as the
        # Bambu path. The RFID decoder remains authoritative for live colour.
        brand_key = self._bambu_identity_key(brand)
        name_key = self._bambu_identity_key(name)
        for filament in self.store.filaments.values():
            if (self._bambu_identity_key(filament.get("brand")) == brand_key
                    and self._bambu_identity_key(filament.get("name"))
                    == name_key):
                return dict(filament)
        return None

    def _apply_third_party_rfid_tag(self, slot, tagdata, note=None):
        if tagdata is None:
            return False
        data = tagdata.as_dict() if hasattr(tagdata, "as_dict") else dict(tagdata)
        vendor = str(data.get("vendor") or "").strip().upper()
        identity = str(data.get("identity_code") or "").strip().upper()
        material = str(data.get("material") or "").strip().upper()
        detail = str(data.get("detailed_filament_type") or "").strip()
        brand = str(data.get("brand") or vendor).strip()
        name = str(data.get("profile_name") or "").strip()
        color = self._normal_color(data.get("color")) or ""
        blocks = data.get("blocks") if isinstance(data.get("blocks"), dict) else {}
        if not vendor or not identity or not material or not name or not color:
            return False

        self._info(
            self.gcode,
            "Third-party RFID decoded %s: vendor=%s UID=%s detail=%r "
            "material=%s expected_profile=%r color=%s identity=%s "
            "block4=%s block5=%s"
            % (self.slot_label(self._runtime_slot(slot)), vendor,
               data.get("uid", ""), detail, material, name, color, identity,
               blocks.get(4, ""), blocks.get(5, "")))

        filament = self._third_party_library_match(data)
        if filament is not None:
            self._info(
                self.gcode,
                "%s RFID library match %s: FILAMENT_ID=%s brand=%r name=%r"
                % (vendor, self.slot_label(self._runtime_slot(slot)),
                   filament.get("id", ""), filament.get("brand", ""),
                   filament.get("name", "")))
        if filament is None:
            filament_id = self._third_party_profile_id(data)
            generic = self.store.materials.get(material) or {}
            value = {
                "material": material,
                "color": "",
                "brand": brand,
                "name": name,
                "target_temp": generic.get("target_temp"),
                "min_temp": data.get("min_hotend_c"),
                "max_temp": data.get("max_hotend_c"),
                "pressure_advance": None,
                "max_flow": None,
                "nominal_length_m": None,
                "rfid_codes": [],
                "aliases": [],
                "spoolman_id": None,
                "source": "rfid",
            }
            self._info(
                self.gcode,
                "%s RFID no library match %s: CODE=%s; creating fallback "
                "profile %s. To bind it to an existing library profile use "
                "_BOX_RFID_ASSOCIATE SLOT=%d FILAMENT_ID=<id>"
                % (vendor, self.slot_label(self._runtime_slot(slot)),
                   identity, filament_id, slot))
            try:
                filament = self.store.set_filament(filament_id, value)
            except Exception as exc:
                _klog(
                    "%s RFID library profile %s could not be saved: %s",
                    vendor, filament_id, exc, level=logging.warning)
                filament = dict(value, id=filament_id)

        profile = {
            "material": str(filament.get("material", material)).strip().upper(),
            "color": color,
            "brand": str(filament.get("brand", brand)).strip(),
            "name": str(filament.get("name", name)).strip(),
            "target_temp": filament.get("target_temp"),
            "pressure_advance": filament.get("pressure_advance"),
            "max_flow": filament.get("max_flow"),
            "nominal_length_m": filament.get("nominal_length_m"),
            "spoolman_id": filament.get("spoolman_id"),
            "filament_id": str(filament.get("id", "")).strip().upper(),
            "source": "rfid",
            "rfid_code": identity,
            "rfid_reserve": "",
        }
        self.set_profile(slot, profile)
        self._ensure_material(profile["material"], profile.get("target_temp"))
        self.unknown_rfid.pop(self._runtime_slot_key(slot), None)

        uid = str(data.get("uid") or "").strip().upper()
        length_m, length_source = self._third_party_length_m(data, filament)
        fields = {
            "supplier": vendor,
            "mat_id": detail.upper() or material,
            "number": uid,
            "color": color,
            "len": str(length_m),
            "reserve": "",
        }
        self._remember_rfid_spool(slot, fields)
        self.rfid_live_slots.add(slot)
        self._clear_rfid_watch(slot)
        self._start_third_party_estimate(slot, length_source)
        self._info(
            self.gcode,
            "%s: %s %s / %s %s"
            % (self.slot_label(self._runtime_slot(slot)),
               ("%s; applied" % note) if note
               else "Creality/Bambu RFID unknown; %s fallback applied" % vendor,
               profile["name"], color,
               ("[%s]" % profile["filament_id"])
               if profile["filament_id"] else ""))
        return True

    def _run_vendor_rfid_fallbacks(self, slot, automatic=False):
        """Identify a tag the stock CFS left unknown; see box_rfid_fallback.

        Returns the name of the decoder that identified it, or None.
        """
        # Observation mode keeps the CFS read-only: no key arming, no reread.
        if (not self.is_physical_slot(slot)
                or getattr(self, "observation_mode", False)):
            return None
        try:
            return self.rfid_fallback.run(slot, automatic)
        except Exception as exc:
            _klog("%s third-party RFID fallback failed: %s",
                  self.slot_label(slot), exc, level=logging.warning)
            return None

    def _vendor_rfid_fallback_ready(self):
        """True when an automatic stock CFS reread cannot disturb anything.

        Not during a print or pause, not during a Box operation and not while
        a filament is loaded toward the printhead (the CFS answers BUSY to RFID
        reads then). The fallback stays pending and runs once that is over.
        """
        if self.operation_depth or self.rfid_read_owner is not None:
            return False
        stats = self.printer.lookup_object("print_stats", None)
        if getattr(stats, "state", None) in ("printing", "paused"):
            return False
        snap = self.snapshot
        loaded = snap.loaded_slot
        if not snap.data_ready or snap.loaded_mask or (
                isinstance(loaded, int) and loaded >= 0):
            return False
        return True

    def _try_pending_vendor_rfid_fallback(self, slot):
        """Automatic fallback for a pending insertion, once per insertion."""
        if (slot in self.rfid_fallback_tried
                or not self.rfid_fallback.decoders()
                or not self._vendor_rfid_fallback_ready()):
            return None
        self.rfid_fallback_tried.add(slot)
        with self._rfid_read_guard("automatic %s fallback" % self.slot_label(slot)):
            return self._run_vendor_rfid_fallbacks(slot, automatic=True)

    def _run_known_vendor_fastpath(self, slot):
        """Read a tag identified before by its decoder, without a stock read."""
        if (not self.is_physical_slot(slot)
                or getattr(self, "observation_mode", False)):
            return None
        try:
            return self.rfid_fallback.run_known(slot)
        except Exception as exc:
            _klog("%s known third-party RFID read failed: %s",
                  self.slot_label(slot), exc, level=logging.warning)
            return None
