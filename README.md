# Kalico K2 Pro — K2-OpenHost fork

This repository is a **Creality K2 Pro / K2-OpenHost integration fork** of **[Jacob10383/kalico](https://github.com/Jacob10383/kalico)**.

It is not a replacement for the original Kalico project identity. Upstream lineage and authorship are:

1. **[Klipper3d/klipper](https://github.com/Klipper3d/klipper)** — original Klipper project and contributors;
2. **[KalicoCrew/kalico](https://github.com/KalicoCrew/kalico)** — Kalico community fork and contributors;
3. **[Jacob10383/kalico](https://github.com/Jacob10383/kalico)** — Jacob's K2-oriented Kalico work and the direct upstream of this fork;
4. **MzTechnology97/kalico-k2pro** — K2 Pro/OpenHost integration and validation branch.

All upstream licenses, authorship and project references remain applicable.

## Why this fork exists

This fork is the integrated Kalico tree used by **[K2-OpenHost](https://github.com/MzTechnology97/K2-OpenHost)**, a project that moves the main Kalico/Klipper workload of a Creality K2 Pro to an external Linux host/CM5 while retaining the original K2 electronics and using the T113 as a hardware/UI bridge.

The active development branch is:

```text
k2-pro-openhost
```

## Branches

### `main`

Tracks the Jacob-derived Kalico base as closely as practical. Fork-specific documentation is kept here so GitHub visitors can understand the repository purpose without confusing it with the generic upstream Kalico repository.

### `k2-pro-openhost`

Current K2 Pro integration branch. It contains:

- the Kalico core inherited from Jacob/Kalico;
- a K2 Pro `.cfg` baseline based on the public work in **luketot/kalico-for-K2-Pro**;
- K2-specific Jacobean extras synchronized from **MzTechnology97/k2-pro-custom-firmware:k2-openhost**;
- the K2 Pro CFS compatibility/observation changes already validated through K2-OpenHost.

The final tuned `.cfg` files will later be migrated from the already-working K2 Pro. The Luke configuration is currently a structural baseline, not the final calibration source.

## Current OpenHost transport

The validated external-host mapping is:

```text
/dev/ttyUSB0 -> T113 ttyGS0 -> ttyS2 -> Main MCU
/dev/ttyUSB1 -> T113 ttyGS1 -> ttyS3 -> Nozzle MCU
/dev/ttyUSB2 -> T113 ttyGS2 -> ttyS5 -> RS-485 / CFS
/dev/ttyUSB3 -> planned Cartographer bridge
```

Main and Nozzle MCU sessions have already been established simultaneously from external Kalico without reflashing the Creality MCUs. CFS observation mode has also been validated end-to-end.

## Documentation

- [K2 Pro / OpenHost integration notes](docs/K2_PRO_OPENHOST.md)
- [K2 configuration context](config/k2/README.md)
- [Canonical K2-OpenHost documentation](https://github.com/MzTechnology97/K2-OpenHost)

For **generic Kalico documentation**, use the upstream project documentation:

- [Kalico documentation](https://docs.kalico.gg/)
- [KalicoCrew/kalico](https://github.com/KalicoCrew/kalico)

For Jacob's upstream K2 Kalico work:

- [Jacob10383/kalico](https://github.com/Jacob10383/kalico)

## Related K2-OpenHost repositories

- **MzTechnology97/K2-OpenHost** — architecture, validation evidence and roadmap;
- **MzTechnology97/k2-pro-custom-firmware** — fork of Jacob's K2 custom firmware used as the source/history for K2 extras and validated K2 Pro patches;
- **MzTechnology97/k2-improvements** — reference fork in the jamincollins/Jacob K2-improvements lineage.

## Project status

Experimental / pre-production. The architecture and several protocol layers are hardware-validated, but the complete production print workflow has not yet been declared ready.