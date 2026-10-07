# K2 Pro + CM5 reference configuration

Snapshot of the configuration running on the K2-OpenHost reference machine
(Creality K2 Pro, one CFS, Raspberry Pi CM5 host) on **2026-10-07**, with
Kalico at `k2-pro-openhost` `2db5ad73`.

It is kept for reproducibility: the generic profile in `config/k2/` is the
starting point, these files record the values that were validated on real
hardware. Do not include them directly; compare and copy what applies to your
machine.

## Not included

- the `SAVE_CONFIG` block (PID results, bed mesh, Cartographer models): these are
  per-machine calibration results and must be regenerated;
- `moonraker.conf` and integrations (Telegram, Obico, OctoEverywhere,
  Mobileraker);
- locally installed include files referenced by `printer.cfg`:
  `Shaketune/shaketune.cfg`, `macros/shell_command.cfg`.

Printer files live in `macros/`, as in the generic profile.
`macros/motor_control.cfg`, `macros/k2_t113.cfg` and `macros/prtouch.cfg`
match the profile in `config/k2/macros/` (apart from comments and whitespace)
and are not duplicated here. The other files in `macros/` are the ones running
on this machine.

## Differences from the generic profile

`printer.cfg`:

- `[mcu rpi]` host MCU on the CM5 (its `CM5-temp` sensor is in `macros/sensors.cfg`);
- extruder sensor `PT1000`, bed sensor `R3men_bed` (custom thermistor
  definitions included), extruder `rotation_distance: 7.0207`;
- microsteps X/Y 32, Z 16 (stock; 64 made the MCU-driven z_align lose steps);
  Z `homing_retract_dist: 10`; TMC2208 Z `stealthchop_threshold: 0`;
- heater bed `max_power: 0.6`; `ptc_power_limiter` bed 0.6 / chamber 0.5 /
  combined 1.0;
- input shaper 48.8 Hz (X) / 40.8 Hz (Y) and ShakeTune resonance settings;
- `gcode_arcs` resolution 0.1; adjusted `axis_twist_compensation` and
  `screws_tilt_adjust` points.

`macros/box.cfg`:

- `retract_length: 20`, `retract_velocity: 6000.0` (profile: 30 / 3000);
- `cut_pos_x: -9.70` from `CALIBRATE_CUT_POS` on this machine.

`macros/print.cfg`: `PRTOUCH_SCRUB` disabled.

`macros/overrides.cfg`: `box_count: 1`.
