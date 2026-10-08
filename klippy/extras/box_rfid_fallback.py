# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Third-party RFID fallback for the CFS: decoder registry, UID cache, budget.

When the stock CFS leaves a tag unknown, Box asks the registered vendor
decoders (box_rfid_bambu, box_rfid_mifare, future extras) to read it. Each
decoder attempt is a stock CFS reread of the slot (~45 s, the RS-485 bus is
busy meanwhile), so this module keeps the number of rereads bounded:

- the tag identity (UID/ATQA/SAK) comes from the CFS internal record that the
  stock read already filled, with no RF activity;
- a UID decoded before is applied from the cache with no reread; a UID that no
  decoder understood is not retried until the set of decoders changes;
- each run has a reread budget (automatic and manual), so adding decoders
  cannot make an insertion read longer than the budget.

A decoder is any printer object with these members:

    RFID_DECODER_NAME      short upper-case name, e.g. "BAMBU"
    RFID_DECODER_PRIORITY  lower runs first
    RFID_DECODER_KIND      "bambu" or "generic" (Box profile builder)
    rfid_decoder_version()                      changes invalidate "unknown"
    rfid_decoder_candidate(identity, automatic) passive check, no RF
    rfid_decoder_cost(identity, automatic)      stock rereads of a full try
    rfid_decoder_read(slot, identity, max_reads, automatic)
                                                -> (tag or None, rereads used)
    rfid_decoder_known(identity)                optional: the decoder holds
                                                a hint for this UID

rfid_decoder_read returns (None, used) when the tag is not the decoder's
(wrong keys, unknown layout) and raises on a CFS or bus error, so an error is
never remembered as "not recognised". A "generic" tag is the
ThirdPartyTagData dictionary of box_rfid_mifare.
"""

from dataclasses import dataclass
import logging

from extras import box_rfid_diag as diag


CACHE_SETTING = "rfid_fallback_cache"
CACHE_LIMIT = 256
REQUIRED_CAPS = (
    diag.CAP_INTERNAL_RECORD | diag.CAP_STOCK_CAPTURE
    | diag.CAP_STOCK_TASK_KEYS3)
DEFAULT_AUTO_BUDGET = 1
DEFAULT_MANUAL_BUDGET = 3


def _klog(msg, *args, level=logging.info):
    level("box_rfid_fallback: " + msg, *args)


@dataclass(frozen=True)
class TagIdentity:
    uid: bytes
    atqa: bytes
    sak: int

    @property
    def uid_hex(self):
        return bytes(self.uid).hex().upper()

    @property
    def mifare_classic_1k(self):
        return (self.atqa == b"\x04\x00" and self.sak == 0x08
                and len(self.uid) == 4 and self.uid != b"\x00" * 4)


def identity_from_internal_record(reply):
    """UID/ATQA/SAK of the stock CFS internal record, or None without a tag."""
    data = getattr(reply, "data", None)
    if data is None or len(data) != 76:
        return None
    data = bytes(data)
    uid = data[62:66]
    if uid == b"\x00" * 4:
        return None
    return TagIdentity(uid=uid, atqa=data[60:62], sak=data[74])


def is_decoder(obj):
    return callable(getattr(obj, "rfid_decoder_read", None))


def remember_manual_read(printer, decoder, tag):
    """Cache a tag that a helper's own manual command decoded.

    Without it, a UID remembered as "not recognised" would stay skipped on
    later insertions although a manual read identified it.
    """
    box = printer.lookup_object("box", None)
    fallback = getattr(box, "rfid_fallback", None)
    if fallback is None or tag is None:
        return
    try:
        identity = TagIdentity(
            uid=bytes(tag.uid), atqa=bytes(tag.atqa), sak=int(tag.sak))
        fallback.remember_tag(identity, decoder, tag)
    except Exception as exc:
        _klog("could not cache the manual %s read: %s",
              getattr(decoder, "RFID_DECODER_NAME", "?"), exc,
              level=logging.warning)


def _plain_tag(tag):
    """JSON-safe dictionary of a decoder's tag data."""
    data = tag.as_dict() if hasattr(tag, "as_dict") else dict(tag)
    data = dict(data)
    blocks = data.get("blocks")
    if isinstance(blocks, dict):
        data["blocks"] = {str(k): v for k, v in blocks.items()}
    return data


def _cached_tag(data):
    data = dict(data)
    blocks = data.get("blocks")
    if isinstance(blocks, dict):
        data["blocks"] = {
            int(k) if str(k).isdigit() else k: v for k, v in blocks.items()}
    return data


class RfidFallback:
    """Runs the vendor decoders for one Box. Box owns one instance."""

    def __init__(self, box, auto_budget=DEFAULT_AUTO_BUDGET,
                 manual_budget=DEFAULT_MANUAL_BUDGET, enabled=None):
        self.box = box
        self.auto_budget = int(auto_budget)
        self.manual_budget = int(manual_budget)
        # Decoder name -> bool, from Box options; unlisted decoders are on.
        self.enabled = dict(enabled or {})
        self.gate = diag.AutoFallbackGate("CFS third-party RFID")
        self.firmware_ok = {}
        self.last_result = None

    # --- decoders ---------------------------------------------------------

    def decoders(self):
        found = []
        for _name, obj in self.box.printer.lookup_objects():
            if not is_decoder(obj):
                continue
            name = str(getattr(obj, "RFID_DECODER_NAME", "")).upper()
            if not name or not self.enabled.get(name, True):
                continue
            found.append(obj)
        found.sort(key=lambda d: (getattr(d, "RFID_DECODER_PRIORITY", 100),
                                  d.RFID_DECODER_NAME))
        return found

    @staticmethod
    def signature(decoders):
        return "|".join(
            "%s:%s" % (d.RFID_DECODER_NAME, d.rfid_decoder_version())
            for d in decoders)

    # --- CFS access -------------------------------------------------------

    def _diag_driver(self, address):
        box_driver = self.box.drivers.get(address)
        if box_driver is None:
            return None
        return diag.RfidDiagDriver(box_driver.serial, address)

    def _firmware_supported(self, address, driver):
        """API7 stock capture on this CFS; asked once per Klipper start."""
        known = self.firmware_ok.get(address)
        if known is not None:
            return known
        info = driver.info(timeout=1.0)
        if info is None:
            self.gate.info_timeout()
            return False
        self.gate.info_ok()
        ok = (info.api_version == diag.API_STOCK_CAPTURE
              and info.capabilities & REQUIRED_CAPS == REQUIRED_CAPS)
        self.firmware_ok[address] = ok
        if not ok:
            self.gate.disable(
                "CFS %d RFID firmware has no API7 stock capture "
                "(api=%d caps=0x%02X)"
                % (address, info.api_version, info.capabilities))
        return ok

    def identity(self, slot):
        """Tag identity from the stock read, without RF activity."""
        address, local = self.box._address_slot(slot)
        driver = self._diag_driver(address)
        if driver is None or not self._firmware_supported(address, driver):
            return None
        return identity_from_internal_record(
            driver.internal_record(local, timeout=1.0))

    # --- UID cache --------------------------------------------------------

    def _cache(self):
        value = self.box.store.setting(CACHE_SETTING, {}) or {}
        if not isinstance(value, dict):
            value = {}
        tags = value.get("tags")
        unknown = value.get("unknown")
        return {
            "tags": dict(tags) if isinstance(tags, dict) else {},
            "unknown": dict(unknown) if isinstance(unknown, dict) else {},
        }

    def _save_cache(self, cache):
        for key in ("tags", "unknown"):
            entries = cache[key]
            for uid in list(entries)[:-CACHE_LIMIT]:
                entries.pop(uid, None)
        self.box.store.set_setting(CACHE_SETTING, cache)

    def remember_tag(self, identity, decoder, tag):
        cache = self._cache()
        uid = identity.uid_hex
        cache["unknown"].pop(uid, None)
        cache["tags"].pop(uid, None)
        cache["tags"][uid] = {
            "decoder": decoder.RFID_DECODER_NAME,
            "kind": getattr(decoder, "RFID_DECODER_KIND", "generic"),
            "tag": _plain_tag(tag),
        }
        self._save_cache(cache)

    def remember_unknown(self, identity, signature):
        cache = self._cache()
        uid = identity.uid_hex
        cache["tags"].pop(uid, None)
        cache["unknown"].pop(uid, None)
        cache["unknown"][uid] = signature
        self._save_cache(cache)

    def forget(self, uid_hex):
        cache = self._cache()
        uid = str(uid_hex or "").strip().upper()
        found = (cache["tags"].pop(uid, None) is not None
                 or cache["unknown"].pop(uid, None) is not None)
        if found:
            self._save_cache(cache)
        return found

    def clear_cache(self):
        self.box.store.set_setting(CACHE_SETTING, {"tags": {}, "unknown": {}})

    # --- run --------------------------------------------------------------

    def _apply(self, slot, kind, tag, note=None):
        # note says how the tag was identified when no stock "unknown"
        # preceded it; Box prints it instead of the stock-fallback wording.
        if kind == "bambu":
            return self.box._apply_bambu_rfid_tag(slot, tag, note=note)
        return self.box._apply_third_party_rfid_tag(slot, tag, note=note)

    def run(self, slot, automatic):
        """Identify an unknown tag. Returns the decoder name or None.

        automatic: the pending-insertion path. It uses the UID cache and the
        automatic budget. A manual reread always reads the tag again (and
        refreshes the cache), within the manual budget.
        """
        if automatic and self.gate.disabled:
            return None
        decoders = self.decoders()
        if not decoders:
            return None
        identity = self.identity(slot)
        if identity is None:
            return None
        uid = identity.uid_hex
        signature = self.signature(decoders)
        label = self.box.slot_label(slot)

        if automatic:
            cache = self._cache()
            hit = cache["tags"].get(uid)
            names = {d.RFID_DECODER_NAME for d in decoders}
            if isinstance(hit, dict) and hit.get("decoder") in names:
                tag = _cached_tag(hit.get("tag") or {})
                note = "%s tag known from the RFID cache, no reread" % (
                    hit["decoder"])
                if self._apply(slot, hit.get("kind"), tag, note=note):
                    self.last_result = ("cache", hit["decoder"], uid, 0)
                    _klog("%s UID=%s applied from cache (%s), no reread",
                          label, uid, hit["decoder"])
                    return hit["decoder"]
            if cache["unknown"].get(uid) == signature:
                self.last_result = ("unknown-cached", None, uid, 0)
                _klog("%s UID=%s not recognised before by %s; skipped",
                      label, uid, signature)
                return None

        budget = self.auto_budget if automatic else self.manual_budget
        used_total = 0
        untried = []
        failed = []
        for decoder in decoders:
            name = decoder.RFID_DECODER_NAME
            try:
                if not decoder.rfid_decoder_candidate(identity, automatic):
                    continue
                cost = max(1, int(decoder.rfid_decoder_cost(
                    identity, automatic)))
            except Exception as exc:
                _klog("%s decoder %s check failed: %s", label, name, exc,
                      level=logging.warning)
                continue
            if budget - used_total <= 0:
                untried.append(name)
                continue
            try:
                tag, used = decoder.rfid_decoder_read(
                    slot, identity, budget - used_total, automatic)
            except Exception as exc:
                # Counted as one reread: the budget stays a hard ceiling.
                _klog("%s decoder %s failed: %s", label, name, exc,
                      level=logging.warning)
                failed.append(name)
                used_total += 1
                continue
            used = max(0, int(used or 0))
            used_total += used
            if tag is not None:
                if not self._apply(
                        slot, getattr(decoder, "RFID_DECODER_KIND", "generic"),
                        tag):
                    continue
                self.remember_tag(identity, decoder, tag)
                self.last_result = ("read", name, uid, used_total)
                _klog("%s UID=%s decoded by %s with %d reread(s)",
                      label, uid, name, used_total)
                return name
            if used < cost:
                untried.append(name)

        self.last_result = ("none", None, uid, used_total)
        if untried or failed:
            parts = []
            if untried:
                parts.append("not tried within %d reread(s): %s"
                             % (budget, ", ".join(untried)))
            if failed:
                parts.append("read error: %s" % ", ".join(failed))
            self.box._info(
                self.box.gcode,
                "%s: RFID UID %s not identified (%s). Run "
                "_BOX_RFID_READ_SLOT SLOT=%d to retry"
                % (label, uid, "; ".join(parts), slot))
        else:
            self.remember_unknown(identity, signature)
            _klog("%s UID=%s not recognised by %s after %d reread(s)",
                  label, uid, signature, used_total)
            notify = getattr(self.box, "notify", None)
            if callable(notify):
                notify("rfid_unknown", "CFS unknown RFID tag",
                       "%s: tag %s not recognised; assign the filament "
                       "in the slot editor" % (label, uid))
        return None

    def known_decoder(self, slot, identity, decoders):
        """Decoder that identified this tag before, or (None, None).

        Known means: the UID is in the cache, a decoder holds a hint for it
        (rfid_decoder_known), or, for slots read before the cache existed,
        the slot profile carries the decoder's code prefix ("BAMBU:...").
        """
        by_name = {d.RFID_DECODER_NAME: d for d in decoders}
        hit = self._cache()["tags"].get(identity.uid_hex)
        if isinstance(hit, dict) and hit.get("decoder") in by_name:
            return by_name[hit["decoder"]], "cache"
        for decoder in decoders:
            known = getattr(decoder, "rfid_decoder_known", None)
            if callable(known) and known(identity):
                return decoder, "hint"
        try:
            code = str(self.box.profile(slot).get("rfid_code") or "")
        except Exception:
            code = ""
        code = code.strip().upper()
        for decoder in decoders:
            if code.startswith(decoder.RFID_DECODER_NAME + ":"):
                return decoder, "slot profile"
        return None, None

    def run_known(self, slot):
        """Manual reread of a tag identified before, straight to its decoder.

        Skips the stock Creality reread that would only answer "unknown"
        (one ~45 s CFS cycle). Returns the decoder name, or None: then the
        caller runs the normal stock-first path, which also refreshes the
        cache if the tag was rewritten.
        """
        if self.manual_budget < 1:
            return None
        decoders = self.decoders()
        if not decoders:
            return None
        identity = self.identity(slot)
        if identity is None or not identity.mifare_classic_1k:
            return None
        decoder, source = self.known_decoder(slot, identity, decoders)
        if decoder is None:
            return None
        name = decoder.RFID_DECODER_NAME
        label = self.box.slot_label(slot)
        try:
            cost = max(1, int(decoder.rfid_decoder_cost(identity, False)))
            tag, used = decoder.rfid_decoder_read(
                slot, identity, min(cost, self.manual_budget), False)
        except Exception as exc:
            _klog("%s known %s read failed: %s; stock read follows",
                  label, name, exc, level=logging.warning)
            return None
        kind = getattr(decoder, "RFID_DECODER_KIND", "generic")
        note = "known %s tag read directly, Creality reread skipped" % name
        if tag is None or not self._apply(slot, kind, tag, note=note):
            _klog("%s UID=%s no longer read by %s (%s); stock read follows",
                  label, identity.uid_hex, name, source)
            return None
        self.remember_tag(identity, decoder, tag)
        self.last_result = ("direct", name, identity.uid_hex, used)
        _klog("%s UID=%s read directly by %s (known from %s), stock "
              "reread skipped", label, identity.uid_hex, name, source)
        return name

    def get_status(self):
        cache = self._cache()
        return {
            "auto_budget": self.auto_budget,
            "manual_budget": self.manual_budget,
            "auto_disabled": self.gate.disabled,
            "cached_tags": len(cache["tags"]),
            "cached_unknown": len(cache["unknown"]),
            "last_result": None if self.last_result is None else {
                "source": self.last_result[0],
                "decoder": self.last_result[1],
                "uid": self.last_result[2],
                "rereads": self.last_result[3],
            },
        }
