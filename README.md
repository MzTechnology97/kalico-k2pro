# Kalico K2 Pro (K2-OpenHost)

Kalico for the Creality **K2 Pro** driven from an external Linux host, as part of [K2-OpenHost](https://github.com/MzTechnology97/K2-OpenHost). It is a fork of [Jacob10383/kalico](https://github.com/Jacob10383/kalico).

> [!WARNING]
> **Experienced users only — use at your own risk.** K2-OpenHost voids the manufacturer's warranty and can damage the printer beyond repair, brick its firmware or, in case of malfunction, cause a fire. The authors accept no liability for damage to property or persons.
> In OpenHost mode the **nozzle and chamber cameras** cannot be managed by the T113 and must be rewired directly to the external Linux host, and the printer's **external USB port** cannot be used to print and stops working completely in gadget mode.
> Read the [disclaimer and hardware limitations](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/DISCLAIMER.md) ([italiano](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/it/DISCLAIMER.md)) before using this repository.

Lineage: [Klipper3d/klipper](https://github.com/Klipper3d/klipper) → [KalicoCrew/kalico](https://github.com/KalicoCrew/kalico) → [Jacob10383/kalico](https://github.com/Jacob10383/kalico) → this repository. The active branch is `k2-pro-openhost`.

## Status

The reference K2 Pro (Raspberry Pi CM5 host, one CFS) prints every day on this branch, with the T113 on slot B and Creality 1.1.7.0 board firmware. What has been verified and what is still open is listed in the [K2-OpenHost test status](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/TEST_STATUS.md). Only the modified CFS firmware and the OrcaSlicer plugin are still experimental.

## What this branch adds

- **K2 Pro profile** in `config/k2/`: a short `printer.cfg` and the printer files in `macros/`, grouped by purpose ([config/k2/README.md](config/k2/README.md)). Closed-loop X/Y/E motors, PRTouch, `[z_align]` and power-loss recovery on the single-Z K2 Pro.
- **Jacob10383's K2 extras**, maintained here: logical tool mapping, the Box pause/resume contract, service moves with Z unhomed, retry tour, clog detection, fan tachometers.
- **CFS on the K2 Pro**: the four-byte state reply of CFS firmware 1.1.3 and the six-byte one of 1.5.3, discovery retried when RS-485 comes back, load and unload from unhomed axes.
- **Filament inventory**: persistent slots, RFID (Creality, Bambu and other tags through the UID cache), remaining filament from the CFS count or Kalico's estimate, a custom filament library with brands (`cfs_filaments.json`), Spoolman and K2-RFID compatibility.
- **Print mapping**: `BOX_PRINT_INFO` / `BOX_PRINT_START` with automatic mapping, warnings that never block a print, a strict rule that never maps a base material onto its CF/GF/KF/AF variant, runout swap groups with a manual order.
- **Per-filament settings**: pressure advance, maximum flow, OrcaSlicer preset id.
- **Front ends**: OrcaSlicer filament Sync through Moonraker's `lane_data`, the fields HelixScreen's CFS page reads, Mainsail switches for clog detection, axis twist and chamber fans.
- **External host**: motor startup delay and retry, an RS-485 link watchdog, `[k2_t113]` for the T113 control service (MCU power, buzzer, bridges), `[host_fan]` for the CM5 fan.

The details, with the commands and options, are in [docs/K2_PRO_OPENHOST.md](docs/K2_PRO_OPENHOST.md).

## Installing

- **External host:** the [K2-OpenHost Installer Helper](https://github.com/MzTechnology97/k2-openhost-installer-helper) installs this branch with Moonraker, the [Mainsail K2-OpenHost fork](https://github.com/MzTechnology97/mainsail-k2openhost) and the official Cartographer3D plugin.
- **Printer T113:** the same helper writes the [T113 bootstrap](https://github.com/MzTechnology97/k2-openhost-t113-bootstrap) to the T113's spare slot B (USB bridges, HelixScreen pointed at the host, Creality board firmware updates). Slot A stays untouched.

## Architecture

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
    |-- this Kalico branch (+ klipper-virtual-pins for the Mainsail switches)
    |-- Moonraker
    |-- Mainsail K2-OpenHost fork
    |-- crowsnest -> chamber and nozzle cameras (USB, rewired to the host)
    `-- direct USB -> Cartographer
```

The installer's udev rule names the three gadget channels `/dev/k2-main`, `/dev/k2-nozzle` and `/dev/k2-rs485`; the `/dev/ttyUSBn` numbers change when the gadget reconnects. The Cartographer is a separate USB device on the host (`/dev/k2-cartographer`).

## Probes

PRTouch homes Z. The Cartographer is optional and comes from the official [Cartographer3D plugin](https://github.com/Cartographer3D/cartographer3d-plugin), not from this repository. The verified setup is mixed mode (`register_as_probe: False`): PRTouch homes Z and the Cartographer scans the bed mesh. Cartographer as the only probe is still open. See the [K2-OpenHost Cartographer guide](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/CARTOGRAPHER.md).

## Updates

Moonraker updates this repository only while its working tree is clean. Files that belong to this fork, such as `klippy/extras/gcode_shell_command.py`, stay tracked here; do not let third-party installers overwrite them. Keep local extras such as ShakeTune out of Git tracking (for example in `.git/info/exclude`).

## Documentation

- [K2 Pro integration notes](docs/K2_PRO_OPENHOST.md) and the [K2 profile](config/k2/README.md)
- [K2-OpenHost documentation](https://mztechnology97.github.io/K2-OpenHost/) ([repository](https://github.com/MzTechnology97/K2-OpenHost))
- [T113 bootstrap](https://github.com/MzTechnology97/k2-openhost-t113-bootstrap)
- K2 extras upstream: [Jacob10383/k2-plus-custom-firmware](https://github.com/Jacob10383/k2-plus-custom-firmware)
- Generic Kalico documentation: [docs.kalico.gg](https://docs.kalico.gg/)
