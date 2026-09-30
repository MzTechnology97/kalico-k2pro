# K2 Pro / K2-OpenHost integration status

This branch is the current integrated Kalico target for the K2-OpenHost project.

## Upstream attribution

The code base is inherited from `Jacob10383/kalico`, itself based on `KalicoCrew/kalico` and ultimately `Klipper3d/klipper`. K2-specific extra implementations are inherited from Jacob/Jacobean's public K2 custom-firmware work. Those upstream authorship references remain intact.

## What has been integrated

- K2 Pro baseline configuration from public `luketot/kalico-for-K2-Pro` work;
- Jacobean K2 extras from `MzTechnology97/k2-pro-custom-firmware:k2-openhost`;
- K2 Pro CFS four-byte `BOX_STATE` compatibility;
- CFS `observation_mode` with a read-only Box-layer guard;
- CI sync/compile workflow for the synchronized extras.

## Why observation mode matters

The K2 RS-485 bus is shared. Global transport blocking would also affect non-CFS devices. The K2-OpenHost protection therefore wraps only the CFS/Box stack and blocks non-whitelisted CFS requests before serial transmission while leaving the underlying `serial_485.py` transport available to other devices.

The real Jacobean `Box()` class completed a hardware-backed observation test with:

```text
35 TX
35 RX
0 CRC errors
0 invalid-length frames
0 unmatched replies
0 timeouts
0 send/reader errors
```

A deliberate `0x0D` mutation was blocked before TX.

## Current test target

Next, this branch will be run as a complete Klippy/Kalico service on the CM5 with:

```text
Main MCU   -> /dev/ttyUSB0
Nozzle MCU -> /dev/ttyUSB1
RS-485/CFS -> /dev/ttyUSB2
```

CFS stays in observation mode for that stage.

## Configuration policy

Do not treat the current `.cfg` files as final tuning. Once the host stack is stable, the final configuration will be migrated from the already-working K2 Pro, including its proven Cartographer and motor-control values.

## Canonical project documentation

See `MzTechnology97/K2-OpenHost` for architecture, test status, roadmap, safety boundaries and complete cross-project credits.