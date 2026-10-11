# CFS v3.22 — read-only stock-task diagnostics (experimental)

**Flashed and physically validated on 2026-10-10 (experimental).** This does NOT fix the independent CFS MCU motor-activity guard (see [issue #9](https://github.com/MzTechnology97/k2-cfs-rfid-tools/issues/9)). The reference printer now runs v3.22, firmware feature `0xD7`, following the operator's controlled update. The motion/write mutex in Kalico must remain enabled.

## Firmware and host capability

The v3.22 firmware prototype reports API v2, 28 normal parameters, and a **unique feature byte `0xD7`**. Existing v3.19 (`0xF7`), v3.20 (`0xB7`) and v3.21 (`0x97`) remain supported by the paired host extra. For all four experimental firmware signatures, bulk APPLY and automatic configuration writes remain disabled, stock parameter IDs 0–6 are protected, and manual SET/RESET requires the existing v3.21/v3.22 host motion/write and RFID exclusion mutex.

The new `BOX_CFS_CONFIG_SNAPSHOT` G-code is **read-only**. It refuses to run except on the exact `0xD7` experimental feature signature with normal API catalogue count 28. It issues exactly four fixed-address firmware GETs (IDs 28–31) and formats four 16-bit little-endian words. It acquires and always releases the shared RFID transport lock.

| Diagnostic GET ID | Internal address | Interpretation |
|---|---|---|
| 28 | `0x2000057E` | Raw bytes 0–1 of a **candidate** stock workflow/motor state |
| 29 | `0x20000580` | Raw bytes 2–3 of that candidate |
| 30 | `0x20003904` | Raw bytes 0–1 of a **candidate** shared task structure |
| 31 | `0x20003906` | Raw bytes 2–3 of that candidate |

**These are not validated motor-active predicates.** No generic memory reader, writable diagnostic parameter, or user-selected address is provided. The firmware rejects SET/RESET of the new IDs. Snapshot reads do not prove motion is safe or stopped, and repeated polling must not degrade RS-485 reliability.

## Tests and deployment status — 2026-10-10

- Firmware built deterministically: 178140 bytes, SHA-256 `40dbaad88de7b6f9435592fa03b081e9b96663a9e84896b1ad8021a8f13cf8c7`.
- ARM emulation: existing 28 GET, 21 advanced SET/GET/RESET, bounds and busy guards passed, as did four exact raw internal-memory GETs, no sidecar writes from those GETs, SET/RESET refusal on IDs 28–31, and rejected GET32.
- Mocked host test `python3 test/test_cfs_v322_readonly_probes.py`: fixed-ID GET list, signature gating, no writes, and guaranteed release of RFID lock on exception passed.
- Existing two timing hooks are unchanged. No new feeder, hub, RFID, motor or heater hook has been enabled.
- **On-device CFS v3.22 validation completed:** after an initial transient `NO_RESPONSE` state that later recovered, API2 feature `0xD7`, 28 normal parameters and the read-only diagnostics were confirmed. Eight idle snapshots all returned `0000:0000` motor candidate and `0001:0000` task candidate. One slot1 heater-free PLA load (11.435 s) and unload (14.681 s) completed, path/sensors clear at end, no parameter changes. **The final independent MCU guard remains an open engineering problem.**

The firmware's only current standalone write guard checks the stock BOX_STATE mode byte. Previous real load tests showed `BOX_STATE=IDLE` even while `box.operation.active=true`; a mode flag alone cannot establish motor inactivity. The host mutex is a defense-in-depth control for Kalico-originated requests, not proof that a raw serial writer is safe.

After a verified diagnostic flash, compare many snapshots with measured load, unload and idle transitions. Require consistent changes across both feeder and hub operations, RFID background work, aborted moves and errors before using any candidate as a control. Never bypass the host mutex to force a write during physical motion.

Related experimental firmware branch: [v3.22 sources and offline validation](https://github.com/MzTechnology97/k2-cfs-rfid-tools/tree/feature/cfs-v322-readonly-state-probes/firmware/v3.22-readonly-state-probes).

## Important on-device timing limitation

G-code requests for `BOX_CFS_CONFIG_SNAPSHOT` dispatched while a physical CFS move was active were **queued until after the move**, taking 10.545 seconds during loading and 13.797 seconds during unloading. Both `0000:0000 / 0001:0000` replies occurred **after physical movement completed**. These measurements must **not** be described as in-motion samples and cannot validate an independent MCU motor-busy guard. Further work requires a correctly timestamped, low-impact MCU-side or passive event sampler that does not bypass the host write interlock.

The temporary initial `NO_RESPONSE` condition recovered; its cause has not been proven. The CFS is operational, with Klipper `ready/standby`, CFS `IDLE`, 28 parameters at baseline, heaters off and no runtime error.

## 2026-10-11 source-level capability cleanup (unflashed)

The v3.22 firmware still exposes a *28-entry* API2 parameter catalogue.
The previous host screen displayed advanced IDs 9–27 as if they were effective
controls. Source-level ARM audit confirms that **ONLY IDs 7 and 8** reach the
original stock execution callsites (`0x08011116`, `0x080112CA`).

| IDs | Firmware capability | Permitted Kalico behavior |
| --- | --- | --- |
| 0–5 | stock speed table, GET only | read-only |
| 6 | stock reverse-detooth toggle, GET only | read-only |
| 7 | hub transition timing, real MOVW hook | supervised manual SET/GET/RESET |
| 8 | insert sensor timeout, real MOVW hook | supervised manual SET/GET/RESET |
| 9–27 | volatile sidecar storage; NO firmware execution hooks | GET for historical bench diagnosis; manual SET now **blocked** |
| 28–31 | fixed opaque MCU-state words | read-only diagnostic SNAPSHOT; never an idle/busy certificate |

The v3.19 feature `0xF7` has **zero** verified active advanced hooks.
The feature signatures `0xB7` (v3.20), `0x97` (v3.21), and `0xD7`
(v3.22) have only `7,8`. The host now calls an explicit feature-to-hook
gate **before submitting any manual SET**. This does NOT change the currently
installed experimental MCU image, which still accepts raw SET on IDs 9–27.
The host cannot protect an external raw RS-485 writer.

Both source config templates now list all 28 recognised parameter names with
English comments, correct units and truthfully marked active/inactive
functionality. The reference config uses `auto_apply: false` with every
parameter commented. No live K2 Pro config has been modified by this patch.

The firmware does not dispatch `CONFIG_V2_DESCRIBE (0x14)`, and old v3.13
legacy `0x0D–0x0F` comments do not reflect the v3.22 dispatcher. Firmware
descriptor defaults and limits in the Python host may be labelled
`HOST_EXPECTED` and are not device-derived in this experimental signature.
The previous `override_mask=0` is a **host placeholder**, not evidence that
the CFS has no currently stored advanced overrides.

The v3.12 stock-geometry third-party remaining code (fixed proxy total 330 m,
type-4 stock odometer) remains unchanged. Kalico separately maintains
spool-specific estimates from tag information or profile/material nominal
lengths. Do not equate these two reference-length systems.

**Release gates remain closed:** independent MCU motor-activity proof, heap
ownership and bootloader flash partition safety are still absent. No flash,
auto-apply, direct unsafe protocol writes or production merge is authorised.
Firmware audit companion:
[fix/cfs-v322-source-audit](https://github.com/MzTechnology97/k2-cfs-rfid-tools/tree/fix/cfs-v322-source-audit).
