# CFS v3.21 — safe runtime write guard (experimental)

> The physical CFS firmware **has not yet been flashed or validated**. The external CM5/Kalico companion has been staged and validated with the existing v3.20 firmware. Keep the v3.13 recovery firmware available.

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

An environment issue prevents guarded installation: `sudo -n true` hangs on the CM5, so the flashing helper cannot safely stop Klipper and hand over the UART to T113. Do not bypass that control. After privileged service access is restored, the first tests must demonstrate both rejection of SET/RESET during active physical load/unload and success after unloading **without an electrical power cycle**. Only then repeat the timing comparisons and consider release.

See [CFS firmware issue #7](https://github.com/MzTechnology97/k2-cfs-rfid-tools/issues/7) and [draft v3.21 firmware PR #8](https://github.com/MzTechnology97/k2-cfs-rfid-tools/pull/8).
