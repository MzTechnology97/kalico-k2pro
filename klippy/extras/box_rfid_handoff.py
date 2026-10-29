# Copyright (C) 2026 K2-OpenHost contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
"""CFS RFID single-cycle handoff. Each exchange releases the owned queue."""

import logging
import secrets
import struct
from contextlib import contextmanager
from dataclasses import dataclass, replace

from extras import box_protocol
from extras.serial_485 import build_485_body

INFO, CONFIG, START, POLL, KEYS, DECLINE = range(0x20, 0x26)
IDLE, RUNNING, WAIT_KEYS, KEYS_READY, DONE = range(5)
NONE, NATIVE_OK, API7_OK, DECLINED, TIMED_OUT, RF_FAILED = range(6)
CMD = 0x57


class HandoffError(RuntimeError):
    pass


class HandoffUnsupported(HandoffError):
    pass


class HandoffBusy(HandoffError):
    pass


def _int(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("%s must be %d..%d" % (name, low, high))
    return value


@dataclass(frozen=True)
class HandoffInfo:
    wait_max_ms: int
    lease_max_ms: int


@dataclass(frozen=True)
class HandoffState:
    state: int
    slot: int
    result: int
    session: int
    token: int
    request_id: int
    uid: bytes
    wait_remaining_ms: int
    flags: int
    measurement_generation: int
    tick: int

    @property
    def terminal_key(self):
        return (self.session, self.token, self.request_id, self.slot,
                self.measurement_generation, self.uid, self.result)


def decode_info(p):
    if (len(p) != 12 or p[:8] != b"RHI1" + bytes((1, 40, 4, 7))
            or struct.unpack_from("<HH", p, 8) != (5000, 10000)):
        raise box_protocol.ProtocolError("unsupported RFID handoff INFO")
    return HandoffInfo(*struct.unpack_from("<HH", p, 8))


def decode_state(p, session):
    if (len(p) != 40 or p[:4] != b"RHO1" or p[4] != 1
            or p[5] > DONE or p[7] > RF_FAILED
            or p[6] not in (0, 1, 2, 3, 255)
            or (p[5] != IDLE and p[6] == 255)):
        raise box_protocol.ProtocolError("invalid RFID handoff state shape")
    sid, token, request = struct.unpack_from("<III", p, 8)
    wait, flags, generation, tick = struct.unpack_from("<IIII", p, 24)
    if (sid != session or flags & ~7 or wait > 5000
            or (p[5] in (WAIT_KEYS, KEYS_READY) and
                (not token or not any(p[20:24])))):
        raise box_protocol.ProtocolError("invalid RFID handoff identity/flags")
    return HandoffState(p[5], p[6], p[7], sid, token, request, p[20:24],
                        wait, flags, generation, tick)


class HandoffDriver:
    def __init__(self, transport, address):
        self.transport = transport
        self.address = _int(address, 1, 4, "address")

    def _exchange(self, payload):
        body = build_485_body(self.address, CMD, payload, header_byte=0xFF)
        frame = self.transport.cmd_send_data_with_response(body, 0.5)
        if not frame:
            raise HandoffError("RFID handoff request timed out")
        reply = box_protocol.decode_reply(frame, self.address, CMD,
                                          context="rfid_handoff")
        if reply.status:
            if reply.payload:
                raise box_protocol.ProtocolError(
                    "rejected RFID handoff unexpectedly has a payload")
            if reply.status == 1:
                raise HandoffUnsupported("RFID handoff unsupported or stale")
            if reply.status == 7:
                raise HandoffBusy("CFS RFID handoff is busy")
            raise box_protocol.ProtocolError("invalid RFID handoff status")
        return reply.payload

    def info(self):
        return decode_info(self._exchange(bytes((INFO,))))

    def configure(self, session, enabled=True, wait_ms=3000, lease_ms=10000):
        _int(session, 1, 0xFFFFFFFF, "session")
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        _int(wait_ms, 500, 5000, "wait ms")
        _int(lease_ms, 1000, 10000, "lease ms")
        payload = struct.pack("<BBBBIHH", CONFIG, int(enabled), 0, 0,
                              session, wait_ms, lease_ms)
        return decode_state(self._exchange(payload), session)

    def poll(self, session):
        _int(session, 1, 0xFFFFFFFF, "session")
        return decode_state(self._exchange(struct.pack(
            "<BBBBI", POLL, 0, 0, 0, session)), session)

    def start(self, slot, session, request_id):
        _int(slot, 0, 3, "slot")
        _int(session, 1, 0xFFFFFFFF, "session")
        _int(request_id, 1, 0xFFFFFFFF, "request ID")
        payload = struct.pack("<BBBBII", START, slot, 0, 0, session, request_id)
        return decode_state(self._exchange(payload), session)

    def answer(self, state, keys=None):
        _int(state.slot, 0, 3, "slot")
        _int(state.session, 1, 0xFFFFFFFF, "session")
        _int(state.token, 1, 0xFFFFFFFF, "token")
        uid = bytes(state.uid)
        if len(uid) != 4 or not any(uid):
            raise ValueError("handoff UID must be four identity bytes")
        if keys is not None:
            keys = bytes(keys)
            if len(keys) != 18:
                raise ValueError("handoff requires three six-byte keys")
        payload = struct.pack(
            "<BBBBII4s", KEYS if keys is not None else DECLINE,
            state.slot, 0, 0, state.session, state.token, uid)
        return decode_state(self._exchange(payload + (keys or b"")), state.session)


class RfidHandoff:
    """Coordinator shared by manual reads and automatic insertion polling."""

    def __init__(self, owner, config):
        self.owner = owner
        self.printer, self.reactor = owner.printer, owner.reactor
        self.enabled = config.getboolean("single_cycle_rfid", True)
        self.wait_ms = config.getint("handoff_wait_ms", 3000, minval=500, maxval=5000)
        self.lease_ms = config.getint("handoff_lease_ms", 10000, minval=1000, maxval=10000)
        self.units = {}
        self.manual_addresses = set()
        self.busy_addresses = set()
        self.last_error = None
        self._timer = self.reactor.register_timer(self._periodic)

    def reset(self):
        self.units.clear()
        self.reactor.update_timer(self._timer, self.reactor.monotonic())

    def _driver(self, transport, address):
        return HandoffDriver(transport, address)

    def _observation(self):
        return bool(getattr(self.owner._box(), "observation_mode", False))

    def _host_idle_for_job(self, address):
        box = self.owner._box()
        if box is None or self._observation():
            return False
        stats = self.printer.lookup_object("print_stats", None)
        state = (stats.get_status(self.reactor.monotonic()).get("state")
                 if stats is not None else None)
        if state in ("printing", "paused"):
            return False
        progress = getattr(box, "operation_progress", None) or {}
        if progress.get("kind") in ("load", "unload", "change", "toolchange"):
            return False
        return (not getattr(box, "operation_depth", 0)
                or address in self.manual_addresses)

    def _exchange(self, address, method, *args):
        if method != "info" and self._observation():
            raise HandoffError("RFID handoff is disabled in observation mode")
        if method == "start" and not self._host_idle_for_job(address):
            raise HandoffBusy("host load, unload or print is active")
        # The session ends before returning, before any reactor.pause().
        with self.owner._transport().request_session() as transport:
            return getattr(self._driver(transport, address), method)(*args)

    def _unit(self, address):
        if address not in self.units:
            self.units[address] = {
                "supported": None, "configured": False,
                "session": secrets.randbits(32) or 1,
                "request": 0, "offers": {}, "answered": set(), "answer_attempts": {},
                "completed": {}, "captures": {}, "pending": {},
            }
        return self.units[address]

    @contextmanager
    def _operation(self, address, parent_read=False):
        # Reactor callbacks may run while a serial request yields. One
        # coordinator must finish copying a capture before another supplies
        # keys (which clear global capture scratch) on the same CFS.
        if address in self.busy_addresses:
            raise HandoffBusy("another RFID handoff operation is active")
        box = self.owner._box()
        acquire = getattr(box, "acquire_rfid_read", None)
        release = getattr(box, "release_rfid_read", None)
        claimed = False
        if getattr(box, "rfid_read_owner", None) is not None:
            if not parent_read:
                raise HandoffBusy("another CFS RFID reader is active")
        elif callable(acquire) and callable(release):
            acquire("box_rfid_handoff")
            claimed = True
        self.busy_addresses.add(address)
        try:
            yield
        finally:
            self.busy_addresses.discard(address)
            if claimed:
                release()

    def _contact(self, address, allow_config=False):
        unit = self._unit(address)
        if unit["supported"] is False:
            return None
        if unit["supported"] is None:
            try:
                self._exchange(address, "info")
            except HandoffUnsupported:
                unit["supported"] = False
                return None
            unit["supported"] = True
        if not unit["configured"]:
            if not allow_config:
                return None
            try:
                self._exchange(address, "configure", unit["session"], True,
                               self.wait_ms, self.lease_ms)
            except HandoffUnsupported:
                # RHI1 INFO may exist on a secure backend on which the
                # legacy/API7 handoff is deliberately unavailable.
                unit["supported"] = False
                return None
            unit["configured"] = True
        # CONFIG alone does not arm the lease.
        return self._exchange(address, "poll", unit["session"])

    def _provider(self, uid):
        box = self.owner._box()
        registry = getattr(box, "rfid_fallback", None)
        if registry is None:
            return None
        from extras.box_rfid_fallback import TagIdentity
        identity = TagIdentity(uid, b"\x04\x00", 8)
        decoders = registry.decoders()
        cache = registry._cache().get("tags", {}).get(identity.uid_hex, {})
        known = cache.get("decoder") if isinstance(cache, dict) else None
        ordered = sorted(decoders, key=lambda d: (
            0 if d.RFID_DECODER_NAME == known else 1,
            getattr(d, "RFID_DECODER_PRIORITY", 100)))
        # Vendor-specific UID hints also precede guesses.
        for decoder in ordered:
            hinted = getattr(decoder, "rfid_decoder_known", None)
            if callable(hinted) and hinted(identity):
                ordered.remove(decoder)
                ordered.insert(0, decoder)
                break
        for decoder in ordered:
            keys = getattr(decoder, "rfid_handoff_keys", None)
            capture = getattr(decoder, "rfid_handoff_decode", None)
            if callable(keys) and callable(capture):
                value = keys(uid)
                if value is not None:
                    value = bytes(value)
                    if len(value) != 18:
                        raise HandoffError("decoder supplied invalid handoff keys")
                    return decoder, value
        return None

    def _advance(self, address, state):
        unit = self._unit(address)
        self._drain_pending(address, state)
        if state.state == WAIT_KEYS:
            offer_key = (state.session, state.token)
            if offer_key not in unit["offers"]:
                unit["offers"][offer_key] = (
                    self._provider(state.uid) if self._host_idle_for_job(address) else None)
            if offer_key not in unit["answered"]:
                provider = unit["offers"][offer_key]
                attempts = unit["answer_attempts"].get(offer_key, 0)
                keys = (provider[1] if provider is not None and attempts < 3
                        and self._host_idle_for_job(address) else None)
                unit["answer_attempts"][offer_key] = attempts + 1
                if keys is not None and offer_key not in unit["pending"]:
                    sample = self._sample(address, state.slot)
                    if sample.current_uid != state.uid:
                        raise HandoffError("RFID handoff UID changed before keys")
                    # Remember the measured slot as well as the global RHO
                    # token. RHO may describe the next queued slot before the
                    # host observes this job's DONE state.
                    unit["pending"][offer_key] = (state, sample.generation)
                try:
                    state = self._exchange(address, "answer", state, keys)
                except HandoffError:
                    # An accepted KEYS request may already have been consumed.
                    # Recover with POLL rather than reauthenticating/rerotating.
                    latest = self._exchange(address, "poll", state.session)
                    if (latest.token != state.token or latest.uid != state.uid
                            or latest.slot != state.slot):
                        raise HandoffError("RFID handoff answer was superseded")
                    state = latest
                if state.state != WAIT_KEYS:
                    unit["answered"].add(offer_key)
        if state.state == DONE and self._host_idle_for_job(address):
            self._finish(address, state)
        return state

    def _drain_pending(self, address, current):
        """Consume a previous slot before accepting keys for the next slot.

        Only this host supplies RH1 keys. The firmware denies ordinary ARM
        and CLEAR while a job is active, and this address is host-owned, so a
        new RH1 key answer is the next operation that can overwrite CAP.
        """
        unit = self._unit(address)
        for offer_key, (previous, baseline) in tuple(unit["pending"].items()):
            if (previous.session == current.session
                    and previous.token == current.token
                    and previous.slot == current.slot):
                continue
            if previous.session != current.session:
                unit["pending"].pop(offer_key, None)
                continue
            sample = self._sample(address, previous.slot)
            expected = baseline % 0xFFFFFFFF + 1
            terminal = replace(previous, state=DONE, result=API7_OK,
                               wait_remaining_ms=0,
                               measurement_generation=expected)
            if self._capture_matches(sample, terminal):
                self._finish(address, terminal)
            # A changed/missing proof cannot be repaired by applying old tag
            # metadata or by starting another physical scan.
            unit["pending"].pop(offer_key, None)

    def _sample(self, address, local):
        with self.owner._transport().request_session() as transport:
            return self.owner._driver(transport, address).snapshot(local)

    @staticmethod
    def _capture_matches(sample, state):
        return (sample.generation == state.measurement_generation
                and sample.current_uid == state.uid
                and sample.measured_uid == state.uid
                and bool(sample.flags & 2))

    def _apply_tag(self, address, state, decoder, tag):
        box = self.owner._box()
        slot = box._global_slot(address, state.slot)
        kind = getattr(decoder, "RFID_DECODER_KIND", "generic")
        if kind == "bambu":
            applied = box._apply_bambu_rfid_tag(
                slot, tag, note="Bambu tag read in one CFS cycle")
        else:
            applied = box._apply_third_party_rfid_tag(
                slot, tag, note="RFID tag read in one CFS cycle")
        if applied:
            from extras.box_rfid_fallback import remember_manual_read
            remember_manual_read(self.printer, decoder, tag)
            return decoder.RFID_DECODER_NAME
        return False

    def _finish(self, address, state, verify_cached=False):
        unit = self._unit(address)
        key = state.terminal_key
        box = self.owner._box()
        slot = box._global_slot(address, state.slot)
        if key in unit["completed"]:
            result = unit["completed"][key]
            if verify_cached and state.result == API7_OK and result:
                sample = self._sample(address, state.slot)
                if not self._capture_matches(sample, state):
                    return False
                spool = getattr(box, "rfid_spools", {}).get(slot)
                if not spool and key in unit["captures"]:
                    return self._apply_tag(address, state, *unit["captures"][key])
            return result
        applied = False
        if state.result == API7_OK:
            provider = unit["offers"].get((state.session, state.token))
            if provider is None:
                # A pre-armed legacy helper owns its own capture and decoder.
                unit["completed"][key] = False
                return False
            before = self._sample(address, state.slot)
            if not self._capture_matches(before, state):
                raise HandoffError("RFID handoff measurement does not match capture")
            decoder = provider[0]
            tag = decoder.rfid_handoff_decode(slot, address)
            if bytes(tag.uid) != state.uid:
                raise HandoffError("RFID handoff capture UID changed")
            after = self._sample(address, state.slot)
            self._exchange(address, "poll", state.session)
            # A queued scan can replace the global RHO state while CAP is
            # copied. The per-slot UID/generation proof must remain unchanged;
            # a different global slot alone does not invalidate this copy.
            if not self._capture_matches(after, state):
                raise HandoffError("RFID handoff capture was superseded")
            unit["captures"][key] = (decoder, tag)
            applied = self._apply_tag(address, state, decoder, tag)
        elif state.result == NATIVE_OK:
            reply = box.drivers[address].query_rfid_records(
                1 << state.slot, timeout=0.5)
            if reply is not None and reply.status == box_protocol.STATUS_OK:
                name = box_protocol.RFID_SLOT_NAMES[state.slot]
                record = reply.records.get(name, "").strip("\x00")
                fields = reply.fields.get(name)
                if len(record) == 40 and fields:
                    applied = box._apply_rfid_record(slot, record, fields)
                    if applied:
                        box.rfid_live_slots.add(slot)
                        box._read_rfid_remaining(slot)
        else:
            self.last_error = "CFS RFID slot %d ended with result %d" % (
                slot, state.result)
        unit["completed"][key] = applied
        unit["pending"].pop((state.session, state.token), None)
        while len(unit["completed"]) > 32:
            first = next(iter(unit["completed"]))
            del unit["completed"][first]
            unit["captures"].pop(first, None)
        while len(unit["offers"]) > 32:
            first = next(iter(unit["offers"]))
            del unit["offers"][first]
            unit["answered"].discard(first)
            unit["answer_attempts"].pop(first, None)
        return applied

    def read_slots(self, address, local_slots):
        """None means older firmware; a set means handled, including failure."""
        if not self.enabled or not self.owner.enabled:
            return None
        box = self.owner._box()
        if getattr(box, "observation_mode", False):
            return None
        with self._operation(address, parent_read=True):
            return self._read_slots(address, local_slots)

    def _read_slots(self, address, local_slots):
        box = self.owner._box()
        self.manual_addresses.add(address)
        try:
            if not self._host_idle_for_job(address):
                raise HandoffBusy("host load, unload or print is active")
            state = self._contact(address, allow_config=True)
            if state is None:
                return None
            unit = self._unit(address)
            applied = set()
            # START replaces the single terminal event and the next API7 arm
            # clears CAP scratch. Consume an automatic DONE before that.
            state = self._advance(address, state)
            for local in local_slots:
                unit["request"] = (unit["request"] % 0xFFFFFFFF) + 1
                request = unit["request"]
                try:
                    state = self._exchange(address, "start", local,
                                           unit["session"], request)
                except HandoffError as exc:
                    # START may have been accepted before its reply was lost.
                    # Reconcile by POLL; never launch a second physical read.
                    state = self._exchange(address, "poll", unit["session"])
                    if state.request_id != request or state.slot != local:
                        raise exc
                deadline = self.reactor.monotonic() + box_protocol.RFID_FORCE_SLOT_TIMEOUT
                while True:
                    if (state.session != unit["session"] or state.slot != local
                            or state.request_id != request):
                        raise HandoffError("CFS RFID job identity changed")
                    state = self._advance(address, state)
                    if state.state == DONE:
                        if self._finish(address, state):
                            applied.add(box._global_slot(address, local))
                        break
                    if self.reactor.monotonic() >= deadline:
                        raise HandoffError("CFS single-cycle RFID read timed out")
                    self.reactor.pause(self.reactor.monotonic() + 0.1)
                    state = self._exchange(address, "poll", unit["session"])
            return applied
        finally:
            self.manual_addresses.discard(address)

    def fallback_result(self, slot):
        """Return handled/result, never scheduling a second physical scan."""
        if not self.enabled or not self.owner.enabled or self._observation():
            return False, None
        box = self.owner._box()
        address, local = box._address_slot(slot)
        unit = self.units.get(address, {})
        if not unit.get("configured"):
            return False, None
        if address in self.busy_addresses:
            return True, None
        with self._operation(address, parent_read=True):
            return self._fallback_result(slot, address, local, unit)

    def _fallback_result(self, slot, address, local, unit):
        box = self.owner._box()
        try:
            state = self._exchange(address, "poll", unit["session"])
            state = self._advance(address, state)
            if state.slot == local and state.state == DONE:
                result = self._finish(address, state, verify_cached=True)
                return True, result if isinstance(result, str) else ("record" if result else None)
            # Another slot may already be scanning. A captured terminal from
            # this slot is reusable only while its UID/generation proof remains.
            for key, capture in reversed(tuple(unit["captures"].items())):
                if key[3] != local:
                    continue
                sample = self._sample(address, local)
                if (sample.generation == key[4] and sample.current_uid == key[5]
                        and sample.measured_uid == key[5] and sample.flags & 2):
                    result = unit["completed"].get(key)
                    if result and not getattr(box, "rfid_spools", {}).get(slot):
                        old = HandoffState(DONE, local, key[6], key[0], key[1],
                                           key[2], key[5], 0, 3, key[4], sample.tick)
                        result = self._apply_tag(address, old, *capture)
                    return True, result if isinstance(result, str) else None
        except Exception as exc:
            self.last_error = str(exc)
        return True, None

    def _periodic(self, eventtime):
        if not self.enabled or not self.owner.enabled:
            return self.reactor.NEVER
        if self.owner.serial is None:
            return eventtime + 0.25
        box = self.owner._box()
        if box is None or getattr(box, "observation_mode", False):
            return eventtime + 0.25
        for address in self.owner._online():
            if address in self.manual_addresses or address in self.busy_addresses:
                continue
            try:
                with self._operation(address):
                    state = self._contact(address, self.owner._can_mutate())
                    self.last_error = None
                    if state is not None:
                        self._advance(address, state)
            except HandoffUnsupported as exc:
                # A reset clears firmware CONFIG/session. Do not reuse tokens.
                self.units.pop(address, None)
                self.last_error = str(exc)
            except Exception as exc:
                if self.last_error != str(exc):
                    logging.info("box_rfid_handoff: %s", exc)
                self.last_error = str(exc)
        return eventtime + 0.25
