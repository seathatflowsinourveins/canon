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
| What runs | `~/.claude/canon/paper-session.sh` makes its own clone, `~/.claude/canon/paper-checkout`, anew at each firing from the `main` the origin has then, detached, keeping only `.venv`, or fails the firing, then runs `mise run paper:session --session auto --minutes 25` there: one window, one node, one process, disposed, its records naming the commit. 25 minutes, with about three of start-up and stop (a regular stop waits its 65 s post-stop delay; an estimate, UNVERIFIED until a window has run), leaves the 300 s gap inside 35 | configure_live_trading: "run nodes as standalone Python scripts or services"; "One LiveNode per process"; actions/checkout's path for a directory it does not reuse (the runner's header) |
| Which session | `auto` takes the regular session when Alpaca's clock says it is open (`is_open`, which is false on weekends and holidays; UNVERIFIED, the clock page says only "Whether or not the market is open"), the extended one when `EXTENDED_READY` and it is open, and otherwise prints "nothing to run" and exits 0 | Alpaca's paper-trading skill: "always check the clock/calendar endpoint" |
| A window that dies | It writes `latest.json` as `running` before the node is built and `ok` or `failed: <why>` after, the account check included; a refusal someone must look at (account not flat) is `refused: <why>`, while a skip that clears by itself (the gap, under 575 s of session left, or start-up reads that left too little of the window) exits 0 and keeps the last result. A `running` record past its `stale_after` (70 minutes) is a window that died | No upstream |
| Status | `scripts/automation status`'s `paper session` row: the last window, its result, age and leftovers; FAILED when the last firing exited non-zero or was stopped by a signal (`launchctl list`); DIED, too, for a `running` record older than a later run's heartbeat (that run held the lock); STALE when the runner's own stamp, `scheduled-firing.json`, written as each scheduled firing starts, is older than the longest firing (its fetch bound, the 105-minute task timeout and 300 s for mise's start) plus an interval and 300 s, since launchd misses the intervals a firing runs through; not ok when the stamp or heartbeat is missing or unreadable, or the installed runner is not the tree's. A check only makes the label worse. A run by hand writes to `by-hand/`, which the row does not read | ADR 0011's status rows |

Installing it is the owner's step: `scripts/automation install`. Whether the agent's `mise run` reads the
login keychain without a dialog is UNVERIFIED until its first firing. A firing while the Mac sleeps is missed.

### Consequences

* Good: once installed, a window starts every 35 minutes while the Mac is awake and a session is open, with
  no one starting it, each on the newest `main`; extended sessions only once `EXTENDED_READY` is on.
* Bad: one paper account still serves the windows and the paper tests; a test can lose the lock to a window
  (ADR 0044). A window refused for an account that is not flat stays refused until someone clears it. A
  node that hangs on stop (ADR 0044's disconnect hang) is waited for at most `STOP_TIMEOUT_SECS` (about
  115 s); the window is then recorded failed, the node's last worker calls waited for (at most about
  129 s), the account checked (polls start for `AFTER_STOP_SECS`, 60 s, and the last runs on), and the
  process ended (ADR 0044).
* Bad: the clone's kept `.venv` is trusted local state, as a person's checkout's is: `--exact` removes
  extraneous packages but not other files (a loose `.pth` still ran, observed), and uv documents no check
  of files outside its packages. Nothing else is kept: the clone, its `.git` and worktree are new each firing.
* Bad: a run that hangs holding the paper lock is killed at its task's deadline, its process group whole
  (`timeout -s KILL`), 60 s before mise's `timeout` (paper:session 105 minutes, paper:test 2 hours, each
  counting the wait for the lock); a window killed there flattens nothing and shows as died, and the next
  starts only once the account is flat. paper:session's deadline, 6240 s from the task's start, holds the
  lock wait (900), a start-up allowance (600: uv's sync after a uv.lock change measured 8 s from an empty
  cache, 464 MB), four Alpaca calls before the node at their longest with alpaca-py's retries (4 x
  `ADAPTER_CALL_SECS`, 517), a host clock behind Alpaca's (`SKEW_SECS`, 60), the longest window
  (`LONGEST_WINDOW_SECS`, 2955) and its bounded end (`AFTER_END_SECS`, 725): 5757, 483 s spare; a test
  reads those bounds from `session_window`. From a
  person's terminal uv runs as before, with mise's `timeout` as the deadline: through timeout(1) one Ctrl-C
  reached Python twice (observed), which could cut a flatten short. A window run as it should holds the lock under
  paper:test's 3900 s wait (ADR 0044); one that hangs holds it to the deadline, past the 70 minutes after
  which status calls it died, and a queued test fails placing nothing. `timeout` is `/usr/bin/timeout`,
  Apple's (signed `com.apple.timeout`, on the sealed system volume of macOS 27.2); which macOS first
  shipped it is UNVERIFIED, and where it is absent the runner, the watchdog and the paper tasks fail.

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
Three second readers' review of that (2026-11-10), each first shown by a failing test in `scripts/test-lanes`
or `tests/test_session_window.py`, one line per fix:
* the deadline had no room for start-up: now the arithmetic above, held by a test against the window's bounds;
* a descendant that ignored SIGTERM kept the lock after mise's timeout (reproduced: lockf exit 75): uv runs
  under `timeout -s KILL`, and both stand-ins, with and without SIGTERM ignored, leave nothing and the lock free;
* nothing bounded a firing before the task: the fetch at 120 s, each keychain read at 30 s, the whole `mise run`;
* a replace ref, a second remote URL, a skip-worktree bit, an info/attributes rule and a mise.local.toml
  beside an unborn HEAD each ran other code under the reported commit: the clone is made new at every firing;
* a token in the origin's query reached the firing's record through git's own message: a query or fragment is refused;
* a firing stopped by a signal read ok: FAILED from `launchctl list`'s negative status, kept after a run by hand;
* a missing heartbeat read ok, and an installed runner older than the tree's read ok once a stamp existed;
* STALE hid DIED, FAILED and REFUSED, and a firing that exited 1 before any record read UNKNOWN;
* a `running` record stayed ok after a later run held the lock: DIED;
* a run by hand stamped the schedule's firing and wrote its records: only `CANON_PAPER_SCHEDULED=1` stamps,
  and a run by hand writes to `by-hand/` (a runner installed before this, which sets only `CANON_COMMIT`,
  still writes the schedule's records until the owner installs again);
* the runner and a window shared one temporary file name: each writer has its own;
* the lane watchdog exited 0 after a failed status write, never retried a failed notification, could hang
  with its row still ok, and alerted a flag again after a stop between recording and remembering it: each fixed.
`/code-review` (high) of those fixes found five more, each fixed with a case: a firing stopped part way
through remaking the clone left a folder every later one refused (now `.git` goes last, `.venv` alone is
accepted); a run by hand moved the gap marker (now only its records go to `by-hand/`); a run by hand or an
install could delete the clone under a running window, which the task's own process group puts out of
reach of the runner's bound and launchd's bootout (now each run holds `<checkout>.lock`, and uninstall
keeps a clone whose lock is held); a stop between recording an alert and queuing its notification lost it.
A second `/code-review` (high) found five more, each fixed with a case: a checkout path that is a link
(refused); a refusal shown as FAILED by its exit code (REFUSED outranks it); `.git` and its mark swapped in
by two renames, so no stop leaves an unmarked one; DIED read from a runner failure that took no lock (only a
run that held it counts); an install's origin unchecked while a window held the clone (`git ls-remote`).
Ctrl-C from a terminal: as above.
An install over a running collector left it unloaded (2026-11-10): `launchctl bootout` returns while a running
job still stops, and the bootstrap that follows is refused with 5 (reproduced with a throwaway label whose job
takes 4 s to exit on SIGTERM). Install and uninstall now wait, at most 30 s, until `launchctl print` no longer
finds each of the three labels, say plainly when nothing is loaded, and repeat every failure after the status
table; `scripts/test-lanes` shows each with a stand-in launchctl that stops as slowly.
Two second readers' review of that (2026-11-10), each first shown by a failing test in `scripts/test-lanes`
or `tests/test_session_window.py`, or by a probe under mise 2026.11.5:
* a SIGTERM to mise, or to the inner timeout(1), before the deadline ended uv while a child that ignored it kept
  the lock (lockf exit 75): the task's shell now waits and kills uv's group whole on a stop and when uv ends;
* the start-up allowance held nothing: uv's sync now runs first under its own bound (`sync`, 600 s), and the
  deadline test counts it and `START_UP_SECS` (the imports, which the shell cannot bound);
* not reproduced: the runner's kill landing in a window. mise runs a task in a process group of its own
  (observed), so that kill ends mise, and a stand-in window finished after it;
* a first making of the clone stopped before its last rename left a marked `.git.new`, now accepted;
* uninstall removed the files of jobs it failed to unload, and install replaced the runner before the old
  job was unloaded: both now leave a job still loaded and its files as they were;
* each `launchctl` call and the watchdog's notification now run under timeout(1); a heartbeat without its
  time and outcome reads UNKNOWN;
* own finding: mise appends a task's arguments to its script's last line (observed), so from a terminal
  `--session` and `-k` were dropped; that line now calls the function that runs uv.
The account check's uncounted lookups (orders in doubt, a lost close's search) stay ADR 0044's; the deadline
test reads `session_window`'s bounds, so it fails once they outgrow the deadline. GitHub's Ubuntu runner has
no lockf(1), plutil or launchd: the runner, the deadline under mise and the install rehearsal run only on a
Mac, and a run that skips them says so last and as a warning annotation. flock(1) is another call, not a stand-in.

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole.

* ADR 0069, 2026-11-10: a second paper account, B, for the windows; the schedule moves to it once
  `paper:session` reads B's pair. Until then this record stands as written.
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
* Three second readers' review of the runner and the watchdog (2026-11-10), fifteen claims, each reproduced
  offline before its fix, or by a probe with mise 2026.11.5 and lockf (run).
* `man timeout` on this machine: it "makes itself the leader of a process group that the command inherits",
  `-s KILL` "terminate[s] timeout along with the command"; observed: without `-s KILL` it exits with its
  direct child and a grandchild that ignores SIGTERM lives on (read; run).
* `man launchctl`, list: a negative status "represents the negative of the signal which stopped the job" (read).
* `man git`: GIT_NO_REPLACE_OBJECTS, GIT_CONFIG_NOSYSTEM; `man mktemp`: a unique name (read).
* actions/checkout v7.0.1 at `3d3c42e5aac5`, `src/git-directory-helper.ts` ("Delete the contents of the
  directory") and `src/git-source-provider.ts` (`git.init`, `remoteAdd`) (fetched, raw).
* https://raw.githubusercontent.com/jdx/mise/v2026.11.5/settings.toml, `task.run_auto_install`: "Install
  missing tools automatically before tasks run" (fetched, raw).
* https://www.rfc-editor.org/rfc/rfc3986.txt, sections 3.4 and 3.5: the query and the fragment (fetched, raw).
* Two second readers' review of the runner, the watchdog and the installer (2026-11-10), nine claims, each
  reproduced offline, by a probe under mise 2026.11.5, or reported as not reproduced (run).
* `man timeout` on this machine: "For any other signals delivered to timeout, it will propagate them to
  command" (read). mise 2026.11.5: a task's arguments reach the last line of its script, and the task runs as
  `sh` with errexit, in a process group of its own (observed).
* https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands, "Setting a warning
  message" (fetched).
* Second reader (`evidence-verifier`), 2026-11-10, on this split: added the skill and clock lines, marked
  the weekend reading `UNVERIFIED`, narrowed the Good consequence and corrected the install line.
* 2026-11-10: the deadline's sum follows ADR 0044's recounted account check (`LONGEST_WINDOW_SECS` 3076 to
  2955, `AFTER_END_SECS` 605 to 725; the two move together, so the sum is 5757 where it was 5758) (run).
