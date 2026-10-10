# K2 Pro CFS v3.19 volatile RAM runtime configuration

**Status: experimental bench-only**, tested on a single `cfs0_050_G32 / cfs0_000_153` CFS. The v3.19 firmware contains 28 readable parameter IDs and 21 advanced manually writable `uint16` overrides, but **none of the advanced motor/RFID hooks is enabled**. Adjustments to advanced values therefore DO NOT currently change actual motor movements.

The firmware BIN, assembly, builder, CRC, SHA-256, emulation tests and validation evidence are published separately in [k2-cfs-rfid-tools v3.19](https://github.com/MzTechnology97/k2-cfs-rfid-tools/tree/main/firmware/v3.19-volatile-ram). The installer-helper menu 40 uses an exact SHA-256 and Creality boot-token match; it never flashes without explicit confirmation. Keep the v3.13 firmware available for rollback.

## Compatibility and feature negotiation

- Runtime API **v1** (v3.13): six stock speed IDs 0–5, `BOX_CFS_CONFIG_INFO`, SET/RESET as before.
- Runtime API **v2 GET-only** (v3.18, feature byte `0xE1`): 28 GET values, no firmware DESCRIBE or writing.
- Runtime API **v2 RAM bench** (v3.19, feature byte **`0xF7`**): 28 GET values; manual `BOX_CFS_CONFIG_SET/RESET` restricted to advanced IDs **7–27**. IDs 0–6 are protected. The firmware rejects out-of-range SET and commands while the stock CFS task is busy.
- Other API v2 firmware is not assumed to support these commands or layout. Unsupported protocol responses and descriptor mismatch are handled as errors.

**Critical rule:** when the firmware returns feature mask `0xF7`, `box_cfs_runtime.py` skips `auto_apply`, refuses `BOX_CFS_CONFIG_APPLY`, and prevents manual SET or individual RESET of IDs 0–6. This ensures the six v1 overrides from an existing `box.cfg` are not inadvertently sent to this experimental build. Status exposes `firmware_features` on Moonraker's `box_cfs_runtime` object.

## Commands for a controlled bench

With the CFS idle and after checking `firmware_features=247` (`0xF7`):

```gcode
BOX_CFS_CONFIG_DIAG ALL=1
```

This queries GET for every ID. With v3.19 the `source=HOST_EXPECTED` label means bounds and defaults come from the Kalico catalogue, **not** from the CFS; only GET values are actual device responses. An ID 6 expected value of `None` means it is hardware-captured and has no fixed default.

On the reference printer one manually supervised check passed for ID 18:

```gcode
BOX_CFS_CONFIG_SET PARAM=rfid_neighbor_detect_delay_ms VALUE=401
BOX_CFS_CONFIG_DIAG ALL=1
BOX_CFS_CONFIG_RESET PARAM=rfid_neighbor_detect_delay_ms
BOX_CFS_CONFIG_DIAG ALL=1
```

The CFS reported 401 after SET and 400 after RESET, but **this is not permission to change other settings in normal operation**. Do not run bulk SET tests, move filament, perform RFID rotations or start printing using v3.19 until the remaining tests pass. For normal motion use the rollback v3.13 or vendor stock firmware as appropriate.

## Persistence and limits

v3.19 stores overrides in a 64-byte volatile RAM sidecar and moves the heap start by 64 bytes. The boot wrapper clears sidecar magic and mask; this has been emulated but **post-power-cycle memory safety has not been hardware proven**. The CFS does not write EEPROM/RFID tags through the new parameter API. Runtime speed and RFID hook changes remain **disabled**. In particular, changing an advanced RFID delay through API v2 does not affect the original timing path until the corresponding firmware hook is added and independently validated.

The six v1 speed settings and 21 advanced examples in `config/k2/macros/box.cfg` are kept commented by default in the distribution. The user must intentionally enable them only for firmware known to support them. Generic automatic application of arbitrary advanced values is not implemented.

## Verification and recovery

- User hardware: v3.19 `0xF7`, 28/28 GET success, ID18 manual SET/GET/RESET success, three further full read-only passes success (2026-10-10).
- ARM emulation: 21 individual advanced set/get/reset cycles, 42 invalid bound rejections, corrupt sidecar fallback, busy-state rejection, heap wrapper and stock-protection checks.
- Not validated: all advanced writes on real hardware, long-running CFS operations, EEPROM persistence (intentionally absent), power-cycle isolation, moved motors/real RFID read timing.
- If the CFS fails to boot or responds with `0xFFFF`, stop bench operations and use the installer helper's v3.13 candidate or the Creality recovery updater. Do not continue writing or moving the CFS during a fault.

Run the host mock regression without touching the printer:

```bash
python3 test/cfs_runtime_v319_standalone.py
```
