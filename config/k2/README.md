# K2 Pro configuration in `k2-pro-openhost`

This directory comes from the K2-oriented Kalico work in **Jacob10383/kalico** and has been adapted in this branch for a **Creality K2 Pro** baseline and the current **K2-OpenHost** architecture.

## Baseline source and attribution

The initial K2 Pro geometry/pin/configuration adaptation is based on the public work in **luketot/kalico-for-K2-Pro**. K2-specific extras and control logic retain their Jacob10383/Jacobean attribution. OpenHost changes are the external-host integration and hardware-validated K2 Pro deltas maintained by this project.

## Current machine status

This configuration is no longer only a passive structural starting point. The external CM5/Kalico stack has now completed hardware tests for:

- Main and Nozzle MCU communication;
- RS-485 motor-control startup;
- normal CoreXY motion;
- X/Y sensorless/stall homing;
- correct Z direction;
- complete homing through the stock PRTouch path;
- bed/nozzle/chamber heaters and PID tuning;
- emergency shutdown of active heater loads;
- Klippain-ShakeTune resonance measurement;
- normalized Box/CFS status through Moonraker.

Machine-specific values should still be changed only when they are backed by the known-good K2 Pro configuration or a controlled OpenHost hardware test.

## OpenHost host paths

The current stable T113 bridge mapping is:

```text
[mcu]              -> /dev/ttyUSB0
[mcu nozzle_mcu]   -> /dev/ttyUSB1
[serial_485 ...]   -> /dev/ttyUSB2
Cartographer       -> direct USB on CM5, preferably /dev/serial/by-id/...
```

Cartographer is **not** planned as `/dev/ttyUSB3` anymore. A fourth gadget/MUX path was prototyped and carried real Cartographer traffic, but direct USB is now preferred because it handles native MCU reset/re-enumeration without adding a PTY/MUX/DEMUX layer.

T113 filesystem paths such as `/mnt/UDISK/...` should be converted to the CM5 `printer_data` layout where required.

## Motor-control profile

The branch contains the K2 Pro X/Y/E closed-loop topology and the hardware-tested tuned `motor_control.cfg` baseline used by OpenHost. Startup logic includes delay/retry handling because the external CM5 can become ready before the K2 motor controllers.

Exactly one bridge/process must own the RS-485 gadget/UART path. A duplicate GS2 bridge discovered during experimental multiplexing caused motor communication failures; returning to one `ttyGS2 <-> ttyS5` bridge restored stable operation.

## CFS print mapping

`box.cfg` now loads the additive mapping layer after `[box]`:

```ini
[box]
# ... normal K2 Box configuration ...

[box_print_mapping]
```

This exposes the current Jacob-style frontend contract without replacing the already validated OpenHost CFS transport:

```text
BOX_PRINT_INFO FILENAME="folder/file.gcode"
BOX_PRINT_START FILENAME="folder/file.gcode" MAP="0:1,1:3"
```

`MAP` is **logical slicer tool -> physical CFS slot**. For example `0:1` means the G-code's logical `T0` uses physical CFS slot `T1`.

The `box` Moonraker object gains `print_mapping_version`, `print_mapping_enabled`, `print_info` and `print_mapping`. `mainsail-k2openhost` uses those fields to show filament mapping inside the normal Print dialog.

`BOX_PRINT_INFO` only reads G-code metadata and is safe for the first validation step. In `observation_mode`, `BOX_PRINT_START` is intentionally blocked. Do not switch to operational Box mode merely to test the UI; validate the metadata path first.

The helper translates Orca purge volumes and nozzle temperatures to physical slots and preserves that translation when the normal `START_PRINT` macro calls `PARSE_FLUSH_VOLUMES`.

## Probe configuration

The current known-good homing baseline uses **PRTouch** with Cartographer disabled. Full homing has been verified in that state.

The companion Cartographer plugin supports:

- `register_as_probe: true` for normal Cartographer-as-probe mode;
- `register_as_probe: false` for future optional mixed mode with PRTouch retaining the canonical Z-reference probe.

Do not enable mixed mode until direct-USB Cartographer has first been validated independently.

## Attribution and caution

- K2 Kalico structure and extras: Jacob10383/Jacobean and upstream contributors.
- CFS print mapping semantics and Orca metadata reader: Jacob10383/Jacobean, adapted additively for the existing OpenHost Box engine.
- K2 Pro baseline adaptation: luketot.
- K2-OpenHost integration: MzTechnology97 project work validated on the actual K2 Pro.

K2 Plus dimensions, service coordinates and hardware mappings must not be assumed to apply unchanged to K2 Pro. The repository remains experimental until the mapped CFS print path, direct-USB Cartographer and a complete supervised print path are validated.
