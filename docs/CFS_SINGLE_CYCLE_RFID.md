# CFS vendor geometry and single-cycle RFID candidate

This is an offline-tested host candidate for the paired CFS firmware SGF1
with SLOT_LENGTH (INFO feature 0x10) and RH1. The paired R3 single-cycle path and RAM geometry binding have been verified on the reference CFS in two Bambu PETG HF slots; broader qualification and R3 print consumption remain pending.

## Configuration

The optional extra is loaded with [box_spool_profiles]. It uses the existing
serial_485 owned request queue. It never keeps a serial request or session
open while waiting for Kalico's reactor or the CFS mechanical cycle.

The shipped example is disabled and contains no calibration values:

    [box_spool_profiles]
    enabled: False
    single_cycle_rfid: True
    handoff_wait_ms: 3000
    handoff_lease_ms: 10000
    reconcile_interval: 5.0
    vendor_geometries:

To enable RH1 for future insertions/manual reads, set enabled to True.
An empty vendor_geometries list enables no geometric correction. It can still
negotiate the single-cycle reader and collect the CFS raw measurement.
No startup RF read is issued merely to restore a cached spool estimate.

After physical calibration, each geometry line has this syntax:

    vendor = profile_id, core_circumference_mm, full_circumference_mm

There are up to four numeric IDs, each assigned to one vendor. Bambulab and
Bambu Lab normalize to bambu. The values are circumferences in millimeters,
not diameters or spool flange dimensions. No example measurements are shipped
as active Bambu values. The synthetic 300/510 values appear only in tests.

Material, color, and filament preset do not select a geometry. The nominal
filament length remains per spool: tag, selected filament profile, then
existing material defaults. The host sends it with SGF1 SET_LENGTH for the
specific UID/slot; vendor catalog entries use total_mm=0.

## Operation

At box:ready, the host discovers RH1 and SGF1 separately from the retained
CFG6 build identity. Geometry entries are uploaded only while idle and read
back. A changed entry first drops its old metadata bindings with BIND0, which
preserves the runtime counter. Removed entries are disabled. If a stale UID
measure cannot satisfy BIND0, removal can still disable the entry; reassignment
waits for consistent UID data instead of reusing the old association.

Geometry writes use Box.acquire_cfs_runtime_write as well as firmware busy
checks. Print/pause, load/unload, other RFID owners, and observation mode defer
mutations. An interrupted upload keeps the host estimate authoritative.

RH1 CONFIG is off after CFS boot. The host creates a fresh nonzero session,
configures bounded wait/lease periods, and periodically polls to keep the
lease alive. A CFS reboot rejects the old session: Kalico forgets its job
tokens, negotiates a new session and reloads the geometry catalog.

For manual reads, the real Box read route uses RH1 START and short POLL
requests before the old known-vendor fast path. For insertions, a separate
timer services the handoff even while the ordinary Box poll is deferred.
The CFS retains its OEM search, rotation measurement and parking sequence.
START requires an idle CFS, a present slot and an empty hub. A busy/occupied
hub is an error; the timer never unloads or moves filament to clear it.

When the CFS asks for keys, a cached decoder for that UID is preferred;
otherwise registered decoder priority determines a single candidate.
Deriving Bambu keys is not manufacturer recognition. Classification occurs
only after the completed capture has been decoded successfully.
The existing Bambu/MIFARE helpers can decode the completed capture without
issuing force_rfid_read or another RF request.

Before applying capture metadata, the host verifies the offered session/token
and the slot UID/generation before and after copying CAP. A new queued slot
may replace global RHO while the previous slot's proof remains valid.
Coordinator operations use the Box RFID read guard and cannot answer the next
slot's key request while a capture is being copied.
The first valid geometry BIND recalculates the already acquired raw measure;
it does not rotate the spool again. The ordinary CFS remaining query then
continues to report its runtime estimate.

A lost START/KEYS acknowledgement is reconciled with POLL. It does not cause
another START or a second complete legacy read. Declined, failed and timed-out
handoffs are reported without an automatic full scan retry. Older firmware
that refuses RH1 retains the existing Box/decoder route.

Observation mode blocks RH1 CONFIG, lease-renewing POLL, START, KEYS and
DECLINE as well as geometry writes. INFO remains a passive capability query.

## Estimate continuity

The existing per-spool cache and odometer logic remain in BoxRfidEstimates.
A missing CFS value (255) leaves that local estimate intact. For a configured
vendor, a hardware percentage may replace it only if a passive SGF1 snapshot
shows the expected UID, geometry ID/revision, nominal length and matching
percentage. This rejects uncalibrated/stale CFS values after RAM loss.
No unsupported measure or new UID is treated as proof of a full spool.

No automatic scan is added to recover the cache. An eligible later natural or
manual scan can establish a fresh geometric anchor. Until then local
consumption continues to lower the persisted amount.

## Validation and known limits

Tests use wire-accurate frames and a simulated CFS. They exercise the real
Box manual-read and RfidFallback entry points, owned-session lifetimes,
cache/odometer continuity, independent material lengths, catalog replacement,
reboot/session recovery, lost acknowledgements, stale UID/generation rejection,
and observation/print/load interlocks. The actual Bambu capture decoder is
also exercised without issuing a new scan.

The firmware has one global capture scratch area and one current RH1 event.
Kalico retains pending key offers with their slot measurement generations.
Before providing keys for a subsequent slot, it drains a previous completed
capture while that slot's proof still matches. RH1 decode does not send CLEAR:
successful firmware authentication has already erased one-shot keys, and
clearing now could destroy a later slot's capture. Tests cover a skipped DONE
poll, a new slot beginning during capture copying, reentrant timer/manual
calls, and a transient capture-read failure before the next key answer.

Clients bypassing the Box read guard/owned queue are outside this serialization
contract. Changed or missing slot proof is rejected and never repaired by
silently launching another complete scan.

One handoff tries one decoder key plan. Known UID hints/cache take priority.
For explicit discovery involving several unknown key plans, the existing
legacy path remains available by setting single_cycle_rfid to False.

Hardware timing, real stack margins, antenna behavior and actual vendor
geometry remain to be qualified. No firmware flash, Kalico restart, motion
or scan was performed by this host implementation step.

## Passive calibration readout

After the paired candidate and extra are installed, read the saved measurement:

    BOX_SPOOL_GEOMETRY_STATUS ADDRESS=1 SLOT=1

SLOT is the local zero-based index; SLOT=1 selects physical slot2. This command
issues one SGF1 snapshot request. It does not initiate scanning, rotate a spool,
renew the RH1 lease or change geometry. It reports the raw circumference,
measurement generation/quality, applied geometry and runtime length/percentage.
Use a fresh valid measurement of the reference spool to establish calibration;
the test constants must not be copied as measured Bambu geometry.

Paired firmware source: https://github.com/MzTechnology97/k2-cfs-rfid-tools/pull/64
Firmware candidate SHA-256: 27cd264f36a217a98552351578f37fb4060e3d631793cc801242c0deaa6ec046

## Firmware negotiation diagnostics

The Moonraker `box_spool_profiles.handoff` status is cached and passive. Its per-CFS entries expose `supported`, `configured`, `session`, `request`, `pending_capture_count` and `error`. Reading it sends no frame and does not renew a lease. `single_cycle_rfid=True` describes configuration; it alone does not establish that the CFS supports RH1. Unsupported INFO/CONFIG responses are retained in the per-CFS error. Keys, offers and raw capture data are never included in this status.

Use the paired firmware **SGF1/RH1 R2**. The earlier `b7cd0ab3…` image is withdrawn because its handoff extension used nominal instead of physical native-call addresses. R2 corrects these calls without changing the host wire contract.


Startup CONFIG eligibility is checked before the coordinator claims its own
RFID read guard, then checked again after INFO can yield to a host operation.
The guard remains held during the wire requests and still blocks geometry
writers. Tests exercise the actual Box acquire/release methods, deferred
startup during print/load/other reads, and activity beginning during INFO.


For a targeted passive UID-length investigation, add `DETAIL=1` to the status
command. It also reads the selected slot's API7 INTERNAL_RECORD and prints
only `record_byte75`, with no authentication keys or capture scratch. It does
not start RF activity. A missing or malformed 76-byte record is rejected.


## Reference R3 hardware result, 29 October 2026

Physical slots2/4 decoded Bambu PETG HF in24.60s/20.47s through RH1, each with one request and flags0x13. The missing-length metadata byte75 remained0. Measured circumferences were508.253mm for the operator-declared new spool and302.823mm for the nearly empty spool.

The active reference configuration uses `bambu = 1, 302.8, 508.3` as a provisional two-point calibration. These values are circumferences, not diameters, and remain operator configuration rather than shipped defaults. The lower anchor includes the small amount of filament still present.

After a host restart, profile1/revision1 was uploaded to RAM, both UID-specific lengths became327000mm and BIND recalculated the retained generation1 measurements. The new session had request0: no second scan was needed. CFS and Kalico reported100%/327m for slot2 and1%/3.27m for slot4. Other slots are not classified as Bambu merely because a manually selected filament profile has that brand.

The CFS returned to IDLE and all23 CFG6 overrides were applied without error. This run does not establish R3 print-time consumption or physical auto-insertion/reconnect behavior; their offline tests remain part of the suite.
