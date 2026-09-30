# K2 configuration context for this fork

The `config/k2/` directory is part of the K2-oriented Kalico work inherited from **Jacob10383/kalico**.

In the `k2-pro-openhost` branch, selected `.cfg` files are adapted for the **Creality K2 Pro**. The initial structural baseline is derived from the public **luketot/kalico-for-K2-Pro** configuration work, with attribution preserved.

## Important

These files are **not yet the final tuned configuration for the K2-OpenHost test machine**.

The final values will be migrated from the already-working K2 Pro after the CM5 transport and observation-mode tests are complete. In particular, the production machine already has its own:

- Cartographer configuration;
- tuned `motor_control.cfg` values;
- thermal/PID settings;
- extruder/pressure-advance settings;
- machine macros and calibration data.

## Host-specific changes

When moving from the T113-local layout to the CM5/OpenHost layout, the validated transport paths are expected to use:

```text
Main MCU:   /dev/ttyUSB0
Nozzle MCU: /dev/ttyUSB1
RS-485/CFS: /dev/ttyUSB2
Cartographer: planned /dev/ttyUSB3 bridge
```

Filesystem paths such as `/mnt/UDISK/...` will also need to be replaced by the CM5 `printer_data` layout where appropriate.

## Attribution

- K2 Kalico base/config structure: **Jacob10383** and upstream Kalico contributors.
- K2 Pro baseline adaptation used for reference: **luketot/kalico-for-K2-Pro**.
- Final K2-OpenHost machine-specific tuning: derived from the project's real K2 Pro only after validation.

Do not assume K2 Plus dimensions, service-zone coordinates or hardware mappings apply unchanged to K2 Pro.