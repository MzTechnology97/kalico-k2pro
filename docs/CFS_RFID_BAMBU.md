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

The CFS continues to provide remaining-filament percentage through the normal Box path. Bambu block 14 is not used by API7.

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

[box_rfid_diag]
serial: serial485
address: 1
allow_active_rf: false
require_idle: true

[box_rfid_bambu]
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
```

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

## Safety

Automatic Bambu fallback is restricted to API7. The older API3 direct-auth diagnostic path is not used automatically. No tag-write primitive is exposed by the API7 firmware or by these extras.
