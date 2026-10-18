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

On the reference printer, all 21 advanced IDs (7–27) have now passed separate SET → GET → RESET cycles, and a four-override isolation plus RESET ALL test restored the complete 28-value baseline. The following is the original manual ID 18 smoke test:

```gcode
BOX_CFS_CONFIG_SET PARAM=rfid_neighbor_detect_delay_ms VALUE=401
BOX_CFS_CONFIG_DIAG ALL=1
BOX_CFS_CONFIG_RESET PARAM=rfid_neighbor_detect_delay_ms
BOX_CFS_CONFIG_DIAG ALL=1
```

The CFS reported 401 after SET and 400 after RESET, but **this is not permission to change other settings in normal operation**. Since the effective-hook guard (kalico-k2pro #70, ported to `k2-pro-openhost`), Kalico refuses this SET: on v3.19 no ID is connected to a stock callsite, and on v3.20-v3.22 only IDs 7 and 8 are. See [CFS runtime v3.22](CFS_RUNTIME_V322.md). Do not run bulk SET tests, move filament, perform RFID rotations or start printing using v3.19 until the remaining tests pass. For normal motion use the rollback v3.13 or vendor stock firmware as appropriate.

## Persistence and limits

v3.19 stores overrides in a 64-byte volatile RAM sidecar and moves the heap start by 64 bytes. The boot wrapper clears sidecar magic and mask; this has been emulated but **post-power-cycle memory safety has not been hardware proven**. The CFS does not write EEPROM/RFID tags through the new parameter API. Runtime speed and RFID hook changes remain **disabled**. In particular, changing an advanced RFID delay through API v2 does not affect the original timing path until the corresponding firmware hook is added and independently validated.

The six v1 speed settings and 21 advanced examples in `config/k2/macros/box.cfg` are kept commented by default in the distribution. The user must intentionally enable them only for firmware known to support them. Generic automatic application of arbitrary advanced values is not implemented.

## Verification and recovery

- Real CFS hardware (2026-10-10): v3.19 `0xF7`, 28/28 GET, **21/21** individual advanced SET/GET/RESET cycles, four independent concurrent overrides and RESET ALL restoring all 28 values.
- Read stress: **60** complete GET sweeps (**1,680** individual GET operations), zero errors and no changes.
- Host guard: five rejected requests (stock IDs, invalid bounds, APPLY), with baseline unchanged. These exercised Kalico's validation, **not a direct MCU protocol-bypass test**.
- Host restart: a temporary ID18 override (402) survived **Klipper restart**, and was reset to 400 after reconnection; this is distinct from rebooting or electrically power-cycling the CFS MCU.
- ARM emulation: 21 individual advanced set/get/reset cycles, 42 invalid bound rejections, corrupt sidecar fallback, busy-state rejection, heap wrapper and stock-protection checks.
- **Physical MCU rail power-cycle validated (2026-10-10):** T113 GPIO-controlled power was measured `ON → OFF (2 s) → ON`. ID 18 RAM override was `400 → 405` before the cut and returned to **400** after reboot without RESET; all 28 CFS values equaled the initial baseline. After a transient startup CFS query timeout, manual **read-only** INFO succeeded. Motor-control discovery recovered on the **second** startup attempt following transient RS-485 timeouts.
- Still unvalidated: repeated/long-term physical power-cycle stability, real load/unload/RFID motion under physical load, direct MCU-boundary rejection bypassing host checks and alternative CFS generations. EEPROM persistence is intentionally absent.
- If the CFS fails to boot or responds with `0xFFFF`, stop bench operations and use the installer helper's v3.13 candidate or the Creality recovery updater. Do not continue writing or moving the CFS during a fault.

## K2-OpenHost firmware identity and when values are written

From this version Kalico writes runtime values **only to K2-OpenHost firmware, and only at the first contact**.

**Identity.** At each contact Kalico reads command `0x7A`. Its reply has the OpenCFS layout: magic, format, variant, bench level, flags, revision and Creality version.

| Answer | Firmware | Values written |
| --- | --- | --- |
| `"K2OH"`, format 1, and `CONFIG_V2_INFO` features `0xC7` | v3.23 K2-OpenHost | yes |
| `"K2OH"` with another feature byte | an incomplete v3.23 build (for example step 1, still `0xD7`) | no |
| `"OCFS"` | OpenCFS | no |
| no answer | stock, v3.13-v3.22 | no |

**When.** Values are written at the first contact after `box:ready`, which is sent at Klipper start and when the CFS comes back. They are skipped while the printer is printing or paused, and applied at the next contact. Each value is read back and must match. A busy CFS is retried. `auto_apply: false` turns writing off.

**Console.** Only `BOX_CFS_CONFIG_INFO`, `DIAG` and `SNAPSHOT` remain, and they are read-only. There is no SET, RESET or APPLY command: values come only from `[box_cfs_runtime]`. `INFO` also prints the identity and the result of the last apply (`apply_state` in the status, together with `identity` and `k2oh`).

**v3.23 contract** (`K2OH_PARAMETERS`; the firmware converts every value to its stock unit):

- **Times are in milliseconds.** `insert_sensor_timeout_ms` defaults to 7000; the earlier catalogue used 10 ms steps, so it showed 700. `feed_total_timeout_ms` defaults to 25000 and `odometer_stall_timeout_ms` to 500; they replace the 20 ms tick options, and the old `*_20ms_ticks` names are still accepted and converted. `rfid_pre_measure_settle_ms` defaults to the stock 400 ms; the old catalogue said 10.
- **Writable:** IDs 1-3, 5 (hub speeds), 6 (reverse de-tooth), 7-9, 11-24 and 27.
- **Read-only:** IDs 0 and 4, because the feeders are driven on/off and a speed has no effect, and IDs 10, 25 and 26, because their stock code never runs. A read-only option in the config is ignored with a warning and listed in `ignored_options`; it does not stop Klipper.
- A value outside the limits, such as the old `insert_sensor_timeout_ms: 700`, is a config error.

The firmware side is tracked in k2-cfs-rfid-tools `firmware/v3.23-k2oh` (draft #57). Tests: `test/test_box_cfs_runtime_k2oh.py`.

## Several CFS units

`[box]` enumerates up to four CFS units (addresses 1-4). `box_cfs_runtime` follows it:

- **Without `address:`**, every CFS that `[box]` brings online gets its own runtime unit: its own probe, feature byte, values and errors. The first contact runs on `box:ready`, after each enumeration and rediscovery. The configured overrides are offered to every unit, and each unit decides by its own firmware. A v3.19-v3.22 build or the stock firmware refuses them as described above, so different firmware on different units is fine.
- **With `address: N`** only that CFS is handled, as before; the other units are left alone.
- **Commands** take `ADDRESS=<1-4>`, for example `BOX_CFS_CONFIG_INFO ADDRESS=2`. The default is the configured address, or the first CFS online. With several units online, the reply starts with `CFS N:`. An offline address, or one outside `address:`, is refused.
- **Status:** the top-level fields describe the default unit, as before, and `boxes` lists every unit that has answered or failed, by address.
- The CFS management itself (enumeration, slots, loads) stays in `[box]` and is not changed by this.

## Experimental builds after v3.19 (v3.20-v3.22)

The CFS on the reference printer has since run later experimental builds, which `box_cfs_runtime.py` recognises by their feature byte. The firmware images are not published yet; the labels are the ones the code uses.

| Feature byte | Build | Host behaviour |
|---|---|---|
| `0xF7` | v3.19 RAM bench | auto-apply and `BOX_CFS_CONFIG_APPLY` refused; manual SET/RESET only on IDs 7-27 |
| `0xB7` | v3.20 | as v3.19 |
| `0x97` | v3.21 | as v3.19, and every manual SET/RESET is guarded (below) |
| `0xD7` | v3.22 diagnostic | as v3.21, plus `BOX_CFS_CONFIG_SNAPSHOT` |

- **Guarded writes (`0x97`, `0xD7`):** a SET/RESET runs only with the CFS `IDLE` (state code 0), no slot loaded, no Box operation active, the printhead filament sensor clear, the printer in `standby` and no RFID read in progress. While it runs, the host holds the Box RFID-read lock and a new CFS runtime-write lock (`Box.acquire_cfs_runtime_write`), which makes `Box._operation()` refuse a load or unload until the write ends. The firmware checks the live CFS state on its own as well; the host guard is defence in depth, not a replacement.
- **`BOX_CFS_CONFIG_SNAPSHOT` (`0xD7` only):** reads the fixed GET IDs 28-31, two opaque internal state words of the CFS firmware, under the RFID-read lock. It is diagnostics only: the values do **not** prove that the motors are idle. No address can be chosen and nothing is written.
- **First print on an experimental build (2026-10-15):** a 34 h PETG-CF print ran with the CFS on `0xD7`. When slot 3 ran out, the automatic runout swap loaded slot 4 in about 43 s and the print went on (K2-OpenHost `TEST_STATUS`). The runtime overrides were not used during the print: `0xD7` refuses auto-apply, so the CFS ran with its stock values. This is one swap on one CFS, not a validation of the builds' motion hooks, which stay disabled.
- Tests: `test/test_box_cfs_runtime_v322.py` covers the feature bytes, the protected IDs, the guarded writes, the snapshot and the Box lock, against mocks.

Run the host mock regression without touching the printer:

```bash
python3 test/cfs_runtime_v319_standalone.py
python3 -m pytest test/test_box_cfs_runtime_v322.py
```

For sanitized evidence and timing measurements, see [v3.19 hardware validation](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/firmware/v3.19-volatile-ram/hardware-validation-2026-10-10.json). One transient startup discovery RS-485 timeout was observed following the Klipper restart; motor initialization completed and subsequent CFS communication recovered.
