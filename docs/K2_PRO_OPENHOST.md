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
- Jacob-compatible CFS print metadata/mapping API for Mainsail;
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
- protected CFS observation stack on the shared RS-485 bus;
- Moonraker visibility of normalized Box/CFS slot and filament-path state.

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

## CFS print mapping API

Jacob's current CFS workflow separates slicer logical tools from physical CFS slots. K2-OpenHost now carries an additive compatibility layer rather than replacing the hardware-validated Box transport/engine wholesale.

Enable it after `[box]`:

```ini
[box_print_mapping]
```

The helper adds:

```text
BOX_PRINT_INFO FILENAME="path/file.gcode"
BOX_PRINT_START FILENAME="path/file.gcode" MAP="0:1,1:3"
```

and extends `printer.objects.box` with:

```text
print_mapping_version: 1
print_mapping_enabled: true|false
print_info
print_mapping
```

`BOX_PRINT_INFO` is metadata-only. It reads the Orca G-code footer and reports used logical tools plus material/color/profile information. `BOX_PRINT_START` validates an exact logical-tool -> slot map, validates CFS slot availability, loads the Virtual SD file, installs the mapping and then starts it.

The compatibility layer also translates Orca purge-matrix and nozzle-temperature arrays from logical tool indices into the physical-slot indices consumed by the current OpenHost `BoxChangeEngine`. It wraps `PARSE_FLUSH_VOLUMES` so the normal K2 `START_PRINT` macro does not overwrite the translated mapping metadata.

When `observation_mode: true`, the mapping/status API remains visible and `BOX_PRINT_INFO` can be tested, but `BOX_PRINT_START` deliberately refuses to execute CFS mutations.

The companion `mainsail-k2openhost` fork uses this API in the normal Print dialog to present a Jacob/Fluidd-style filament mapping step. This mapped-print path is implemented but still requires staged hardware validation.

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

1. metadata-only `BOX_PRINT_INFO` validation on real sliced files;
2. staged CFS operational-mode validation of `BOX_PRINT_START` and logical tool mapping;
3. direct-USB Cartographer cold boot, reset/reconnect and persistent by-id path;
4. controlled Cartographer probe/touch/scan and bed mesh;
5. first complete supervised print path;
6. optional mixed PRTouch + Cartographer validation.

## Canonical project documentation

See `MzTechnology97/K2-OpenHost` for architecture, test status, roadmap, safety boundaries and complete cross-project credits.
