# K2 Pro + CM5 reference configuration

Snapshot of the configuration running on the K2-OpenHost reference machine
(Creality K2 Pro, one CFS, Raspberry Pi CM5 host) on **2026-10-18**, with
Kalico at `k2-pro-openhost` `83756e97` and the CFS on K2-OpenHost firmware
`v323-cfg5t` (live type-4 remaining).

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
`macros/motor_control.cfg`, `macros/k2_t113.cfg` (apart from `host`, which the
installer helper fills in) and `macros/prtouch.cfg` match the profile in
`config/k2/macros/` (apart from comments and whitespace) and are not
duplicated here. The other files in `macros/` are the ones running
on this machine.

## Choices specific to this machine

This printer runs on a UPS, so a mains power loss does not stop a print. For
that reason only:

- `[power_loss_recovery]` is commented out in `macros/system.cfg`;
- `[z_align]` is commented out in `printer.cfg`. It is needed only to find Z
  again after a power loss, and without it Z can run at 64 microsteps (the
  MCU-driven z_align loses steps at 64).

Without a UPS keep both enabled, as in the generic profile, and Z at 16
microsteps. The `microsteps: 64` comment in `printer.cfg` still describes
the z_align limit.

## Differences from the generic profile

`printer.cfg`:

- `[mcu rpi]` host MCU on the CM5 (its `CM5-temp` sensor is in `macros/sensors.cfg`);
- extruder sensor `PT1000`, bed sensor `R3men_bed` (custom thermistor
  definitions included), extruder `rotation_distance: 7.0207`;
- microsteps X/Y 32, Z 64 with TMC2208 `interpolate: False` (possible only
  without `[z_align]`, see below); Z `homing_retract_dist: 10`; TMC2208 Z
  `stealthchop_threshold: 0`;
- heater bed `max_power: 0.6`; `ptc_power_limiter` bed 0.6 / chamber 0.5 /
  combined 1.0;
- input shaper 50 Hz (X) / 40.8 Hz (Y) and ShakeTune resonance settings;
- `gcode_arcs` resolution 0.1; adjusted `axis_twist_compensation` and
  `screws_tilt_adjust` points.

`macros/box.cfg`:

- `retract_length: 20`, `retract_velocity: 6000.0` (profile: 30 / 3000);
- `cut_pos_x: -10.60` from `CALIBRATE_CUT_POS` on this machine;
- `[box_cfs_runtime]` enabled with `auto_apply: False` and the eight options
  the CFG5T firmware applies (IDs 1, 2, 3, 5, 7, 8, 20, 22): hub speeds
  100/220/100/150, the timeouts at stock. The other catalogue entries are
  listed as comments only, because CFG5T has no firmware hook for them;
- `cfs_live_remaining: True`: the CFS type-4 remaining leads, the local
  estimate is the fallback.

`macros/print.cfg`: `PRTOUCH_SCRUB` disabled.

`macros/overrides.cfg`: `box_count: 1`.

`macros/host_fan.cfg`: CM5 fan `target_temp: 50`, `full_speed_temp: 70`
(profile: 55 / 75).

Third-party RFID: this machine's CFS runs the API7 RFID firmware, so
`printer.cfg` includes `macros/box_rfid_diag.cfg`, `macros/box_rfid_bambu.cfg`
and `macros/box_rfid_mifare.cfg` (commented out in the generic profile; see
`docs/CFS_RFID_BAMBU.md`).

## Filament libraries

`cfs_filaments.json` (custom filament library, `library_path`) and
`k2_rfid_custom_materials.json` (K2-RFID import, `material_database_path`) are
this machine's libraries. On the printer both live in `~/printer_data/config/`.


## CFS overlay updated 29 October 2026

The base snapshot above retains its other machine-specific settings. The RFID
geometry overlay now includes `macros/box_spool_profiles.cfg` for CFG6 SGF1/RH1
R3, BIN SHA `a6dec25c74e7d259eec0387193548a0c3cc78ec9fc4ddc35824f0fe0965385b8`.
The active reference Bambu geometry is `bambu = 1, 260.8, 508.3`: approximately
83 mm bare cardboard outer diameter and a measured full-spool circumference.
It replaces the provisional nearly-empty anchor. With the retained generation1
measurements, CFS reported slot2=100% and slot4=12%, without another scan.
See [the calibration procedure](../../../../docs/CFS_SPOOL_CALIBRATION.md).
