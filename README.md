# Kalico K2 Pro — active K2-OpenHost branch

This repository is a Creality **K2 Pro / K2-OpenHost** integration fork of **[Jacob10383/kalico](https://github.com/Jacob10383/kalico)**.

> [!WARNING]
> **Experienced users only — use at your own risk.** K2-OpenHost voids the manufacturer's warranty and can damage the printer beyond repair, brick its firmware or, in case of malfunction, cause a fire. The authors accept no liability for damage to property or persons.
> In OpenHost mode the **nozzle and chamber cameras** cannot be managed by the T113 and must be rewired directly to the external Linux host, and the printer's **external USB port** cannot be used to print and stops working completely in gadget mode.
> Read the [disclaimer and hardware limitations](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/DISCLAIMER.md) ([italiano](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/it/DISCLAIMER.md)) before using this repository.

Upstream lineage is deliberately preserved:

1. **Klipper3d/klipper** — original Klipper project and contributors;
2. **KalicoCrew/kalico** — Kalico community fork and contributors;
3. **Jacob10383/kalico** — direct upstream for the K2-oriented Kalico base used here;
4. **MzTechnology97/kalico-k2pro** — K2 Pro/OpenHost integration branch.

## Active branch

```text
k2-pro-openhost
```

This is the branch currently used on the external CM5/OpenHost machine.

## What is integrated

- Jacob/Kalico core lineage;
- K2 Pro configuration baseline;
- K2-specific Jacobean extras synchronized from the K2/OpenHost source history;
- K2 Pro CFS four-byte state compatibility;
- protected CFS `observation_mode` safety layer;
- K2 Pro closed-loop motor-control topology and tuned configuration;
- external-host motor-control startup delay/retry handling;
- tracked `gcode_shell_command.py` needed by the current host setup;
- CI/sync checks for K2-specific extras.

## K2-OpenHost architecture

```text
K2 LCD/touch
    |
Allwinner T113
    |-- UI / future HelixScreen
    |-- USB ConfigFS gadget
    |-- ttyGS0 -> ttyS2 -> Main MCU
    |-- ttyGS1 -> ttyS3 -> Nozzle MCU
    `-- ttyGS2 -> ttyS5 -> RS-485/CFS/closed-loop
           |
           | service Micro-USB
           v
Raspberry Pi CM5 / external Linux host
    |-- this Kalico branch
    |-- Moonraker
    |-- Mainsail / Fluidd
    `-- direct USB -> Cartographer
```

The three K2 gadget channels map to `/dev/ttyUSB0`, `/dev/ttyUSB1` and `/dev/ttyUSB2`. Cartographer is now intended to connect directly to the CM5 USB host through a persistent `/dev/serial/by-id/...` path rather than sharing the K2 gadget serial transport.

## Hardware-validated milestone — 2026-10-02

This branch has now been exercised as a real Klippy/Kalico service on the K2 Pro hardware.

Validated from the external host:

- native AArch64 C helper build and runtime;
- simultaneous Main MCU + Nozzle MCU sessions at 230400 baud;
- RS-485 transport on `/dev/ttyUSB2`;
- closed-loop X/Y controller startup and runtime communication;
- automatic recovery from transient motor-controller discovery failures through startup delay/retry logic;
- normal CoreXY motion;
- X/Y sensorless/stall homing;
- correct Z direction;
- **complete homing using the original PRTouch stack** with Cartographer disabled;
- nozzle, bed and chamber heater operation;
- PID tuning from the external host;
- emergency shutdown while heater loads were active, with the measured load removed correctly;
- successful **Klippain-ShakeTune** resonance test;
- protected CFS/Box observation baseline plus operational Box mode on the real K2 Pro;
- K2 Pro 4-byte `BOX_STATE` decoding with temperature/humidity adaptation;
- persistent CFS filament inventory v2 in `~/printer_data/filament_box.json`;
- generated 61-profile Creality + Generic K2-RFID system catalog;
- persistent manual/RFID slot lifecycle with startup restore that does not require a full RFID sweep;
- RFID reported/estimated remaining-filament state and explicit full-box/per-slot reread paths;
- Jacob-compatible `BOX_PRINT_INFO` / `BOX_PRINT_START` mapping API with backend auto-map and safe Generic fallback.

The earlier experimental Cartographer MUX/DEMUX path reached live Cartographer MCU streaming, but it is no longer the target architecture. Cartographer will be finalized using direct USB on the CM5.

## Probe strategy

Two Cartographer roles are supported by the official [Cartographer3D plugin](https://github.com/Cartographer3D/cartographer3d-plugin):

- `register_as_probe: true` — Cartographer becomes the canonical Klipper/Kalico probe;
- `register_as_probe: false` — optional mixed mode where PRTouch remains the primary Z-reference probe while Cartographer stays available for scan/mesh functions.

The current known-good homing baseline is **PRTouch-only**. Mixed mode remains optional and pending hardware validation after direct-USB Cartographer is stable.

## Configuration policy

The project has moved from a structural baseline to hardware-backed machine integration. Values that have been proven on the working K2 Pro can now be migrated into the OpenHost configuration, but host-specific serial paths and startup ordering must remain explicit.

Do not treat an experimental Cartographer bridge or unvalidated mixed-probe setting as production-ready merely because the parser accepts it.

## Local plugin hygiene / Moonraker updates

Moonraker expects the Kalico Git working tree to remain clean. Files that belong to this fork, such as `klippy/extras/gcode_shell_command.py`, should stay tracked from Git rather than being overwritten by third-party installers.

Cartographer is not part of this fork. Install the official [Cartographer3D plugin](https://github.com/Cartographer3D/cartographer3d-plugin), which supports Kalico and the K2 directly (for example through [K2-OpenHost Installer Helper](https://github.com/MzTechnology97/k2-openhost-installer-helper)); its installer puts the package in `~/klippy-env` and the loader in `klippy/plugins/cartographer.py`, which Git ignores. See the [K2-OpenHost Cartographer guide](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/CARTOGRAPHER.md).

Locally installed extras such as ShakeTune can be kept outside Git tracking (for example through `.git/info/exclude`) so the `k2-pro-openhost` branch remains updateable from Mainsail.

## Documentation

- [K2 Pro/OpenHost integration notes](docs/K2_PRO_OPENHOST.md)
- [K2 configuration context](config/k2/README.md)
- [Canonical K2-OpenHost documentation](https://github.com/MzTechnology97/K2-OpenHost)
- [T113 bootstrap for the printer side](https://github.com/MzTechnology97/k2-openhost-t113-bootstrap)
- K2 extras upstream: [Jacob10383/k2-plus-custom-firmware](https://github.com/Jacob10383/k2-plus-custom-firmware). The former mirror `MzTechnology97/k2-pro-custom-firmware` is archived (read-only history).
- [Cartographer3D plugin (official)](https://github.com/Cartographer3D/cartographer3d-plugin) and the [K2-OpenHost Cartographer guide](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/CARTOGRAPHER.md)

For generic Kalico documentation and original project information, use:

- [Kalico documentation](https://docs.kalico.gg/)
- [KalicoCrew/kalico](https://github.com/KalicoCrew/kalico)
- [Jacob10383/kalico](https://github.com/Jacob10383/kalico)

## Status

Experimental / pre-production. Core machine control now reaches full PRTouch homing, thermal tests, resonance measurement and an operational persistent CFS inventory from the external host. Remaining major milestones are direct-USB Cartographer validation, controlled mapped CFS printing/tool-change/runout validation, remaining-filament tracking over a complete print and a complete supervised OpenHost print path.