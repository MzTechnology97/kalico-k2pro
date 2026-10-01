# K2 Pro / K2-OpenHost integration status

This branch is the current integrated Kalico target for the K2-OpenHost project.

Last updated: **2026-10-01**.

## Upstream attribution

The code base is inherited from `Jacob10383/kalico`, itself based on `KalicoCrew/kalico` and ultimately `Klipper3d/klipper`. K2-specific extra implementations are inherited from Jacob/Jacobean's public K2 custom-firmware work. Those upstream authorship references remain intact.

## Integrated components

- K2 Pro baseline configuration;
- Jacobean K2 extras used by the real K2 hardware;
- K2 Pro CFS four-byte `BOX_STATE` compatibility;
- protected CFS `observation_mode` with a CFS-layer guard;
- K2 Pro closed-loop motor-control topology and tuned configuration;
- startup delay/retry handling for external-host boot timing;
- tracked loader modules for Cartographer and G-code shell command support;
- synchronization/compile checks for K2-specific extras.

## Current transport

```text
Main MCU   -> /dev/ttyUSB0 -> T113 ttyGS0 -> ttyS2
Nozzle MCU -> /dev/ttyUSB1 -> T113 ttyGS1 -> ttyS3
RS-485/CFS -> /dev/ttyUSB2 -> T113 ttyGS2 -> ttyS5
Cartographer -> direct USB on CM5 (preferred target)
```

The earlier Cartographer MUX/DEMUX experiment is not the final topology. It carried live Cartographer MCU traffic, but reset/re-enumeration and PTY lifecycle add unnecessary complexity. The three gadget serial channels are now reserved for the original K2 buses.

## Hardware validation completed

The real K2 Pro has now been run from this external Kalico branch with the following validated:

- native AArch64 `c_helper.so` build/runtime;
- Main and Nozzle MCU simultaneous sessions;
- RS-485 transport and closed-loop X/Y controller communication;
- motor-control startup recovery across transient first-attempt discovery failures;
- normal CoreXY motion;
- X/Y sensorless/stall homing;
- correct Z direction;
- complete `G28` using the stock **PRTouch** Z-probe path;
- bed, nozzle and chamber heater operation;
- heater PID tuning;
- emergency shutdown with active heater loads removed correctly;
- successful Klippain-ShakeTune resonance test;
- protected CFS observation stack on the shared RS-485 bus.

## CFS observation mode

The K2 RS-485 bus is shared. Global transport blocking would also affect motor-control and other devices. The K2-OpenHost protection therefore wraps only the CFS/Box layer and blocks non-whitelisted CFS requests before serial transmission while leaving `serial_485.py` available to the rest of the machine.

A hardware-backed reference run of the Jacobean `Box()` class completed:

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

## Motor-control startup policy

The CM5 can become ready before the K2 motor controllers. The OpenHost integration therefore uses an explicit startup delay plus multiple retries instead of treating the first missing response as a permanent failure. Hardware restart testing demonstrated successful recovery on later attempts.

The production transport must have exactly one owner per UART/gadget endpoint. A duplicate GS2 bridge was discovered during the experimental Cartographer multiplexing work and caused RS-485 instability; after returning to a single bridge, motor-control operation returned to normal.

## Probe baseline and Cartographer

The current known-good Z-homing baseline is the stock PRTouch stack. Full homing has been tested successfully with Cartographer disabled.

Cartographer support lives in `MzTechnology97/cartographer3d-plugin-k2openhost`. The plugin now supports:

- `register_as_probe: true` for standalone Cartographer-as-probe operation;
- `register_as_probe: false` as the basis for optional mixed mode, where PRTouch remains the primary Z-reference probe and Cartographer is used for scanning/mesh work.

Direct-USB Cartographer validation on the CM5 is the next probe milestone. Mixed mode is optional and should only be enabled after standalone Cartographer is stable.

## Local extras and clean Git updates

Moonraker/Mainsail update management expects this repository to remain clean. Files tracked by this fork should not be overwritten by external installers. Locally installed extras such as ShakeTune can remain outside Git tracking so they do not mark the Kalico repository dirty.

## Next milestones

1. direct-USB Cartographer cold boot, reset/reconnect and persistent by-id path;
2. controlled Cartographer probe/touch/scan and bed mesh;
3. first complete supervised print path;
4. optional mixed PRTouch + Cartographer validation;
5. real CFS loaded-path semantics and later controlled load/unload mutations.

## Canonical project documentation

See `MzTechnology97/K2-OpenHost` for architecture, test status, roadmap, safety boundaries and complete cross-project credits.