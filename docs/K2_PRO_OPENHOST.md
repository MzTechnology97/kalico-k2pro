# K2 Pro / K2-OpenHost integration status

This branch is the current integrated Kalico target for the K2-OpenHost project.

Last updated: **2026-10-02**.

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
- tracked loader modules for Cartographer and G-code shell command support;
- synchronization/compile checks for K2-specific extras.

## Current transport

```text
Main MCU   -> /dev/ttyUSB0 -> T113 ttyGS0 -> ttyS2
Nozzle MCU -> /dev/ttyUSB1 -> T113 ttyGS1 -> ttyS3
RS-485/CFS -> /dev/ttyUSB2 -> T113 ttyGS2 -> ttyS5
Cartographer -> direct USB on CM5 (preferred target)
```

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

Runout groups are formed from present slots with the same material and colour. When RFID percentages are known, both automatic print mapping between otherwise equal candidates and runout replacement chains prefer the lowest remaining percentage first; slots without a known percentage are used after known RFID spools. Manual slot selection still remains authoritative for the currently loaded source.

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
- HelixScreen's `lane_data` convention remains its own standard Moonraker persistence layer. K2-OpenHost does not require HelixScreen-specific fields in `filament_box.json`, so upstream HelixScreen can keep using `lane_data` and its own override merge logic unchanged.

This compatibility layer is additive: current Mainsail, Orca and the K2-OpenHost filament inventory continue to use the flat API, while HelixScreen sees the stock K2 contract it already supports.

## OrcaSlicer direct CFS printing

Jacob10383's OrcaSlicer fork detects `box.print_mapping_version == 1`, queries `printer.objects.box`, uploads without auto-start, asks the printer to inspect the stored G-code with `BOX_PRINT_INFO`, and starts it with `BOX_PRINT_START` plus the selected logical-tool to physical-slot map. K2-OpenHost deliberately keeps `print_mapping_version: 1`, so this path is protocol-compatible without a stock Creality mapping endpoint.

For an ordinary Moonraker print start, K2-OpenHost also has an optional backend auto-mapper:

```ini
[box_print_mapping]
auto_map_prints: true
auto_map_block_unresolved: true
```

The auto-mapper uses material family plus perceptual OKLab color matching derived from Jacob's Orca mapping logic. Exact Orca profile-name matches remain strongest; when the slicer name is not present in the slot inventory, an otherwise compatible `Generic` profile is preferred over an unrelated vendor profile with the same material/color. The same suggestion is published in `box.auto_mapping` when `BOX_PRINT_INFO` inspects a file, so Mainsail and the firmware share one mapping decision path. On the validated K2-OpenHost profile `auto_map_prints` is enabled; a normal Orca/Moonraker print start installs the mapping before the first `T` command. If any used tool cannot be resolved, the start is rejected rather than silently printing from the wrong spool. Explicit `BOX_PRINT_START` mappings always take precedence.

Hardware-backed metadata validation on `cubo.gcode` detected PETG tools T0/T1. With a temporary black PETG profile on physical slot 1 and cyan PETG profile on slot 2, `BOX_PRINT_INFO` produced `auto_mapping: {0:1, 1:2}`; after the temporary profiles were removed the same file returned both tools unresolved, confirming the fail-safe behavior without starting a print.

## Motor-control startup policy

The CM5 can become ready before the K2 motor controllers. The OpenHost integration therefore uses an explicit startup delay plus multiple retries instead of treating the first missing response as a permanent failure. Hardware restart testing demonstrated successful recovery on later attempts.

The production transport must have exactly one owner per UART/gadget endpoint. A duplicate GS2 bridge was discovered during the experimental Cartographer multiplexing work and caused RS-485 instability; after returning to a single bridge, motor-control operation returned to normal.

## Probe baseline and Cartographer

The current known-good Z-homing baseline is the stock PRTouch stack. Full homing has been tested successfully with Cartographer disabled.

Cartographer support lives in `MzTechnology97/cartographer3d-plugin-k2openhost`. The plugin now supports:

- `register_as_probe: true` for standalone Cartographer-as-probe operation;
- `register_as_probe: false` as the basis for optional mixed mode, where PRTouch remains the primary Z-reference probe and Cartographer is used for scanning/mesh work.

Direct-USB Cartographer validation on the CM5 is the next probe milestone. Mixed mode is optional and should only be enabled after standalone Cartographer is stable.

## Power-loss recovery on the K2 Pro

Upstream `power_loss_recovery.py` re-references Z through `[z_align]`: the MCU drops the bed onto the bottom photoelectric switch, away from the nozzle and the part, and the checkpoint stores where that switch sits in the print's Z coordinates. The K2 Pro has the same bottom switch with a single Z motor (Creality F012 stock: `endstop_pin_z: PA15`), so OpenHost enables `[z_align]` with the stock K2 Pro values and adapts `z_align.py` to accept a one-stepper reference frame.

`PLR_RECOVER CONFIRM=1` then follows the upstream flow: drop to the switch, home X/Y, rise to `max_print_z + recovery_lift` (capped by `maximum_recovery_z` and `zmax`) at the K2 Pro `max_z_velocity` of 10 mm/s, and restore heaters, CFS, mesh and position. Because `[z_align]` is configured, the first `G28` after boot also drops the bed to the switch before the fast rise and PRTouch homing, as on the stock firmware. State files default to `~/printer_data` on the CM5.

For single-Z printers without a bottom switch, `z_reference: trusted_position` restores the physical Z stored with each checkpoint instead; it is not used on the K2 Pro.

Integrated homing through `[z_align]` and the recovery path still need supervised validation on the real K2 Pro, including a power cut.

## Local extras and clean Git updates

Moonraker/Mainsail update management expects this repository to remain clean. Files tracked by this fork should not be overwritten by external installers. Locally installed extras such as ShakeTune can remain outside Git tracking so they do not mark the Kalico repository dirty.

Fixes tested directly on the CM5 must be committed to this branch. A modified tracked file blocks Moonraker updates, and a hard recovery from the update manager discards it.

The CFS extras are mirrored to `MzTechnology97/k2-pro-custom-firmware:k2-openhost`. The read-only `K2 OpenHost - check extras drift` workflow fails when the two trees diverge; it never syncs files.

## Next milestones

1. controlled single-tool `BOX_PRINT_START`, then a mapped multimaterial tool change with purge matrix and temperatures;
2. adopt the upstream Box pause/resume flow (`_BOX_PAUSE_CAPTURE` / `_BOX_RESUME_PREPARE` / `_BOX_RESUME_COMMIT`) together with the matching upstream `box.py`, instead of importing the K2 Plus macros alone;
3. direct-USB Cartographer cold boot, reset/reconnect and persistent by-id path;
4. controlled Cartographer probe/touch/scan and bed mesh;
5. first complete supervised print path, including a supervised power cut with `PLR_RECOVER`;
6. optional mixed PRTouch + Cartographer validation.

## Canonical project documentation

See `MzTechnology97/K2-OpenHost` for architecture, test status, roadmap, safety boundaries and complete cross-project credits.