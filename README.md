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
- K2 Pro configuration baseline (closed-loop motors, PRTouch, `[z_align]` and power-loss recovery on the single-Z K2 Pro);
- K2-specific Jacobean extras, **maintained in this repository** (the former `k2-pro-custom-firmware` mirror is archived), including Jacob10383's 071c813 update: native logical-tool mapping and the `_BOX_PAUSE_CAPTURE` / `_BOX_RESUME_PREPARE` / `_BOX_RESUME_COMMIT` pause contract;
- K2 Pro CFS state compatibility: the four-byte reply of CFS firmware 1.1.3 and the original six-byte reply of 1.5.3 (Creality 1.1.7.0), plus the protected `observation_mode` safety layer;
- Jacob10383's K2 extras up to his 2026-10-06 release: service moves that work with Z unhomed, configurable retry tour and clog detection, native fan tachometers (`fan_feedback`);
- CFS filament inventory:
  - persistent slots and RFID remaining estimates;
  - a separate custom filament library (`cfs_filaments.json`) with brands;
  - K2-RFID and Spoolman compatibility;
- CFS print mapping (`BOX_PRINT_INFO` / `BOX_PRINT_START`, backend auto-mapping):
  - warnings for low filament and material variants that never block a print;
  - a strict rule that never maps a base material onto its CF/GF/KF/AF variant;
- runout swap groups with a manual order (`_BOX_SET_RUNOUT_ORDER`, editable from the Mainsail Runout swap widget);
- unload and slot change heat the nozzle over the wastebin after homing X/Y; no RFID reread of the slot loaded toward the printhead; a live low-filament check during the print;
- pressure advance and max flow per filament profile; RS-485 counters per print and `link_lost_action`; motor startup that recovers when RS-485 comes back;
- `START_PRINT` with a hot nozzle clean before probing; Mainsail switches and sliders for the axis twist calibration, clog detection and the chamber exhaust fans;
- K2 profile in `config/k2/`: a lean `printer.cfg` and the printer files in `macros/` grouped by purpose (see [config/k2/README.md](config/k2/README.md));
- official OrcaSlicer filament **Sync**: the CFS slots are published to Moonraker's `lane_data` namespace (`publish_lane_data`);
- external-host motor-control startup delay/retry handling;
- tracked `gcode_shell_command.py` needed by the current host setup;
- CI: Ruff, firmware build and the strict MkDocs build.

## Installing

- **External host:** [K2-OpenHost Installer Helper](https://github.com/MzTechnology97/k2-openhost-installer-helper) installs this branch with Moonraker, the [Mainsail K2-OpenHost fork](https://github.com/MzTechnology97/mainsail-k2openhost) and the official Cartographer3D plugin.
- **Printer T113:** the same installer runs the [T113 bootstrap](https://github.com/MzTechnology97/k2-openhost-t113-bootstrap) from its menu. It writes a K2-OpenHost system to the T113's spare slot B (USB gadget bridges, HelixScreen pointed at this host, Creality MCU/motor/CFS firmware updates). Slot A is untouched. It was prepared and tested on stock firmware 1.1.0.94, and is not guaranteed on newer releases.

## K2-OpenHost architecture

```text
K2 LCD/touch
    |
Allwinner T113 (slot B: K2-OpenHost T113 bootstrap)
    |-- HelixScreen -> Moonraker on the external host
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

The three K2 gadget channels get stable names from the installer's udev rule: `/dev/k2-main`, `/dev/k2-nozzle` and `/dev/k2-rs485` (the `/dev/ttyUSBn` numbers change when the gadget reconnects). Cartographer connects directly to the CM5 USB host as `/dev/k2-cartographer` rather than sharing the K2 gadget serial transport.

## Hardware-validated milestone — 2026-10-02

This branch has now been exercised as a real Klippy/Kalico service on the K2 Pro hardware.

Validated from the external host:

- native AArch64 C helper build and runtime;
- simultaneous Main MCU + Nozzle MCU sessions at 230400 baud;
- RS-485 transport on the third gadget channel (now `/dev/k2-rs485`);
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

Experimental / pre-production. Core machine control now reaches full PRTouch homing, `[z_align]` homing, thermal tests, resonance measurement and an operational persistent CFS inventory from the external host.

Validated on the reference printer since then (see the [K2-OpenHost test status](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/TEST_STATUS.md)): long CFS prints from the T113 slot B, a runout swap and the RS-485 watchdog seen live, load and unload from unhomed axes, the CFS six-byte state on firmware 1.5.3, native fan tachometers, the clog detection switch and service moves with Z unhomed and a mesh loaded.

Built but not yet fully hardware-validated:
- mapping warnings and the strict variant rule;
- the manual runout order;
- OrcaSlicer `lane_data` Sync, verified with OrcaSlicer 2.4.2;
- the hot nozzle clean in a real `START_PRINT`.

Remaining major milestones:
- direct-USB Cartographer validation;
- controlled mapped CFS printing, tool change and runout validation;
- remaining-filament tracking over a complete print;
- a supervised power cut with `PLR_RECOVER`;
- a complete supervised OpenHost print path, including the T113 bootstrap on slot B.