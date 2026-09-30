# Kalico K2 Pro — active K2-OpenHost branch

This repository is a Creality **K2 Pro / K2-OpenHost** integration fork of **[Jacob10383/kalico](https://github.com/Jacob10383/kalico)**.

Upstream lineage is deliberately preserved:

1. **Klipper3d/klipper** — original Klipper project and contributors;
2. **KalicoCrew/kalico** — Kalico community fork and contributors;
3. **Jacob10383/kalico** — direct upstream for the K2-oriented Kalico base used here;
4. **MzTechnology97/kalico-k2pro** — K2 Pro/OpenHost integration branch.

## Active branch

You are looking at:

```text
k2-pro-openhost
```

This is the branch currently assembled for CM5/OpenHost testing.

It contains:

- the Jacob/Kalico core;
- K2 Pro `.cfg` baseline files adapted from the public work in **luketot/kalico-for-K2-Pro**;
- K2-specific Jacobean extras synchronized from **MzTechnology97/k2-pro-custom-firmware:k2-openhost**;
- the validated K2 Pro CFS four-byte state compatibility;
- the validated CFS `observation_mode` safety layer;
- an automated sync/compile workflow for the K2 extras.

No unrelated Kalico core module has been modified for the current K2 Pro/OpenHost integration.

## K2-OpenHost architecture

```text
K2 LCD/touch
    |
Allwinner T113
    |-- UI / future HelixScreen
    |-- USB ConfigFS gadget
    |-- ttyGS0 -> ttyS2 -> Main MCU
    |-- ttyGS1 -> ttyS3 -> Nozzle MCU
    `-- ttyGS2 -> ttyS5 -> RS-485/CFS
           |
           | service Micro-USB
           v
Raspberry Pi CM5
    |-- this Kalico branch
    |-- Moonraker
    `-- Mainsail / Fluidd
```

Validated external paths currently map to `/dev/ttyUSB0`, `/dev/ttyUSB1` and `/dev/ttyUSB2`. A fourth channel for Cartographer is planned.

## Current verified milestone

The external-host path has already been validated for:

- simultaneous Main MCU + Nozzle MCU Kalico sessions;
- RS-485 traffic;
- closed-loop X/Y read queries;
- CFS discovery/address/read queries;
- the real Jacobean `Box()` class in observation mode;
- 35 TX / 35 RX with zero transport errors;
- deliberate CFS mutation `0x0D` blocked before TX.

## Configuration status

The K2 Pro `.cfg` files currently in this branch are a structural baseline. They are **not yet the final tuned configuration** for the project machine.

After the transport/control stack is stable, the project will migrate the already-working K2 Pro settings, including the real Cartographer configuration and tuned `motor_control.cfg` values.

## Documentation

- [K2 Pro/OpenHost integration notes](docs/K2_PRO_OPENHOST.md)
- [K2 configuration context](config/k2/README.md)
- [Canonical K2-OpenHost documentation](https://github.com/MzTechnology97/K2-OpenHost)
- [K2 extra source/patch history](https://github.com/MzTechnology97/k2-pro-custom-firmware/tree/k2-openhost)

For generic Kalico documentation and original project information, use:

- [Kalico documentation](https://docs.kalico.gg/)
- [KalicoCrew/kalico](https://github.com/KalicoCrew/kalico)
- [Jacob10383/kalico](https://github.com/Jacob10383/kalico)

## Status

Experimental / pre-production. This branch is for controlled K2 Pro validation and should not yet be treated as a finished drop-in production firmware.