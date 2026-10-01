# Kalico K2 Pro — K2-OpenHost fork

This repository is a **Creality K2 Pro / K2-OpenHost integration fork** of **[Jacob10383/kalico](https://github.com/Jacob10383/kalico)**.

It is not a replacement for the original Kalico project identity. Upstream lineage and authorship are:

1. **[Klipper3d/klipper](https://github.com/Klipper3d/klipper)** — original Klipper project and contributors;
2. **[KalicoCrew/kalico](https://github.com/KalicoCrew/kalico)** — Kalico community fork and contributors;
3. **[Jacob10383/kalico](https://github.com/Jacob10383/kalico)** — Jacob's K2-oriented Kalico work and the direct upstream of this fork;
4. **MzTechnology97/kalico-k2pro** — K2 Pro/OpenHost integration and validation work.

All upstream licenses, authorship and project references remain applicable.

## Why this fork exists

This fork is the integrated Kalico tree used by **[K2-OpenHost](https://github.com/MzTechnology97/K2-OpenHost)**, a project that moves the main Kalico/Klipper workload of a Creality K2 Pro to an external Linux host/CM5 while retaining the original K2 electronics and using the T113 as a hardware/UI bridge.

The active hardware-validation branch is:

```text
k2-pro-openhost
```

## Branches

### `main`

Tracks the Jacob-derived Kalico base as closely as practical. Fork-specific documentation is kept here so GitHub visitors can understand the repository purpose without confusing it with the generic upstream Kalico repository.

### `k2-pro-openhost`

Current K2 Pro integration branch. It contains the real OpenHost runtime work, including K2-specific extras, motor-control integration and the current machine configuration/test documentation.

## Current OpenHost transport

The stable external-host mapping is:

```text
/dev/ttyUSB0 -> T113 ttyGS0 -> ttyS2 -> Main MCU
/dev/ttyUSB1 -> T113 ttyGS1 -> ttyS3 -> Nozzle MCU
/dev/ttyUSB2 -> T113 ttyGS2 -> ttyS5 -> RS-485 / CFS / closed-loop
Cartographer -> direct USB on CM5, preferably /dev/serial/by-id/...
```

A fourth multiplexed T113 gadget channel for Cartographer was prototyped and carried real Cartographer MCU traffic, but it is no longer the preferred architecture. Direct USB handles Cartographer reset/re-enumeration more naturally and keeps the three K2 gadget channels dedicated to the original hardware buses.

## Current hardware milestone — 2026-10-01

On the real K2 Pro, branch `k2-pro-openhost` has now validated:

- native AArch64 Kalico runtime and C helper;
- simultaneous Main MCU + Nozzle MCU sessions;
- RS-485 closed-loop motor communication;
- normal CoreXY movement;
- X/Y sensorless/stall homing;
- correct Z direction;
- complete homing with the original PRTouch stack;
- bed/nozzle/chamber heater operation and PID tuning;
- emergency shutdown of active heater loads;
- successful Klippain-ShakeTune resonance measurement;
- protected CFS observation mode and K2 Pro 4-byte steady state support.

The next major hardware milestone is Cartographer connected directly to the CM5 USB host, followed by controlled probing/mesh and the first complete supervised print path.

## Cartographer integration

Cartographer is maintained in:

- **[MzTechnology97/cartographer3d-plugin-k2openhost](https://github.com/MzTechnology97/cartographer3d-plugin-k2openhost)**

The plugin supports:

- `register_as_probe: true` — standalone Cartographer-as-probe mode;
- `register_as_probe: false` — optional mixed mode where PRTouch remains the canonical Z-reference probe and Cartographer is used for scanning/mesh functions.

The current known-good homing baseline is PRTouch-only; mixed mode is still pending hardware validation.

## Documentation

On the active branch:

- [K2 Pro / OpenHost integration notes](https://github.com/MzTechnology97/kalico-k2pro/blob/k2-pro-openhost/docs/K2_PRO_OPENHOST.md)
- [K2 configuration context](https://github.com/MzTechnology97/kalico-k2pro/blob/k2-pro-openhost/config/k2/README.md)
- [Canonical K2-OpenHost documentation](https://github.com/MzTechnology97/K2-OpenHost)

For **generic Kalico documentation**, use the upstream project documentation:

- [Kalico documentation](https://docs.kalico.gg/)
- [KalicoCrew/kalico](https://github.com/KalicoCrew/kalico)

For Jacob's upstream K2 Kalico work:

- [Jacob10383/kalico](https://github.com/Jacob10383/kalico)

## Related K2-OpenHost repositories

- **MzTechnology97/K2-OpenHost** — architecture, validation evidence and roadmap;
- **MzTechnology97/k2-pro-custom-firmware** — source/history for Jacobean K2 extras and validated K2 Pro patches;
- **MzTechnology97/cartographer3d-plugin-k2openhost** — Cartographer K2/OpenHost integration;
- **MzTechnology97/k2-improvements** — reference fork in the jamincollins/Jacob K2-improvements lineage;
- **MzTechnology97/mainsail-k2openhost** — Mainsail fork reserved for OpenHost UI integration.

## Project status

Experimental / pre-production. The architecture now supports real homing, heaters and resonance testing from the external host. Direct-USB Cartographer and the complete production print workflow are the next major validation stages.