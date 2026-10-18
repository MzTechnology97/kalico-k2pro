# Kalico CFG6 - Advanced runtime controls (English)

**Experimental development, not hardware-qualified. Do not use CFG6 for production prints.** The physical CFS still runs CFG5T; no active Kalico module is replaced.

Required firmware identity: K2OH revision v323-cfg6, features 0xE3, API v2, 28 settings. All fields are checked before allowing SET. OpenCFS (OCFS), stock Creality, CFG4 and CFG5T cannot receive CFG6 writes.

## Writable parameters

New IDs: 6 (reverse de-tooth), 9 (hub pullback), 11/12/13 (RFID hub speeds), 14/15/16/17 (RFID timings), 18/19 (neighbor spool motion and settling), 21 (feed recovery timeout), 23 (neighbor search attempts), 24 (de-tooth timeout), 27 (pretension cycles).

Existing 1/2/3/5 (hub speeds), 7/8 (hub timing), 20 (total feed timeout), 22 (odometer stall) remain effective: **23 writable IDs total**. IDs 0/4 and 10/25/26 remain GET-only because there is no effective stock path to tune.

## Safety and unit contract

- ID6 is a private volatile RAM gate for the existing de-tooth motor action; unknown stock status bytes are not written.
- ID17 is correctly hooked to the 400ms RFID pre-read delay at 0x08012A60, not the distinct 10ms delay.
- ID27 controls actual pretension-loop comparison at 0x08012616.
- ID8 values must be divisible by 10ms; ID20 and ID22 by 20ms.
- No console SET/RESET/APPLY. Configuration is accepted from box_cfs_runtime only at Klipper startup with deliberately enabled auto_apply.
- CFG5T's live type-4 accounting remains present with the G-code consumption fallback; physical hardware verification is still necessary.

## Example (commented pending hardware qualification)

    [box_cfs_runtime]
    auto_apply: False
    #reverse_detooth_enable: True
    #hub_pullback_timeout_ms: 5000
    #rfid_measure_speed: 150
    #rfid_neighbor_retry_count: 1
    #load_retry_count: 1

The host tests are mocks; no firmware or printer motion is exercised. Retain CFG5T and v3.22 rollback images.

[Guida italiana](CFS_RUNTIME_CFG6_IT.md) | [CFG6 firmware source](https://github.com/MzTechnology97/k2-cfs-rfid-tools/tree/feat/cfg6-full-hooks-lab/firmware/v3.25-cfg6-lab)
