---
status: proposed
date: 2026-11-14
decision-makers: lead orchestrator (Claude) for the three properties; the landing lane (Claude) for the script
review-by: 2027-02-12
---

# A command under an account lock runs in its own process group to a deadline, is told of a stop once and may not prompt (ADR 0121's script); open: no task calls it, and one wait has no failing case

## Context and Problem Statement

ADR 0121's `scripts/account-lock` holds an account's locks while one command runs. As first built it ran
the command under timeout(1) off a terminal and exec'd it from one, so that a Ctrl-C reached it once. From
a terminal the command's process was then the only holder of the lock's descriptors: one that reopened
descriptor 9 freed the live lock while it still ran, and a second live run started (a reader's finding,
reproduced). How is the command run, bounded and stopped, so that the lock outlives it everywhere?

## Decision Drivers

The lead's three properties, 2026-11-14:

* The lock is held until the command and everything it started have ended.
* A SIGTERM or SIGINT to the script reaches the command, and the script ends after it, with its status.
* Both hold the same with a terminal and without.

## Considered Options

1. Two paths, as first built: timeout(1) off a terminal, `exec` of the command from one.
2. One path: the command always under timeout(1), in a process group of its own, never exec'd.

## Decision Outcome

Chosen: **option 2**, `proposed` until the gap below closes. Option 1 fails the first property from a
terminal. The script's header carries the detail of each clause.

1. **One path.** The command runs under timeout(1), which "makes itself the leader of a process group that
   the command inherits", from a terminal or not. It is never exec'd, so the script's shell keeps
   descriptors 9 and 7, and a command that closes or reopens either frees no lock. Once timeout has ended
   the group is killed, and the script ends only when none of it is left. The command reads the script's
   standard input.
2. **The deadline.** With no `--grace` the signal is SIGKILL, as `paper:test` and `paper:session` send;
   with one, SIGTERM first and SIGKILL after the grace, for a command that cancels what it sent when told
   to end, as `paper:close` runs.
3. **A stop** (SIGTERM, SIGINT or SIGHUP to the script, a Ctrl-C at its terminal included) is passed on as
   that signal to timeout alone, which passes it to the command, so the command is told once; a second
   stop meanwhile is only noted. With a grace the group is killed once timeout has ended, at most that
   long after the stop; with none the deadline bounds it. The run's status is the command's, 128 + the
   signal where a signal ended it.
4. **Job control.** timeout is started under `set -m`, since without it "asynchronous commands ignore
   SIGINT" and timeout would hand that on to the command. It also puts timeout in a group of its own.
5. **No prompt under the lock.** In a group of its own the command is not a terminal's foreground group.
   It can write to a terminal, but a read of it (SIGTTIN) or a change of its settings such as `stty -echo`
   (SIGTTOU) stops it, timeout takes the stop for its end and exits 1, the group is killed, and the run
   ends at once with status 1, the read unanswered or the change not made. A task that prompts for typed
   input, a password included, asks before the script or runs off a terminal.

**The gap, and what would close it.** No task calls the script (ADR 0121's gap). Owed before the first
`live:*` task runs under the lock: a case that fails without the script's last wait for the group to be
gone (clause 1). No case does today: with that wait taken out of a copy, every case passes.

### Consequences

* Good: the lock outlives everything the command left in its group, and a stop means the same everywhere.
* Bad: a command cannot prompt from a terminal (clause 5), and a stop with no grace waits for the command
  or the deadline, where the first build killed at once.
* Limit: a script started with SIGINT ignored (an asynchronous command of a shell without job control)
  keeps it ignored, as bash allows no trap of it, so a SIGINT to it reaches nothing.
* Limit: a descendant that puts itself into another process group and closes 9 and 7 is neither killed
  nor waited for.
* Limit, by reading: a first stop that lands between the group's end and the removal of the script's
  traps is sent to timeout's old pid, with a grace a SIGKILL after it, and reaches another process only
  if the system has handed that pid out again within those lines. "It can write to a terminal" holds
  while the terminal's `tostop` is off.
* Limit: under job control bash may put a line of its own on stderr when timeout ends (seen at a
  deadline's SIGKILL: "Killed: 9"). No status changes.

### Confirmation

`scripts/test-account-lock`, on a copy of the script as ADR 0121's Confirmation says: the deadline, and a
SIGTERM to the script or to the group's leader, leave nothing running and both locks free; one stop tells
the command once, a second during the grace is only noted, and the run ends with the command's status; a
command that ignores SIGTERM keeps the lock held until the grace's or the deadline's SIGKILL; a command
that closes or reopens 9 and 7 and leaves a child running keeps both live locks held, a second run
refused, until that child is gone; a SIGTERM or SIGINT to the script reaches the command as that signal;
the grace; a piped line; on a pseudo-terminal, the descriptors, the child, the two stops, one Ctrl-C,
which reaches the command once, and clause 5: a write goes through, and a `read` and a `stty -echo` each
end the run with status 1 at once, both locks free. The SIGINT cases start their copy with SIGINT at its
default, so they hold when the test is started as `scripts/ci` starts a job too. With one guard taken out
of a copy, cases fail: the repeated wait, the second stop's guard, job control. Clause 5's cases pass with
job control or timeout's own group taken out, since either alone puts the command in a group of its own;
with both out, on a probe by hand, the `read` waits and the `stty` goes through. The last wait for the
group is UNVERIFIED by a case (the gap). Also UNVERIFIED: bash 5 and Linux, where these cases skip by
name; a SIGHUP as the first stop; a stop that arrives before timeout's pid is known, which the script
holds and passes on once it is. The counts are in the landing's pull request. No paper run applies.

## Evidence

Read 2026-11-14.

* `man bash` on this machine (bash 3.2.57) — SIGNALS: "When job control is not in effect, asynchronous
  commands ignore SIGINT and SIGQUIT"; `-m`: "Background processes run in a separate process group and a
  line containing their exit status is printed upon their completion". Bundled.
* `man timeout` on this machine (macOS 27.2) — "If the --foreground option is not specified, timeout
  makes itself the leader of a process group that the command inherits and sends the signal to that
  process group"; "A descendant that puts itself into another process group, for example by calling
  setsid(2), is not signalled"; "For any other signals delivered to timeout, it will propagate them to
  command"; creating the group would "cause reads from the terminal to stop the command with SIGTTIN";
  `-k`, `-s`. The script does not pass `--foreground`. Bundled.
* https://man.freebsd.org/cgi/man.cgi?query=timeout&sektion=1&format=ascii — FreeBSD 15.1's timeout(1),
  April 11, 2026, which differs: it "runs as the reaper (see also procctl(2)) of the command and its
  descendants" and names no process group and no SIGTTIN; it shares the sentence on other signals. So the
  group and clause 5 rest on this machine's page and on the cases, not on FreeBSD's. Fetched, whole.
* `timeout -v` on a copy of the script on a pseudo-terminal, this machine — "child terminated: ...
  reason=5, status=21" for a `read` and "status=22" for `stty -echo` (SIGTTIN and SIGTTOU here), the run
  ending with status 1 in about 0.1 s. Run by hand; this machine's timeout page documents the read alone.
* `mise.toml` on `main` — `bounded` and `-s KILL` in `paper:test` and `paper:session`; `paper:close` under
  `timeout -s TERM -k 30`; the note above `paper:test` on one Ctrl-C. Read.
* Second readers, 2026-11-14 (`/code-review`, a review seat): a stop sent SIGTERM twice, a second stop
  cut the grace short, and the command lost its standard input. Corrected, each on a case that failed first.
* A fourth reader, 2026-11-14 (a high read): from a terminal the script exec'd the command, so one that
  reopened descriptor 9 freed the live lock and a second live run started (reproduced). Closed by clause
  1's one path, each property on a case that failed first.
* A fifth reader, 2026-11-14: the self-test's two SIGINT cases failed when started as `scripts/ci` starts
  a job, SIGINT ignored (reproduced); clause 5 rested on a probe and no case; a pseudo-terminal case that never
  ended would hang the test. Closed in the test: the reset, clause 5's cases, a bound on each driver.
