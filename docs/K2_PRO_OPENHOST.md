# K2 Pro / K2-OpenHost integration

## Upstream lineage

This repository is directly forked from **Jacob10383/kalico**, which is based on the **KalicoCrew/kalico** community firmware, itself derived from **Klipper3d/klipper**. K2-OpenHost preserves those upstream credits and limits its own claims to the integration/configuration work performed here.

## Repository role

`kalico-k2pro` is intended to become the single integrated Kalico tree used by the external CM5 in the K2-OpenHost architecture.

The `main` branch is the Jacob-derived reference line. The active `k2-pro-openhost` branch is where K2 Pro integration is assembled and tested.

## What the active branch adds

- a K2 Pro configuration baseline derived from the public `luketot/kalico-for-K2-Pro` work;
- Jacobean K2 extras synchronized from `MzTechnology97/k2-pro-custom-firmware:k2-openhost`;
- K2 Pro CFS four-byte state compatibility;
- protected CFS observation mode;
- an automated extra-sync/compile workflow.

The project has intentionally avoided modifying unrelated Kalico core modules for the current K2 Pro integration.

## Why the final CFG files are not here yet

The K2 Pro test machine already has known-good Cartographer, motor-control and other tuned `.cfg` values. Those will be migrated only after the CM5 transport/control stack is stable. This avoids mixing protocol debugging with machine-calibration changes.

## Hardware state already proven

- Main MCU bridged to external Kalico;
- Nozzle MCU bridged simultaneously;
- RS-485 path bridged;
- closed-loop X/Y read traffic verified;
- CFS discovery/address/read traffic verified;
- real Jacobean `Box()` observation test completed with 35 TX / 35 RX and zero transport errors;
- mutation function `0x0D` blocked before TX by the CFS observation guard.

See **MzTechnology97/K2-OpenHost** for the detailed evidence and test chronology.

## Next step

Run a complete Kalico/Klippy service from the `k2-pro-openhost` branch on the CM5 with CFS still in observation mode, before enabling any mechanical CFS operation.