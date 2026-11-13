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
| What fires | A second launchd user agent, `local.canon.paper-session`, installed, reported and removed by `scripts/automation` beside the lane watchdog; every 2100 s (from 2026-11-11, 600 s after each firing ends: below), not at load; a firing while the job still runs or the Mac sleeps is missed (launchd.plist(5)) | ADR 0011's launchd agent |
| What runs | `~/.claude/canon/paper-session.sh` makes its own clone, `~/.claude/canon/paper-checkout`, anew at each firing from the `main` the origin has then, detached, keeping only `.venv`, or fails the firing, then runs `mise run paper:session --session auto --minutes 25` there: one window, one node, one process, disposed, its records naming the commit. 25 minutes, with about three of start-up and stop (a regular stop waits its 65 s post-stop delay; an estimate, UNVERIFIED until a window has run), leaves the gap (300 s then, 60 s since ADR 0044's line of 2026-11-11) inside 35 | configure_live_trading: "run nodes as standalone Python scripts or services"; "One LiveNode per process"; actions/checkout's path for a directory it does not reuse (the runner's header) |
| Which session | `auto` takes the regular session when Alpaca's clock says it is open (`is_open`, which is false on weekends and holidays; UNVERIFIED, the clock page says only "Whether or not the market is open"), the extended one when `EXTENDED_READY` and it is open, and otherwise prints "nothing to run" and exits 0 | Alpaca's paper-trading skill: "always check the clock/calendar endpoint" |
| A window that dies | It writes `latest.json` as `running` before the node is built and `ok` or `failed: <why>` after, the account check included; a refusal someone must look at (account not flat) is `refused: <why>`, while a skip that clears by itself (the gap, under 575 s of session left, or start-up reads that left too little of the window) exits 0 and keeps the last result. A `running` record past its `stale_after` (70 minutes) is a window that died | No upstream |
| A stop (2026-11-12) | SIGINT or SIGTERM, the first one named and the copies `timeout(1)` and uv pass on ignored: before the node the window ends `failed: WindowSignalled: stopped by <signal>`; during it the node is asked to stop and the wind-down (flatten, account check, record) runs; after it the wind-down finishes and the record names the signal. While the record reads `running`, the handler writes it as `stopping: <signal> received; the window has not finished` at once, so after a signal it never reads `running` again, and a kill before the window's end leaves that; a result already set is left as it is. A stopped window exits 128 + the signal's number. `bounded()` in `paper:session` passes a stop on as SIGTERM and waits `grace` before it kills the group: 785 s, `STOP_GRACE_SECS`, the longest wind-down (`AFTER_END_SECS`, 755 s with `PART_SLOTS`, seven names, in each of pre-market, regular, after-hours and overnight; 725 s with one name; the bound does not depend on the part) and 30 s for the record and the exit (UNCALIBRATED). `paper:test` leaves `grace` unset and kills at once, as before. Limit: a stop from launchd itself (a shutdown, or the owner's `launchctl bootout`) is bounded by launchd's `ExitTimeOut`, which the plist leaves at its default, "system-defined" (launchd.plist(5)); the maintenance install holds account B's lock and so never unloads a running window (above) | CPython 3.14 `signal` and `asyncio.Runner` docs and `Lib/asyncio/runners.py:112-113` at the pin; NautilusTrader `LiveNode.run_async` ("The host owns the loop and its signal handling"); `man timeout`; uv `crates/uv-command-support/src/child.rs:117-122` |
| Status | `scripts/automation status`'s `paper session` row: the last window, its result, age and leftovers; FAILED when the last firing exited non-zero or was stopped by a signal (`launchctl list`); DIED, too, for a `running` record older than a later run's heartbeat (that run held the lock); STALE when the runner's own stamp, `scheduled-firing.json`, written as each scheduled firing starts, is older than the longest firing (its fetch bound, the 105-minute task timeout and 300 s for mise's start) plus an interval and 300 s, since launchd misses the intervals a firing runs through; not ok when the stamp or heartbeat is missing or unreadable, or the installed runner is not the tree's. A check only makes the label worse. A run by hand writes to `by-hand/`, which the row does not read | ADR 0011's status rows |

Installing it is the owner's step: `scripts/automation install`. Whether the agent's `mise run` reads the
login keychain without a dialog is UNVERIFIED until its first firing. A firing while the Mac sleeps is missed.

### Consequences

* Good: once installed, a window starts every 35 minutes while the Mac is awake and a session is open, with
  no one starting it, each on the newest `main`; extended sessions only once `EXTENDED_READY` is on.
* Since 2026-11-11 the windows run on paper account B and the paper tests on account A, each under its
  own lock, and both take a stream lock last, since Alpaca allows a user one market-data stream connection
  per endpoint on many subscriptions, which for a second paper account of one login is UNVERIFIED
  (ADR 0069). What each waits for:
  * A paper test waits up to 3900 s for account A's lock, which only another `paper:test` run holds (a
    full run about five minutes, observed; one that hangs, to its 7140 s deadline). A test in a module that
    streams (the `alpaca_stream` marker) then waits up to 3900 s for the stream lock, and never past the
    point where less than 900 s (`TEST_ROOM_SECS`) would be left before that deadline. A window holds that
    lock from after uv's sync to its bounded end: `START_UP_SECS` (30), the first clock read with retries
    (`ADAPTER_CALL_SECS`, 129), the window (`LONGEST_WINDOW_SECS`, 2955, or 1500 for a scheduled 25
    minutes), `SKEW_SECS` (60) and `AFTER_END_SECS` (725). That is 3899 s at the longest and about 2444 s
    for a scheduled window, so the test's wait outlasts one window that runs as it should. A test that
    does not stream waits for no window.
  * A window waits for account B's lock, which only another window holds (one by hand, or a firing:
    launchd skips a firing while one runs). It then waits for the stream lock, which a streaming paper
    test holds for that test alone (a whole paper run took about five minutes, observed; how long one
    test takes is UNVERIFIED). Both waits share the one 900 s wait in the deadline
    below, so its sum is unchanged; past it, the window places nothing.
  * No run holds the stream lock while it waits for an account's lock, so the three locks cannot deadlock.
* Bad: each streaming test waits for the stream lock on its own, so the waits of one run add up across its
  streaming tests; once only 900 s would be left before `paper:test`'s deadline, a test that finds the lock
  held fails placing nothing, and the tests after it may fail the same way. The tests before it may already
  have placed and cleaned up their orders. A test that takes longer than 900 s once it has the lock can
  still be killed at the deadline, its cleanup unrun, as any run killed there. From a terminal only mise's
  7200 s `timeout` ends the run, and the fixture still counts its room from the task's 7140 s. A window refused for an account that is not flat stays refused until
  someone clears it. A node that hangs on stop (ADR 0044's disconnect hang) is waited for at most
  `STOP_TIMEOUT_SECS` (about 115 s). The window is then recorded failed, the node's last worker calls
  waited for (at most about 129 s), the account checked (polls start for `AFTER_STOP_SECS`, 60 s, and the
  last runs on; ADR 0060), and the process ended.
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
  (`LONGEST_WINDOW_SECS`, 2955) and its bounded end (`AFTER_END_SECS`, 725), with the imports after the
  sync (`START_UP_SECS`, 30): 5787, 453 s spare; a test
  reads those bounds from `session_window`. From a
  person's terminal uv runs as before, with mise's `timeout` as the deadline: through timeout(1) one Ctrl-C
  reached Python twice (observed), which could cut a flatten short. A window run as it should holds the stream lock under
  a streaming paper test's 3900 s wait (above); one that hangs holds it to the deadline, past the 70 minutes
  after which status calls it died, and a queued streaming test fails placing nothing. `timeout` is `/usr/bin/timeout`,
  Apple's (signed `com.apple.timeout`, on the sealed system volume of macOS 27.2); which macOS first
  shipped it is UNVERIFIED, and where it is absent the runner, the watchdog and the paper tasks fail.
* Bad (2026-11-11): a reinstall restarts the interval. Each time `scripts/automation install`
  reloads this agent, the interval starts again from the load. That holds whether the owner, a
  coordinator or the maintenance agent (ADR 0065, addition of 2026-11-11) runs the install. Launchd
  documents no other start for `StartInterval`, so this is UNVERIFIED on real launchd. Install reloads
  this agent only when its runner or plist changed, or it is not loaded. So only landings that change
  those, less than the interval apart (35 minutes until 2026-11-11, now 10 after a firing ends), can
  hold windows off. The maintenance agent does not repeat an
  install within a day for the same differences at the same origin/main. Its install runs while it holds account B's lock, which a window
  holds (ADR 0069), so it never unloads a window that is running; a scheduled firing then waits for the
  lock, at most 900 s.

### The interval counts from a firing's end (2026-11-11)

launchd starts a `StartInterval` job an interval after its last run ended, not after it started. This was
observed on macOS 27.2.

* **Throwaway agents.** Two agents had a 20 s interval and a 15 s job, one with `ProcessType` Background
  and one with Standard. Both started every 35 or 36 s.
* **The real agent.** It was loaded at about 01:14 New York time and first fired at 01:48. That firing's
  window ended at 02:13:35, and the next firing started at 02:48:36: 2,100 s after the end. Nothing was
  missed, and the Mac did not sleep.
* **What launchd.plist(5) says.** It describes `ProcessType` as resource limits only (CPU and I/O
  throttling), not timer coalescing. The lane watchdog, also Background, kept its 600 s interval from its
  runs' ends.

So with 2,100 s, windows came about an hour apart: the firing's own length plus 35 minutes.

The interval is now 600 s, the pause after a firing. That is above the gap (300 s then, 60 s since ADR 0044's line of 2026-11-11), and a 25-minute window
with its start and stop then comes about every 35 minutes, as decided above. A closed session's firing
ends in seconds and checks the clock again 10 minutes later: a fresh clone (0.5 s) and one clock call,
about 144 a day when no session is open where there were about 40, each holding account B's lock for its
few seconds.

The row's STALE bound (the longest firing plus an interval and 300 s) already assumed this. The row now
also flags a firing overdue by more than one interval. When the job is not running and nothing has been
written for two intervals and 300 s, the row is STALE. "Nothing written" covers the firing's stamp,
heartbeat, record and log, and the plist an install rewrites as it reloads the job, which restarts the
interval. A sleep that long shows too, as the message says. `scripts/test-lanes` shows the interval and the overdue row, each test failing before
its change.

UNVERIFIED: that launchd keeps counting from the end on other macOS releases. The page says only "started
every N seconds".

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
The account check's lookups (orders in doubt, a lost close's search) stay ADR 0060's; the deadline
test reads `session_window`'s bounds, so it fails once they outgrow the deadline. GitHub's Ubuntu runner has
no lockf(1), plutil or launchd: the runner, the deadline under mise and the install rehearsal run only on a
Mac, and a run that skips them says so last and as a warning annotation. flock(1) is another call, not a stand-in.

**The schedule's switch (2026-11-11).** The owner directed on 2026-11-11 that the SPY window leaves paper, and
the schedule had no switch short of an uninstall. launchd's own (`launchctl disable`, the plist's `Disabled`
key) is state on the machine, which only an install or a person changes, and the maintenance agent defers the
paper runner and plist while a window holds account B's lock (ADR 0065), so a switch that has to be installed
cannot be relied on while a window runs. The switch is therefore a tracked value: the first line of
`canon/paper-schedule`, which `paper:session` reads at every scheduled firing, from the main that firing has
just cloned, before it takes a lock. `off`: the firing skips by name, exit 0, heartbeat `skipped: ...`.
`mover`: it runs only with `--mover` among its arguments, and one without, from a plist installed before
main's, is refused by name. Any other first line, or no file, fails the firing by name. A run by hand does
not read it, and `scripts/automation status` shows a schedule that is off as each firing's own heartbeat. A
landing on main therefore turns the schedule at the next firing with nothing to install. Main's value is
`off` from this change, until the change that gives the schedule a mover window sets it, together with a
plist that carries `--mover` (a check holds the two together). Run under mise in `scripts/test-lanes`,
account B's lock held by another process: each of the three outcomes takes no lock and starts nothing.
Run once by hand with the schedule's mark set, from the change's own tree at 17:58:45 UTC on 2026-11-11,
while a window held account B's lock: `skipped` by name within a second, exit 0, the heartbeat written. A
firing launchd itself starts is UNVERIFIED until the first one after the landing.
Limits: mise renders the task's env before its body, so a skipped firing still reads account B's pair into
its own environment, which only a check in the installed runner would spare; while the schedule is off the
row keeps the label of the last window's record; and a skip's heartbeat names no commit, so a record still
`running` reads as died only once it is stale, as after a runner's own failure.
The first firing launchd started after the landing, at 20:52:21 UTC on 2026-11-11 from the main of that
minute, skipped by name with its heartbeat written; the installed plist was still the one without `--mover`.

**Turned on (2026-11-13).** The owner directed on 2026-11-13 that account B be in a window whenever Alpaca's
clock says a session is open, with no lane holding a timer for it, and that a proof take its turn through
account B's lock. Main's value is `mover` from this change, which #289 had set to `off` on 2026-11-12 until the
universe landed. The plist `scripts/automation` writes runs `--session auto --minutes 25 --mover
--regular-strategy Mover` an interval after each firing ends (above), and each firing reads the switch from the
main it clones. The interval goes from 600 s to 70 s with this change, by the lead's ruling of 2026-11-13 that
account B be idle about a minute between two scheduled windows, not ten: above the 60 s gap, so the firing
after a window is not skipped. With a firing's clone, sync and imports, a window then follows the last within
about two minutes while a session is open. The new interval reaches the machine only through the maintenance
agent's install from main (ADR 0065), which rewrites and reloads the plist; until then the installed one keeps
600 s. A closed session's firing then checks the clock about every 70 s, some 1,200 a day where there were
144, each a fetch of main and one clock call holding account B's lock for a few seconds. The status row's
overdue bound, two intervals and 300 s, becomes about 7 minutes. This change lands only after the one that makes the per-symbol trail the Mover window's default, so a
scheduled window, which passes no trail flag, runs that default. A firing launchd starts after this landing is
UNVERIFIED until the first one. Limits:

* **Account B is idle between windows** for the interval and a firing's start-up, about two minutes. That
  is UNVERIFIED until the first firings at 70 s have run.
* **A proof waits at most 900 s.** A run by hand waits for account B's lock as a firing does, at most
  `lockwait` (900 s), and a scheduled window holds it for about 27 minutes. A run started early in a scheduled
  window therefore fails by name with nothing placed, and is started again after that window's end;
  `paper:session` skips a window that would start within 60 s of the last one's end. A firing that waits out
  900 s behind a longer run by hand fails the same way, and the next firing comes 600 s later.
* **The history load moves to account A.** ADR 0093's history load takes no account lock; with the schedule
  on, it runs on account A, not B. Nothing enforces this; it is a rule for the lane that runs a load.

**The records kept (2026-11-11).** A window's records and logs are written only under the per-user temporary
folder, which a restart empties (whether the system also clears it while running is UNVERIFIED). `paper:session`
therefore copies them with macOS's `/usr/bin/rsync` into `<config>/canon/watch/windows/`, beside the runner's
log. Every `window-*.json`, by hand or scheduled, is copied at each run's start, before the switch is read
and with no lock, so a firing that skips keeps what a run from a terminal left; after a window, whatever its
exit, which the task still returns, the records again and each `window-*.log` of 1 MB or less, with account
B's lock still held so that a log is copied whole. Each copy is held to 20 s; one that fails is said on
stderr and changes no exit code; status and the windows keep reading the temporary folder. Run under mise in
`scripts/test-lanes` with a stand-in window. On this machine the first firing after the change, at 21:57:47
UTC on 2026-11-11 with the schedule turned to `mover`, read the switch, took the lock and ended with nothing
to run outside the regular session; afterwards the kept folder held all 23 records of the temporary folder,
the one of that day's last run by hand identical byte for byte, and 21 of its 22 logs, the one left out
being 5.4 MB. A window that trades is UNVERIFIED until the first regular session. Limits: nothing prunes the kept folder; a log over 1 MB is left out and not named, and the log of a
window run from a terminal waits for the next window's end; the heartbeats and `last-window-end` are not
copied; the kept folder takes the temporary folder's mode and time at each copy; and the folder follows the
caller's `CLAUDE_CONFIG_DIR`, which the paper agent is not given, so a run by hand under another config
directory keeps its records apart from the schedule's.

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole.

* ADR 0069, 2026-11-10: a second paper account, B, for the windows. Since 2026-11-11 `paper:session`, and
  so the schedule, reads B's pair only, under B's lock and the stream lock (`mise.toml`;
  `scripts/test-lanes` and `tests/test_session_window.py`, run). The waits above are taken from
  `session_window`'s bounds (run).
* 2026-11-12, for the stop row: CPython v3.14.10's `Doc/library/signal.rst:20-22`,
  `Doc/library/asyncio-runner.rst:170-178` and `Lib/asyncio/runners.py:112-113,160-167`; NautilusTrader's
  `crates/live/src/python/node.rs` (`run_async`) at the pin; uv's `crates/uv-command-support/src/child.rs:117-122`
  (`scripts/canon src`, read); `man timeout` on this machine (`bundled`); the stops, the ignored copies, the
  `stopping` mark under a kill and `bounded()`'s grace in `tests/test_session_window.py` (run).
* 2026-11-12, the stop row on paper, three short windows by hand on account B in the overnight part, each
  started as the schedule starts one (`timeout -s KILL` around `mise run paper:session --session auto --mover`,
  in the background, so with SIGINT ignored), each signal sent only after a read of 0 positions and 0 open
  orders (run): SIGTERM to the task after a ranking had run in the node; SIGINT to the task; SIGINT to the
  window's own process. Each record read `stopping: <signal> received; the window has not finished` within 6 s,
  never `running` again, and closed 9 to 10 s after the signal as `failed: WindowSignalled: stopped by <signal>`,
  with no order, no error and the account flat. A SIGINT sent to the task reaches the window as SIGTERM:
  `bounded()`'s shell traps it (`trap 'stop 130' INT`) and passes the stop on as SIGTERM to the group, as the
  row says (read in `mise.toml`); the window's own SIGINT path ran in the third window.
* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md: "run nodes as standalone
  Python scripts or services"; "One LiveNode per process" (fetched, raw).
* `scripts/automation` and ADR 0011: the launchd agent, its install, status and uninstall (read).
* 2026-11-11, for the records kept: `man rsync` on this machine, where `/usr/bin/rsync` is openrsync
  ("protocol version 29", "rsync version 2.6.9 compatible"): `-a`, `--include`, `--exclude` and `--max-size`
  (read, `bundled`); the two calls run on a scratch folder: a 1.1 MB log left out, a record of that size
  and a log of exactly 1 MiB copied, other files left (run). `/code-review` at medium found that a log
  copied while it grows keeps its short copy once it passes the bound, which is why logs are copied only
  at a window's end.
* 2026-11-11, for the schedule's switch: `man launchctl` on this machine, `enable | disable`: "Once a
  service is disabled, it cannot be loaded in the specified domain until it is once again enabled. This
  state persists across boots"; `man launchd.plist`, `Disabled`: "this state is kept externally" (bundled).
  https://mise.jdx.dev/tasks/task-configuration.html: a task's `skip_if` is a command, and "If it exits 0,
  mise skips the task"; the page lists no key that fails or refuses a task on a condition (fetched
  2026-11-11 through a summarising fetch, not read whole). Not used: a firing must also be refused or
  failed by name with exit 1. `home/.claude/canon/maintenance.sh`: a round that finds account B's lock held
  runs `install --defer-paper` and leaves the paper part to the session boundary (read).
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
* 2026-11-10: the deadline's sum follows the recounted account check (then in ADR 0044, now ADR 0060) (`LONGEST_WINDOW_SECS` 3076 to
  2955, `AFTER_END_SECS` 605 to 725; the two move together, so the sum is 5757 where it was 5758) (run).
* 2026-11-11: the sum written here now counts `START_UP_SECS` (30), as the deadline test always has: 5787,
  453 s spare (`evidence-verifier`, run).
