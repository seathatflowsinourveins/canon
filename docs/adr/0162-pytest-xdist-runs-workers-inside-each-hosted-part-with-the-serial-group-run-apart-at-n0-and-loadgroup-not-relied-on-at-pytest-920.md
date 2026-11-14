---
status: accepted
date: 2026-11-14
decision-makers: the lead (the hosted check's architecture, stage S2: workers inside each part, 2026-11-14 14:09, as the deputy relays it, on the owner's order to fix the time limit by design); Claude (the off-gate lane) for the record, under the owner's delegation (ADR 0074)
review-by: 2027-02-12
---

# pytest-xdist runs workers inside each hosted part, with the serial group run apart at `-n 0`, and `loadgroup` not relied on at pytest 9.2.0

## Context and Problem Statement

The hosted check ran one serial pytest of 12,562 cases, 1,862 s of a run of about 36 minutes, on a 4-core
runner, under a limit raised from 40 to 90 minutes as a stopgap. Stage S1 splits it into parts by a hash of the test function; stage S2 runs
workers inside each part, so each part ends in under 5 minutes hosted and a full run on this Mac in under
8 minutes. Some cases take a lock, a port, a fixed path, the home folder or the wall clock, and must never
share a core with a parallel worker.

## Decision Drivers

* The documented, maintained pytest plugin for workers in one process tree, pinned exactly.
* Every case runs exactly once, in parallel or in the serial group.
* No grouping is relied on until a probe against our pytest pin shows it works.

## Considered Options

1. **pytest-xdist, with the serial group run apart under `-n 0`.**
2. **pytest-xdist, with the serial group kept on one worker by `--dist loadgroup` and `xdist_group`.**
3. **More hosted parts and no workers.**

## Decision Outcome

Chosen: **option 1**. Option 2 does not work at our pin (below), and even when it works a group on one
worker still runs beside the other workers. Option 3 needs a job per core-equivalent and leaves a full local
run serial.

1. **The library.** `pytest-xdist==3.8.0` (2025-07-01, the newest release), MIT, in `[dependency-groups]
   dev` (`uv add --dev pytest-xdist==3.8.0`); it brings `execnet` 2.1.2. Registry entry `pytest-xdist`,
   role `tool`, tag `v3.8.0`. Its plugin loads in every pytest run and distributes nothing unless `-n` (or `--tx` with `--dist`) is given;
this repository sets neither in its pytest options.
2. **`loadgroup` is broken at pytest 9.2.0, and is not used.** xdist 3.8.0 groups by rewriting
   `item._nodeid` to add `@<group>`; pytest 9.2.0 reads `nodeid` from a structured id (`Node.nodeid`
   returns `str(self._id)`, `_pytest/nodes.py:290-292`), so the write lands unread in the node's `__dict__`
   (`nodes.py:138-147`). pytest's changelog (#14758) calls the change "fully backward compatible for
   plugins"; for this private attribute it is not, as xdist #1386 reports and the probe shows. The probe below found 18 cases marked
   `xdist_group("serial")` spread over all four workers under `--dist loadgroup`, as under `load`, with no
   `@serial` in their ids. The fix (xdist #1388, merged 2026-10-07) is in no release; a release that carries
   it is adopted through `adopt-upstream` and this probe re-run before any grouping is relied on.
3. **The serial group.** A pytest marker names the cases the split lane's shared-state audit lists; each
   part runs `-m "not <marker>"` with workers, then `-m "<marker>"` with `-n 0`, in the main process with no
   worker beside it (the pattern pandas uses with `single_cpu`, its workflow read 2026-11-14).
4. **The distribution mode** (`load`, `loadfile`, `loadscope` or `worksteal`) and the worker count are
   chosen by measurement on the real suite when S2 is wired into `scripts/ci`, not by this record. All of
   them ran every probe case exactly once.
5. **Not used.** `--dist loadgroup`, `--looponfail`, remote `--tx` hosts, and the `psutil` and
   `setproctitle` extras.

### Consequences

* Good: workers come from the maintained plugin pytest's own organisation hosts; a full local run can use
  this Mac's cores.
* Good: the serial group needs only a marker and `-n 0`, which work at our pins.
* Bad: cases in a part's parallel run must not share state; one the audit misses can fail by timing.
* Bad: `-s` and `--pdb` do not work under workers (xdist's known limitations); a lane debugging runs `-n 0`.
* Bad: until an xdist release carries #1388, any `xdist_group` mark silently does nothing.

### Confirmation

The probe ran on this branch's environment (pytest 9.2.0, pytest-xdist 3.8.0, execnet 2.1.2): three files,
each with 6 grouped and 10 ungrouped cases, `-n 4`. `loadgroup` and `load` both spread the grouped cases over
four workers (4/6/5/3 and 5/6/4/3); `loadfile` kept each file on one worker; `worksteal` spread files;
each mode ran 48 of 48 cases once; `-n 0 -k grouped` ran 18 on `master`. S2's own landing shows the suite's
count unchanged under workers and the times against the targets; until then, `UNVERIFIED`.

## Evidence

Read 2026-11-14. "Fetched at the pin" is the tree `scripts/canon src pytest-xdist` fetched at `1e3e4dc1`.

* `scripts/canon resolve pytest-xdist` — `refs/tags/v3.8.0` to `1e3e4dc16523`; no newer tag — resolved.
* https://pypi.org/pypi/pytest-xdist/json — latest 3.8.0, uploaded 2025-07-01, `MIT`, requires
  `execnet>=2.1` and `pytest>=7.0.0` — fetched (the JSON API, parsed whole).
* https://raw.githubusercontent.com/pytest-dev/pytest-xdist/v3.8.0/LICENSE — MIT — fetched at the pin.
* https://raw.githubusercontent.com/pytest-dev/pytest-xdist/v3.8.0/src/xdist/remote.py — `loadgroup` rewrites
  `item._nodeid` with `@` and the group names — fetched at the pin.
* https://raw.githubusercontent.com/pytest-dev/pytest-xdist/v3.8.0/src/xdist/plugin.py — `--numprocesses`
  is a shortcut for `--dist=load`; unset, nothing is distributed — fetched at the pin.
* https://raw.githubusercontent.com/pytest-dev/pytest-xdist/v3.8.0/docs/known-limitations.rst — `-s` and
  `--pdb` under workers — fetched at the pin.
* https://raw.githubusercontent.com/pytest-dev/pytest/9.2.0/doc/en/changelog.rst — #14758, node ids held
  as a structured `NodeId` — fetched at the pin (`scripts/canon src pytest`).
* https://github.com/pytest-dev/pytest-xdist/issues/1386 and https://github.com/pytest-dev/pytest-xdist/pull/1388 —
  closed and merged 2026-10-07, "Fix loadgroup with pytest main node IDs"; merge commit `4616feb8` not in
  `v3.8.0`; `master` 112 commits past `v3.8.0` — fetched (`gh api`).
* pytest 9.2.0 `src/_pytest/nodes.py:138-147,214-218,290-292` — `__dict__` in `Node.__slots__`, `_id` set
  in the constructor, `nodeid` as `str(self._id)` — fetched at the pin (`scripts/canon src pytest`).
* The lead's ruling on the hosted check's architecture, 2026-11-14 (outside the repository) — 12,562 cases,
  one serial run 1,862 s, the stages and properties — fetched (a file outside the repository).
* The CI scouts' reports of 2026-11-14 (outside the repository) — fetched (files outside the repository).
* https://github.com/pandas-dev/pandas/blob/main/.github/workflows/unit-tests.yml — lines 178-200, `not
  single_cpu` at `numprocesses: 'auto'` and `single_cpu` at `0` — fetched by the evidence-verifier
  2026-11-14, on `main`, unpinned.
* The probe, 2026-11-14 — the counts in Confirmation — UNVERIFIED as a source (a local run, not a
  document); the evidence-verifier re-ran it in its own folder with the same result, `loadscope` included
  (one worker per file).
* Corrected 2026-11-14 after the evidence-verifier's pass: the Context's run time and limit, the reach of
  "distributes nothing", the code path behind the broken rewrite and the changelog's contrary claim.
