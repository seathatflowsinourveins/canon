---
status: proposed
date: 2026-11-14
decision-makers: master orchestrator (Claude); the lead rules on C and on the node-report states; repo owner ratifies
review-by: 2027-02-12
---

# The exit monitor runs as a per-account 60 s agent, GET only, on A and B by their install and on C only on the lead's standing word; amends ADR 0114 in part (proposed: open until staging (ii) and (iii) have run)

## Context and Problem Statement

`src/canon_trading/exit_monitor.py` judges whether each holding of one paper account has a working exit,
and `scripts/exit-monitor-notify` delivers its alert outbox as a macOS notification. It ran only by hand and
refused C. A read of every protection fault on 2026-11-14 found that none reached the owner. The lead ruled
that nodes report per holding, the monitor judges and notifies, and it runs first on C, where cover is a
stop Alpaca holds. ADR 0114's closing sentence gives C's pair to no other task; this record amends it.

## Decision Drivers

* One producer of the owner's alerts, one 15-minute repeat; a stale report is itself the notice.
* No credential in any file, argument or output; C's pair reaches the task only on the lead's word; nothing
  written can place, change or cancel an order; off by default (plain `install` never starts it).

## Considered Options

1. **A per-account launchd agent running `paper:exit-monitor` every 60 s from a clone of its own**, C behind
   a standing word file read at every firing.
2. The runner copies each node's `ALERT` line into the outbox: withdrawn (a second writer, mixed cadences).
3. By hand after each step, as today: nothing unattended reaches the owner.

## Decision Outcome

Chosen: **option 1**. **The pass**, GET only: positions, open orders, the trade date's ended orders, the clock, the calendar and
SIP bars; at most 8 trading GETs, bounded at 45 s, under a monitor-only lock, never an account or stream
lock. It writes only `canon/exit-monitor/<A|B|C>/`: the pass's `status.json`, `memory.json`,
`memory.running`, `records/` and outbox `alerts.jsonl`; the carrier's `delivered.json`,
`notify-status.json` and `.notify.lock`; the agent's `runner.lock` and `clone-refreshed`; and on C, `go`.

**The node-report contract** (`reports.json`, the module's docstring). Cover is an acknowledged stop-kind
sell of ours at the venue, or a report fresh within three node steps whose state covers:

| State | The monitor |
|---|---|
| `armed`, `counting` | cover |
| `cold` | cover, named: the stop is on the node's fixed fallback distance before a full ATR window of sound bars; counted in `status.json`, no notice |
| `guarded`, `waiting` | no cover |
| `held`, `source` `word` or `order` | reads `held`, never uncovered; another source adds no cover, named |
| `frozen`, `result` `denied` or `rejected` | reads frozen |

A state it does not know, a missing one included, costs that holding the report's cover, named, and never
the pass. An optional `client_order_id` names one stop of our lineage: not added where the venue shows the
same stop, counted once if repeated; a report without one adds nothing where another exit could be the
same. A missing or stale report reads "no protector running". The nodes report; the monitor judges.
**Not built:** no node writes `reports.json` yet; the warming bound (the node reports `full_window_at`, and
a holding still `cold` after it raises one notice per holding and node run; one with too few bars, overnight
say, stays cold and quiet); one notice when a hold by the owner's order begins with shares uncovered.

**The schedule.** `scripts/automation install-exit-monitor <tests|windows|protect>`, from a tree on `main`,
writes `local.canon.exit-monitor-<account>`: `StartInterval` 60, not run at load. A firing runs `mise run
paper:exit-monitor` once in `exit-monitor-<account>-checkout`, a clone of origin's main of its own (ADR
0050), refreshed by the recorder's `paper-session.sh --recorder-checkout` at most every 30 minutes; HEAD
must be the clone's `origin/main` with no tracked change, or nothing runs (`not_main`). One firing per
account at a time; one `scripts/automation status` row per agent; `uninstall-exit-monitor` keeps records.

**C: ADR 0114 amended in part.** One more task gains C's pair: `paper:exit-monitor protect`, GET only, no
order and no lock, behind the lead's standing word read at every firing, from main at every firing. "No task
reads two pairs" stands: one account per run. The word is `canon/exit-monitor/C/go` reading exactly
`exit-monitor protect`; the agent, the task and `monitor_task` each refuse C without it, and only the lead
writes it, after the staging. The undo: removing the go file, which the owner or the lead may do, ends
the reading of C at the next firing. What it reads of C stays in that folder: no symbol, price or quantity of C in
a tracked file, a report, a pull request or a message, but for the owner's notice on this machine and what
goes to the lead. A and B stay off for now: without node reports, every holding protected locally there
would read "no protector running". The order of enabling: C first, then A once the protector reports, then
B once the Mover does.

**The repeat.** Told once 120 s old, then every 900 s, at once when frozen. The age runs from, first that
applies: a carried episode, the loss of cover, the removal of the owner's hold, then the fill if the trade
date's orders show one, else first seen.

**The bound**, correcting the request's "every 60 s": `StartInterval` misses a firing while the job runs
(launchd.plist(5)), so the period is 60 s only while a firing ends inside 60 s; a new uncovered long reaches
a banner in at most two periods plus 120 s plus one firing (measured: `UNVERIFIED` until staging (ii)).

**Not seen:** a stop a node holds locally until it reports; orders submitted on an earlier trade date; a
node's `ALERT` lines; anything while the Mac sleeps. **Limit:** a stop Alpaca holds counts toward cover in
every part of the day, but Alpaca's trailing stop "will not trigger outside of the regular market hours"
(ADR 0129, ADR 0142). The monitor counts it active only in the regular session, so outside it C's holding
reads `dormant`, told once on entering that state and then quiet: a stop exists, not one that can fire.

### Consequences

* Good: an independent, unattended check on C's venue stops from Monday.
* Bad: a protector run on C stays attended until its report is shown; 1,440 passes a day per account
  against a rate limit `UNVERIFIED` here (Evidence).
* To review: `status.json` counts uncovered holdings by a fixed reason kind (`uncovered_by_reason`), never a
  report's free text. Whether a reason the owner cannot act on, a name with no quote overnight say, keeps
  the 15-minute repeat is decided from the first nights' counts, not now. A node's no-quote reason is
  counted only as `node waiting`; a finer split is not built.

### Confirmation

`scripts/test-exit-monitor-runner` (macOS only; skips without `lockf` or `plutil`): install and its
refusals, the clone's refresh and `not_main`, one firing at a time, the row, uninstall. N9 in
`scripts/test-exit-monitor-notify` and `tests/test_exit_monitor.py`: C behind its word, `cold`, `held`, and
an invented state costing only its holding. Staging, named in the landing: (i) the offline tests; (ii) an
uncovered long on A, Sunday night after the proof's cells if A's lock is free, with A enabled by hand for it
alone: the owner or the lead runs `scripts/automation install-exit-monitor tests`; a `paper:test` case opens
the long, waits for two firings more than 120 s apart and one notice, and closes the long in its `finally`;
then `scripts/automation uninstall-exit-monitor tests` switches A off, and `scripts/automation status` shows
no exit-monitor row for it; (iii) a
long covered by a venue stop on A, Monday's regular session, by hand in sequence (open, `paper:protect
tests`, two `mise run paper:exit-monitor tests` passes, `--cancel`, `paper:close tests`), since `paper:protect
tests` takes `paper:test`'s lock.

## Evidence

Read 2026-11-14.

* https://github.com/alpacahq/alpaca-py/blob/c42add7c0a7026e2ead3ed64e76f4def2b99fda4/alpaca/trading/client.py
  — `get_orders` (:107-125), `get_all_positions` (:235-244), `get_clock` (:422-431) and `get_calendar`
  (:438-456) are each one `self.get` — `resolved` (v0.45.0, `scripts/canon src alpaca-py`).
* https://docs.alpaca.markets/llms.txt — the parts read verbatim list no Trading API rate-limit page; about
  100,000 characters came back summarised — `fetched`, truncated. A support page the deputy's verifier
  cites gives 200 requests a minute per account; not read here, so `UNVERIFIED`.
* `man 5 launchd.plist` on this Mac — `StartInterval` misses a firing while the job runs or the system sleeps;
  `RunAtLoad` defaults to false — `bundled`.
* `src/canon_trading/exit_monitor.py`, `scripts/automation`, `home/.claude/canon/paper-session.sh` — the
  contract, the protector runner's go pattern, the recorder's clone mode — read in this repository.
* ADR 0114, read whole (its closing sentence, amended here); ADR 0142 (the two tasks it admits on C); ADR
  0103 (no credential read, nothing of the owner's account in this repository); ADR 0050 (a clone of its own).
* 2026-11-14, corrected after the deputy's evidence-verifier pass: C's reach amends ADR 0114, not ADRs 0103
  and 0142; "from main" holds at every firing; the identity rule, `held`, the age order, the rate and the
  folder list are stated; a home path is removed; the trailing-stop limit and the undo are added.
