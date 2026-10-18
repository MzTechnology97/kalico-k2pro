# Kalico — Experimental CFG5T Integration (EN)

**Status: draft PR, not hardware-qualified.** The patched stock CFS K2OH CFG5T image identifies with opcode 0x7A as K2OH / v323-cfg5t, API2 and features 0xE5. This runtime contract is NOT compatible with stock Creality, arbitrary third-party firmware, or the independent OpenCFS OCFS family.

The box_cfs_runtime module applies parameters exclusively from CFG at startup. SET/RESET/APPLY console commands are unavailable. Actual writable CFG5T IDs are 1, 2, 3, 5, 7, 8, 20, 22. IDs 20/22 use real milliseconds in exact 20ms steps; ID8 requires multiples of 10ms.

Live-remaining capability is enabled only for the exact CFG5T identity/features contract. Box can follow CFS-reported consumption and preserve G-code estimation as fallback. OpenCFS is recognized separately and receives no CFG5T SET operations.

## Suggested hardware verification order

1. Flash the supervised experimental CFG5T candidate only with the printer idle and verified CFG4/v3.22 rollback images available.
2. Keep the old runtime extra and auto_apply disabled initially; verify RS-485, stepper discovery and the K2OH v323-cfg5t identity.
3. Only after the read-only bus check, deploy the matching PR extra, restart idle Kalico, and validate diagnostic responses.
4. Enable CFG auto_apply deliberately only for the intended values, never during a print.
5. Confirm physical type-4 consumption during a controlled print; simulated ARM accounting cannot substitute for real odometer and transport testing.

[Firmware source and BIN](https://github.com/MzTechnology97/k2-cfs-rfid-tools/tree/main/firmware/v3.24-cfg5t) | [Guida italiana](CFS_RUNTIME_CFG5T_IT.md)
