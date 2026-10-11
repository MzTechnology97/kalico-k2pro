# CFS passive host operation trace (opt-in, experimental)

The host trace implementation remains in the independent
`box_cfs_trace.py` extra, separate from `box_cfs_runtime.py`.
An **optional, minimal** notification hook in `box.py` emits
`box:cfs_operation_stage` on each existing
`_set_operation_progress()` transition and
`box:cfs_operation_end` when the outer operation context exits.
Subscribers are isolated with exception handling: a broken trace
callback must not terminate load or unload.

The extra also reads only **already-cached Python state** from the Kalico
`box` instance. It does **not** open `/dev/k2-rs485`, call CFS drivers, send
requests, acquire RFID locks, set MCU parameters, write NVM, touch
firmware, move motors, or modify Klipper's existing state.

**Not MCU motor telemetry.** It reports what the **host** knows about
a CFS load/unload stage, not encoder counts, step rates or driver
current. In particular, a stage may begin and finish between samples.

## Why cache-only

An earlier physical CFS 1 Hz GET 28–31 experiment yielded **three
samples and zero samples during actual load/unload motion**.
Polling those opaque MCU words during a motor operation is not a
validated method of obtaining motion telemetry. This helper instead
uses the state already collected by the existing `box` component,
adds **zero bus load**, and explicitly detects long reactor gaps.

The reactor can be blocked during synchronous BOX_LOAD/BOX_UNLOAD.
**The new `HOST_STAGE_EVENT` callbacks capture existing Python phase
transitions synchronously, even when timed polling is blocked.**
`HOST_OPERATION_END` records context exit, including failure, with
`operation_result=unknown` (it MUST NOT be labelled success).
These are **host procedure boundaries only**, not start/stop signals
from a motor or encoder. A CFS motor may continue moving after the host
has issued a command, and the host may perform operations without a
tracked stage.

Missing timed samples must **not** be interpreted as motor idle or a
constant internal state. A `MISSED_WINDOW` record includes the elapsed
gap, and the UI exposes `reactor_gaps` and `longest_gap_s`.
No interpolation or assertion of physical movement is performed.
The status now also shows `host_stage_events` and
`host_operation_end_events`.

## Enable explicitly

Add the following optional section to a local printer configuration
and restart **Kalico only when scheduled** (do not change the
production instance to test this PR):

```ini
[box_cfs_trace]
# Sampling uses CACHED Python fields only; it does NOT send MCU requests.
# sample_interval: 0.20

# Maximum in-memory change/gap records; oldest records are discarded.
# max_events: 256
```

Even when the extra is loaded, capture is **off by default**. Nothing
is sampled until the operator explicitly starts a capture.

Commands:

```text
BOX_CFS_TRACE_START
# Run a standard BOX_LOAD / BOX_UNLOAD if already intended by operator.
BOX_CFS_TRACE_STOP
BOX_CFS_TRACE_SHOW LIMIT=30
BOX_CFS_TRACE_CLEAR
```

**The trace commands do not initiate motor movement**; a separate,
normal BOX command would. Avoid starting an operation solely to obtain
these diagnostic records on a production machine.

The `box_cfs_trace` object status includes: active, sample interval,
samples, number of cached state changes, detected reactor gaps,
longest gap, in-memory retained event count, error and last eight
events. This is an opt-in Moonraker-visible status, not a disk archive.
The reactor callbacks suppress and record their own unexpected
errors rather than breaking an ongoing CFS operation.

## Exactly which host fields are copied

The helper reads `box.snapshot` and `box.operation_progress`
plus `box.operation_depth`. Copies only:

- `operation_active`, `operation_kind`, `operation_slot`, `operation_stage`;
- `box_data_ready`, `box_state_code`, `box_status_code`;
- `loaded_slot`, `tracking`, `filament_detected`,
  `filament_sensor_error`.

It does **not** call `box.get_status()` or `box.read_live_state()`:
the latter can transmit CFS state, buffer, encoder and topology
queries, especially when printing. It performs no `driver.get_v2()`
diagnostic reads either.

Because this is an optional, host-level timeline, do **not** use it
as a safety interlock or as evidence of motor inactivity. The
CFS MCU write guards and the firmware v3.23 event-ring feature
remain separate and unimplemented.

## Offline test

```sh
python3 -m pytest -q test/test_box_cfs_trace.py
```

The test uses fakes whose `lookup_object` rejects any serial /
driver / MCU lookup. It exercises manual start/stop, timer-based cache observation,
a deliberate two-second scheduling gap, bounded ring capacity,
disconnect cleanup, error suppression, and missing CFS-box failure.
Additional tests compile and execute **the real `Box` operation-method
AST in isolation** to verify that all three host stages are captured
while the reactor is blocked, that context exit is not confused with
success, and that a failing callback cannot interrupt an ongoing
operation. No motor/RS-485 command is generated by the tests. No printer or test hardware is required.

**Security/operational scope:** do not include raw RFID identifiers,
subscriber data, raw serial frames or credentials in trace records.
This extra uses none of those inputs. No persistent log or HTTP
endpoint is created by this PR.
