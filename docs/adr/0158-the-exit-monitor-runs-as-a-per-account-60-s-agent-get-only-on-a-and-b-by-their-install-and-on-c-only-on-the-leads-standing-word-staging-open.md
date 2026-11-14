---
status: proposed
date: 2026-11-14
decision-makers: master orchestrator (Claude); the lead rules on C; repo owner ratifies
review-by: 2027-02-12
---

# The exit monitor runs as a per-account 60 s agent, GET only, on A and B by their install and on C only on the lead's standing word; staging on paper is still open

## Context and Problem Statement

`src/canon_trading/exit_monitor.py` judges, for one paper account, whether each holding has a working exit,
and writes an alert outbox that `scripts/exit-monitor-notify` delivers as a macOS notification. Until now
it ran only by hand, through `mise run paper:exit-monitor`, and refused C. A read of every protection fault
on 2026-11-14 found that nothing reached the owner for any of them. The lead ruled that the path is the
one the monitor's own contract already defines: a node reports its state per holding, and the monitor
judges and notifies. The lead also ruled that the monitor runs first on C, where cover is a stop Alpaca
holds, and C's pair reaching a new task is a reach that ADR 0103 and ADR 0142 record.

## Decision Drivers

* One producer of the owner's alerts, with one fifteen-minute repeat. A stale report is itself the notice
  that a protector is gone.
* No credential in any file, argument or output. C's pair reaches a task only on the lead's word, by name.
* Off by default. Plain `install`, which the maintenance agent runs, must never start it.
* Nothing the monitor writes can place, change or cancel an order.

## Considered Options

1. **A per-account launchd agent that runs `paper:exit-monitor` every 60 s from the main clone**, with C
   behind a standing word file read at every firing.
2. The runner carries each node's `ALERT` line into the outbox (the lead's first lean, withdrawn). It means
   a second writer, ALERT lines that differ in cadence, and order IDs to strip.
3. Run the monitor by hand after each step (today's state). The owner is told nothing unattended.

## Decision Outcome

Chosen: **option 1**.

**What the monitor is.** One pass per account, GET only: positions, open orders, the trade date's ended
orders, clock and calendar, and SIP bars for the tape. It takes a monitor-only lock that excludes a second
pass, and never an account or stream lock. A pass is bounded at `PASS_SECONDS` (45 s), the task at 3
minutes. It writes only its own folder, `canon/exit-monitor/<A|B|C>/`:
* `status.json`;
* `memory.json`;
* `records/`;
* the outbox `alerts.jsonl`.

The carrier then sends the new keys and records them in `delivered.json`.

**The contract nodes report by** (`exit_monitor.py`, its docstring). Cover is either an acknowledged
stop-kind sell of ours at the venue, or an entry in `reports.json`. That entry must be fresh within three
steps, with state `armed` or `counting`. A missing or stale report reads "no protector running".

The states a node reports per holding are `armed`, `guarded`, `counting`, `waiting` and `frozen`, and, by
the lead's addition of 2026-11-14, `cold`. A `cold` holding has its stop on the fixed fallback distance,
because no full ATR of sound bars exists for it yet. The nodes report; the monitor judges.

Proposed, for the lead: `cold` counts as cover, as `armed` does, since a stop is working. It is shown as a
count in `status.json`, the record and the status row, and raises no notice. A notice would repeat every
15 minutes for a stop that is working, and the owner would learn nothing new from it. Not built: no node
writes `reports.json` yet, and the monitor does not yet read `cold`.

**The schedule.**
* `scripts/automation install-exit-monitor <tests|windows|protect>` writes
  `local.canon.exit-monitor-<account>`: `StartInterval` 60, not run at load.
* Each firing runs the installed runner's `run-exit-monitor`. That runs `mise run paper:exit-monitor` once,
  in the checkout install ran from, which must be on `main`. The maintenance agent keeps that checkout on
  main, as for the nightly study.
* One firing per account at a time, under the folder's `runner.lock`.
* The runner records `watch/exit-monitor-<account>.json`, and `scripts/automation status` gives one row
  per installed agent.
* `uninstall-exit-monitor` removes the agent and keeps the records.

**The accounts.**
* A and B by their install.
* C only on the lead's standing word. The file `canon/exit-monitor/C/go` reads exactly
  `exit-monitor protect`. Only the lead writes it, when giving the word, and no script writes it.
* The runner reads the word at every firing and passes it as `--lead-go`. The task and `monitor_task`
  each check it again, and each refuses C without it.
* The lead's ruling enables C first and not A or B. Without node reports, every holding a node of ours
  protects locally reads "no protector running" on A or B.

**The repeat.** An uncovered holding is told once it is 120 s old (`ALERT_AGE_SECONDS`), dated from its
fill when the trade date's orders show one, else from when it was first seen. It is told again every 900 s
(`REPEAT_SECONDS`), and at once when frozen.

**The bound.** Corrects the request's "every 60 s": `StartInterval` misses a firing while the job runs
(launchd.plist(5)), so the period is 60 s only while a firing ends inside 60 s, and the next multiple
otherwise. The time from a new uncovered long to a banner is at most two periods, plus 120 s, plus one
firing. Its measured value is `UNVERIFIED` until staging (ii).

**What it does not see.**
* A stop a node holds locally, until that node writes `reports.json`. No writer exists today.
* Orders submitted on an earlier trade date.
* A node's ALERT lines.
* Anything while the Mac sleeps: firings are missed, not queued.
* Outside the regular session, C's regular-session stop reads `dormant`, and is told once on entering that
  state. That judgment is unit one's.

### Consequences

* Good: an unattended, independent check on C's venue stops from Monday. The owner is told by the same
  carrier and key as by hand.
* Bad: a protector run on C stays attended by its lane until its report is shown. The monitor alone reads
  that holding as uncovered every 15 minutes.
* Bad: about 1,440 passes a day per account, GET only, against the account's rate limit
  (`UNVERIFIED` against ADR 0093's row).

### Confirmation

* `scripts/test-exit-monitor-runner` covers install, refusals, one firing at a time, the status row and
  uninstall.
* N9 in `scripts/test-exit-monitor-notify` and the `tests/test_exit_monitor.py` cases cover C behind its
  word.
* Staging, in three parts, each named in the landing:
  - (i) the offline self-tests above, before landing;
  - (ii) an uncovered long on A, by hand, on Sunday night after the proof's cells, if A's lock is free and
    A holds nothing of the proof;
  - (iii) a long covered by a venue stop on A, Monday in the regular session, not notified.

## Evidence

Read 2026-11-14. One line per source.

* https://github.com/alpacahq/alpaca-py/blob/c42add7c0a7026e2ead3ed64e76f4def2b99fda4/alpaca/trading/client.py
  — `get_orders` (:107-125), `get_all_positions` (:235-244), `get_clock` (:422-431) and `get_calendar`
  (:438-456) are each one `self.get`, so a pass sends only GETs — `resolved` (v0.45.0, read through
  `scripts/canon src alpaca-py`).
* https://docs.alpaca.markets/llms.txt — the index lists no Trading API rate-limit page in the parts read
  verbatim; about 100,000 characters in the middle came back summarised — `fetched`, truncated. The rate
  cost above stays `UNVERIFIED`.
* `src/canon_trading/exit_monitor.py` at `b660d298`, docstring and constants — the node-report contract,
  `ALERT_AGE_SECONDS`, `REPEAT_SECONDS`, `PASS_SECONDS`, `INTERVAL_SECONDS` and the due rule — read in
  this repository.
* `man 5 launchd.plist` on this Mac (Darwin 27.2.0) — `StartInterval` misses a firing while the job runs or
  the system sleeps; `RunAtLoad` defaults to false — `bundled`.
* `scripts/automation` at `b660d298` — the protector runner's install and go pattern and the nightly
  study's run from the main clone — read in this repository.
* `/Users/noeron/.claude/canon/deputy-eebefdab/handoff/c-alert-path/TABLE.md` — what reached the owner per
  fault on 2026-11-14 — written this session, read only.
* ADR 0103, ADR 0142 — the records of the credential reaches this one extends — titles read; text
  `UNVERIFIED` this session.
