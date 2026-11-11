---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Paper session windows on a schedule: a launchd agent every 35 minutes, each firing on the origin's `main`

## Context and Problem Statement

ADR 0044 runs `SessionMaCross` on paper in bounded windows, one node per window, and ADR 0047 says what a
window knows when it starts. Nothing starts a window by itself. How do windows run through the weekday
sessions?

## Decision Drivers

* Upstream's own features and patterns first (ADR 0001); a window starts from Alpaca's state, not ours.
* Paper only (ADR 0009); a window that dies or is refused is visible, not silent.

## Considered Options

1. Windows started by hand, `mise run paper:session`, as ADR 0044 left them.
2. A launchd user agent that fires a window every 35 minutes, beside ADR 0011's lane watchdog: the
   mechanism ADR 0011 already uses for an unattended job on this Mac. No other scheduler was weighed.

## Decision Outcome

Chosen: **option 2**, split from ADR 0047 on 2026-11-10 with no change of decision
(the second reader's corrections to the moved text are listed under Evidence). No upstream schedules
bounded windows; each part below follows the upstream named:

| Part | How | Follows |
|---|---|---|
| What fires | A second launchd user agent, `local.canon.paper-session`, installed, reported and removed by `scripts/automation` beside the lane watchdog; every 2100 s, not at load; a firing while the job still runs or the Mac sleeps is missed (launchd.plist(5)) | ADR 0011's launchd agent |
| What runs | `~/.claude/canon/paper-session.sh` brings its own clone, `~/.claude/canon/paper-checkout`, to the `main` the origin has at that firing, detached, or fails the firing, then runs `mise run paper:session --session auto --minutes 25` there: one window, one node, one process, disposed, its records naming the commit. 25 minutes, with about three of start-up and stop (a regular stop waits its 65 s post-stop delay; an estimate, UNVERIFIED until a window has run), leaves the 300 s gap inside 35 | configure_live_trading: "run nodes as standalone Python scripts or services"; "One LiveNode per process"; actions/checkout's reuse of a directory (the runner's header) |
| Which session | `auto` takes the regular session when Alpaca's clock says it is open (`is_open`, which is false on weekends and holidays; UNVERIFIED, the clock page says only "Whether or not the market is open"), the extended one when `EXTENDED_READY` and it is open, and otherwise prints "nothing to run" and exits 0 | Alpaca's paper-trading skill: "always check the clock/calendar endpoint" |
| A window that dies | It writes `latest.json` as `running` before the node is built and `ok` or `failed: <why>` after, the account check included; a refusal someone must look at (account not flat) is `refused: <why>`, while a skip that clears by itself (the gap, under 515 s of session left) exits 0 and keeps the last result. A `running` record past its `stale_after` (70 minutes) is a window that died | No upstream |
| Status | `scripts/automation status`'s `paper session` row: the last window, its result, age and leftovers; FAILED when the last firing exited non-zero (one that ended before Python wrote anything); STALE when the runner's own stamp, `scheduled-firing.json`, written as each firing starts, is older than the longest firing (its 85-minute deadline) plus an interval and 300 s, since launchd misses the intervals a firing runs through, and not ok when it is missing or unreadable, whatever a window run by hand wrote | ADR 0011's status rows |

Installing it is the owner's step: `scripts/automation install`. Whether the agent's `mise run` reads the
login keychain without a dialog is UNVERIFIED until its first firing. A firing while the Mac sleeps is missed.

### Consequences

* Good: once installed, a window starts every 35 minutes while the Mac is awake and a session is open, with
  no one starting it, each on the newest `main`; extended sessions only once `EXTENDED_READY` is on.
* Bad: one paper account still serves the windows and the paper tests; a test can lose the lock to a window
  (ADR 0044). A window refused for an account that is not flat stays refused until someone clears it. A
  node that hangs on stop (ADR 0044's disconnect hang) is waited for at most `STOP_TIMEOUT_SECS` (about
  115 s); the window is then recorded failed, the node's last worker calls waited for (at most about
  129 s), the account checked (at most `AFTER_STOP_SECS`, 60 s, and three requests), and the process ended
  (ADR 0044).
* Bad: the clone's kept `.venv` is trusted local state, as a person's checkout's is: `--exact` removes
  extraneous packages but not other files (a loose `.pth` still ran, observed), and uv documents no check
  of files outside its packages. Its `.git` is not: hooks are off and a foreign local config is remade.
* Bad: a run that hangs holding the paper lock is stopped by mise's task `timeout` (paper:session 85
  minutes, paper:test 2 hours, each counting the wait for the lock); a window stopped there flattens nothing
  and shows as died, and the next starts only once the account is flat.

### Confirmation

Offline: `--session auto` picks the session from a stand-in clock and calendar and runs
nothing on a Saturday; a refusal, a failure after start and a failed account check become the latest
record, after `running`; a skip exits 0 and keeps the last result; every run writes its heartbeat
(`tests/test_session_window.py`). After a second reader's review (2026-11-10), each first shown by a
failing test there: a window that never ran (its node not built) no longer writes the gap marker, so a
retry is not skipped as if one had ended; a refused window now updates the heartbeat. `scripts/test-lanes`
rehearses the install with `AUTOMATION_SKIP_LAUNCHD=1`
and a stub `launchctl`: the agent's arguments, interval, working folder, `RunAtLoad` and the interval
arithmetic; the row for ok, running, died, a record's own `stale_after`, failed, refused, no record, a
non-zero exit under an ok record, and three silent hours; the uninstall.
Loaded on this machine on 2026-11-10; launchd had not fired it when this was written.
The same test fires the runner at a stand-in origin: each firing runs its newest `main`, detached, with
`CANON_COMMIT` and a kept `.venv`; a failed fetch runs nothing and fails the row; uninstall removes runner,
clone and its mise trust. On GitHub: a fresh clone 0.5 s, its environment 6.6 s, a kept one 0.4 s. One
firing from GitHub's `main`, run with no environment but `HOME`, read the pair, found no session open and
recorded its commit; it first failed until the runner exported `PATH`, which mise's `exec()` needs (observed).
After a second reader's review of the runner (2026-11-10), each first shown by a failing test in
`scripts/test-lanes`: a hung owner of the paper lock is now stopped at the task's `timeout` and the lock
held until all it started has ended (with `lockf <file> uv`, the lock was freed while the uv kept running);
a schedule gone silent is STALE or UNKNOWN even after a window run by hand, with no heartbeat or an
unreadable one; a post-checkout hook, a local branch and a `core.fsmonitor` left in the clone no longer act;
an origin URL with user information is refused at install and by the runner, and messages name host and path.
`/code-review` (high) of the fix found six more (a same-second heartbeat, a long firing, a branch named
`origin/main`, scp-style credentials, git's init keys, `@` in a path), each fixed with a case there.

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole.

* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md: "run nodes as standalone
  Python scripts or services"; "One LiveNode per process" (fetched, raw).
* `scripts/automation` and ADR 0011: the launchd agent, its install, status and uninstall (read).
* `man launchd.plist` on this machine: `StartInterval` "causes the job to be started every N seconds",
  a firing while the Mac sleeps "will be missed", and "If the job is running during an interval firing,
  that interval firing will likewise be missed"; `RunAtLoad` defaults to false; `WorkingDirectory` (read).
* ADR 0044 and ADR 0047: the windows and what each knows at start-up (read).
* Second reader (`evidence-verifier`) and `/code-review` (high), 2026-11-10: the 35-minute interval left no
  room for the gap, a failed account check was recorded `ok`, status missed a failing or silent schedule,
  routine skips looked like refusals, the agent pointed at a worktree; all fixed above.
* The offline tests above (run).
* Correction, 2026-11-10: the agent ran in the main checkout, which nothing kept on `main`; a clone fetched
  at each firing replaces it, same intent. actions/checkout v7.0.1 at `3d3c42e5aac5`, `src/git-*.ts`; mise
  v2026.11.5 `docs/cli/trust.md` (`mise run` trusts its config; observed, `--untrust` undoes it); uv 0.13.7
  `docs/concepts/projects/sync.md` (`uv run` is inexact without `--exact`) (all fetched, raw); `man git-clean`
  (`-x` keeps `-e`) and `man launchd.plist` (`AbandonProcessGroup`) (read).
* A second reader's review of `main` (2026-11-10): the gap marker after a window that never ran and the
  heartbeat after a refusal, both reproduced offline and fixed (run).
* https://raw.githubusercontent.com/alpacahq/alpaca-skills/0e7c35163ae462730de1f4a0bfafceac82b7d59b/skills/trading-api/paper-trading/SKILL.md:
  "always check the clock/calendar endpoint"; the same page says "NEVER auto-submit orders in a loop
  without user awareness", which an unattended schedule sits against: the owner installs it, and every
  window is bounded, capped at one share and reported (ADR 0044) (fetched by the second reader, raw).
* https://docs.alpaca.markets/us/reference/legacyclock.md: `is_open`, "Whether or not the market is open"
  (fetched by the second reader, raw).
* `home/.claude/canon/paper-session.sh`, `src/canon_trading/session_window.py` and `scripts/test-lanes`:
  the runner, the records, the timeouts and the rehearsal (read).
* A second reader's review of the runner (2026-11-10): the lock owner's missing deadline, the status row
  kept green by a run by hand, state in the kept clone, and an origin URL carrying credentials; each
  reproduced offline and fixed or recorded above (run).
* https://raw.githubusercontent.com/jdx/mise/v2026.11.5/docs/tasks/task-configuration.md, "timeout": SIGTERM
  to the task's processes, then SIGKILL 5 s later; observed with mise 2026.11.5 to reach a child that ignores
  SIGTERM only when the task's own process is still waiting, hence `exec uv` (fetched, raw; run).
* `man lockf` on this machine: locking a descriptor the shell opened ("Protect a section of a shell script
  with a lock"); the lock then lasts while any process holding the descriptor runs (read; observed).
* `man git-config`, core.hooksPath: "disable all hooks entirely by setting core.hooksPath to /dev/null ...
  on a per-command basis" with `git -c` (read).
* https://docs.astral.sh/uv/reference/cli/ (`uv run --exact`, "removing extraneous packages"; `uv sync
  --check`, whether the environment "is synchronized with the project") and uv 0.13.7
  `docs/concepts/projects/sync.md`; a loose `.pth` in a venv ran under `uv run --locked --exact` (fetched,
  raw; observed with uv 0.13.7).
* https://www.rfc-editor.org/rfc/rfc3986.txt, section 3.2.1: user information precedes "@" before the host,
  and "user:password" there is deprecated (fetched, raw).
* Second reader (`evidence-verifier`), 2026-11-10, on this split: added the skill and clock lines, marked
  the weekend reading `UNVERIFIED`, narrowed the Good consequence and corrected the install line.
