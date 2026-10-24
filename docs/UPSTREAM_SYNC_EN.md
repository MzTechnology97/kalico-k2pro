# K2 Pro OpenHost: following the upstreams

`k2-pro-openhost` is a fork of
[Jacob10383/kalico](https://github.com/Jacob10383/kalico), which is itself a
fork of [KalicoCrew/kalico](https://github.com/KalicoCrew/kalico). Both keep
moving: Jacob adds K2 work and merges Kalico from time to time, Kalico fixes
and adds features. This page describes how their new commits are found,
checked and brought here.

Nothing in this process merges on its own. It reports, and people decide.

## The weekly report

Every Monday at 06:00 UTC (and on demand from the Actions tab) the
[Upstream watch](https://github.com/MzTechnology97/kalico-k2pro/actions/workflows/upstream-watch.yaml)
workflow:

1. fetches `main` of both upstreams;
2. runs
   [`scripts/upstream_sync.py`](https://github.com/MzTechnology97/kalico-k2pro/blob/k2-pro-openhost/scripts/upstream_sync.py),
   which lists the upstream commits that `k2-pro-openhost` does not have;
3. tries each merge in memory (`git merge-tree`) and lists the files in
   conflict;
4. writes the report into the open issue with the `upstream-sync` label
   (it creates the issue the first time);
5. for an upstream that merges without conflicts, makes the merge in the
   runner and runs the Klippy tests with Python 3.11, the version on the
   reference CM5, then comments the result on the issue.

The report leaves out:

- commits whose change is already here under another hash (cherry-picks,
  ports);
- Kalico commits already in Jacob's `main`: they arrive with Jacob;
- commits with a recorded decision (see below).

## Categories

Each pending commit gets one category, from its files:

| | Category | Meaning |
|---|---|---|
| 🔴 | motion / timing / MCU | Touches step generation, kinematics, homing, the MCU link (`klippy/chelper/`, `toolhead.py`, `stepper.py`, `mcu.py`, `serialhdl.py`, `clocksync.py`, `motion_queuing.py`…) or the shared MCU sources in `src/`. |
| 🟡 | touches files we changed | A file that `k2-pro-openhost` changed since the last common point with that upstream. It needs a port by hand. |
| 🟢 | files we never changed | Only files that are still as upstream has them. |
| ⚪ | other | Only docs, CI, example configs or MCU boards that no K2-OpenHost machine runs (stm32, rp2040, avr…). |

Flags in the last column:

- **K2**: K2 modules (`box*`, `motor_control`, `prtouch`, `serial_485`,
  `z_align`…) or `config/k2/`;
- **MCU**: shared MCU sources;
- **ours**: a red commit on files we changed.

Red matters because the K2 MCUs run Creality's firmware, not this tree's
`src/`. A host change that expects new MCU behaviour can pass CI and still
fail on the printer. The CI image is pinned for the same reason (see
`ci-build_test.yaml`).

## From the report to `k2-pro-openhost`

Work in batches, on a branch and through a pull request, never on
`k2-pro-openhost` directly.

| Category | What is needed before merging |
|---|---|
| ⚪ | Nothing: usually skipped or taken with the next batch. |
| 🟢 | CI green. |
| 🟡 | A catch-up pull request with tests for the behaviour that was ported (like #39, `box: catch up with Jacob10383 k2-plus`). |
| 🔴 | CI green, then on the reference printer, while idle: restart Klipper, read `klippy.log`, run a short print. Only then merge. |

Prefer Jacob's `main` for Kalico changes he already merged, since he
reconciled them with the K2 code. Take a Kalico commit directly when Jacob
has not merged it yet and it fixes something we need.

## Recording decisions

Every pending commit ends with a decision, so the next report only shows new
work. Record it with the script, then commit the change to
`.github/upstream-sync.json` with the pull request that did the work:

```
python3 scripts/upstream_sync.py decide <commit>... --decision taken --pr 95 \
    --note "core fix, merged with the October batch"
python3 scripts/upstream_sync.py decide <commit> --decision ported --pr 96 \
    --note "box.py: ported by hand"
python3 scripts/upstream_sync.py decide <commit> --decision skipped \
    --note "stm32 SPI: board not used on K2-OpenHost"
```

`taken` means merged as is, `ported` means rewritten for our code, `skipped`
means not wanted, with the reason. Commits that arrive through a merge of
the upstream disappear from the report on their own and need no decision.

## Running it locally

```
python3 scripts/upstream_sync.py report --fetch --markdown report.md
```

`--fetch` adds the remotes `jacob` and `kalico` if missing and fetches
them. `--ref jacob=upstream/main` uses another ref, `--no-merge` skips the
trial merge, `--json` writes the raw data. The script needs only Python 3 and
git 2.38 or newer.
