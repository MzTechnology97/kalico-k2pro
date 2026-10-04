# K2 Pro / K2-OpenHost integration status

This branch is the current integrated Kalico target for the K2-OpenHost project.

Last updated: **2026-10-04**.

## Upstream attribution

The code base is inherited from `Jacob10383/kalico`, itself based on `KalicoCrew/kalico` and ultimately `Klipper3d/klipper`. K2-specific extra implementations are inherited from Jacob/Jacobean's public K2 custom-firmware work. Those upstream authorship references remain intact.

## Integrated components

- K2 Pro baseline configuration;
- Jacobean K2 extras used by the real K2 hardware;
- K2 Pro CFS four-byte `BOX_STATE` compatibility;
- protected CFS `observation_mode` with a CFS-layer guard;
- Jacob-compatible CFS print metadata/mapping API for Mainsail;
- K2 Pro closed-loop motor-control topology and tuned configuration;
- startup delay/retry handling for external-host boot timing;
- Jacob10383's 071c813 update: native logical-tool mapping and the `_BOX_PAUSE_CAPTURE` / `_BOX_RESUME_PREPARE` / `_BOX_RESUME_COMMIT` pause contract;
- CFS mapping warnings, strict material-variant rule and manual runout order;
- OrcaSlicer filament Sync through Moonraker `lane_data`;
- tracked G-code shell command support (Cartographer comes from the official plugin and is not tracked here);
- CI: Ruff, firmware build and the strict MkDocs build.

## Current transport

```text
Main MCU   -> /dev/ttyUSB0 -> T113 ttyGS0 -> ttyS2
Nozzle MCU -> /dev/ttyUSB1 -> T113 ttyGS1 -> ttyS3
RS-485/CFS -> /dev/ttyUSB2 -> T113 ttyGS2 -> ttyS5
Cartographer -> direct USB on CM5 (preferred target)
```

On the printer side the [T113 bootstrap](https://github.com/MzTechnology97/k2-openhost-t113-bootstrap) runs these three bridges from the T113's slot B at every boot, installed from the [K2-OpenHost Installer Helper](https://github.com/MzTechnology97/k2-openhost-installer-helper). It was prepared on stock firmware 1.1.0.94 and is not yet hardware-validated.

The earlier Cartographer MUX/DEMUX experiment is not the final topology. It carried live Cartographer MCU traffic, but reset/re-enumeration and PTY lifecycle add unnecessary complexity. The three gadget serial channels are now reserved for the original K2 buses.

## Hardware validation completed

The real K2 Pro has now been run from this external Kalico branch with the following validated:

- native AArch64 `c_helper.so` build/runtime;
- Main and Nozzle MCU simultaneous sessions;
- RS-485 transport and closed-loop X/Y controller communication;
- motor-control startup recovery across transient first-attempt discovery failures;
- normal CoreXY motion;
- X/Y sensorless/stall homing;
- correct Z direction;
- complete `G28` using the stock **PRTouch** Z-probe path;
- bed, nozzle and chamber heater operation;
- heater PID tuning;
- emergency shutdown with active heater loads removed correctly;
- successful Klippain-ShakeTune resonance test;
- protected CFS observation stack on the shared RS-485 bus;
- operational CFS Box mode (`observation_mode: false`) with persistent RFID inventory, `BOX_PRINT_INFO` on real sliced files and backend auto-mapping;
- Moonraker visibility of normalized Box/CFS slot and filament-path state.

## CFS observation mode

The K2 RS-485 bus is shared. Global transport blocking would also affect motor-control and other devices. The K2-OpenHost protection therefore wraps only the CFS/Box layer and blocks non-whitelisted CFS requests before serial transmission while leaving `serial_485.py` available to the rest of the machine.

A hardware-backed reference run of the Jacobean `Box()` class completed:

```text
35 TX
35 RX
0 CRC errors
0 invalid-length frames
0 unmatched replies
0 timeouts
0 send/reader errors
```

A deliberate `0x0D` mutation was blocked before TX.

## CFS print mapping API

Jacob's current CFS workflow separates slicer logical tools from physical CFS slots. K2-OpenHost now carries an additive compatibility layer rather than replacing the hardware-validated Box transport/engine wholesale.

Enable it after `[box]`:

```ini
[box_print_mapping]
```

The helper adds:

```text
BOX_PRINT_INFO FILENAME="path/file.gcode"
BOX_PRINT_START FILENAME="path/file.gcode" MAP="0:1,1:3"
```

and extends `printer.objects.box` with:

```text
print_mapping_version: 1
print_mapping_enabled: true|false
print_info
print_mapping
```

`BOX_PRINT_INFO` is metadata-only. It reads the Orca G-code footer and reports used logical tools plus material/color/profile information. `BOX_PRINT_START` validates an exact logical-tool -> slot map, validates CFS slot availability, loads the Virtual SD file, installs the mapping and then starts it.

The compatibility layer also translates Orca purge-matrix and nozzle-temperature arrays from logical tool indices into the physical-slot indices consumed by the current OpenHost `BoxChangeEngine`. It wraps `PARSE_FLUSH_VOLUMES` so the normal K2 `START_PRINT` macro does not overwrite the translated mapping metadata.

When `observation_mode: true`, the mapping/status API remains visible and `BOX_PRINT_INFO` can be tested, but `BOX_PRINT_START` deliberately refuses to execute CFS mutations.

The companion `mainsail-k2openhost` fork uses this API in the normal Print dialog to present a Jacob/Fluidd-style filament mapping step. `BOX_PRINT_INFO`, live inventory and backend auto-map decisions have been exercised against the real K2 Pro; controlled `BOX_PRINT_START`, real tool-change/runout behavior and a complete supervised print still require staged hardware validation.

### HelixScreen command aliases

K2-OpenHost deliberately keeps the public Flat/Fork CFS contract used by upstream HelixScreen so a HelixScreen fork is not required. The base `box.api_version` remains `1`; K2-OpenHost extensions such as the reusable filament inventory are additive and versioned separately.

The compatibility surface includes the flat `slots[]` payload, `external: true` spool entry, `runout_swap_enabled`, `_BOX_SLOT_SET`, `_BOX_SLOT_CLEAR`, high-level `T<n>` / `BOX_UNLOAD`, plus aliases used by upstream HelixScreen: `BOX_INFO_REFRESH ADDR=<n> NUM=<mask>`, `BOX_ENABLE_AUTO_REFILL ENABLE=0|1`, and `BOX_NOZZLE_CLEAN`. Unknown additive slot fields such as RFID remaining estimates may be ignored by older clients.

## CFS filament inventory and K2-RFID interoperability

K2-OpenHost keeps Jacob's `box.api_version: 1` contract for OrcaSlicer compatibility and advertises the additive library separately as `filament_inventory_version: 2`. The inventory layer adds a persistent filament library on top of the existing slot profiles. The library and slot assignments live in the configured `state_path` (normally `~/printer_data/filament_box.json`) and are published through `printer.objects.box.filaments` and `printer.objects.box.slots`.

With `library_path` (default in `config/k2/box.cfg`: `~/printer_data/config/cfs_filaments.json`) the custom profiles live in their own JSON file:
- it is visible in the Mainsail file manager and in Moonraker backups;
- a replaced file is reloaded automatically or with `_BOX_FILAMENT_RELOAD`;
- profiles from an older state file move there once, with a backup `<state_path>.pre-library`.

Brands are managed from the Mainsail filament library.

Reusable filament profiles contain an ID, material, default color, brand, name, flush/target temperature, optional nozzle min/max range, optional pressure-advance metadata, optional RFID material code and optional Spoolman ID. The target temperature is copied into each assigned slot, so two saved profiles using the same material family may still keep different purge/fallback temperatures. They can be created from the Mainsail **CFS filament library** or from G-code:

```text
_BOX_FILAMENT_SET ID=00014 MATERIAL=PETG-CF COLOR="#202020" BRAND=Generic NAME="Generic PETG-CF" TARGET_TEMP=250 RFID_CODE=00014
_BOX_SLOT_ASSIGN SLOT=1 FILAMENT_ID=00014
_BOX_FILAMENT_DELETE ID=00014
```

A non-RFID slot may also be edited directly with `_BOX_SLOT_SET`. A physical slot with a live RFID record is treated as RFID-managed and manual assignment is rejected until the live tag is no longer controlling that slot. Unknown RFID records are exposed per slot to Mainsail; **Map RFID** opens the filament-library editor prefilled with the tag code and color. Saving a matching custom filament immediately re-resolves the pending RFID record without requiring another scan. The external reader uses the same pending-resolution path. The external spool is a first-class slot and can use the same saved library.

The ID model is intentionally compatible with DnG-Crafts/K2-RFID and Creality's catalog convention. K2-RFID stores a five-character material ID in its database and writes the RFID `filamentId` as a six-character value prefixed with `1`. K2-OpenHost therefore resolves both the saved five-character ID and the corresponding `1xxxxx` RFID value to the same custom filament profile. The color still comes from the RFID record, so one material profile can be used for multiple spool colors. The printer remains read-only for RFID media; writing tags is left to a dedicated writer such as K2-RFID.

RFID resolution order is: Spoolman ID in `reserve` when present, K2-OpenHost custom filament library, saved RFID mapping, then the built-in Creality-compatible catalog. Successful RFID resolution populates the slot automatically and marks its source as `rfid` or `spoolman`.

K2-OpenHost ships `config/k2/cfs_system_filaments.json`, generated from the current DnG-Crafts/K2-RFID `db/k2.json` by `scripts/generate_cfs_system_filaments.py`. The shipped catalog contains the complete Creality + Generic subset (currently 30 Creality and 31 Generic profiles) including material type, target/min/max nozzle temperatures, pressure-advance metadata and RFID material IDs. System profiles are read-only in Mainsail. A separate Creality/K2-RFID `material_database.json` (or the compact K2-OpenHost `materials` JSON form) can still be supplied with `material_database_path`; it extends the system catalog and is reloaded when its mtime changes. When `auto_register_rfid_filaments: true`, inserting a tag whose material ID exists in either catalog automatically creates or reuses the K2-OpenHost library profile. Profile identity is based on brand/name/material, not spool color, so differently coloured tags reuse one material profile while each slot keeps the colour read from the tag. `auto_seed_material_database: true` imports both catalogs at startup.

RFID remaining filament is tracked in two layers. The CFS-reported percentage is retained as `rfid_reported_percent`; K2-OpenHost also keeps an estimated `rfid_percent`/`rfid_remaining_m` from the RFID spool length and `print_stats.filament_used`. `BOX_RFID_SCAN` performs an explicit all-populated-slot scan, while `_BOX_RFID_READ_SLOT SLOT=n` rereads one physical bay on demand. During printing the estimate is decremented from actual positive extrusion usage and persisted periodically and when printing stops. A later hardware reread may lower the estimate but a stale CFS percentage never increases it. `_BOX_RFID_READ_SLOT SLOT=n` forces a single-slot RFID reread and refreshes the hardware percentage. Confirmed runout persists the source estimate at zero and clears its active slot profile after a successful swap (or before pausing when the CFS has positively reported runout but no replacement exists). K2-RFID Windows/Android tags that use the non-unique serial `000001` use a portable fingerprint based on supplier/material/color/nominal length/reserve, so moving a spool between CFS slots preserves its local remaining estimate. If two simultaneously inserted tags are indistinguishable by those fields, the second live instance is deliberately split by slot to prevent the two physical spools from corrupting each other's estimate. The previous slot-scoped estimate format is migrated on first use.

Runout groups are formed from present slots with exactly the same material string and colour. When RFID percentages are known, both automatic print mapping between otherwise equal candidates and runout replacement chains prefer the lowest remaining percentage first; slots without a known percentage are used after known RFID spools. Manual slot selection still remains authoritative for the currently loaded source.

The order can also be set by hand, useful for spools without RFID (a full 1 kg spool, a 300 g one and a nearly empty one):

```text
_BOX_SET_RUNOUT_ORDER ORDER=2,1,0   # physical slots, first used first
_BOX_SET_RUNOUT_ORDER AUTO          # back to the automatic order
```

The order is persisted as the `runout_order` setting. Each slot reports its `runout_rank`, and the strategy shows `manual_order`. The Mainsail Runout swap widget edits it with up/down arrows.

Slot inventory persistence is deliberately event-driven. `filament_box.json` stores both manual and RFID slot assignments plus spool-identity remaining estimates. On startup K2-OpenHost performs one CFS slot-presence mask query, restores cached metadata only for occupied bays, and leaves per-tag RFID reads disabled by default. A live present→absent transition clears the corresponding slot immediately; a bay already absent during boot is cleared only after repeated topology confirmation to avoid destroying valid inventory because of a transient RS-485 startup sample. RFID records are read on insertion, explicit per-slot reread, or only when the optional startup-reread setting is enabled.

## HelixScreen compatibility

K2-OpenHost deliberately keeps compatibility with **both** the original/upstream HelixScreen CFS implementation and Jacob10383's experimental `feat/k2-box-fork-support` branch, without requiring a K2-OpenHost-specific HelixScreen fork. The original implementation expects the stock Creality nested `box` schema, while the experimental branch can also understand the Flat/API-v1 Box contract. Mainsail and Jacob's Orca integration use the richer K2-OpenHost flat API. The backend therefore publishes both representations and accepts both command dialects at the same time.

- `box.api_version` stays exactly `1`; K2-OpenHost extensions use separate version fields.
- `box.slots[]` remains the canonical K2-OpenHost flat slot list for Mainsail/Orca, including inventory, RFID percentage and estimated remaining length.
- In parallel, `box.T1`..`box.T4`, `map`, `same_material`, `auto_refill`, `filament_useup` and `filament` mirror the stock K2 shape consumed by upstream HelixScreen. Each unit exposes `color_value`, `material_type`, `remain_len`, `vender`, environment values and the active A/B/C/D lane.
- Manual K2-OpenHost slot metadata is projected into the stock-compatible nested fields as well. This means an untagged slot manually assigned in Mainsail remains visible to HelixScreen without a Helix-specific database or fork.
- HelixScreen's `BOX_MODIFY_TN` and `BOX_MODIFY_TN_DATA ... PART=color_value` commands are accepted. Tool mapping is persisted in K2-OpenHost and the T0..T15 fallbacks honor that map when no per-print `BOX_PRINT_START` map is active.
- The stock K2 command envelope emitted by HelixScreen (`BOX_SAVE_FAN`, `BOX_MODE_WAIT`, `CR_BOX_*`, etc.) is accepted. OpenHost deliberately collapses those steps onto the already validated high-level change/unload engine so the stock envelope does not duplicate purge, cut or RS-485 operations.
- The current `feat/k2-box-fork-support` branch deliberately gives the nested `T1` stock schema precedence if a payload contains both nested and flat representations. With K2-OpenHost's dual payload this means that branch currently follows the same stock-compatible path as original HelixScreen. If upstream later prefers the Flat/API-v1 path, the native Fork commands (`T<n>`, `BOX_UNLOAD`, `_BOX_SLOT_SET`, `_BOX_SLOT_CLEAR`) remain available as well.
- K2-OpenHost publishes the occupied CFS slots to Moonraker's `lane_data` namespace (`publish_lane_data`, see OrcaSlicer below), the AFC/Happy Hare convention that HelixScreen also reads. No HelixScreen-specific fields are needed in `filament_box.json`, and HelixScreen keeps its own override merge logic unchanged.
- The [T113 bootstrap](https://github.com/MzTechnology97/k2-openhost-t113-bootstrap) installs upstream HelixScreen on the printer screen, pointed at this host's Moonraker.

This compatibility layer is additive: current Mainsail, Orca and the K2-OpenHost filament inventory continue to use the flat API, while HelixScreen sees the stock K2 contract it already supports.

## OrcaSlicer direct CFS printing

**Official OrcaSlicer (2.3.2 and later), filament Sync:**
- `box_lane_data.py` keeps one `lane<N>` entry per occupied physical slot in Moonraker's `lane_data` namespace, with `lane`, `material`, `color`, `nozzle_temp`, `spool_id`, `name`, `vendor` and `filament_id`;
- the Sync button in OrcaSlicer fills its filament list from these entries;
- writes run in a background thread and send only what changed;
- `publish_lane_data: false` in `[box]` turns it off;
- OrcaSlicer 2.4.2 sends a placeholder API key that Moonraker rejects; the [K2-OpenHost OrcaSlicer guide](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/ORCASLICER.md) has the nginx fix.

**Jacob10383's OrcaSlicer fork:**

Jacob10383's OrcaSlicer fork detects `box.print_mapping_version == 1`, queries `printer.objects.box`, uploads without auto-start, asks the printer to inspect the stored G-code with `BOX_PRINT_INFO`, and starts it with `BOX_PRINT_START` plus the selected logical-tool to physical-slot map. K2-OpenHost deliberately keeps `print_mapping_version: 1`, so this path is protocol-compatible without a stock Creality mapping endpoint.

For an ordinary Moonraker print start, K2-OpenHost also has an optional backend auto-mapper:

```ini
[box_print_mapping]
auto_map_prints: true
auto_map_block_unresolved: true
```

The auto-mapper uses material family plus perceptual OKLab color matching derived from Jacob's Orca mapping logic. Exact Orca profile-name matches remain strongest; when the slicer name is not present in the slot inventory, an otherwise compatible `Generic` profile is preferred over an unrelated vendor profile with the same material/color. The same suggestion is published in `box.auto_mapping` when `BOX_PRINT_INFO` inspects a file, so Mainsail and the firmware share one mapping decision path. On the validated K2-OpenHost profile `auto_map_prints` is enabled; a normal Orca/Moonraker print start installs the mapping before the first `T` command. If any used tool cannot be resolved, the start is rejected rather than silently printing from the wrong spool. Explicit `BOX_PRINT_START` mappings always take precedence.

Mapping rules added on 2026-10-04:
- **Warnings that never block.** `mapping_warnings` (also in `box.auto_mapping.warnings`, logged to the console) reports:
  - `low_filament`: the estimated need, `length_mm/1000 * 1.1 + 1` m, is more than the slot plus its same-material, same-colour runout partners hold;
  - `material_variant` and `material_mismatch`.
- **Strict variants.** A base material is never auto-mapped onto its filled variant (CF, GF, KF and AF fillers): a PETG print does not start on PETG-CF because no similar PETG is loaded. The loaded-filament fallback also requires a compatible material.

Hardware-backed metadata validation on `cubo.gcode` detected PETG tools T0/T1. With a temporary black PETG profile on physical slot 1 and cyan PETG profile on slot 2, `BOX_PRINT_INFO` produced `auto_mapping: {0:1, 1:2}`; after the temporary profiles were removed the same file returned both tools unresolved, confirming the fail-safe behavior without starting a print.

## Motor-control startup policy

The CM5 can become ready before the K2 motor controllers. The OpenHost integration therefore uses an explicit startup delay plus multiple retries instead of treating the first missing response as a permanent failure. Hardware restart testing demonstrated successful recovery on later attempts.

The production transport must have exactly one owner per UART/gadget endpoint. A duplicate GS2 bridge was discovered during the experimental Cartographer multiplexing work and caused RS-485 instability; after returning to a single bridge, motor-control operation returned to normal.

## Losing a serial link behind the T113

The external host sees the K2's three buses as USB gadget serial ports bridged by the T113 (ttyGS0 → Main MCU, ttyGS1 → Nozzle MCU, ttyGS2 → RS-485). Measured on the development K2 Pro, in standby, on 2026-10-04:

| What was lost | What the host did | Why it is safe or not |
| --- | --- | --- |
| Main or Nozzle MCU bridge | shutdown after 5.2 s: `Lost communication with MCU 'nozzle_mcu'` | Klipper's own MCU protocol notices. Heater outputs are configured with a 3 s `max_duration`, so an MCU that stops getting host updates turns its heaters off by itself. |
| RS-485 bridge (CFS, X/Y motor boards) | nothing: Klipper stayed `ready`, `motor_ready` stayed true, only request timeouts in the log | a print would go on with the CFS and the motor diagnostics silent |

Notes from those tests:
- An MCU that lost its link may not be in shutdown: it never received the command. The first `FIRMWARE_RESTART` can then fail with `Failed automated reset of MCU`. Here a second one worked; a power cycle of the MCU rail (below) always resets the MCUs cleanly.
- Motor faults still stop the printer during an RS-485 outage. The stall pins go to the Main and Nozzle MCUs, not to RS-485, and a stall whose protection query fails is handled as an unverified fault (X/Y shutdown).
- When the RS-485 bridge comes back, traffic resumes on its own, with no restart.

### RS-485 link watchdog (`serial_485`)

`[serial_485 serial485]` now tracks whether any device on the bus answers.

**When the link counts as lost:** no device has answered for `link_lost_timeout` seconds (10 by default) and at least `link_lost_timeouts` requests in a row timed out (3). A single absent device, such as a CFS that is not connected, does not count while the others answer.

**What happens then:**
- **During a print:** `link_lost_action` applies. `pause` is the default: the print is paused the same way the extruder fault pause does it. `warn` only reports; `shutdown` stops Klipper.
- **When idle:** only a console warning.
- **Always:** the event `serial_485:link_lost` is sent.

**When it comes back:** the first answer from any device logs `RS-485 link restored` and sends `serial_485:link_restored`.

**Status:** `serial_485 serial485` and `SERIAL_STATUS` add:
- `link_state` (`unknown`, `ok`, `degraded`, `lost`);
- `link_ok_age`, `consecutive_timeouts`, `link_lost_count`, `link_lost_for`, `link_lost_action`.

### The T113 from Kalico (`[k2_t113]`)

`config/k2/k2_t113.cfg` talks to `k2oh-ctl`, the control service of the [T113 bootstrap](https://github.com/MzTechnology97/k2-openhost-t113-bootstrap) (slot B). Every request runs in a worker thread, so the reactor never waits for the network. With `host` empty the section does nothing; the installer helper fills in `host` and copies the shared token to `token_file`.

| G-code | Effect |
| --- | --- |
| `T113_STATUS` | last T113 telemetry: slot, MCU power, SoC temperature, uptime, UDISK, USB gadget, bridges |
| `T113_BEEP [MS=200] [COUNT=1]`, `M300 [P<ms>]` | the printer buzzer (GPIO164, fixed tone). `M300` is registered only if no macro defines it. |
| `T113_BRIDGES_RESTART CONFIRM=1` | restarts the three USB bridges. It takes under 5 s, so Klipper stays connected (tested). |
| `T113_SCREEN_RESTART` | restarts HelixScreen |
| `T113_MCU_POWER_CYCLE CONFIRM=1` | cuts the MCU rail (GPIO140) for 2 s with the bridges stopped, then `FIRMWARE_RESTART`. It also works while Klipper is shut down. Tested: ready in 9 s, CFS in 16 s, motors in 21 s. |

The T113 refuses the power cycle and the bridge restart unless the host's Moonraker reports a known idle print state. When Klippy is already shut down, these G-codes pass `force`, which the T113 accepts only in that state.

| Option | Default | Effect |
| --- | --- | --- |
| `estop_on_shutdown` | `off` | `m112`: an emergency stop also cuts the MCU power rail, so heaters and motors lose power even if an MCU stopped answering. `any`: every shutdown does. |
| `auto_power_cycle` | `False` | after `Lost communication with MCU` while no print was running, power-cycle the MCUs and restart, at most once every 10 minutes |

`get_status` (`k2_t113`) carries `enabled`, `connected`, `age`, `error`, `telemetry` and `last_action`.

**External RFID beep:** `[external_rfid_reader] beep_backend` can be `local` (the stock PWM buzzer on the host board), `t113` (through `[k2_t113]`) or `none`. `config/k2/box.cfg` sets `t113`, because on OpenHost the buzzer is wired to the T113, not to the host.

The MCU power rail is also available as a Moonraker power device; the installer helper writes it.

## Motor readiness

`motor_ready` only says that startup finished. `motor_control.readiness.<axis>` says what was verified on the way, without reading hardware from `get_status`.

| field | meaning |
| --- | --- |
| `reachable` | the axis answered target discovery in this startup |
| `parameters.state` | `verified`: every `motor_control.cfg` override read back equal to its value; `degraded`: some read, write, apply or readback failed or differed; `failed`: the overrides could not be applied at all; `unknown`: not checked yet |
| `calibration.state` | `read`; `suspect`: the electrical offset is near 0; `read_failed`; `unknown` |
| `configured` / `calibration_verified` | `parameters.state == verified` / `calibration.state == read` |
| `operational` | startup finished and the axis is not blocked by the policy |
| `degraded` | operational, but parameters or calibration are not verified |
| `reasons` | short texts for everything that is not verified |

Problems are listed as `critical_problems`, `diagnostic_problems` and `unclassified_problems`. Each entry has `key`, `op`, `target`, `read`, `error` and `confirmed_mismatch`.

**Critical overrides:** control-loop gains and filters (`controller_pos/spd/cur_loop_*`, `controller_cur_filter_*`, `controller_leso_*`) and protection thresholds (`protection_param_prt_*`, `pos_over_limit_*`, `encoder_mutation_*`, `power_voltage_min`, `mcu_temp_max`, `err_code_mask`). They set how the closed loop behaves and when a fault trips.

**Diagnostic overrides:** `protection_param_protect_report` and `protection_param_warning_code_mask`, which only change what is reported.

**Unclassified overrides:** any other key keeps the previous behaviour, a warning only.

**Policy** (`override_policy` in `[motor_control]`, set to `warn` in `config/k2/motor_control.cfg`):
- `warn` (default): as before, problems are warnings and the axis runs `degraded`.
- `block`: startup fails, and retries, only when a critical override is confirmed wrong. A confirmed wrong value means a write failed after reading a different value, or the readback after writing differs. Read, verify or apply errors leave the value unknown and do not block. Calibration never blocks.

**Tuning is unchanged.** Edit the values in `motor_control.cfg` and restart: the overrides are read, written when different, applied and read back exactly as before. A tuned value that the board accepts is `verified` under both policies.

**Calibration ids 9 and 25:**
- 9 is `param_elec_offset`. A value near 0 may be an uncalibrated motor, so it is reported as `suspect`.
- 25 is `param_elec_offset_err_deg`, the residual error of the calibration. Near 0 is a good result, so it is reported raw and no longer triggers the near-zero warning.

Every startup or retry resets readiness.

### Nozzle transport for the E motor

X and Y talk over the shared RS-485 bus: its counters are in `serial_485` and cover every device on that bus. E talks through the Nozzle MCU's transparent transport (`transparent_send` / `transparent_response`), which has its own counters in `motor_control.nozzle_transport`.

| field | meaning |
| --- | --- |
| `configured` / `busy` | the transparent command is registered / a send is in progress |
| `sends` | calls from the motor firmware client: one logical command attempt each, because the client owns retries and calls again |
| `wire_attempts` | `transparent_send` commands on the Nozzle link; a send with `attempts=N` makes up to N |
| `responses` / `timeouts` | wire attempts answered / not answered before the host deadline |
| `no_response` | sends whose every wire attempt timed out |
| `busy_rejections` | sends refused because another send was in progress |
| `send_errors` / `protocol_errors` | the host could not send / the answer had an unexpected payload type |
| `last_result`, `last_error`, `last_at` | the latest outcome (monotonic time; raw bytes scrubbed) |
| `latency_ms` | `last`, `min`, `max`, `avg` from send to response, on answered wire attempts |

**Timeouts:** the device timeout (`timeout_ms` sent to the Nozzle MCU, from the command timeout) differs from the host wait. The host waits `max(timeout + 0.25 s, 0.5 s)` unless a `response_timeout` is given.

**Response matching:** the protocol has no transaction id. A response counts only if the MCU stamped it after the current query started (`#sent_time`). The callback is removed when the wait ends, so a response that arrives later is not counted and cannot be attributed. One send at a time is enforced by `busy`.

**Budget:** this adds no traffic. The temperature poll reads one axis every 6 s (E every 18 s) and the protection poll runs every 60 s; these counters only describe that traffic. Timeouts, retries and polling are unchanged.

### Motor event history

`motor_control` keeps a bounded, in-memory history of protection events and recovery steps, so a pause or recovery can be explained after the fact. The cached fault itself is cleared after a confirmed recovery; the history is not.

| type | when |
| --- | --- |
| `fault_detected` / `warning_detected` | a valid protection answer reports an error or a warning |
| `query_failed` | a protection query timed out or was unverified |
| `policy_shutdown` / `policy_recover` / `policy_homing_abort` / `policy_startup_cleanup` | the action chosen for a confirmed error (X/Y shutdown, E pause and clear, abort during homing, deferred clear during startup) |
| `pause_requested` | the extruder fault paused the print |
| `clear_requested` | a clear was sent; `result: not_acknowledged`, because the command has no ACK |
| `clear_confirmed` / `clear_persistent` | the next valid query found the axis healthy, or still faulted |

Each event has:
- `seq`, `type` and `axis`;
- the codes with their labels;
- `source` (`periodic_poll`, `stall_pin:<n>`, ...) and `context` (`startup`, `homing`, `printing`, `paused`, `idle`);
- `result`, `error` and `session`;
- `at` and `last_at` (reactor monotonic seconds), `wall` (wall-clock time of the first occurrence) and `count`.

An event identical to the previous one of the same axis is not repeated: its `count` and `last_at` grow. An unchanged fault seen by every 60 s poll is therefore one line.

**Limits:**
- `event_history` in `[motor_control]` sets the size: 50 by default, 10 to 500. The oldest events drop out first.
- Nothing is written to disk.
- A new startup session starts a new deduplication.

**Reading it:**
- `get_status` carries the newest 20 events (`motor_control.events`), with no I/O.
- `MOTOR_EVENTS [COUNT=n]` prints the history.
- `MOTOR_EVENTS VERBOSE=1` prints it as JSON for a report. Error texts have raw packet bytes replaced by `<hex>`; there are no serial numbers or credentials.

### Cached override and calibration values

`motor_control.param_cache` keeps the last value read from each motor board, so clients and `MOTOR_STATUS` do not send packets to show them.

Per axis it holds:
- `overrides.<key>`: `target`, `read`, `match` (true, false, or null when unknown), `error`, `phase` (`read`, `write`, `verify`, `skip`), `source`, `at`, `session`, `age` and `current`;
- `apply_error`, when the apply command after writes failed;
- `calibration`: the `summary` (state, values, errors, suspect keys), `source`, `at`, `age` and `current`.

Each part is filled by:

| source | when |
| --- | --- |
| `startup` | the startup override apply and calibration read |
| `manual` | `MOTOR_CFG_OVERRIDE_STATUS` |
| `refresh` | `MOTOR_STATUS REFRESH=1` |

No write is made to fill the cache. A value from `motor_control.cfg` or the registry is only a `target`, never a readback. Every startup or retry opens a new session: older values stay, with `current: false`.

**`MOTOR_STATUS`:**
- By default it shows the cached calibration, with `cached Ns ago (source)`, and reads nothing.
- `MOTOR_STATUS REFRESH=1` rereads calibration only: 2 parameters per axis, as before this change. It stops starting new axes after 6 s, so one silent axis cannot hold the G-code queue for every axis's retries; skipped axes say so.
- `VERBOSE=1` still prints the JSON, with `calibration_source: cache|refresh`.

`MOTOR_CFG_OVERRIDE_STATUS` still reads every override from the boards, and now also updates the cache.

## Motor MCU temperatures

`motor_control` reads the MCU temperature of X, Y and E (GET index 17), one axis every 6 s, so each axis is read every 18 s. The polling is unchanged; subscriptions never send packets.

Each axis appears in two places:
- `temperature_sensor motor_X_MCU` (`_Y_`, `_E_`) is registered with `heaters`, so Mainsail and Moonraker show it like any temperature sensor;
- `motor_control.temperatures.<axis>` carries the full sample.

The standard sensor keeps the last `temperature` so graphs stay continuous. Its `valid`, `state` and `sample_age` fields say whether that value is a current measurement.

`state` is one of these, checked in this order:

| state | meaning | valid |
| --- | --- | --- |
| `stopped` | the monitor is not running (startup, retry, shutdown) | no |
| `failed` | the last read in this session failed | no |
| `never` | no read has ever succeeded | no |
| `previous_session` | the value was read before the last stop/start | no |
| `stale` | older than 36 s (two full X/Y/E rounds) | no |
| `current` | read in this session, recently, last read succeeded | yes |

Every stop/start opens a new acquisition session (`current_session`). Earlier values stay as history but are not current again until a new read succeeds.

`motor_control.temperatures.<axis>` also has:
- `temperature`: `null` until the first read;
- `last_update` and `last_attempt` (reactor monotonic seconds);
- `last_error`, `read_errors` and `consecutive_errors`;
- `session`, the session of the last successful read.

Zero and negative readings are accepted as measurements. NaN, infinity and non-numeric answers count as failed reads.

A `[temperature_sensor motor_X_MCU]` section in the configuration conflicts with these objects and is rejected with a clear error.

## MOT2 protection answers

The protection query (`FUNC_PROTECTION` `0x0C`, data `11`) must answer with exactly 8 bytes: `error_code` then `warning_code`, each a little-endian uint32. `motor_map.json` types the matching firmware parameters (`protection_param_err_code_mask`, `protection_param_warning_code_mask` and the `*_backup` values) as `uint32_t`. No other data value or length is verified, so none is accepted. A firmware extension needs a documented format first; it is never padded or cut.

The bit meanings are in `ERROR_CODE_LABELS` and `WARNING_CODE_LABELS`. They match [Jacob's error explanation](https://jacob10383.github.io/k2-plus-custom-firmware/error-explanation/#motor-control); that page explains each bit and what to check.

The frame status byte carries latch bits: `0x01` stall, `0x02` error and `0x04` warning.

| Answer | Result |
| --- | --- |
| 8 bytes, latch bits consistent with the masks | decoded; `active`, `has_error`, `stalled` |
| a mask set without its latch bit | decoded as active; `status_mismatch: true` |
| error or warning latch bit set, matching mask 0 | unverified |
| unknown status bits | unverified |
| payload not 8 bytes | unverified |
| bad CRC, address or function | transport error; retried once by the client |

An unverified answer raises `ProtectionResponseError`, the same as a failed query:
- the cached fault stays;
- the axis is never reported healthy;
- the existing policies apply unchanged: X/Y shut down, E pauses and can be cleared, a stall-pin query that fails is handled as an unverified fault.

`motor_control.py` is bundled from Jacob's K2 firmware. On the next sync, keep `PROTECTION_PAYLOAD_FORMATS`, `ProtectionResponseError` and the strict `decode_protection_payload()`. The upstream version pads short payloads with zeros.

Hardware still to confirm: the real latch-bit behaviour after a clear, with the printer idle.

### Protection validity and clears

`motor_control.faults.<axis>.validity` says how far the cached protection data can be trusted. It is filled only by queries the controller already makes: the 60 s periodic poll, stall-pin queries, startup, homing recovery and the clear commands. `get_status` never sends a packet.

`state` is one of these, checked in this order:

| state | meaning |
| --- | --- |
| `clear_pending` | a clear was sent and no valid query has confirmed its outcome yet |
| `query_failed` | the last query of this axis failed (timeout, unverified answer) |
| `unknown` | no valid answer in this session; every motor startup or retry opens a new session |
| `stale` | the last valid answer is older than 126 s |
| `current` | a valid answer in this session, recent enough; `valid: true` only here |

126 s is two 60 s polls plus the worst case of one poll round: 3 axes × 2 attempts × 1 s. One missed poll does not make the data stale; two do. The 36 s temperature threshold is not reused.

`validity` has these fields:
- `last_attempt`, `last_success`, `query_age` (reactor monotonic seconds);
- `last_error`, `total_errors`, `consecutive_errors`;
- `session` and `current_session`;
- `source` of the last valid answer (`query`, `periodic_poll`, `stall_pin:<n>`, `manual`, `clear-postcheck`, ...);
- `last_confirmed_fault`, kept as history after a clear: codes, time, session, source;
- `clear`: `requested_at`, `result`, `verified_at`, `unexpected_response`, `recheck_errors`, `last_recheck_error`.

**Clears:** the MOT2 clear command has no ACK. `clear_fault_latches()` therefore keeps the cached fault and marks the axis `clear_pending`. The next valid query sets `clear.result`:
- `confirmed`: the axis answered healthy;
- `persistent`: the fault is still there.

A failed or unverified recheck leaves it `pending` and counts in `recheck_errors`. A new startup session turns a pending clear into `abandoned`.

**Group queries:** axes are queried one at a time. If one axis fails, the axes that replied are still applied, and the error (`ProtectionQueryError`) carries both `partial` and `errors`. A single-axis query raises the original error.

A failed poll never shuts the printer down by itself. The policies on confirmed faults are unchanged.

## Probe baseline and Cartographer

The current known-good Z-homing baseline is the stock PRTouch stack. Full homing has been tested successfully with Cartographer disabled.

Cartographer uses the official [Cartographer3D plugin](https://github.com/Cartographer3D/cartographer3d-plugin) (see the [K2-OpenHost Cartographer guide](https://github.com/MzTechnology97/K2-OpenHost/blob/main/docs/en/CARTOGRAPHER.md)). It supports:

- `register_as_probe: true` for standalone Cartographer-as-probe operation;
- `register_as_probe: false` as the basis for optional mixed mode, where PRTouch remains the primary Z-reference probe and Cartographer is used for scanning/mesh work.

Direct-USB Cartographer validation on the CM5 is the next probe milestone. Mixed mode is optional and should only be enabled after standalone Cartographer is stable.

## Power-loss recovery on the K2 Pro

Upstream `power_loss_recovery.py` re-references Z through `[z_align]`: the MCU drops the bed onto the bottom photoelectric switch, away from the nozzle and the part, and the checkpoint stores where that switch sits in the print's Z coordinates. The K2 Pro has the same bottom switch with a single Z motor (Creality F012 stock: `endstop_pin_z: PA15`), so OpenHost enables `[z_align]` with the stock K2 Pro values and adapts `z_align.py` to accept a one-stepper reference frame.

`PLR_RECOVER CONFIRM=1` then follows the upstream flow: drop to the switch, home X/Y, rise to `max_print_z + recovery_lift` (capped by `maximum_recovery_z` and `zmax`) at the K2 Pro `max_z_velocity` of 10 mm/s, and restore heaters, CFS, mesh and position. Because `[z_align]` is configured, the first `G28` after boot also drops the bed to the switch before the fast rise and PRTouch homing, as on the stock firmware. State files default to `~/printer_data` on the CM5.

For single-Z printers without a bottom switch, `z_reference: trusted_position` restores the physical Z stored with each checkpoint instead; it is not used on the K2 Pro.

Integrated homing through `[z_align]` is hardware-validated (three consecutive `G28` / `M84` cycles aligned at the first MCU attempt on 2026-10-03). It requires the stock 16 Z microsteps: at 64 the MCU-driven routine lost steps and failed with photoelectric errors. `PLR_RECOVER` still needs a supervised power-cut validation.

## Local extras and clean Git updates

Moonraker/Mainsail update management expects this repository to remain clean. Files tracked by this fork should not be overwritten by external installers. Locally installed extras such as ShakeTune can remain outside Git tracking so they do not mark the Kalico repository dirty.

Fixes tested directly on the CM5 must be committed to this branch. A modified tracked file blocks Moonraker updates, and a hard recovery from the update manager discards it.

The K2 extras, CFS stack included, are maintained here. They used to be mirrored to `MzTechnology97/k2-pro-custom-firmware:k2-openhost`, checked by a drift workflow. That repository is archived since 2026-10-04 as read-only history, and the mirror and its workflow are gone.

Upstream changes from Jacob10383 are reviewed directly against [Jacob10383/k2-plus-custom-firmware](https://github.com/Jacob10383/k2-plus-custom-firmware) (`extras/`) and [Jacob10383/kalico](https://github.com/Jacob10383/kalico), then ported here as before.

## Next milestones

1. controlled single-tool `BOX_PRINT_START`, then a mapped multimaterial tool change with purge matrix and temperatures;
2. hardware-validate the upstream Box pause/resume flow (`_BOX_PAUSE_CAPTURE` / `_BOX_RESUME_PREPARE` / `_BOX_RESUME_COMMIT`), integrated with the 071c813 `box.py`;
3. direct-USB Cartographer cold boot, reset/reconnect and persistent by-id path;
4. controlled Cartographer probe/touch/scan and bed mesh;
5. first complete supervised print path, including a supervised power cut with `PLR_RECOVER`;
6. optional mixed PRTouch + Cartographer validation;
7. T113 bootstrap on the real printer: slot B trial boot, gadget/bridges, HelixScreen, `k2oh-mcu-fw`.

## Canonical project documentation

See `MzTechnology97/K2-OpenHost` for architecture, test status, roadmap, safety boundaries and complete cross-project credits.