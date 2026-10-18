# CFG6 v3.25 — What all 28 CFS settings actually do (EN)

> **Scope:** experimental K2OH `v323-cfg6` firmware, feature `0xE3`, API2, G32/APP153 board. 23 effective writable settings and 5 GET-only. **Behavior is mapped from stock ARM callsites and Kalico code**; actual CFS SET/GET was verified on 18 October 2026. Physical RFID and movement effects have not all been measured during a print.

## How to interpret the settings

- Motor speeds `1–255 raw` are command values for the hub motor; **no proven mm/s or RPM conversion exists**. Spool feeders are ON/OFF. Do not interpret `220` as 220mm/s.
- `ms` means actual milliseconds in the host config; ID8 must be divisible by 10, IDs20/22 divisible by 20, because the OEM firmware uses internal ticks.
- A **timeout** affects failure/recovery deadlines, not speed. A **delay** changes waiting time; a **count** changes loop repetitions.
- Overrides live in **volatile CFS RAM** and are re-applied on Kalico startup only after firmware identity verification and `auto_apply: True`. Do not change values during a print.
- The "CM5" values reflect the **18 Oct 2026** readback, not recommendations. "Stock" indicates OEM defaults.
- IDs **0, 4, 10, 25, 26** are GET-only: 10/25/26 lack reachable OEM hook paths; feeders 0/4 have no proportional speed control.

## Complete catalogue

| ID | `box.cfg` parameter | Stock | Range / unit | CM5 | Writable |
|---:|---|---:|---|---:|:---:|
| 0 | `feeder_forward_speed` | 255 | 1–255 (catalogue only) | non operativo | No |
| 1 | `hub_forward_speed` | 100 | 1–255 raw | 100 | Yes |
| 2 | `hub_transition_speed` | 200 | 1–255 raw | 220 | Yes |
| 3 | `hub_insert_speed` | 155 | 1–255 raw | 100 | Yes |
| 4 | `feeder_reverse_speed` | 255 | 1–255 (catalogue only) | non operativo | No |
| 5 | `hub_reverse_speed` | 80 | 1–255 raw | 150 | Yes |
| 6 | `reverse_detooth_enable` | 1 | 0/1 boolean | True | Yes |
| 7 | `hub_transition_wait_ms` | 3200 | 100–10000 ms | 3200 | Yes |
| 8 | `insert_sensor_timeout_ms` | 7000 | 1000–50000 ms, step 10 | 7000 | Yes |
| 9 | `hub_pullback_timeout_ms` | 5000 | 500–15000 ms | 5000 | Yes |
| 10 | `unload_sensor_timeout_ms` | 1000 | 100–5000 ms | GET-only | No |
| 11 | `rfid_seek_speed` | 160 | 1–255 raw | 160 | Yes |
| 12 | `rfid_measure_speed` | 120 | 1–255 raw | 150 | Yes |
| 13 | `rfid_reposition_speed` | 100 | 1–255 raw | 100 | Yes |
| 14 | `rfid_seek_move_ms` | 800 | 100–5000 ms | 800 | Yes |
| 15 | `rfid_seek_settle_ms` | 200 | 10–2000 ms | 200 | Yes |
| 16 | `rfid_post_auth_wait_ms` | 1000 | 100–5000 ms | 1000 | Yes |
| 17 | `rfid_pre_measure_settle_ms` | 400 | 1–2000 ms | 400 | Yes |
| 18 | `rfid_neighbor_detect_delay_ms` | 400 | 50–2000 ms | 400 | Yes |
| 19 | `rfid_neighbor_settle_ms` | 100 | 10–1000 ms | 100 | Yes |
| 20 | `feed_total_timeout_ms` | 25000 | 5000–60000 ms, step 20 | 25000 | Yes |
| 21 | `feed_recovery_timeout_ms` | 3000 | 500–10000 ms | 3000 | Yes |
| 22 | `odometer_stall_timeout_ms` | 500 | 100–2000 ms, step 20 | 500 | Yes |
| 23 | `rfid_neighbor_retry_count` | 3 | 1–10 attempts | 1 | Yes |
| 24 | `reverse_detooth_timeout_ms` | 1000 | 100–10000 ms | 1000 | Yes |
| 25 | `buffer_fill_timeout_ms` | 30000 | 1000–60000 ms | GET-only | No |
| 26 | `transition_detect_timeout_ms` | 300 | 50–5000 ms | GET-only | No |
| 27 | `load_retry_count` | 3 | 1–10 cycles | 1 | Yes |

## Runtime behavior, one parameter at a time

The descriptions below refer specifically to **G32/APP153 v3.25 CFG6**, not arbitrary Creality firmware or OpenCFS.

### ID 00 — `feeder_forward_speed`

The spool feeder is commanded ON/OFF; the catalogue value 255 does not select a speed or accelerate loading.

**Boundaries and evidence:** No proportional-speed hook; GET-only. Stock: **255**; accepted range: **1–255 (catalogue only)**; CM5 value: **non operativo**; SET: **no**.

### ID 01 — `hub_forward_speed`

Commands hub speed during the initial forward push from the CFS. Increasing it may shorten this phase but raise mechanical stress; it does not change toolhead extruder speed.

**Boundaries and evidence:** Speed byte in the stock configuration used by the hub motor. (hook: `stock configuration speed byte`.) Stock: **100**; accepted range: **1–255 raw**; CM5 value: **100**; SET: **yes**.

### ID 02 — `hub_transition_speed`

Selects hub speed for the filament transfer along the route toward the buffer and toolhead. This is the most relevant CFS transfer-speed setting, not a direct mm/s value.

**Boundaries and evidence:** May affect PTFE drag and transfer time; not the final insertion phase or toolhead extruder. (hook: `stock configuration speed byte`.) Stock: **200**; accepted range: **1–255 raw**; CM5 value: **220**; SET: **yes**.

### ID 03 — `hub_insert_speed`

Controls hub motion during sensor-guided final insertion. It is not the rate at which the toolhead extruder gears feed the filament into the hotend.

**Boundaries and evidence:** Increasing it can aggravate binding if the path resists insertion. (hook: `stock configuration speed byte`.) Stock: **155**; accepted range: **1–255 raw**; CM5 value: **100**; SET: **yes**.

### ID 04 — `feeder_reverse_speed`

The spool feeder reverses via ON/OFF control rather than proportional speed. ID4 cannot speed up unloading.

**Boundaries and evidence:** GET-only; do not confuse with ID5. Stock: **255**; accepted range: **1–255 (catalogue only)**; CM5 value: **non operativo**; SET: **no**.

### ID 05 — `hub_reverse_speed`

Regulates hub motor speed while the CFS pulls filament back toward its spool during unloading. It does not directly change the toolhead extruder's retract rate.

**Boundaries and evidence:** High settings can raise tension or slipping; verify with brittle filament. (hook: `stock configuration speed byte`.) Stock: **80**; accepted range: **1–255 raw**; CM5 value: **150**; SET: **yes**.

### ID 06 — `reverse_detooth_enable`

Enables/disables the actual reverse de-tooth action (gear release/repositioning during reverse movement). False skips that branch; it does not disable all normal reverse motion.

**Boundaries and evidence:** Separate RAM sidecar bit21/byte50, gated at ID24 hook; disabling it may break unloading or recovery. (hook: `0x080108F0`.) Stock: **1**; accepted range: **0/1 boolean**; CM5 value: **True**; SET: **yes**.

### ID 07 — `hub_transition_wait_ms`

Pause after the hub push, before continuing along the buffer path. Shortening it saves waiting time but may start the next state too early.

**Boundaries and evidence:** Calls stock delay; it is not a total loading timeout. (hook: `0x08011116`.) Stock: **3200**; accepted range: **100–10000 ms**; CM5 value: **3200**; SET: **yes**.

### ID 08 — `insert_sensor_timeout_ms`

Maximum sensor/insert-path waiting window during a loading stage. Expiry follows the stock failure/recovery path; it does not raise movement speed.

**Boundaries and evidence:** CFG in milliseconds; firmware converts to 10ms ticks. (hook: `0x080112CA`.) Stock: **7000**; accepted range: **1000–50000 ms, step 10**; CM5 value: **7000**; SET: **yes**.

### ID 09 — `hub_pullback_timeout_ms`

Time limit for the pullback/buffer stage during unloading. Too short a value may trigger a timeout before retraction finishes.

**Boundaries and evidence:** Wrapper replaces a stock stage timeout, not reverse speed. (hook: `0x08011CB6`.) Stock: **5000**; accepted range: **500–15000 ms**; CM5 value: **5000**; SET: **yes**.

### ID 10 — `unload_sensor_timeout_ms`

Catalogue entry for an unload sensor timeout whose identified stock execution path is unreachable in this variant. Writing it would have no proven effect.

**Boundaries and evidence:** SET/RESET blocked. Stock: **1000**; accepted range: **100–5000 ms**; CM5 value: **GET-only**; SET: **no**.

### ID 11 — `rfid_seek_speed`

Hub speed while moving the spool to locate its RFID tag; it does not control transfer toward the toolhead.

**Boundaries and evidence:** Feeds the stock hub-speed call `0x0801E164` during tag search. (hook: `0x08012A9E`.) Stock: **160**; accepted range: **1–255 raw**; CM5 value: **160**; SET: **yes**.

### ID 12 — `rfid_measure_speed`

Speed used while the spool is moved during RFID measurement. It changes reading-stage motor motion, not hotend extrusion flow.

**Boundaries and evidence:** RFID reading quality at higher speed requires real-tag testing. (hook: `0x08012AC0`.) Stock: **120**; accepted range: **1–255 raw**; CM5 value: **150**; SET: **yes**.

### ID 13 — `rfid_reposition_speed`

Hub speed while repositioning the spool between RFID steps, for example after an inconclusive measurement.

**Boundaries and evidence:** Does not control final filament insertion. (hook: `0x08012E8A`.) Stock: **100**; accepted range: **1–255 raw**; CM5 value: **100**; SET: **yes**.

### ID 14 — `rfid_seek_move_ms`

Duration of the tag-search movement in the stock RFID loop. Increasing it lengthens the search movement, not its speed.

**Boundaries and evidence:** Replaces the duration constant at `0x08012A76`; watch for spool travel limits. (hook: `0x08012A76`.) Stock: **800**; accepted range: **100–5000 ms**; CM5 value: **800**; SET: **yes**.

### ID 15 — `rfid_seek_settle_ms`

Pause after RFID-seek movement before the next step. Reducing it can speed the sequence but remove settling time.

**Boundaries and evidence:** Delay, not speed or retry count. (hook: `0x08012AAE`.) Stock: **200**; accepted range: **10–2000 ms**; CM5 value: **200**; SET: **yes**.

### ID 16 — `rfid_post_auth_wait_ms`

Stock waiting interval around the post-authentication/reverse RFID transition before subsequent actions. Changes a delay, not reversal speed.

**Boundaries and evidence:** Hook replaces delay call at `0x08012E7A`; physical necessity needs real RFID tests. (hook: `0x08012E7A`.) Stock: **1000**; accepted range: **100–5000 ms**; CM5 value: **1000**; SET: **yes**.

### ID 17 — `rfid_pre_measure_settle_ms`

The actual 400ms stock pause preceding RFID reading/measurement. Shortening it may save time but read while the spool is still moving.

**Boundaries and evidence:** Correct hook at `0x08012A60`, not the separate 10ms stock delay. (hook: `0x08012A60`.) Stock: **400**; accepted range: **1–2000 ms**; CM5 value: **400**; SET: **yes**.

### ID 18 — `rfid_neighbor_detect_delay_ms`

Timing constant for the motion/positioning step used when detecting an adjacent spool's RFID tag; not a filament loading timeout.

**Boundaries and evidence:** Although named 'delay', it feeds the movement sequence; do not assume it is a purely stationary wait. (hook: `0x0801304A`.) Stock: **400**; accepted range: **50–2000 ms**; CM5 value: **400**; SET: **yes**.

### ID 19 — `rfid_neighbor_settle_ms`

Pause after searching the neighbouring spool/slot; allows settling before continuing or retrying.

**Boundaries and evidence:** Hook in delay call `0x08013078`. (hook: `0x08013078`.) Stock: **100**; accepted range: **10–1000 ms**; CM5 value: **100**; SET: **yes**.

### ID 20 — `feed_total_timeout_ms`

Overall timeout for stage-4 feeding toward the toolhead path. Longer values allow more time for a slow transfer; they do not increase speed.

**Boundaries and evidence:** Firmware converts milliseconds to 20ms ticks; hook `0x08032C72`. (hook: `0x08032C72`.) Stock: **25000**; accepted range: **5000–60000 ms, step 20**; CM5 value: **25000**; SET: **yes**.

### ID 21 — `feed_recovery_timeout_ms`

Maximum time allowed for a recovery operation after a stage-4 feeding difficulty. Changes the recovery failure/retry deadline, not forward speed.

**Boundaries and evidence:** Two stock branches use this parameter (`0x08032D46`, `0x08032DAC`). (hook: `0x08032D46 + 0x08032DAC`.) Stock: **3000**; accepted range: **500–10000 ms**; CM5 value: **3000**; SET: **yes**.

### ID 22 — `odometer_stall_timeout_ms`

How long the odometer may show no progress during feed before the motion is judged stalled. Low values may give false stalls; high values delay fault detection.

**Boundaries and evidence:** Does not change distance accounting; only the 20ms-tick stall threshold. (hook: `0x08032CEC`.) Stock: **500**; accepted range: **100–2000 ms, step 20**; CM5 value: **500**; SET: **yes**.

### ID 23 — `rfid_neighbor_retry_count`

Number of iterations/attempts in the neighbouring-spool RFID search loop. Reducing it to 1 shortens the sequence but may miss harder-to-read tags.

**Boundaries and evidence:** Stock `r4` comparison at `0x0801307C`; not a global retry count for all CFS errors. (hook: `0x0801307C`.) Stock: **3**; accepted range: **1–10 attempts**; CM5 value: **1**; SET: **yes**.

### ID 24 — `reverse_detooth_timeout_ms`

Time threshold for the reverse de-tooth branch. It gates when that gear-release action is performed; not unload speed.

**Boundaries and evidence:** Interacts with ID6: disabling ID6 forces the branch to be skipped; hook `0x080108F0`. (hook: `0x080108F0`.) Stock: **1000**; accepted range: **100–10000 ms**; CM5 value: **1000**; SET: **yes**.

### ID 25 — `buffer_fill_timeout_ms`

Catalogue buffer-fill timeout but with no reachable hooked stock code. It does not control physical filling time in CFG6.

**Boundaries and evidence:** Not exposed to SET/RESET. Stock: **30000**; accepted range: **1000–60000 ms**; CM5 value: **GET-only**; SET: **no**.

### ID 26 — `transition_detect_timeout_ms`

Transition-detection timeout present only as a catalogue field, with no effective stock hook in this build.

**Boundaries and evidence:** Not exposed to SET/RESET. Stock: **300**; accepted range: **50–5000 ms**; CM5 value: **GET-only**; SET: **no**.

### ID 27 — `load_retry_count`

Number of forward/backward pretension cycles in the loading path. It is not the number of complete retry attempts following every spool-load failure.

**Boundaries and evidence:** Hook changes the real loop compare at `0x08012616`; value 1 reduces stock repetitions from 3 to 1. (hook: `0x08012616`.) Stock: **3**; accepted range: **1–10 cycles**; CM5 value: **1**; SET: **yes**.

## Loading/unloading speed: CFS versus the toolhead

**The CFS hub motor** primarily uses ID2 (`hub_transition_speed`) for transfer toward the toolhead, ID3 (`hub_insert_speed`) for final insertion, ID1 (`hub_forward_speed`) for initial push and ID5 (`hub_reverse_speed`) for unloading. IDs11–13 are *RFID motion controls*, not standard filament transport speed.

**The toolhead extruder** is instead governed by Kalico's `[box]` settings, not these 28 IDs: `hotend_feed_speed` is volumetric mm³/s; `retract_velocity` and `external_feed_velocity` are feedrates in mm/min. They operate a different motor system from CFS feeders/hub.

## Evidence and remaining validation

- Source-verified: Kalico catalogue, stock defaults, limits and original ARM addresses; Unicorn differential ABI/flag and type-4 tests.
- Hardware-confirmed: `v323-cfg6` identity, 23 startup SET operations, 28 readable IDs, and a 23/23 GET match. **No** Z homing, axis movement, loading/unloading or RFID movement took place in that validation.
- Still unverified physically: dynamic behavior of the 15 advanced hooks, tag reliability, extended RS-485 stability and type-4 remaining values during real printing.
- **Conservative testing:** preserve a CFG5T rollback and change advanced RFID/timeouts individually, with observed results.

## Code references

[builder](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/firmware/v3.25-cfg6-lab/build-v3_23.py) · [ARM wrappers](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/firmware/v3.25-cfg6-lab/handler-v3_23.S) · [automated tests](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/firmware/v3.25-cfg6-lab/test_all_offline.py)

[Kalico integration guide](https://github.com/MzTechnology97/kalico-k2pro/blob/k2-pro-openhost/docs/CFS_RUNTIME_CFG6_EN.md) · [Versione italiana](CFS_RUNTIME_CFG6_PARAMETERS_IT.md)
