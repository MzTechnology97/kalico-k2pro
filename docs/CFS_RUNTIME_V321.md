# CFS v3.21 — safe runtime write guard (experimental)

> The experimental v3.21 CFS was **flashed and physically tested on 2026-10-10**, including successful SET/RESET immediately after unloading. **It is NOT approved for production release:** the independent MCU motor-active guard still requires hardening/verification. Keep the v3.13 recovery firmware available.

## Root cause corrected in the v3.21 firmware candidate

The old firmware checked `STOCK_STATE.active_slot_raw` (RAM `0x200001F2`) for whether the motor was busy. A real slot-1 load/unload leaves this byte at `1` even when the CFS is `IDLE` and the printhead sensor is clear. Thus v3.20 rejects SET and RESET until power cycling the MCU.

The original Creality `CMD_BOX_STATE=0x0A` sender at `0x0801BD26` reads its operating state from RAM byte **`0x200037D2`** (`0x200037D0+2`), independently of the retained RFID slot. The candidate firmware now permits SET/RESET only when that byte equals `0` (**IDLE**); it rejects `1=PRELOAD`, `2=PRINT`, `3=RELOAD`, `4=ERROR`, `5=TEST` and unknown states. This is a fail-closed control and must still be tested while the motors operate.

## Kalico host protection

The v3.21 candidate advertises API v2, 28 parameters and the **`0x97`** feature marker. The companion extra continues to accept v3.19 `0xF7` and v3.20 `0xB7`, while keeping **auto-apply and bulk APPLY disabled** for all three experimental signatures. Stock IDs 0–6 remain protected.

For `0x97` manual writes, the host requires the following conditions *before and again after acquiring the shared RFID read lock*:

- `box.state == IDLE`, `state_code == 0`, no active operation and `loaded_slot == -1`.
- Printhead filament switch sensor must explicitly report `false`.
- Printer `print_stats.state == standby`.
- No other RFID read owns the lock; the runtime write holds the same lock until the SET/RESET response is received.

The firmware performs an **independent** check against the MCU state. Host status is asynchronously polled, so the host checks alone do not establish movement safety.

## Validation

- A real CFS load/unload previously confirmed retained RFID slot `1` alongside actual `IDLE` and a clear head sensor.
- v3.21 ARM emulation passed: all 28 GET, 21 individual advanced SET/GET/RESET cycles, 42 invalid bound rejections, preserving original ID7/ID8 callsites, and rejection of SET and RESET for all states 1–6 and 255 without sidecar writes. The retained RFID slot is no longer confused with motor state.
- Host mock regression `python3 test/test_cfs_v321_idle_guard.py` passed, including error/unknown/printing/sensor-active and RFID-lock races.
- The CM5 was restarted with this Kalico extra and stayed `ready/standby` on v3.20 `0xB7`. No v3.21 firmware has been flashed.

## Deployment gate

The exact v3.21 binary was built locally on CM5: 178100 bytes, SHA-256 `4a918febbee1e411ae736acb7fd79eb718e3401a641ee947008bbecf1e6c0b19`. It is staged in the local installer helper, with v3.20 and v3.13 rollbacks. It is **not** currently shipped as a validated binary on GitHub.

The earlier `sudo -n true` installer issue was overcome by the device operator, who installed firmware v3.21. On-device checks found signature `0x97`, 28/28 default GET and `ready/standby` with CFS `IDLE`. A real PLA slot-1 load (11.529 s) and unload (14.536 s) completed; both ID7 and ID8 SET/RESET succeeded immediately after unloading **without electrical MCU restart**, even though RFID stock `active_slot_raw` remained 1. After two cycles, **all 21** advanced IDs passed SET/GET/RESET and all 28 defaults were restored.

**Critical distinction:** a harmless ID18 SET sent during active loading was rejected by Kalico *before* load completion. An unload-time request was queued until movement finished, and was correctly accepted once idle; it is not proof of unload-time rejection. During early `feeding_to_buffer`, Klipper's CFS status showed `box.state=IDLE` but `box.operation.active=true`. Thus the independent MCU candidate checking only `BOX_STATE==IDLE` is **not proven to block writes during every active motor stage**. Do not bypass the host preflight, do not auto-apply values, and do not merge for production until the MCU guard is strengthened or independently validated. See [full v3.21 real-hardware evidence](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/feature/cfs-v321-busy-guard-forensics/firmware/v3.21-busy-guard/hardware-validation-2026-10-10.json).

See [CFS firmware issue #7](https://github.com/MzTechnology97/k2-cfs-rfid-tools/issues/7) and [draft v3.21 firmware PR #8](https://github.com/MzTechnology97/k2-cfs-rfid-tools/pull/8).

## Additional host operation mutex (2026-10-10)

The `Box._operation()` context now refuses to begin a physical CFS operation
while a manual runtime-config write is in progress. Conversely, SET/RESET
claims `Box.acquire_cfs_runtime_write()` and refuses to proceed if a physical
operation has already begun or a filament change is pending. Its owner-checked
release occurs in `finally`, and the existing RFID transport lock is held
throughout the on-wire write. The operation/transaction locks are acquired
in the same Klipper G-code reactor and do not introduce another blocking
serial operation between checking and reserving the mutex.

This closes an **application-level time-of-check/time-of-use gap** that could
occur if a physical operation were started after a host status preflight but
before the SET/RESET transaction completed. It does not authorize firmware
writes while motors run.

**Important remaining firmware limitation:** The v3.21 MCU checks the stock
`CMD_BOX_STATE` load-mode byte (`0x200037D2`). On real hardware its value
was `IDLE` during the early `feeding_to_buffer` motor operation. Independent
CFS MCU motor/task-running status has **not** been identified or proven; this
host lock must not be presented as a firmware-side fix. A direct firmware
protocol caller that bypasses the Klipper safety layer remains outside
the demonstrated safety boundary. Never disable the host guard or auto-apply.

The true box/feeder protocol also differentiates an idle state from
feed/change mode and specific transient busy events. Do not repurpose
`BOX_STATE` mode values or `active_slot_raw` to represent all physical
movement. Until independent MCU task activity can be verified, keep the
CFS v3.21 firmware release experimental and leave the production merge
gated on [issue #9](https://github.com/MzTechnology97/k2-cfs-rfid-tools/issues/9).

Offline regression tests:
`test/test_cfs_v321_idle_guard.py` and
`test/test_cfs_v322_motion_mutex.py`.
