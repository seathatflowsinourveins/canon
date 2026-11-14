---
status: proposed
date: 2026-11-14
decision-makers: lead orchestrator (Claude) for ADR 0110's change (f); the live lane (Claude) for the reader
review-by: 2027-02-12
---

# The live request budget is read from a local settings file outside the repository, the paper figure when it is absent (ADR 0110, change (f)); open: no node reads it yet

## Context and Problem Statement

ADR 0110's change (f) asks for "the live request budget, read from an untracked local settings file", and
ADR 0103's point 6 keeps everything about the owner's account out of tracked files. Paper work plans on one
figure in `request_budget.py` (ADR 0083). Where does a live node get its figure, and what does it do
when the file is missing or wrong?

## Decision Drivers

* No figure for the live account is in a tracked file, a commit, a pull request, a log line or an error.
* Absent means the paper figure, said so: never a guess at the live one.
* A file that is there and wrong is an error, never read as absent.

## Considered Options

1. An environment variable set by each `live:*` task.
2. A file under the checkout, ignored by git.
3. A file under the config folder, outside every checkout.

## Decision Outcome

Chosen: **option 3**, `proposed` until the gap below closes. Option 1 puts the figure in a task's text or
in a shell's history. Option 2 keeps it one `git add -f` from a commit, and a new worktree would lack it.

1. **The file** is `canon/live/settings.toml` under the config folder (`CLAUDE_CONFIG_DIR`, else
   `~/.claude`), where `paper:session` keeps its window records. It is TOML, read with Python's `tomllib`.
   The owner or the lead writes it by hand; nothing in the repository writes it.
2. **One key:** `trading_requests_per_minute`, a positive whole number: the Trading API's request limit a
   minute for the live account, as Alpaca gives it for that account.
3. **`canon_trading.live_budget.read_request_budget()`** returns the figure and where it came from:
   * no file, or a file that does not set the key: the paper figure
     (`request_budget.TRADING_REQUEST_LIMIT_PER_MINUTE`), with a source that says which case it was;
   * a file that cannot be read, is not TOML, or sets anything but a positive whole number: an error by
     name, with no cause chained and nothing of the file repeated, since a wrong value may be a slip of
     the real one;
   * a path inside the checkout the module runs from: refused, there or not.
4. **The figure is kept out of** `repr`, `str` and every message of the module. A caller that records it
   does so outside the repository (ADR 0103, point 6).

**The gap, and what would close it.** No node reads the budget. Closed when the live node's configuration
(ADR 0110, change (b)) takes its request ceiling from it, with a test that an absent file gives the paper
figure there.

### Consequences

* Good: the figure cannot be committed from where it is kept, and a missing file is safe and visible.
* Bad: the file is on one machine and is not backed up by the repository; a second machine starts on the
  paper figure until someone writes it.
* Bad: the paper figure may be below the live account's real limit, so a node without the file plans
  tighter than it must.

### Confirmation

`tests/test_live_budget.py`, 18 cases with made-up figures in a temporary folder: each case above, the
config folder's path with and without `CLAUDE_CONFIG_DIR`, and that the figure is in no `repr` or message.
With the module absent the file failed at collection. No test reads this machine's config folder. No paper
run applies: the module reaches no account.

## Evidence

Read 2026-11-14.

* https://docs.python.org/3/library/tomllib.html — "Added in version 3.11"; `tomllib.load`: "The first
  argument should be a readable and binary file object"; "A `TOMLDecodeError` will be raised on an invalid
  TOML document". Fetched.
* `src/canon_trading/request_budget.py` on `main` — `TRADING_REQUEST_LIMIT_PER_MINUTE`, the paper figure,
  "a documented planning budget" (ADR 0083). Read.
* `mise.toml` on `main`, `paper:session` — the window records under
  `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/canon/watch/windows`. Read.
* ADR 0103, point 6, and ADR 0110, change (f). Read.
