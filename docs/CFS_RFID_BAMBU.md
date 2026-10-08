# K2 Pro CFS — Bambu RFID fallback

In my `k2-pro-openhost` branch I added support for the CFS RFID **v3.3 / API7 stock-capture** firmware that I validated on hardware.

## Behaviour

Creality RFID remains the primary path. Bambu fallback runs only when the normal CFS record is empty/unknown and only when the CFS advertises API7.

The API7 path does not issue direct host RF reads. It:

1. reads the UID already discovered by the CFS;
2. derives the Bambu sector-1 Key A;
3. arms a one-shot three-key override;
4. invokes Creality's normal `force_rfid_read()`;
5. reads the captured block 4 material detail and block 5 RGBA from CFS scratch;
6. clears the override;
7. applies or creates the matching Bambu filament profile.

The stock CFS remaining path is **not** authoritative for Bambu API7 tags. Hardware tests show `CMD_RFID_REMAINING (0x03)` returning `0xFF` for Bambu spools, including PLA Matte, PETG HF and PETG Basic. I therefore treat `0xFF` as unavailable rather than as a percentage.

API7 v3.3 does not currently capture Bambu block 14. I am using the tag UID as the persistent spool identity and investigating a read-only path for nominal length plus CFS-encoder-based consumption without writing the tag.

## Required firmware

```text
hardware       cfs0_050_G32
application    cfs0_000_153
API            7
caps           0xE8
SHA-256        5bab3acff49253a54089e779ea473d2cf587db09ab0d9c07c4d6c2e31b810388
```

I publish the firmware, source handler and standalone porting documentation in:

`MzTechnology97/k2-cfs-rfid-tools/firmware/v3.3-stockcapture/`

## Configuration

My K2 profile includes:

```ini
[box]
auto_bambu_rfid_fallback: true
auto_mifare_rfid_fallback: true

[box_rfid_diag]
serial: serial485
address: 1
allow_active_rf: false
require_idle: true

[box_rfid_bambu]
serial: serial485
address: 1

[box_rfid_mifare]
serial: serial485
address: 1
```

`allow_active_rf: false` is intentional. Normal API7 fallback uses the original CFS stock RFID task rather than the older direct diagnostic RF commands.

## Diagnostics

Useful passive/manual commands:

```text
BOX_RFID_DIAG_INFO
BOX_RFID_DIAG_RUNTIME
BOX_RFID_DIAG_STATE
BOX_RFID_DIAG_INTERNAL SLOT=<0..3>
BOX_RFID_BAMBU_READ SLOT=<global-slot>
BOX_RFID_DIAG_STOCK_CAPTURE SLOT=<0..3> KEY0=<12hex> KEY1=<12hex> KEY2=<12hex> CONFIRM=1
```

The Bambu read now writes a structured diagnostic line with the values actually decoded from the tag: UID, ATQA/SAK, detailed filament type, normalized material, expected profile name, RGB/RGBA and the captured block 4 / block 5 bytes. This is intended to make library-profile corrections possible directly from `klippy.log`.

The generic `BOX_RFID_DIAG_STOCK_CAPTURE` command uses the same API7 stock-task path with explicit Key-A values. It is read-only and is useful for researching other MIFARE Classic spool formats.

I also added `box_rfid_mifare.py`, a generic decoder registry for third-party MIFARE Classic spool formats. QIDI is the first provider:

```text
BOX_RFID_MIFARE_READ SLOT=<global-slot>
```

I hardware-validated it with a QIDI PET-CF tag (UID `37101573`, block 4 `250201...`). The decoder resolves it as `QIDI:PET-CF`, colour Black `#060606`, and matches my existing `90003 / Qidi PET-CF` library profile. After the first successful identification the UID-to-decoder hint is persisted, so normal `_BOX_RFID_READ_SLOT` rereads go directly to QIDI instead of trying the Bambu KDF first.

Expected API7 identity:

```text
API=7
readers=2
slots/reader=2
caps=0xE8
max_index=3
cache_size=16
```

On my validated K2 Pro the runtime backend is legacy with blocks `4,5,6`.

## Hardware-validated Bambu spool matrix

I extracted these results from my K2 Pro `klippy.log` history. Every row below is a genuine Bambu Lab tag that completed the API7 stock-task capture/fallback path on hardware.

| Material detail | UID | Normalized material | Tag colour |
| --- | --- | --- | --- |
| PLA Matte | `233A111D` | PLA | `#FFFFFF` |
| PETG HF | `666F9EC6` | PETG | `#BC0900` |
| PETG Basic | `AE2CE2A0` | PETG | `#FCE300` |
| ABS | `54CFACD5` | ABS | `#87909A` |
| ASA | `5A8AD5A6` | ASA | `#00A6A0` |
| ASA-CF | `8B81F7FC` | ASA-CF | `#000000` |
| PAHT-CF | `8AD2B5B4` | PAHT-CF | `#000000` |
| ABS-GF | `8A8CADFD` | ABS-GF | `#000000` |
| PETG-CF | `E7DC557A` | PETG-CF | `#000000` |
| PC | `8BD9CFFC` | PC | `#000000` |

These are hardware-validation results, not inferred library entries. The more detailed raw block captures are recorded in `k2-cfs-rfid-tools/docs/v3.3-stock-capture.md`.

## Experimental stock remaining-state diagnostics

For the CFS 1.5.3 remaining-filament reverse engineering I added an optional
read-only API7 capability used by the v3.4 diagnostic candidate:

```text
BOX_RFID_DIAG_REMAIN_STATE SLOT=<0..3>
```

The command exposes only the fixed stock fields needed to understand
`CMD_RFID_REMAINING (0x03)`: stock state/current remaining byte, runtime
type/flags, used and nominal-total counters, status, and the four ASCII bytes
used as the nominal length field. It is deliberately **not** an arbitrary RAM
reader and it performs no RFID/tag writes.

The validated v3.3 firmware does not advertise this capability, so the host
command fails closed on v3.3. The v3.4 diagnostic firmware remains a separate
experimental candidate until hardware validation is completed.

## Remaining-filament tracking for non-Creality RFID

The stock CFS `CMD_RFID_REMAINING (0x03)` can return `0xFF` for Bambu and other third-party tags even after a successful API7 read. I therefore added a host-side estimator that is independent of tag writes.

The estimator uses a stable RFID spool identity (UID where available) and a nominal spool filament length. During printing it prefers the **CFS path encoder** exposed by the normal box state because that measures physical filament draw from the spool/buffer path. `print_stats.filament_used` remains a fallback when the CFS encoder is unavailable.

A library profile can optionally define:

```json
"nominal_length_m": 250.0
```

I do not guess this value from spool mass because filament density and diameter tolerances vary by material. When the nominal length is not present in the profile it can be set explicitly for the current spool:

```text
_BOX_RFID_SPOOL_NEW SLOT=<global-slot> TOTAL_M=<metres> REMAINING=<0..100>
```

`REMAINING` defaults to 100. The estimate is persisted by RFID spool identity and exposed as:

```text
rfid_percent
rfid_estimated_percent
rfid_total_m
rfid_remaining_m
rfid_usage_source
```

`rfid_usage_source` is `cfs_encoder` when the physical CFS encoder is available during tracking and `print_stats` otherwise. Reverse motion and encoder/counter resets are ignored rather than increasing or falsely consuming the estimate.

This estimator is a fallback for third-party tags. I am separately reverse-engineering the CFS RAM structures used by the stock remaining algorithm so that a future firmware revision may be able to initialize the stock odometer without writing the RFID tag.

## Experimental native CFS type-4 remaining mode

Static reverse engineering of CFS firmware 153 identified a second, RAM-only
remaining path (runtime_type = 4). Unlike the type-7 path, the type-4 branch
updates the native remaining byte from the CFS physical path encoder and exits
before the RFID write-back routine.

The experimental v3.5 firmware candidate therefore adds only two constrained
API7 operations:

    BOX_RFID_DIAG_REMAIN_INIT4 SLOT=<0..3> TOTAL_M=<10..2000> REMAINING=<1..100> CONFIRM=1
    BOX_RFID_DIAG_REMAIN_CLEAR4 SLOT=<0..3> CONFIRM=1

INIT4 is fail-closed: the CFS stock worker must be idle, the slot must not
have an active stock Creality database record, the cached tag must be a
conservative MIFARE Classic candidate, and the UID supplied internally by the
host must still match the current cached UID. The firmware exposes no arbitrary
RAM-write primitive and does not call a physical RFID write function.

The type-4 model reverse engineered from firmware 153 is:

    consumed_percent = floor(used_mm * 100 / total_mm)
    remaining = max(initial_percent - consumed_percent, 0)

The normal CFS load/start transition captures the physical encoder baseline;
the unload/stop transition closes the interval. Hardware validation of v3.5 is
still pending. Until it is validated, the encoder-first host estimator remains
the production-safe fallback.

## Associating a tag with an existing library profile

I also extended the existing RFID mapping mechanism so a decoded tag can be bound directly to an existing filament-library profile:

```text
_BOX_RFID_ASSOCIATE SLOT=<global-slot> FILAMENT_ID=<library-id>
```

The association stores the RFID identity separately from the filament profile. Future reads reuse the selected profile's temperature, pressure advance and max-flow settings while keeping the colour reported by the physical tag.

For Bambu API7 tags the stable identity is based on the decoded material detail, for example:

```text
BAMBU:PETG BASIC
BAMBU:PETG HF
```

The same command also works with the existing unknown Creality/custom RFID flow and is designed so future third-party decoders can expose their own stable identity codes.

## Safety

Automatic Bambu fallback is restricted to API7. The older API3 direct-auth diagnostic path is not used automatically. No tag-write primitive is exposed by the API7 firmware or by these extras.