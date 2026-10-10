# CFS v3.22 — read-only stock-task diagnostics (experimental)

**Not yet flashed.** This does NOT fix the independent CFS MCU motor-activity guard (see [issue #9](https://github.com/MzTechnology97/k2-cfs-rfid-tools/issues/9)). The running printer remains on v3.21, firmware feature `0x97`. The motion/write mutex in Kalico must remain enabled.

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
- **The CFS v3.22 firmware has not been flashed; no live diagnostic values are available.** The final independent MCU guard remains an open engineering problem.

The firmware's only current standalone write guard checks the stock BOX_STATE mode byte. Previous real load tests showed `BOX_STATE=IDLE` even while `box.operation.active=true`; a mode flag alone cannot establish motor inactivity. The host mutex is a defense-in-depth control for Kalico-originated requests, not proof that a raw serial writer is safe.

After a verified diagnostic flash, compare many snapshots with measured load, unload and idle transitions. Require consistent changes across both feeder and hub operations, RFID background work, aborted moves and errors before using any candidate as a control. Never bypass the host mutex to force a write during physical motion.

Related experimental firmware branch: [v3.22 sources and offline validation](https://github.com/MzTechnology97/k2-cfs-rfid-tools/tree/feature/cfs-v322-readonly-state-probes/firmware/v3.22-readonly-state-probes).
