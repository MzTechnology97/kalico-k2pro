# CFS RFID diagnostic adapter for K2-OpenHost

## Purpose

Implement an **optional, read-only** Kalico extra that exposes the experimental CFS RFID diagnostic API provided by the patched Creality K2 Pro CFS firmware.

The implementation must integrate with the existing K2-OpenHost RS-485 stack and must **not** open the RS-485 serial device independently.

The first hardware target is:

```text
CFS hardware:      cfs0_050_G32
stock application: cfs0_000_153
diagnostic rev:    v2.1
diagnostic API:    3
opcode:            0x57
```

The primary compatibility requirement is:

> Normal proprietary Creality RFID behavior must remain completely unchanged.

The diagnostic layer is strictly optional and must not alter the normal `Box` / CFS code path when it is not configured.

---

## Existing transport architecture

K2-OpenHost already owns the shared RS-485 bus through:

```text
klippy/extras/serial_485.py
```

The CFS and closed-loop motor traffic share the same physical channel:

```text
CM5
 |
 | /dev/serial/by-id/...-if02-port0
 v
Serial_485_Wrapper
 |
 v
T113 USB gadget bridge
 |
 | /dev/ttyS5
 v
CFS / closed-loop controllers
```

### Mandatory transport rule

The RFID diagnostic implementation must use the existing:

```python
Serial_485_Wrapper
```

request queue.

It must **not**:

- open `/dev/ttyUSB*` directly;
- open `/dev/k2-rs485` directly;
- create another pyserial reader;
- create another RS-485 listener;
- bypass `Serial_485_Wrapper`.

This is essential because a second reader on the gadget serial channel steals responses from Klipper and breaks the CFS/motor communication.

---

## New optional Kalico extra

Recommended file:

```text
klippy/extras/box_rfid_diag.py
```

Recommended config section:

```ini
[box_rfid_diag]
serial: serial485
address: 1
allow_active_rf: false
require_idle: true
```

If this section is absent, the module must not be instantiated and no behavior of the current `box` implementation may change.

Do not add mandatory includes to the existing K2 profile in the first PR.

---

## CFS diagnostic application protocol

The CFS application-level command is:

```text
opcode 0x57
```

The underlying RS-485 framing is the same normal CFS frame used by the existing box driver.

The implementation should use existing helpers from:

```text
klippy/extras/serial_485.py
klippy/extras/box_protocol.py
```

Do not duplicate CRC/framing logic.

### Subcommands

```text
0x00 INFO
0x01 CACHED_TAG_INFO
0x02 POLL
0x03 READ_BLOCK / READ_PAGE
0x04 READ_BLOCK_AUTH_A
0x05 STOCK_STATE
```

### Status values

```text
0 OK
1 BAD_REQUEST
2 NO_TAG / request failure
3 ANTICOLLISION_FAILED
4 SELECT_FAILED
5 AUTH_FAILED
6 READ_FAILED
7 STOCK_RFID_BUSY
```

Unknown status values must be treated as protocol errors.

---

## API v2.1 / protocol API 3 requirements

Active commands must only be allowed when `INFO` reports the exact validated API shape:

```text
api_version        3
physical_readers   2
slots_per_reader   2
capabilities       includes STOCK_STATE + STOCK_GUARD
max_read_index     255
cache_record_size  16
```

Known capability bits:

```text
bit 0  cache
bit 1  poll
bit 2  read
bit 3  Key-A auth + read
bit 4  CL2/cascade UID support
bit 5  STOCK_STATE
bit 6  stock busy guard
```

The exact active command should be refused if this shape does not match.

Passive INFO may still display an unsupported/older API version for diagnostics.

---

## Logical slot mapping

K2-OpenHost exposes four CFS logical slots:

```text
slot 0 -> reader 0, local slot 0
slot 1 -> reader 0, local slot 1
slot 2 -> reader 1, local slot 0
slot 3 -> reader 1, local slot 1
```

Recommended helper:

```python
reader = logical_slot // 2
slot = logical_slot % 2
```

Reject logical slots outside `0..3`.

---

## Request payloads

### INFO

```text
00
```

### CACHE

```text
01 reader slot
```

### POLL

```text
02 reader slot
```

### READ

```text
03 reader slot index
```

`index` is one byte:

```text
0..255
```

The exact interpretation is tag-family dependent.

### READ_BLOCK_AUTH_A

```text
04 reader slot block keyA[6]
```

Initial implementation must conservatively restrict:

```text
block 0..63
```

### STOCK_STATE

```text
05
```

---

## Response framing

Use the normal CFS reply decoder from `box_protocol.py`.

The status byte is the frame header/status field already handled by the current CFS protocol.

Do not create an alternate packet parser if the existing `decode_reply()` path can be reused.

---

## INFO response

Successful INFO payload length:

```text
6 bytes
```

Layout:

```text
byte 0  api_version
byte 1  physical_readers
byte 2  slots_per_reader
byte 3  capability_flags
byte 4  max_read_index
byte 5  cache_record_size
```

For the validated v2.1 firmware:

```text
03 02 02 7F FF 10
```

---

## STOCK_STATE response

Successful payload length:

```text
4 bytes
```

It exposes the stock CFS RFID manager state beginning at:

```text
0x200001F0
```

Current interpretation:

```text
byte 0  auth-sector cache / invalidation state
byte 1  stock state/gate byte, exact semantics not fully established
byte 2  active stock RFID logical slot
byte 3  additional stock state, expose raw
```

For byte 2:

```text
0..3  stock RFID manager active on that logical slot
>=4   stock manager idle
```

The implementation should expose:

```text
busy
active_slot
active_slot_raw
raw payload
```

Do not invent semantics for bytes 1 or 3.

---

## CACHED_TAG_INFO / POLL response

Successful payload length:

```text
16 bytes
```

Recovered stock discovery-cache layout:

```text
00..01  ATQA
02..05  CL1 response
06..09  CL2 response
0A..0C  stock/private working bytes
0D      current BCC
0E      final SAK
0F      stock/private working byte
```

Expose at minimum:

- raw 16-byte payload;
- ATQA;
- CL1;
- CL2;
- BCC;
- SAK;
- reconstructed UID.

### UID reconstruction

For a normal single-level UID:

```text
UID = CL1[0:4]
```

For a cascade UID where:

```text
CL1[0] == 0x88
```

reconstruct:

```text
UID = CL1[1:4] + CL2[0:4]
```

Do not interpret the stock/private bytes unless further reverse engineering proves their semantics.

---

## READ response

Successful payload:

```text
16 bytes
```

Return raw bytes only at this layer.

Do not decode Bambu, QIDI, Creality, or any other vendor spool format inside the firmware protocol adapter.

Vendor decoding belongs above the generic adapter.

---

## Bambu Lab first interoperability target

The first third-party hardware test will use real Bambu Lab RFID spools.

Public reverse engineering shows common Bambu spool tags as:

```text
MIFARE Classic 1K
UID   4 bytes
ATQA  00 04
SAK   08
```

The Kalico adapter must remain vendor-neutral.

For first hardware validation, success means:

1. passive cache can expose the tag if the stock discovery cache sees it;
2. active POLL returns stable UID/ATQA/SAK;
3. Key-A authenticated reads can return a known block.

Key derivation stays host-side and must not be embedded in the CFS firmware.

A separate repository already contains the public-reference Bambu key derivation helper:

```text
MzTechnology97/k2-cfs-rfid-tools
tools/bambu_keys.py
```

---

## QIDI later target

QIDI RFID will be tested only after Bambu is understood.

Do not add QIDI-specific firmware behavior in this PR.

The generic path must remain:

```text
presence
-> UID / ATQA / SAK
-> classify host-side
-> bounded read-only memory access
-> vendor decode host-side
```

---

## Required G-code commands

Recommended command names:

```text
BOX_RFID_DIAG_INFO
BOX_RFID_DIAG_STATE
BOX_RFID_DIAG_CACHE
BOX_RFID_DIAG_POLL
BOX_RFID_DIAG_READ
BOX_RFID_DIAG_READ_AUTH_A
```

### INFO

Example:

```text
BOX_RFID_DIAG_INFO
BOX_RFID_DIAG_INFO ADDRESS=1
```

Passive.

### STATE

```text
BOX_RFID_DIAG_STATE
```

Passive.

### CACHE

```text
BOX_RFID_DIAG_CACHE SLOT=0
```

Passive.

### POLL

```text
BOX_RFID_DIAG_POLL SLOT=0 CONFIRM=1
```

Active.

### READ

```text
BOX_RFID_DIAG_READ SLOT=0 INDEX=4 CONFIRM=1
```

Active.

### READ_AUTH_A

```text
BOX_RFID_DIAG_READ_AUTH_A SLOT=0 BLOCK=4 KEY=AABBCCDDEEFF CONFIRM=1
```

Active.

The command may accept `ADDRESS=1` override, with config default `address: 1`.

---

## Passive-first policy

The default configuration must be:

```ini
allow_active_rf: false
```

With that default, only these are allowed:

```text
INFO
STATE
CACHE
```

The active commands must be rejected before any RF request is queued.

---

## Active RF safety gates

All active commands must require all conditions below.

### 1. Configuration opt-in

```ini
allow_active_rf: true
```

### 2. Per-command opt-in

Every active G-code command must require:

```text
CONFIRM=1
```

### 3. Printer must be idle

If `require_idle: true`, allow only known idle print states.

Recommended accepted states:

```text
standby
complete
cancelled
```

Reject:

```text
printing
paused
unknown/unavailable state
```

### 4. Box observation mode must not be active

If the `box` object exists and reports:

```text
observation_mode = true
```

reject active RFID diagnostics.

### 5. One serialized RS-485 session

Use:

```python
Serial_485_Wrapper.request_session()
```

for the complete preflight + active request.

Within the **same session** perform:

```text
INFO
-> validate exact v2.1/API3 shape
-> STOCK_STATE
-> require idle
-> active POLL/READ/AUTH
```

This prevents unrelated host-side request producers from being interleaved between the host preflight and the diagnostic transaction.

### 6. Firmware-side guard still required

The CFS v2.1 firmware independently checks its own stock active-slot byte immediately before active RF operations and returns:

```text
status 7 = STOCK_RFID_BUSY
```

Therefore the Kalico-side check is a second safety layer, not the only guard.

---

## Important concurrency limitation

The firmware busy guard is not an atomic mutex shared with the unmodified stock CFS manager.

A theoretical race still exists:

```text
host sees stock idle
CFS stock task starts a transaction
diagnostic reaches firmware
```

The firmware re-check significantly narrows this condition, but first hardware tests must still be controlled and idle.

Do not claim production-grade concurrent RFID safety in this PR.

---

## Read-only boundary

This PR must expose no operation capable of modifying a tag.

Forbidden features:

- MIFARE write `0xA0`;
- arbitrary raw transceive;
- UID mutation;
- key changes;
- sector trailer writes;
- OTP/lock changes;
- tag formatting;
- EEPROM writes.

There must be no G-code command named WRITE.

---

## Interaction with existing Box code

The implementation must not modify the normal proprietary Creality RFID parser.

Avoid changes to `box.py` unless absolutely necessary.

Preferred architecture:

```text
box.py
    normal Creality behavior
    unchanged

box_rfid_diag.py
    optional generic diagnostic path
        |
        v
Serial_485_Wrapper
```

The new module may inspect the existing `box` object's `observation_mode` state for safety, but should not modify Box state.

---

## serial_485 function-name map

For better debugging, adding:

```python
0x57: "RFID_DIAG"
```

to the existing command/function-name map in `serial_485.py` is acceptable.

No other serial_485 behavior should be changed.

---

## Object status

Recommended `get_status()` fields:

```text
serial
address
allow_active_rf
require_idle
transport_ready
api_version
api_capabilities
last_result
last_error
```

Do not expose authentication keys in status or logs.

---

## Key handling

`READ_AUTH_A` accepts exactly 6 bytes.

Accepted textual formats may include:

```text
AABBCCDDEEFF
AA:BB:CC:DD:EE:FF
AA BB CC DD EE FF
```

Normalize internally.

Never log the key value.

Never persist it in printer state.

---

## Timeout behavior

Use the existing request/response timeout mechanism.

A timeout must be reported distinctly from an RFID protocol status.

Do not interpret timeout as `NO_TAG`.

---

## Error behavior

Protocol errors should be raised when:

- response opcode/address mismatches;
- payload length is invalid;
- unknown diagnostic status is returned;
- a non-success status unexpectedly carries payload where v2.1 specifies none;
- API shape is incompatible for an active operation.

A valid nonzero RFID status such as:

```text
NO_TAG
AUTH_FAILED
STOCK_RFID_BUSY
```

should be preserved as a diagnostic result rather than converted into an unrelated transport error.

---

## Tests required

Add a dedicated test module, recommended:

```text
test/test_box_rfid_diag.py
```

Minimum offline coverage:

### Framing

- INFO request body;
- STOCK_STATE request body;
- slot 0..3 mapping;
- READ index 0 and 255;
- reject READ 256;
- AUTH block 63 accepted;
- AUTH block 64 rejected;
- strict 6-byte key validation.

### INFO

Fixture:

```text
03 02 02 7F FF 10
```

Verify fields and capability flags.

### STOCK_STATE

Fixtures:

```text
40 00 04 00 -> idle
40 00 02 00 -> busy slot 2
```

### Tag cache

Include at least:

- 4-byte UID fixture;
- 7-byte cascade UID fixture;
- non-success status with empty payload;
- reject invalid payload length.

A useful public Bambu-style identity fixture is:

```text
UID  EAFE5CFC
ATQA 0004
SAK  08
```

This is only a protocol fixture, not proof of hardware compatibility.

### Memory read

- 16-byte success payload;
- auth failure;
- read failure.

### Shared transport

Verify that the adapter calls:

```text
cmd_send_data_with_response()
```

on the injected existing serial485 object.

It must not instantiate a serial port.

### Active-policy tests

Verify:

- `allow_active_rf=false` blocks before bus use;
- missing `CONFIRM=1` blocks before bus use;
- observation mode blocks before bus use;
- printing/paused/unknown print states block;
- wrong API version blocks after INFO and before STOCK_STATE;
- missing required v2.1 capability blocks;
- busy STOCK_STATE blocks before POLL/READ;
- idle sequence is exactly:
  ```text
  INFO -> STOCK_STATE -> active command
  ```
  inside one request session.

### Regression tests

Run the existing K2/OpenHost Box tests, at minimum:

```text
test_box_openhost_fixes.py
test_box_rfid_tracking.py
test_box_rfid_spool_estimates.py
test_box_k2rfid_catalog.py
test_box_filament_inventory.py
test_box_auto_mapping.py
```

No existing test should regress.

---

## Documentation required

Update:

```text
docs/K2_PRO_OPENHOST.md
```

Document:

- optional config section;
- passive vs active commands;
- shared serial ownership;
- exact active safety gates;
- v2.1/API3 requirement;
- no-write boundary;
- Bambu-first test intent;
- warning that hardware validation is still pending.

Do not enable the section by default in `printer.cfg` in this PR.

---

## First hardware validation order

Once the CFS v2.1 firmware is actually flashed, the Kalico adapter must be validated in this order:

```text
1. verify normal Creality RFID operation with no diagnostic commands
2. BOX_RFID_DIAG_INFO
3. verify Creality RFID still normal
4. BOX_RFID_DIAG_STATE
5. observe idle/busy transitions
6. BOX_RFID_DIAG_CACHE on a Creality tag
7. verify Creality RFID still normal
8. controlled busy-rejection test
9. no-tag POLL
10. Creality-tag POLL
11. Bambu passive CACHE
12. Bambu POLL
13. compare UID/ATQA/SAK
14. single authenticated Bambu block read
15. only later: read-only Bambu dump
16. QIDI after Bambu
```

Any regression in normal Creality RFID behavior blocks further testing.

---

## Acceptance criteria

This PR is ready to merge only if all are true:

- no direct serial port access is introduced;
- all diagnostic traffic uses `Serial_485_Wrapper`;
- module is optional;
- no normal Box behavior changes when it is not configured;
- passive commands work independently;
- active commands require explicit double opt-in;
- active preflight is serialized;
- exact API3/v2.1 shape is enforced;
- stock-busy state blocks active operations;
- no write primitive exists;
- new unit tests pass;
- existing Box/OpenHost tests pass;
- documentation clearly marks hardware validation as pending.

---

## Related repository

Firmware patch, protocol details, reverse-engineering evidence, Bambu test plan and hardware runbook:

```text
https://github.com/MzTechnology97/k2-cfs-rfid-tools
```

Important current candidate identity:

```text
cfs0_050_G32-cfs0_000_153-rfid-diag-ro-v2_1.bin
SHA-256:
3cf3385dcbc56960c9fe3adcaff516a7d66a0f8ad2b43b5d47d40c341824549c
```

The firmware binary is intentionally not committed to GitHub.
