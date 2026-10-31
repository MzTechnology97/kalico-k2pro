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

## Layout

`printer.cfg` keeps the MCUs, motion, heaters, `[input_shaper]` and the
includes: the sections `SAVE_CONFIG` writes (PID, input shaper, axis twist)
must stay in it. Every other printer file lives in `macros/`, grouped by
purpose:

| file | content |
| --- | --- |
| `macros/system.cfg` | print files, power-loss recovery, link monitor, timeouts, arcs, object exclusion, pause/resume |
| `macros/sensors.cfg` | MCU and host temperatures, printhead filament sensor, accelerometer, resonance tester |
| `macros/leds.cfg` | chamber light and its idle switch-off |
| `macros/print.cfg` | `START_PRINT` and its nozzle clean, `PAUSE` / `RESUME` / `CANCEL_PRINT` / `END_PRINT`, homing and mesh helpers |
| `macros/kamp.cfg` | KAMP purge line (`LINE_PURGE`, `_KAMP_Settings`) |
| `macros/fans.cfg` | fans with their tachometers, `M106`/`M107` (toolhead part fan P0, side part fan P2, chamber exhaust P3), `M141`/`M191` |
| `macros/maintenance.cfg` | rail lubrication, PID, motion stress tests kept on the bed area: `WARMUP`, `AUTO_WARMUP`, `TEST_SPEED`, and `ACCELL_TEST_X` / `ACCELL_TEST_Y` (one axis each; every pass sets `ACCEL_START + n*ACCEL_STEP` and `VELOCITY + n*VELOCITY_STEP` above the configured limits, with `MINIMUM_CRUISE_RATIO=0`, logs it, and the limits are restored at the end) |
| `macros/box.cfg`, `macros/motor_control.cfg`, `macros/prtouch.cfg`, `macros/cartographer.cfg`, `macros/k2_t113.cfg`, `macros/openhost_controls.cfg` | printer modules |
| `macros/overrides.cfg` | local values, loaded last |

Moonraker, Mainsail, webcam and integration files stay in the config root.

## OpenHost host paths

The three T113 bridges and the Cartographer:

```text
[mcu]              -> /dev/k2-main    (T113 ttyGS0 -> ttyS2)
[mcu nozzle_mcu]   -> /dev/k2-nozzle  (T113 ttyGS1 -> ttyS3)
[serial_485 ...]   -> /dev/k2-rs485   (T113 ttyGS2 -> ttyS5)
Cartographer       -> direct USB on the host, /dev/serial/by-id/usb-Cartographer_...
```

The `/dev/k2-*` names come from the installer helper's udev rule and are the same with the T113 on slot A or slot B. `printer.cfg` in this folder ships the `by-id` names of slot A's stock gadget; the installer switches them to the udev names. Never use `/dev/ttyUSBn`: the numbers change when the gadget reconnects. The G-code folder is the CM5's `~/printer_data/gcodes`.

`[z_align]` drives the K2 Pro bottom photoelectric switch (`PA15`, as in the Creality F012 stock config). Integrated `G28` uses it when Z is unknown, and `power_loss_recovery` (`z_reference: z_align`) uses it to re-reference Z after a power loss (see `docs/K2_PRO_OPENHOST.md`).

The tuned configuration of the reference K2 Pro + CM5 machine is kept in [`reference/k2pro-cm5/`](reference/k2pro-cm5/README.md).

## Motor-control profile

The branch contains the K2 Pro X/Y/E closed-loop topology and the hardware-tested tuned `motor_control.cfg` baseline used by OpenHost. Startup logic includes delay/retry handling because the external CM5 can become ready before the K2 motor controllers.

Exactly one bridge process may own the RS-485 path (`ttyGS2 <-> ttyS5`): a second one on the same port caused motor communication failures.

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

PRTouch homes Z. The official Cartographer3D plugin adds the Cartographer ([K2-OpenHost Cartographer guide](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/CARTOGRAPHER.md)):

- `register_as_probe: False`, mixed mode: PRTouch stays the Z probe and the Cartographer scans the bed mesh. Verified on the reference K2 Pro on 2026-10-26. The `BED_MESH_CALIBRATE` wrapper in `macros/print.cfg` adds `PROBE=cartographer` when the call names no probe, so a mesh never falls back to nozzle touches.
- `register_as_probe: True`: the Cartographer is the only probe. Not verified yet.

### Nozzle cleaning and axis twist at print start

`START_PRINT` cleans the nozzle before any nozzle contact, in every probe setup (PRTouch as the probe, Cartographer alone, PRTouch homing with a Cartographer mesh):

1. `_NOZZLE_HOT_CLEAN` heats the nozzle over the wastebin to the print temperature minus `variable_hot_clean_offset` (10 °C), retracts `variable_hot_clean_retract` (2 mm) and runs `NOZZLE_CLEAN` while residue is soft;
2. the nozzle cools to the probing temperature (140 °C) over the wastebin with the part fan at full speed, then the fan returns to its previous speed;
3. a last `NOZZLE_CLEAN` at the probing temperature removes the strings left while cooling.

`variable_hot_clean: 0` keeps only step 3. The hot clean is skipped when the print temperature is within 30 °C of the probing temperature. Nothing scrubs the nozzle on the plate; `variable_prtouch_scrub` stays 0 on the reference machine.

The axis twist calibration at print start is switched from Mainsail with the **Axis Twist Compensation** switch (`openhost_controls.cfg`: a `virtual_pin:axis_twist_compensation` output pin, needs [klipper-virtual-pins](https://github.com/pedrolamas/klipper-virtual-pins) linked into `klippy/extras`), or from the console with `START_PRINT_ATC ENABLE=1` or `ENABLE=0`; `START_PRINT_ATC` alone shows it. Every change is saved in `save_variables` (`~/printer_data/config/k2_start_print_variables.cfg`) and the switch is restored to its last state one second after each Klipper start. Without `openhost_controls.cfg` the console command still works with the saved value.

**Clog detection** pauses the print when the extruder feeds `clog_extruder_length` (80 mm) while the CFS does not refill. It is switched in Mainsail's CFS settings menu next to Runout swap, or with `_BOX_SET_CLOG_DETECTION ENABLE=0|1`; the CFS saves it in its own state, so it survives restarts, and `clog_detection` in `box.cfg` is the default until then.

`filament_retry_moves` in `box.cfg` is the tour between wastebin visits before a stalled load or blocked unload is retried; Klipper refuses to start when a move is outside the X/Y travel (the K2 Plus default `Y350` does not fit the K2 Pro's 332 mm).

The chamber exhaust fans keep their temperature control. `generic_fan: True` on `[temperature_fan_manual_floor chamber_exhaust_fans]` also shows them in Mainsail's Miscellaneous panel as **Chamber Exhaust Fans**, a `fan_generic` slider that sets the manual speed floor (`SET_FAN_SPEED FAN=chamber_exhaust_fans SPEED=<0..1>`). The fans run at the higher of the floor and the temperature control; `M106 P3` sets the same floor, so the slider always shows it. The pin keeps a single owner, the `temperature_fan`: no `duplicate_pin_override`. Until a value is saved, `variable_adaptive_axis_twist_comp` is the default; a print's own `ATC=` parameter still overrides both. With Cartographer alone it runs `CARTOGRAPHER_AXIS_TWIST_COMPENSATION`, in mixed mode `PRTOUCH_AXIS_TWIST_COMPENSATION`. With PRTouch as the probe the nozzle itself touches the bed, so there is no twist to compensate: the calibration is skipped with a message.

## Attribution and caution

- K2 Kalico structure and extras: Jacob10383/Jacobean and upstream contributors.
- CFS print mapping semantics and Orca metadata reader: Jacob10383/Jacobean, adapted additively for the existing OpenHost Box engine.
- K2 Pro baseline adaptation: luketot.
- K2-OpenHost integration: MzTechnology97 project work validated on the actual K2 Pro.

K2 Plus dimensions, service coordinates and hardware mappings must not be assumed to apply unchanged to K2 Pro. The reference K2 Pro prints every day on this profile; the open tests are in the [K2-OpenHost test status](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/TEST_STATUS.md#open).

## CFS R3 vendor geometry

[`macros/box_spool_profiles.cfg`](macros/box_spool_profiles.cfg) contains the
reference Bambu profile: `bambu = 1, 260.8, 508.3` (core/full circumferences in
millimeters). It requires the paired CFS SGF1/RH1 R3 image and the Bambu RFID
decoder. Its include is commented in the generic printer profile; enable it
once when installing the matching firmware. The CM5 reference profile includes
it. The standalone example remains disabled until explicitly enabled.

See [calibration and installation](../../docs/CFS_SPOOL_CALIBRATION.md) and
[protocol/operation](../../docs/CFS_SINGLE_CYCLE_RFID.md). Geometry is selected
from the decoded RFID vendor; material/color do not choose the geometry, and
nominal filament length remains per UID/slot.
