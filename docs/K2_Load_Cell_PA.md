# K2 load cell capture (APAX) and experimental pressure advance

`[k2_load_cell_pa]` records the K2 nozzle load cell (CS1237) continuously through the stock nozzle firmware. It also offers diagnostics, CSV export, offline replay and an **experimental** pressure advance analysis. It is optional: without the section nothing changes, and PRTouch, homing, Cartographer, nozzle cleaning and the start print macros work as before.

Status: the software is tested offline only. Neither the capture on the printer nor the pressure advance result has been validated yet; the [hardware procedure](#hardware-validation-procedure) lists what is left.

## Why APAX

The probe path (`start_prtouch_pres` + `read_prtouch_pres`) keeps only the last 64 samples, about 50 ms at 1280 samples/s, and reading them pages through that buffer. It cannot record an extrusion.

The stock prtouch_v3 firmware has a second mode, **APAX**. Once started, it samples the sensor on its own and sends blocks of samples to the host, each with the MCU tick and the E stepper's step interval. No new MCU firmware is needed.

## What was verified, and how

Labels used below:
- **PROVEN** means read directly from a binary or source file;
- **INFERRED** means derived from proven facts;
- **TO VERIFY** means it needs the printer.

| Fact | Status | Source |
| --- | --- | --- |
| The four APAX messages exist in the nozzle firmware of this printer | PROVEN | Dictionary extracted from the stock image `noz0_130_G30-noz0_021_000.bin`. Its version string `1.1.0.48-293-g493f9a0f-dirty-20241220_143931` is the one the printer's nozzle MCU reports in `klippy.log`. CLOCK_FREQ is 120 MHz. |
| Message formats | PROVEN | Same image; `src/prtouch_v3_compile.c` at CrealityOfficial/K2_Series_Klipper `bc0a520` |
| `start_prtouch_apax` acks `err=0 expar0=0 expar1=acq_tick`; `stop_prtouch_apax` acks 0/0/0 | PROVEN | Disassembly of `command_start/stop_prtouch_apax` in `src/prtouch_v3.o` (same commit, PRTouch v71) |
| The ack carries the APAX oid; **blocks carry the pressure sensor oid** (`pr_pres.oid`) | PROVEN | `prtouch_apax_task` loads the oid of `pr_pres` for `resault_prtouch_apax` |
| Start clears only `pr_pres.buf_pres` (the probe sample buffer, 3488 bytes at +0x14); sensor pins and probe configuration stay | PROVEN | `memset(pr_pres + 0x14, 0, 0xda0)`, struct layout from DWARF |
| Each new sample appends tick, counts and `step_prtouch_get_ivt(oid_estp)`; a block is sent when the three packed series exceed 41 bytes | PROVEN | `prtouch_apax_task` |
| The partial block at stop is not sent | PROVEN | `command_stop_prtouch_apax` only clears `acq_tick` |
| With the CS1237 (no analog ADC), `acq_tick` only enables the task; the rate is the sensor's (`cfg_regs`) | PROVEN | The `acq_tick` timing gate applies only when `use_adcx != 0` |
| `espds` = signed interval between queued E steps in MCU ticks, 0 when no steps are pending | PROVEN | `step_prtouch_get_ivt` in `src/stepper.c` |
| Positive extrusion gives a positive `espds` with an inverted dir pin (`!nozzle_mcu:PB4`) | INFERRED, TO VERIFY | The MCU negates when the queued direction bit is set; Klipper applies the dir pin inversion to that bit |
| `espds` is commanded, not measured: it says nothing about real filament motion (slip, grinding) | PROVEN | Source of `step_prtouch_get_ivt` |
| During the sensor reconfiguration after start, the firmware still records conversions made with the previous setting | PROVEN | `pres_csx_w_cfg` returns the conversion it reads while writing the configuration |
| Packing: count, 2-bit width codes, first value absolute, signed deltas; widths chosen on the int32 delta | PROVEN | `prtouch_write_zip`/`prtouch_read_zip` |
| A tick just below 2^32 is sent in fewer than 4 bytes and must be sign-extended | INFERRED from the line above | Tested with the firmware-style encoder |
| The nozzle firmware build behaves like the v71 object | INFERRED | Same dictionary formats. The image code was not compared instruction by instruction. |
| Signal quality of this load cell while extruding | TO VERIFY | — |

Background reading, used as references and not copied:
- [BD Pressure](https://github.com/markniu/bd_pressure) `72e91d3` (`klipper/bdpressure.py`, `firmware_src/Core/Src/pa.c`): the measuring principle only. Its ADS1220 scaling, thresholds and scores do not apply to the CS1237, and it disables the XY motors, which this module never does. Two problems noted in its host code: it can reuse `old_res` for a new candidate, and its removal of the first samples is wrong on short series.
- [grant0013/K2-OpenKlipper](https://github.com/grant0013/K2-OpenKlipper) `c3d5c4d`: the PA15 → PC7 contact path and the probe protocol on the K2 Plus.
- [gitstonelabs/creality-klipper-unlocked](https://github.com/gitstonelabs/creality-klipper-unlocked) `db0990b`, `docs/protocol/README.md`: the shared prtouch_v3 packing (Creality Hi).
- The Creality wrapper `prtouch_v3_wrapper.cpython-39.so` names WAVE_APAX, TEST_APAX and AUTO_APAX. It is not used and not distributed here.

## Architecture

```text
start_prtouch_apax ──► nozzle MCU: prtouch_apax_task (every new CS1237 sample)
                          tick, counts, E interval ──► packed block
resault_prtouch_apax (oid = pres oid) ◄────────────────┘
        │ serial thread: append to a queue, nothing else
        ▼
reactor timer (every 50 ms): decode, 64-bit ticks, checks ──► CaptureSession
        │
stop_prtouch_apax ─► ack ─► grace period ─► finish ─► CSV written in a thread
```

- **Files:** `klippy/extras/k2_load_cell_pa.py` (transport, session, commands), `klippy/extras/k2_pa_analysis.py` (pure Python analysis, also used on a PC), `klippy/extras/prtouch_codec.py` (the packing, shared with `prtouch.py`), `scripts/k2_pa_replay.py`.
- **OIDs:** the APAX oid is a new oid used only by `config_prtouch_apax`, which allocates nothing in the firmware. Its acks therefore never reach PRTouch's ack handler. Blocks are registered on the pressure sensor oid under their own message name.
- **Sensor ownership:** one owner at a time.
  - A capture refuses to start while the probe is armed, while a print is running or paused, or while another capture runs.
  - A homing or probing move during a capture aborts the capture and fails the move, which you then repeat.
  - The probe re-arms with `start_prtouch_pres`, which reconfigures the sensor after APAX.
- **Timing:** ticks are extended to 64 bits with the Klipper clock sync (`clock32_to_clock64`), never with USB arrival times. Duplicated or backwards samples, gaps (more than 3 nominal periods) and blocks from before the session start are counted, not used.
- **Settle time:** samples in the first `settle_time` after start are dropped, because the sensor is still being reconfigured.
- **Limits:** the capture duration is capped by `max_duration` and the number of samples by `max_samples`. CSV files are rotated (`max_files`).
- **Faults:** shutdown or disconnect aborts the session without sending anything to the MCU. A failed start sends a stop. A cleanup error is logged and does not hide the original error.

**Link load:** each block holds roughly 8-14 samples, so at 1280 samples/s that is about 100-150 blocks/s. The diagnostic prints the measured payload rate against the nozzle link (230400 baud ≈ 23 kB/s). Measuring it is part of the hardware validation.

## Probe, homing and Z offset

This section is separate from PRTouch. It does not change how the probe triggers, how homing and Z offset are computed, or any probe threshold or filter. With the section absent, Klipper loads nothing from it.

What they share:

- **The sensor:** both use the same load cell, never at the same time (see the ownership rules above).
- **The decoder:** the packed-series decoder moved to `prtouch_codec.py` and `prtouch.py` uses it unchanged. In `prtouch.py` it is only called by the probe diagnostic report, not by homing or probing.
- **One fix in that diagnostic:** the first tick is now sign-extended. It only changes the diagnostic printout.

## Configuration

Add to `printer.cfg` (or uncomment in `config/k2/prtouch.cfg`), then run `FIRMWARE_RESTART`: the section adds one MCU config command.

```ini
[k2_load_cell_pa]
#channel: 0              # pres_cs channel (the K2 Pro has one)
#cfg_regs:               # default: [prtouch] pres_cfg_regs (60 = 1280 Hz, gain 128)
#acq_tkms:               # default: [prtouch] pres_acq_tkms (enable value for APAX)
#max_duration: 10        # seconds, hard limit for one capture
#settle_time: 0.05       # seconds dropped after start
#ack_timeout: 0.5
#block_grace: 0.25       # seconds blocks are still accepted after stop
#max_samples:            # default: max_duration * nominal rate * 1.25
#export: True            # write a CSV after each capture
#output_dir:             # default: the Klipper log directory
#max_files: 20
#pa_calibration: disabled  # experimental enables K2_PA_CALIBRATE
#pa_flows: 2, 5          # mm/s of filament
#pa_pulse_time: 1.0      # seconds of extrusion per pulse
#pa_rest_time: 0.8       # seconds of rest before and after
#pa_replicates: 3
#pa_max_filament: 150    # mm, refused above this
#pa_purge_gcode:         # e.g. BOX_GO_TO_WASTEBIN; empty = POSITION_CONFIRMED=1
```

When the nozzle firmware lacks APAX, the section stays loaded but reports `unavailable` with the missing messages, and nothing is sent. The same happens if the extruder stepper is not on the sensor's MCU.

## Commands

| Command | What it does |
| --- | --- |
| `K2_LOAD_CELL_CAPTURE [DURATION=2] [LABEL=text] [WAIT=1]` | Starts a capture. With `WAIT=1` it returns the summary at the end. |
| `K2_LOAD_CELL_STOP` | Stops the running capture and prints its summary. |
| `K2_LOAD_CELL_DIAGNOSTIC` | Availability, sensor setting, link baud, blocks received outside a session, and the last summary. The summary has samples, effective rate, baseline, noise, drift, range, link load, and decode/length/stale/gap/duplicate counters. |
| `K2_PA_ANALYZE [FILES=a.csv,b.csv]` | Runs the analysis on the last calibration or on CSV files from the output directory. Changes nothing. |
| `K2_PA_CALIBRATE [FLOWS=2,5] [REPLICATES=3] [POSITION_CONFIRMED=1] [APPLY=0]` | Experimental, disabled unless `pa_calibration: experimental`. See below. |

`printer["k2_load_cell_pa"]` reports `available`, `unavailable_reason`, `state`, `session`, the last summary and `pa_calibration`.

Values are raw CS1237 counts; the relative column subtracts the baseline. They are not grams or newtons: no force calibration exists.

## CSV format and replay

The file is `k2_load_cell_pa_<date>-<time>_<session>.csv`. `#` lines hold the metadata:
- session, label, date;
- MCU, mcu_version, clock and software version;
- cfg_regs with the decoded rate, gain and channel, and acq_tick;
- E step distance and the derived direction sign;
- extruder temperature and target, pressure advance and smooth_time, part fan;
- the summary counters, the final state (complete, aborted or invalid) with its reason, and notes.

Columns:

```text
tick,time_s,raw_counts,rel_counts,e_interval_ticks,e_velocity_mm_s_derived
```

`e_velocity_mm_s_derived` = sign × step_distance × clock / interval. It is commanded velocity, valid only if the sign is confirmed on the printer.

Offline, on any PC with Python 3:

```bash
python3 scripts/k2_pa_replay.py k2_load_cell_pa_*.csv
python3 scripts/k2_pa_replay.py --json --opt min_snr=6 k2_load_cell_pa_*.csv
```

Captures are grouped by their `flow=<mm/s>` label (set by `K2_PA_CALIBRATE`).

## Pressure advance method (experimental)

Kalico applies ordinary pressure advance to moves that combine XY and positive extrusion. A plain `G1 E…` is not affected by PA, so trying PA values with E-only moves would prove nothing. Instead, the calibration identifies the melt flow dynamics from E-only pulses; the result is then checked on a printed test.

1. The nozzle must already be hot (the command never heats), over the purge area, at rest.
2. For each feed rate and replicate, a capture records: rest (baseline), an E-only pulse at a constant feed rate, then rest for the decay.
3. After the pulse ends, the load follows the melt pressure relaxing. The signal is fitted with `y = c + a·exp(-(t - t_stop)/τ)`.
4. **Model `first_order_lag`:** if the flow at the nozzle lags the commanded flow with a first-order time constant τ, the advance `K·dE/dt` that cancels it is K = τ seconds. This is the candidate.
5. Rejected, with the reason:
   - saturation;
   - no baseline;
   - not exactly one pulse;
   - capture ending before the fit window;
   - SNR below 8 (fitted amplitude against baseline noise);
   - R² below 0.85;
   - τ at the edge of the search range.
6. Each feed rate needs 3 accepted replicates whose spread is at most 25 %.
7. τ must not change by more than 1.5× between feed rates: otherwise the linear model does not hold and no candidate is given.
8. The candidate must be between 0 and 0.2 s.

**Limits:**
- the load cell measures the force on the hotend, not the melt pressure;
- τ also contains the sensor filter, the mount compliance and the filament path;
- the model ignores nonlinear viscosity, temperature, nozzle and material changes.

A candidate is valid only for the material, temperature, nozzle, feed rates and `smooth_time` of the run. Lower force is not better: it can mean less material or slip.

**Safety of `K2_PA_CALIBRATE`:**
- it refuses to run:
  - when the nozzle is not at its target temperature or cannot extrude;
  - when a feed rate exceeds `max_extrude_only_velocity`, or a pulse exceeds `max_extrude_only_distance`;
  - when the plan exceeds `pa_max_filament`;
  - while printing or with the probe armed;
- it moves only through `pa_purge_gcode`, if set. Otherwise it requires `POSITION_CONFIRMED=1`;
- G-code state is saved and restored (`SAVE_GCODE_STATE`/`RESTORE_GCODE_STATE`), including relative extrusion;
- pressure advance changes only with `APPLY=1` and a valid candidate, for this Klipper session. It is never saved, and `SAVE_CONFIG` is never called. On error the capture is aborted, the state restored and PA left as it was;
- it does not tune flow ratio, maximum flow, `smooth_time` or the motors.

## Hardware validation procedure

Run the steps in order, and stop at the first failure.

1. `FIRMWARE_RESTART`, then `K2_LOAD_CELL_DIAGNOSTIC`: expect `available`.
2. Nozzle cold, printer idle: `K2_LOAD_CELL_CAPTURE DURATION=2 LABEL=rest`.
   - Expect: `complete`; rate 1250-1310 Hz; no decode, length, gap or duplicate errors.
   - Note the noise and the link load.
   - Stop if Klipper reports `Timer too close` or MCU retransmits grow.
3. Press the nozzle lightly by hand during a 3 s capture: the counts must change and come back.
4. Probe still works: `G28 Z` or `PRTOUCH_HOME`, then a probe accuracy test, before and after captures.
5. Nozzle hot, over the purge area: capture while extruding 10 mm at 3 mm/s.
   - Confirm that `e_interval_ticks` is non-zero only during the move and that its sign gives positive `e_velocity_mm_s_derived`.
   - If the sign is reversed, fix `e_dir_sign` before going on.
6. Repeat step 5 three times, and at a second feed rate. The decay must be visible and repeatable.
7. `pa_calibration: experimental`, then `K2_PA_CALIBRATE POSITION_CONFIRMED=1`. Read the report; `APPLY` stays 0.
8. Print a standard pressure advance test with the candidate and with a range around it. Only the printed result validates the value.

**Rollback:** remove or comment out `[k2_load_cell_pa]`, then `FIRMWARE_RESTART`. Nothing else was changed: the probe configuration, thresholds and filters are untouched.
