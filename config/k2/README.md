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

`printer.cfg` in this folder already uses these `/dev/ttyUSB*` paths and the CM5 `~/printer_data/gcodes` layout. `[z_align]` drives the K2 Pro bottom photoelectric switch (`PA15`, as in the Creality F012 stock config). Integrated `G28` uses it when Z is unknown, and `power_loss_recovery` (`z_reference: z_align`) uses it to re-reference Z after a power loss (see `docs/K2_PRO_OPENHOST.md`).

The tuned configuration of the reference K2 Pro + CM5 machine is kept in [`reference/k2pro-cm5/`](reference/k2pro-cm5/README.md).

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

`BOX_PRINT_INFO` only reads G-code metadata and stays available in `observation_mode`, where `BOX_PRINT_START` is intentionally blocked. The profile ships with `observation_mode: false`: operational Box mode, `BOX_PRINT_INFO` and auto-mapping are validated on the reference K2 Pro, while mapped `BOX_PRINT_START` prints with real tool changes are still being validated. Set `observation_mode: true` as a read-only fallback.

The helper translates Orca purge volumes and nozzle temperatures to physical slots and preserves that translation when the normal `START_PRINT` macro calls `PARSE_FLUSH_VOLUMES`.

### CFS inventory and startup policy

The current K2-OpenHost Box contract deliberately keeps `api_version: 1` and adds `filament_inventory_version: 2` plus `print_mapping_version: 1`. The operational state file is `~/printer_data/filament_box.json`; it persists custom/manual/RFID slot metadata and remaining estimates.

`config/k2/cfs_system_filaments.json` is generated from the public DnG-Crafts/K2-RFID K2 catalog and currently contains 61 read-only Creality + Generic profiles. The backend restores cached metadata only for physically occupied slots after the CFS presence query and leaves a full RFID startup sweep disabled by default. Use `BOX_RFID_SCAN` or the per-slot reread path when an explicit refresh is required.

## Probe configuration

The current known-good homing baseline uses **PRTouch** with Cartographer disabled. Full homing has been verified in that state.

The official Cartographer3D plugin supports:

- `register_as_probe: true` for normal Cartographer-as-probe mode;
- `register_as_probe: false` for future optional mixed mode with PRTouch retaining the canonical Z-reference probe.

Do not enable mixed mode until direct-USB Cartographer has first been validated independently.

### Nozzle cleaning and axis twist at print start

`START_PRINT` cleans the nozzle before any nozzle contact, in every probe setup (PRTouch as the probe, Cartographer alone, PRTouch homing with a Cartographer mesh):

1. `_NOZZLE_HOT_CLEAN` heats the nozzle over the wastebin to the print temperature minus `variable_hot_clean_offset` (10 °C), retracts `variable_hot_clean_retract` (2 mm) and runs `NOZZLE_CLEAN` while residue is soft;
2. the nozzle cools to the probing temperature (140 °C) over the wastebin with the part fan at full speed, then the fan returns to its previous speed;
3. a last `NOZZLE_CLEAN` at the probing temperature removes the strings left while cooling.

`variable_hot_clean: 0` keeps only step 3. The hot clean is skipped when the print temperature is within 30 °C of the probing temperature. Nothing scrubs the nozzle on the plate; `variable_prtouch_scrub` stays 0 on the reference machine.

The axis twist calibration at print start is switched from Mainsail with the **Axis Twist Compensation** switch (`openhost_controls.cfg`: a `virtual_pin:axis_twist_compensation` output pin, needs [klipper-virtual-pins](https://github.com/pedrolamas/klipper-virtual-pins) linked into `klippy/extras`), or from the console with `START_PRINT_ATC ENABLE=1` or `ENABLE=0`; `START_PRINT_ATC` alone shows it. Every change is saved in `save_variables` (`~/printer_data/config/k2_start_print_variables.cfg`) and the switch is restored to its last state one second after each Klipper start. Without `openhost_controls.cfg` the console command still works with the saved value.

The **Clog Detection** switch (`openhost_controls.cfg`) turns the CFS clog check on or off: the extruder feeds `clog_extruder_length` (80 mm) while the CFS does not refill, and the print pauses. The CFS saves the switch in its own state (`_BOX_SET_CLOG_DETECTION ENABLE=0|1`), so it survives restarts; `clog_detection` in `box.cfg` is the default until then.

`filament_retry_moves` in `box.cfg` is the tour between wastebin visits before a stalled load or blocked unload is retried; Klipper refuses to start when a move is outside the X/Y travel (the K2 Plus default `Y350` does not fit the K2 Pro's 332 mm).

The chamber exhaust fans keep their temperature control. `generic_fan: True` on `[temperature_fan_manual_floor chamber_exhaust_fans]` also shows them in Mainsail's Miscellaneous panel as **Chamber Exhaust Fans**, a `fan_generic` slider that sets the manual speed floor (`SET_FAN_SPEED FAN=chamber_exhaust_fans SPEED=<0..1>`). The fans run at the higher of the floor and the temperature control; `M106 P3` sets the same floor, so the slider always shows it. The pin keeps a single owner, the `temperature_fan`: no `duplicate_pin_override`. Until a value is saved, `variable_adaptive_axis_twist_comp` is the default; a print's own `ATC=` parameter still overrides both. With Cartographer alone it runs `CARTOGRAPHER_AXIS_TWIST_COMPENSATION`, in mixed mode `PRTOUCH_AXIS_TWIST_COMPENSATION`. With PRTouch as the probe the nozzle itself touches the bed, so there is no twist to compensate: the calibration is skipped with a message.

## Attribution and caution

- K2 Kalico structure and extras: Jacob10383/Jacobean and upstream contributors.
- CFS print mapping semantics and Orca metadata reader: Jacob10383/Jacobean, adapted additively for the existing OpenHost Box engine.
- K2 Pro baseline adaptation: luketot.
- K2-OpenHost integration: MzTechnology97 project work validated on the actual K2 Pro.

K2 Plus dimensions, service coordinates and hardware mappings must not be assumed to apply unchanged to K2 Pro. The repository remains experimental until the mapped CFS print path, direct-USB Cartographer and a complete supervised print path are validated.