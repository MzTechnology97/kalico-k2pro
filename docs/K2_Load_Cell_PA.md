# K2 load cell capture (APAX) and experimental pressure advance

`[k2_load_cell_pa]` records the K2 nozzle load cell (CS1237) continuously through the stock nozzle firmware. It also offers diagnostics, CSV export, offline replay and an **experimental** pressure advance analysis. It is optional: without the section nothing changes, and PRTouch, homing, Cartographer, nozzle cleaning and the start print macros work as before.

Status: tested on the K2 Pro on 2026-10-06 with all eight steps of the [hardware procedure](#hardware-validation-procedure), see [Results on the K2 Pro](#results-on-the-k2-pro). **It matches a printed test for one material and not for another:** Bambu PLA Basic at 225 °C, candidate 0.0297 against 0.032 printed; Generic PETG-CF at 250 °C, about 0.023 against **about 0.040** printed (40 % low). Use the candidate only as a starting point for a printed test.

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
| A step that is queued but not yet due also shows: Klipper queues steps up to ~2 s ahead, so before a pulse `espds` holds the first step's interval from the previous E step (seconds, about 0 mm/s) | PROVEN | Printer, 2026-10-06: 775 808 434 ticks (6.5 s) for the whole rest before a `G1 E10` |
| Positive extrusion gives a positive `espds` with an inverted dir pin (`!nozzle_mcu:PB4`) | PROVEN | Printer, 2026-10-06: `G1 E10 F180` gives +3.00 mm/s derived |
| `espds` is commanded, not measured: it says nothing about real filament motion (slip, grinding) | PROVEN | Source of `step_prtouch_get_ivt` |
| During the sensor reconfiguration after start, the firmware still records conversions made with the previous setting | PROVEN | `pres_csx_w_cfg` returns the conversion it reads while writing the configuration |
| Packing: count, 2-bit width codes, first value absolute, signed deltas; widths chosen on the int32 delta | PROVEN | `prtouch_write_zip`/`prtouch_read_zip` |
| A tick just below 2^32 is sent in fewer than 4 bytes and must be sign-extended | INFERRED from the line above | Tested with the firmware-style encoder |
| The nozzle firmware build behaves like the v71 object | INFERRED | Same dictionary formats. The image code was not compared instruction by instruction. |
| Signal quality of this load cell while extruding | PROVEN | Printer, 2026-10-06: see [Results on the K2 Pro](#results-on-the-k2-pro) |

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
#pa_calibration: disabled  # experimental enables LOAD_CELL_PA_CALIBRATE
#pa_model: fast_component  # or first_order_lag (one exponential)
#pa_flows: 5, 8          # mm/s of filament, only when no max flow is known
#pa_flow_fractions: 0.2, 0.3, 0.4  # of the max volumetric flow (MAX_FLOW= or the slot profile)
#pa_pulse_time: 0.25     # seconds per pulse: keep it under ~2 mm of filament
#pa_rest_time: 1.5       # seconds of rest before and after
#pa_replicates: 3
#pa_warmup: 1            # uncaptured pulses before the first capture (WARMUP=)
#pa_prime: 20            # mm purged slowly into the wastebin first (PRIME=, 0 skips)
#pa_max_filament: 150    # mm, refused above this
#pa_purge_gcode:         # custom moves to the purge spot; overrides the box
#pa_box: auto            # auto/yes/no: use [box] for the wastebin and cleaning
#pa_clean: capture       # capture/end/never: when the cleaning runs
#pa_clean_gcode:         # custom cleaning; default: box flush-clean-snap
#pa_reprime: 3.0         # mm pushed back after each cleaning (REPRIME=): its 1.2 mm retract plus the ooze
```

When the nozzle firmware lacks APAX, the section stays loaded but reports `unavailable` with the missing messages, and nothing is sent. The same happens if the extruder stepper is not on the sensor's MCU.

## Commands

| Command | What it does |
| --- | --- |
| `LOAD_CELL_CAPTURE [DURATION=2] [LABEL=text] [WAIT=1]` | Starts a capture. With `WAIT=1` it returns the summary at the end. |
| `LOAD_CELL_STOP` | Stops the running capture and prints its summary. |
| `LOAD_CELL_DIAGNOSTIC` | Availability, sensor setting, link baud, blocks received outside a session, and the last summary. The summary has samples, effective rate, baseline, noise, drift, range, link load, and decode/length/stale/gap/duplicate counters. |
| `LOAD_CELL_PA_ANALYZE [FILES=a.csv,b.csv]` | Runs the analysis on the last calibration or on CSV files from the output directory. Changes nothing. |
| `LOAD_CELL_PA_CALIBRATE [SLOT=n] [TEMP=c] [SAVE=1] [MAX_FLOW=mm3/s] [FLOWS=a,b] [REPLICATES=3] [WARMUP=1] [POSITION_CONFIRMED=1] [CLEAN=capture\|end\|never] [APPLY=0]` | Experimental, disabled unless `pa_calibration: experimental`. See below and [Calibrating a filament profile](#calibrating-a-filament-profile). |

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

Captures are grouped by their `flow=<mm/s>` label (set by `LOAD_CELL_PA_CALIBRATE`).

## Pressure advance method (experimental)

Kalico applies ordinary pressure advance to moves that combine XY and positive extrusion. A plain `G1 E…` is not affected by PA, so trying PA values with E-only moves would prove nothing. Instead, the calibration identifies the melt flow dynamics from E-only pulses; the result is then checked on a printed test.

1. The nozzle must already be hot (the command never heats), over the purge area, at rest.
2. For each feed rate and replicate, a capture records: rest (baseline), an E-only pulse at a constant feed rate, then rest for the decay.
3. After the pulse ends, the load follows the melt pressure relaxing.
   - **Model `fast_component` (default):** fitted with `y = c + a₁·exp(-(t - t_stop)/τ₁) + a₂·exp(-(t - t_stop)/τ₂)`, τ₂ ≥ 3 τ₁, on up to 1.5 s after the stop. On the K2 Pro the decay has a fast part (τ₁ ≈ 30 ms) and a slow tail (0.2-0.6 s, filament path and mount). When a component is under 15 % of the amplitude, the decay is taken as a single exponential.
   - **Model `first_order_lag`:** one exponential, `y = c + a·exp(-(t - t_stop)/τ)`, on 0.6 s. On the K2 Pro it mixes the two parts (τ 64-142 ms, replicates 94 % apart).
4. If the flow at the nozzle lags the commanded flow with a first-order time constant τ, the advance `K·dE/dt` that cancels it is K = τ seconds (τ₁ for the fast component). This is the candidate.
5. Rejected, with the reason:
   - saturation;
   - no baseline;
   - not exactly one pulse;
   - capture ending before the fit window;
   - SNR below 8 (fitted amplitude against baseline noise);
   - R² below 0.85;
   - τ at the edge of the search range.
6. Each feed rate needs 3 accepted replicates whose spread ((max - min) / median) is at most 35 %. With a hot nozzle the fast components of three replicates spread 28-32 % on the K2 Pro, while their median matched the printed test.
7. τ must not change by more than 1.5× between feed rates: otherwise the linear model does not hold and no candidate is given.
8. The candidate must be between 0 and 0.2 s.

**Limits:**
- the load cell measures the force on the hotend, not the melt pressure;
- τ also contains the sensor filter, the mount compliance and the filament path;
- the model ignores nonlinear viscosity, temperature, nozzle and material changes.

A candidate is valid only for the material, temperature, nozzle, feed rates and `smooth_time` of the run. Lower force is not better: it can mean less material or slip.

**Safety of `LOAD_CELL_PA_CALIBRATE`:**
- it refuses to run:
  - when the nozzle is not at its target temperature or cannot extrude;
  - when a feed rate exceeds `max_extrude_only_velocity`, or a pulse exceeds `max_extrude_only_distance`;
  - when the plan exceeds `pa_max_filament`;
  - while printing or with the probe armed;
- it moves only to purge and clean (see "Waste handling" below). Without a box and without `pa_purge_gcode` it requires `POSITION_CONFIRMED=1`;
- G-code state is saved and restored (`SAVE_GCODE_STATE`/`RESTORE_GCODE_STATE`), including relative extrusion;
- pressure advance changes only with `APPLY=1` and a valid candidate, for this Klipper session. It is never saved, and `SAVE_CONFIG` is never called. On error the capture is aborted, the state restored and PA left as it was;
- it does not tune flow ratio, maximum flow, `smooth_time` or the motors.

### Waste handling

With a `[box]` (CFS) and `pa_box: auto`, the filament never lands on the bed:

1. Before the first pulse the head goes to the wastebin (the same move as `BOX_GO_TO_WASTEBIN`). If X/Y are not homed, the box homes them first.
2. Every pulse is extruded there.
3. After each capture (`pa_clean: capture`) the box flush-clean-snap runs: part fan for 3 s to stiffen the blob, a 1.2 mm retract, then the scraper passes that push the waste out of the bin and the silicone pad wipe. This is the same routine the box runs between purge chunks during a color change.
4. Before the next pulse the 1.2 mm retract is pushed back (`pa_reprime`) and the nozzle rests for `pa_rest_time`, so the baseline does not contain that small extrusion.

The cleaning never runs during a capture. `CLEAN=end` cleans once after the last capture, `CLEAN=never` skips it. `pa_purge_gcode` and `pa_clean_gcode` replace the box moves with your own macros.

## Calibrating a filament profile

With a `[box]` that keeps filament profiles (pressure advance and max flow per filament, kalico-k2pro `box/filament-pa-maxflow`), the calibration works on a slot's profile. Mainsail's **Calibrate PA** buttons on the CFS slots and in the filament library send this command.

```text
LOAD_CELL_PA_CALIBRATE SLOT=1 SAVE=1
```

- **`SLOT=n`:** loads the slot if another one is loaded (`BOX_SELECT_SLOT`), then heats to the profile's temperature (`TEMP=` overrides) with `M109`. Only this explicit request heats; without `SLOT` the command still never heats. Without `SLOT` the loaded slot is used.
- **Feed rates:** without `FLOWS`, they are `pa_flow_fractions` (20, 30 and 40 %) of the maximum volumetric flow, divided by the filament cross-section. The max flow comes from `MAX_FLOW=` or the slot's profile (filament, material, or the OrcaSlicer generic value). Only without any max flow are `pa_flows` used. The report says which.
  - Why: the K2 Pro bench matched the printed test at 24-40 % of the maximum flow (PETG-CF) and read 40 % low at 80-128 %.
- **`SAVE=1`:** a valid candidate is stored in the slot's filament profile (the custom library filament and every slot that uses it, otherwise the slot profile) and applied. Without a valid candidate nothing is saved, and the report gives the reason.

## Hardware validation procedure

Run the steps in order, and stop at the first failure.

1. `FIRMWARE_RESTART`, then `LOAD_CELL_DIAGNOSTIC`: expect `available`.
2. Nozzle cold, printer idle: `LOAD_CELL_CAPTURE DURATION=2 LABEL=rest`.
   - Expect: `complete`; rate 1250-1310 Hz; no decode, length, gap or duplicate errors.
   - Note the noise and the link load.
   - Stop if Klipper reports `Timer too close` or MCU retransmits grow.
3. Press the nozzle lightly by hand during a 3 s capture: the counts must change and come back.
4. Probe still works: `G28 Z` or `PRTOUCH_HOME`, then a probe accuracy test, before and after captures.
   - Compare at the same nozzle temperature, with a clean nozzle. Clean before homing: a blob on the nozzle shifts the Z zero.
5. Nozzle hot, over the purge area: capture while extruding at 3 mm/s.
   - Confirm that `e_velocity_mm_s_derived` equals the feed rate during the move, with a positive sign.
   - Before the move `e_interval_ticks` can already be non-zero, with an interval of seconds (about 0 mm/s): the first step is queued ahead. The analysis counts only intervals up to `max_step_interval` (50 ms) as extrusion.
   - Keep pulses short and clean the nozzle between captures. A 10 mm pulse built a blob that reached the bottom of the wastebin and added a flat mechanical load (see the results).
   - If the sign is reversed, fix `e_dir_sign` before going on.
6. Repeat step 5 three times, and at a second feed rate. The decay must be visible and repeatable.
7. `pa_calibration: experimental`, then `LOAD_CELL_PA_CALIBRATE` (with the box) or `LOAD_CELL_PA_CALIBRATE POSITION_CONFIRMED=1` (without). Read the report; `APPLY` stays 0.
8. Print a standard pressure advance test with the candidate and with a range around it. Only the printed result validates the value.

Switch the heaters off before restarting the Klipper service: with the nozzle heater on, the nozzle MCU shuts down by itself (`Scheduled digital out event will exceed max_duration`) and needs `FIRMWARE_RESTART`.

**Rollback:** remove or comment out `[k2_load_cell_pa]`, then `FIRMWARE_RESTART`. Nothing else was changed: the probe configuration, thresholds and filters are untouched.

## Results on the K2 Pro

2026-10-06, development K2 Pro, nozzle firmware `1.1.0.48-293-g493f9a0f`, Kalico on the CM5 host, `cfg_regs` 60.

| Step | Result |
| --- | --- |
| 1. Diagnostic | `available`; CS1237 1280 Hz nominal, gain 128; no late blocks. The first attempt failed at startup: Klipper refuses command names whose second character is a digit (`K2_…` parses as `K` with argument `2`). The commands are now `LOAD_CELL_…`. |
| 2. Rest, nozzle cold | `complete`: 2505 samples in 1.95 s, **1283.8 Hz**; noise 210 counts, drift 76 counts/s; no decode, length or duplicate errors; no `Timer too close`; nozzle MCU retransmits unchanged. **Link load 11.7 kB/s, 51% of the 230400 baud link.** One gap was counted inside the 50 ms settle window, where the sensor pauses about 5 ms while it is reconfigured (57 samples dropped instead of ~64); gaps now count only in kept data. |
| 3. Press by hand, nozzle at 45 °C | Four 10 s captures, all `complete`, 0 gaps. A light upward push on the nozzle gives **−130 000 to −172 000 counts** (pushing up lowers the counts; extrusion, which pushes the nozzle down, raises them). After every release the value comes back to rest within 1 000 counts (under 1 % of the change), and three releases in one capture land on the same level. No saturation (the CS1237 range is ±8.4 million). |
| 4. Probe after captures | `PROBE_ACCURACY SAMPLES=5` at the center: range 0.0023 mm, standard deviation 0.0009 mm. |
| 5. Extrusion, 215 °C PLA, 10 mm at 3 mm/s over the wastebin | `complete`, 8662 samples, 0 gaps, link load 13 kB/s. Derived velocity **+3.00 mm/s** for 3.34 s (3.33 s commanded). Load: rest 157 ± 800 counts; **~49 000 within 0.1 s** of the start, easing to ~33 000 at 0.9 s; then a rise to a **flat ~92 000 from 1.5 s to the stop**, the blob resting on the bottom of the wastebin. After the stop: 71 000 → 26 000 (0.1 s) → 14 000 (0.3 s) → 7 700 (1 s) → 5 000 (2 s). |
| 4. Probe, again after the hot captures | First reading -0.394 mm average: the `G28` after a Klipper restart had touched on the blob left by step 5. Re-homed with a clean nozzle at 119 °C: -0.029 mm, range 0.027 mm (soft PLA on the tip). **Cold and clean: range 0.0039 mm, standard deviation 0.0011 mm over 10 samples**, as before the captures. APAX does not affect PRTouch. |
| 6–7. `LOAD_CELL_PA_CALIBRATE FLOWS=2,5 REPLICATES=3`, 215 °C PLA | Ran end to end: wastebin, cleaning after each capture, G-code state restored, PA unchanged. All 6 captures `complete`, 0 gaps, link load 13.2 kB/s (57%), nozzle MCU retransmits unchanged. **No candidate.** 2 mm/s: 3/3 rejected (R² 0.75–0.77, SNR 7–8: noise at rest is ~850 counts with the nozzle hot, about 4× the cold value). 5 mm/s: 3/3 accepted, but τ 64–142 ms, spread 94%. |
| Decay, offline | A two-exponential fit over 1.5 s after the stop gives a **fast τ₁ of 27–44 ms (about 32 ms) in all six captures**, and a slow τ₂ of 0.18–0.62 s that varies. At 5 mm/s it fits with R² 0.95–0.996. The single exponential over 0.6 s mixes the two, which explains the spread. The slow part is probably mechanical (filament path, blob, mount). For comparison, the printer's configured pressure advance is 0.038. |
| Nozzle firmware output | `mcu 'nozzle_mcu': #output: Timer too close` twice, right after the calibration ended. A firmware text message, not a shutdown: Klipper stayed ready, no retransmits. Cause not identified. |

| 6–7 again, model `fast_component`, 215 °C (replay of the six captures) | 5 mm/s: τ₁ 0.0263 / 0.0297 / 0.0336 s, τ₂ 0.17-0.39 s, fast share 44-65 %, R² 0.994-0.999. 2 mm/s: SNR 6-7.6, rejected. |
| 6–7 again, `pa_flows: 5, 8`, `pa_pulse_time: 0.25`, `pa_rest_time: 1.5`, **225 °C** | All 6 fits accepted, R² 0.977-0.992, fast share 62-76 %. 5 mm/s: τ₁ 0.0365 / 0.0285 / 0.0285 s (median 0.0285, spread 28 %). 8 mm/s: τ₁ 0.0252 / 0.0310 / 0.0350 s (median 0.0310, spread 32 %). Feed-rate ratio 1.09. No candidate only because the spread is over 25 %. Median of all twelve fast components at 215 and 225 °C: **~0.030 s**. |
| 8. Printed test, 225 °C | OrcaSlicer pressure advance line test, Bambu PLA Basic @K2. Coarse pass 0.020-0.060 (step 0.005, printed at 231 °C): 0.020-0.025 bulge at the end of the fast segment, 0.055-0.060 thin out, 0.035-0.045 most even. **Fine pass 0.030-0.052 (step 0.002) at 225 °C: best line about 0.032**; from about 0.044 up the transitions visibly pinch. The calibration's candidate is **0.0297** (with the 35 % spread limit; at 25 % it gave none): 0.002 from the print. A hand-tuned 0.049 used so far was too high for this line test. |
| "Timer too close" after the run | The analysis took 0.27 s of pure Python on the reactor; the nozzle firmware printed the line right after each report. It now runs in a thread: three analyses and two test prints since then added no line. Still to confirm after a full calibration. |

| PETG-CF, why it read low | The force did not grow with the feed rate (27 800 counts at 5 mm/s, 28 000 at 8 mm/s; PLA 27 000 → 34 000). The IEMAI PETG-CF @K2 profile allows 15 mm³/s: 5 mm/s is 12 mm³/s (80 %), 8 mm/s is 19 mm³/s (128 %). The run was at or past the material's flow limit. |
| PETG-CF at low feed rates, warm-up pulse, 5 replicates, 250 °C | 1.5 mm/s (3.6 mm³/s, 24 %): τ₁ **0.045** (0.040-0.049), 5/5 accepted, R² 0.994-0.997. 2.5 mm/s (6.0 mm³/s, 40 %): τ₁ **0.0365**, 3/5. 3.5 mm/s (8.4 mm³/s, 56 %): τ₁ 0.019, 2/5. τ₁ falls as the flow rises (shear thinning), and the feed-rate check correctly gave no single candidate (ratio 2.3). **At 24-40 % of the maximum flow the fast component is 0.037-0.045, around the printed 0.040.** |
| Calibration and the slicer profile | The calibration uses nothing from the slicer profile: E-only pulses at fixed feed rates, no XY motion, no flow ratio, no accelerations, and PA does not act on E-only moves. It needs the print temperature and feed rates well below the material's maximum volumetric flow. |
| PETG-CF, printed test | OrcaSlicer PA line test at 250 °C (bed 70 °C, chamber 40 °C), 0.010-0.050 step 0.002: **best about 0.040**. The load cell's fast component (about 0.023) is 40 % low for this material. |
| PETG-CF (Generic, slot 1), 250 °C, before the warm-up pulse | All 6 fits accepted (R² 0.956-0.988), noise ~800 counts, 0 gaps, no new "Timer too close" (the threaded analysis is confirmed on a full run). 5 mm/s: τ₁ 0.0285 / 0.0214 / 0.0206 s (spread 37 %, rejected). 8 mm/s: 0.0242 / 0.0232 / 0.0252 s (median 0.0242, spread 8 %). Median of all six 0.0237; about 0.023 without the first capture. Printed test pending. |
| Calibrate PA button, PETG-CF slot left loaded after a print | `LOAD_CELL_PA_CALIBRATE SLOT=0 SAVE=1`, feed rates 1.25 / 1.87 / 2.49 mm/s from the profile's 15 mm³/s. All nine captures rejected (SNR 1-2.4): the pulse force was 1 200 → 5 100 counts in the first captures and stayed at 6 000-8 500, against 14 000-33 000 after a fresh load. The nozzle was not full (filament retracted after the print), so `pa_prime` (20 mm, 2 mm/s, cleaned) now fills it before the warm-up pulse. Nothing was saved. |
| Calibrate PA button again, with the priming purge | After the purge the pulse force was 28 500 counts, then fell to 7 000 in four captures as the nozzle emptied between captures (1.2 mm reprime against the 1.2 mm cleaning retract plus what oozes at 250 °C). The first two captures at 1.25 mm/s gave **τ₁ 0.0448 and 0.0430** (R² 0.99), one at 1.87 mm/s 0.0365: around the printed 0.040. Too few accepted replicates, nothing saved. `pa_reprime` is now 3.0 mm (`REPRIME=`). |
| Cleaning modes, 2026-10-06 afternoon (feed rates 20/30/40 % of the profile's max flow, nothing saved unless noted) | **PETG-CF 250 °C**: `CLEAN=end` force stable 17-27k, 7/9 accepted, median τ₁ **0.041** (printed 0.040) but replicates 49-68 % apart; `CLEAN=capture` + `CONDITION=1` force 26-29k, 9/9 fits, median ≈0.050. **PLA**: `CLEAN=end` grows a solid blob on the nozzle, force 82-229k, τ₁ ≈0.05-0.055 (one run saved 0.0549 by `SAVE=1`; reverted); `CLEAN=capture` 215 °C → 0.043, 225 °C → τ₁ 0.051 / 0.038 / 0.034 falling with the feed rate (no candidate); `CONDITION=1` 225 °C → 0.053, force rising at 5.49 mm/s (blob). Printed 0.032. **Sunlu PETG 245 °C**: `CLEAN=end` blob (force 41k → 200k); `CLEAN=capture` + `CONDITION=1` force stable 38-46k, τ₁ 0.045 / 0.041 / 0.031 falling with the feed rate. |
| What the printed tests exercise | OrcaSlicer's PA line test: slow 20 mm/s (2.2 mm³/s) and fast 200 mm/s (22 mm³/s, PLA) or 13 / 134 mm/s (1.4 / 15.5 mm³/s, PETG-CF capped at its max flow). The tuned PA covers that whole range, while τ₁ falls as the flow rises; one feed rate band does not match both materials. |
| Assessment | Within a run τ₁ is repeatable, but it moves by ±30 % with the cleaning mode, the nozzle fill, a blob on the nozzle, the temperature (profile 215 °C against the slicer's 225 °C) and the feed rate. Two of the day's runs matched the printed value (PLA 0.0297 vs 0.032, PETG-CF 0.041 vs 0.040), others did not. **Not reliable enough to save automatically**: use the candidate as the centre of a printed line test. |
| First capture of a run | In both runs at 225/250 °C the first capture read highest (PLA 0.0365 against 0.0285 / 0.0285; PETG-CF 0.0285 against 0.0214 / 0.0206): it starts from another nozzle state (a cleaning or a retract done before the run). `pa_warmup` (default 1) now extrudes one uncaptured pulse, cleaned like the others, before the first capture. |

Note on the CFS: `T0` at print start sets the nozzle to the file's `nozzle_temperature`, not to `START_PRINT EXTRUDER_TEMP`: the coarse test was sliced at 230 °C and printed at 231 °C although the start macro asked for 220.

Next:
- choose the feed rates from the material's maximum volumetric flow (for example `MAX_FLOW=15` → 20, 30 and 40 % of it), not fixed 5 and 8 mm/s;
- reject a run whose force stops growing with the feed rate (past the flow limit);
- repeat PLA at low feed rates, to see whether 0.030 holds;
- until then the candidate is a starting point for a printed test, not a value to apply.
