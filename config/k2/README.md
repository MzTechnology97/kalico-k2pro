# K2 Pro configuration in `k2-pro-openhost`

This directory comes from the K2-oriented Kalico work in **Jacob10383/kalico** and has been adapted in this branch for a **Creality K2 Pro** baseline.

## Baseline source

The initial K2 Pro geometry/pin/configuration adaptation is based on the public work in **luketot/kalico-for-K2-Pro**. That contribution is used as a reference baseline and remains credited here.

## Not the final project calibration

The project K2 Pro already has a working configuration that will be migrated later. The current files are intentionally used only to provide a K2 Pro-appropriate structural starting point while OpenHost transport/control is being validated.

Final values will be taken from the working machine for items such as:

- Cartographer;
- tuned `motor_control.cfg`;
- PID and thermal configuration;
- extruder and pressure advance;
- machine macros and calibration values.

## OpenHost host paths

The current architecture uses the T113 as a bridge to the CM5. Final CM5 configuration will therefore replace T113-local serial paths with the validated external mappings:

```text
[mcu]              -> /dev/ttyUSB0
[mcu nozzle_mcu]   -> /dev/ttyUSB1
[serial_485 ...]   -> /dev/ttyUSB2
Cartographer       -> planned /dev/ttyUSB3 bridge
```

T113 filesystem paths such as `/mnt/UDISK/...` will also be converted to the CM5 `printer_data` layout where required.

## Attribution and caution

- K2 Kalico structure and extras: Jacob10383/Jacobean and upstream contributors.
- K2 Pro baseline adaptation: luketot.
- K2-OpenHost integration: MzTechnology97 project work validated on the actual K2 Pro.

K2 Plus dimensions, service coordinates and hardware mappings must not be assumed to apply unchanged to K2 Pro.