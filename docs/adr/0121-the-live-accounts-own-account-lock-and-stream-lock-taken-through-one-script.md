---
status: proposed
date: 2026-11-14
decision-makers: lead orchestrator (Claude) for ADR 0110's change (g); the live lane (Claude) for the script
review-by: 2027-02-12
---

# The live account's own account lock and stream lock, taken through one script with a deadline (ADR 0110, change (g)); open: no task calls it yet

## Context and Problem Statement

ADR 0110's change (g) asks for "the live account's own account lock and stream lock". The paper accounts'
locks (ADR 0069, clause 3) are opened inside each paper task's body, each body carrying its own copy of the
wait and of the deadline. Where do the live account's locks live, and how are they taken?

## Decision Drivers

* The live locks are named apart from every paper lock, in one place.
* A live task's shape is proven on a paper account first, under the lock a paper run there waits for.
* A hung command must neither keep a lock for good nor outlive it (the note above `paper:test`).

## Considered Options

1. A copy of the lock wait and the deadline in each `live:*` task's body, as the paper tasks have.
2. One script that holds an account's locks while one command runs.

## Decision Outcome

Chosen: **option 2**, `proposed` until the gap below closes. Option 1 would add a copy to every live task
and a test to hold the copies equal; one script is tested once and names the live locks once.

1. **The script.** `scripts/account-lock <account> [--stream] --wait <secs> --deadline <secs>
   [--grace <secs>] -- <command>` runs the command while holding the account's lock, and with `--stream`
   that account's market-data stream lock, taken after it (ADR 0069's order), both inside one `--wait`.
2. **The names.** `live`: `canon-live.lock` and `canon-alpaca-stream-live.lock`, which nothing else in the
   repository names (a test holds that). `tests`, `windows` and `protect`: the lock files `mise.toml`'s
   paper tasks open, so a run through the script and a paper task exclude each other. `protect` has an
   account lock and no stream lock on `main`, so `--stream` is refused for it.
3. **The folder** is the per-user temporary folder, and no option or variable moves it: two runs that
   named different folders would not exclude each other. With no such folder known, nothing is run.
4. **How the lock is held** is the paper tasks' way: the lock file is opened on a descriptor and that
   descriptor is locked, so every process of the command inherits the lock; the wait is `paper:test`'s own
   `lock_wait` (a test compares the text), which polls `lockf -t 0` and does not spin.
5. **The deadline.** The command runs under timeout(1), which "makes itself the leader of a process group
   that the command inherits", and once timeout has ended, or the script is told to stop, the whole group
   is killed. With no `--grace` the signal is SIGKILL, as `paper:test` and `paper:session` send; with one,
   SIGTERM first and SIGKILL after the grace, for a command that cancels what it sent when told to end,
   as `paper:close` runs. From a terminal the command is exec'd in the terminal's process group.

**The gap, and what would close it.** No task calls the script. Closed by the first `live:*` task that
writes, proven on a paper account under that account's lock, with its paper run recorded in that task's
record.

### Consequences

* Good: one place names the live locks, and the same script serves the paper proof and the live task.
* Bad: the paper tasks keep their own copies, so the wait exists in two forms held equal by tests.
* Bad: the script's cases need lockf(1) and timeout(1) at `/usr/bin`, so they skip by name on the hosted
  Ubuntu run, and a landing's local run does not include the script's self-test (ADR 0144).

### Confirmation

`scripts/test-account-lock`, on a copy of the script whose `getconf` names a folder of the test's own (a
copy that still names the real one is not run): the lock held while the command runs and free after; a
second run waits its bound and runs nothing; the deadline, and a SIGTERM to the script or to the group's
leader, leave nothing running and both locks free; the live locks and the paper locks do not wait for each
other; an unknown temporary folder stops the run before any lock; the grace; a run on a pseudo-terminal.
54 cases on macOS 27.2, run by hand; 9 failed with no script, and account C's 5 before `protect`. No case
touches a real lock, a keychain or the network. No paper run applies: the script reaches no account.

## Evidence

Read 2026-11-14 unless a line gives another date.

* `man lockf` on this machine (macOS 27.2) — "BSD-style locking is used, as described in flock(2)"; "lockf
  may also be used to operate on a file descriptor instead of a file"; `-t`: "A timeout of 0 may be given,
  in which case lockf will fail unless it can acquire the lock immediately"; `EX_TEMPFAIL`: "already locked
  by another process"; its example, "Protect a section of a shell script with a lock". Bundled.
* https://man.freebsd.org/cgi/man.cgi?query=lockf&sektion=1&format=ascii — FreeBSD's lockf(1), dated
  February 26, 2026, the page macOS's descends from: the descriptor form, the `-t 0` sentence and the
  shell-script example are there in the same words. Fetched; that macOS's lockf behaves as FreeBSD's is
  shown only by the cases above.
* `man timeout` on this machine — "timeout makes itself the leader of a process group that the command
  inherits and sends the signal to that process group"; "A descendant that puts itself into another process
  group, for example by calling setsid(2), is not signalled"; `-k`, `-s`. Bundled.
* `mise.toml` on `main` — the paper tasks' lock files, `lock_wait` and `bounded`, and `protect`'s account
  lock with no stream lock. Read.
* ADR 0069, clause 3 — one lock per account, one stream lock per pair, the stream lock taken last; the
  measurement that `lockf -t <secs>` on a descriptor spins. Read; the measurement is not repeated here.
* ADR 0110, change (g), and ADR 0114. Read.
