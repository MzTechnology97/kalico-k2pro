# CFS Runtime Config v3.23 CFG4 and OpenCFS detection

This Kalico module detects both families with read-only RS-485 command
0x7A after A1/A0 enumeration:

- K2OH: patched-stock CFS firmware, format 1, variant 4,
  revision v323-cfg4, version cfs0_000_153; INFO v2 features 0xD7.
- OCFS: separate OpenCFS firmware family, variants RELEASE (1),
  INERT (2), BENCH (3), format 1; detected independently even if
  its 0x57 CONFIG API is not available.
- Stock/unknown: status-only, no K2OH configuration writes.

**The CFG4 parameter write feature is compatible ONLY with matching
modified K2-OpenHost firmware distributed by
[k2-cfs-rfid-tools](https://github.com/MzTechnology97/k2-cfs-rfid-tools).
Creality stock and any other modified firmware, including OpenCFS, are
not compatible with K2OH CFG4 SET.**

The 0xD7 feature byte cannot differentiate legacy v3.22 from CFG4 v3.23.
Always check 0x7A magic/revision/variant and firmware application version.

On Klipper start / CFS rediscovery, Kalico reads
[box_cfs_runtime] and applies only IDs 1, 2, 3, 5, 7 and 8 for
v323-cfg4. The remaining IDs may be shown as status/diagnostic values
but are **not written**. The standard console has no
BOX_CFS_CONFIG_SET, RESET or APPLY commands.

The v3.23 parameters are volatile RAM: they revert on CFS MCU reset
and are reapplied at the next completed CFS enumeration. This is a
best-effort startup process, not a mechanism to certify motor standstill.

For a complete Italian description, stock defaults, bounds and units
for all 28 parameters, use the
[firmware project's parameter guide](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/docs/CFS_V323_PARAMETER_GUIDE_IT.md).

Example:

    [box_cfs_runtime]
    auto_apply: true
    hub_transition_speed: 220
    hub_insert_speed: 100
    hub_reverse_speed: 150
    #hub_transition_wait_ms: 3200
    #insert_sensor_timeout_ms: 7000

CFS hardware remains unqualified for new v3.23 CFG4 behaviour. Keep
the v3.22 custom-image rollback mechanism available.
