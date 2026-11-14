---
status: proposed
date: 2026-11-14
decision-makers: lead orchestrator (Claude) for ADR 0110's change (g); the live lane (Claude) for the script
review-by: 2027-02-12
---

# The live account's own account lock and stream lock, taken through one script with a deadline (ADR 0110, change (g)); open: no task calls it yet

## Context and Problem Statement

ADR 0110's change (g) asks for "the live account's own account lock and stream lock". The paper accounts'
locks (ADR 0069, clause 3) are opened inside each shell paper task's body, each body carrying its own copy
of the wait and of the deadline; `paper:protector` takes its two in Python (`session_window.paper_start`).
Where do the live account's locks live, and how are they taken?

## Decision Drivers

* The live locks are named apart from every paper lock, in one place.
* A live task's shape is proven on a paper account first, under the lock a paper run there waits for.
* A hung command must neither keep a lock for good nor outlive it (the note above `paper:test`).
* Two runs through the script on one account never both proceed, whatever the call or its environment holds.

## Considered Options

1. A copy of the lock wait and the deadline in each `live:*` task's body, as the paper tasks have.
2. One script that holds an account's locks while one command runs.

## Decision Outcome

Chosen: **option 2**, `proposed` until the gap below closes. Option 1 would add a copy to every live task
and a test to hold the copies equal; one script is tested once and names the live locks once.

1. **The script.** `scripts/account-lock <account> [--stream] --wait <secs> --deadline <secs>
   [--grace <secs>] -- <command>` runs the command while holding the account's lock, and with `--stream`
   that account's market-data stream lock, taken after it (ADR 0069's order), both inside one `--wait`.
   A wrong call is refused by name. Each number is whole and has no leading zero, which the shell's
   arithmetic would read as octal.
2. **The names.** `live`: `canon-live.lock` and `canon-alpaca-stream-live.lock`, which nothing else in
   `mise.toml`, `home/`, `scripts/`, `src/`, `packages/` or `tests/` names (a test holds that). `tests`,
   `windows` and `protect`: the account lock `mise.toml`'s paper tasks open and the stream lock
   `canon-alpaca-stream-<account>.lock`, so a run through the script and a paper task exclude each other.
   For `protect` that stream lock is on `main` today, taken by `session_window.paper_start` under which
   `paper:protector` starts, so `--stream` takes that same file there as for the other two.
3. **The folder** is the per-user temporary folder, and no option or variable moves it, `TMPDIR` included:
   two runs that named different folders would not exclude each other. With no such folder known, nothing
   is run. One variable moves the folder on the other side: `CANON_PAPER_SESSION_DIR` names the records
   folder whose parent the Python holder locks in. While it is set the script refuses by name, for every
   account, before any lock.
4. **How the lock is held** is the paper tasks' way: the lock file is opened on a descriptor and that
   descriptor is locked, so every process of the command inherits the lock; the wait is `paper:test`'s own
   `lock_wait` (a test compares the text), which polls `lockf -t 0` and does not spin.
5. **The lock step fails closed.** The command is reached only past a marker that each step sets once
   `lock_wait` has returned 0, read on a line of its own: in bash an error inside `lock_wait` ends the
   whole command it was called from, a refusal after `||` included, and as first written `--wait 08` ran
   the command with no lock held. Such an error, another return, a lock file that cannot be opened, a
   clock that gives no time and a shell that ends inside a step each end in a refusal by name, status
   above 0. (One end has bash's message alone: in POSIX mode bash 3.2 ends itself, status 1, at such an
   error. The checks of the numbers keep every call from it.)
6. **The deadline and a stop.** The command runs under timeout(1), which "makes itself the leader of a
   process group that the command inherits", from a terminal or not; it is never exec'd, so the script's
   shell keeps descriptors 9 and 7, and a command that closes or reopens either frees no lock. Once timeout
   has ended the group is killed, and the script ends only when none of it is left. At the deadline, with no
   `--grace`, the signal is SIGKILL, as `paper:test` and `paper:session` send; with one, SIGTERM first and
   SIGKILL after the grace, for a command that cancels what it sent when told to end, as `paper:close`
   runs. A stop (SIGTERM, SIGINT or SIGHUP to the script, a Ctrl-C at its terminal included) is passed on
   as that signal to timeout alone, which passes it to the command, so the command is told once; a second
   stop meanwhile is only noted. With a grace the group is killed that long after a stop; with none the
   deadline bounds it. The run's status is the command's, 128 + the signal where a signal ended it.
   timeout is started under job control, since without it "asynchronous commands ignore SIGINT" (`man
   bash`). The command reads the script's standard input; in a process group of its own it can write to
   a terminal but cannot read it or change its settings: a read stops it (SIGTTIN), as does a change such
   as `stty -echo` (SIGTTOU), timeout takes the stop for its end and exits 1 (both observed), the group is
   killed, and the run ends at once with status 1, the read unanswered or the change not made.

**The gap, and what would close it.** No task calls the script. Closed by the first `live:*` task that
writes, proven on a paper account under that account's lock, with its paper run recorded in that task's
record.

### Consequences

* Good: one place names the live locks, and the same script serves the paper proof and the live task.
* Bad: the paper tasks keep their own copies, so the wait exists in two forms held equal by tests.
* Limit: the lock cases run only by hand. They need lockf(1) and timeout(1) at `/usr/bin`, so they skip by
  name on the hosted Ubuntu run; the hosted macOS job runs `test-lanes` and `test-maintenance` alone; and a
  landing's local run leaves this self-test out (ADR 0144). No automated run, local or hosted, executes
  them. Closed by adding the script to the hosted macOS job or to the landing's local set.
* Limit: the refusal in clause 3 is this script's side only. A `paper:protector` started with
  `CANON_PAPER_SESSION_DIR` set in its own environment locks in another folder, where neither this script
  nor a shell paper task looks. That is `main`'s today, in `session_window.py` and `protector_node.py`,
  both on the trading path, and is closed by a unit there. In this repository only tests set the variable
  (`tests/conftest.py`, `scripts/test-lanes` and this self-test; a search for its assignments).
* Limit: a script started with SIGINT ignored (an asynchronous command of a shell without job control)
  keeps it ignored, as bash allows no trap of it, so a SIGINT to it reaches nothing. A descendant that puts
  itself into another process group and closes 9 and 7 is neither killed nor waited for. From a terminal, a
  command that asks for typed input or sets the terminal (a password prompt) ends 1 at once: it takes
  that before the script, or runs off a terminal. A command can return the script's own 1 and 3: the
  refusal's line on stderr tells them apart. The clock is checked once, before any lock: a later empty
  reading at the stream step leaves that wait without its bound, under the account's lock, and runs
  nothing unlocked. The script is not on `scripts/land`'s trading-path list.

### Confirmation

`scripts/test-account-lock`, on a copy of the script whose `getconf` names a folder of the test's own (a
copy that still names the real one is not run): the lock held while the command runs and free after; a
second run waits its bound and runs nothing, with `TMPDIR` naming another folder too; the deadline, and a
SIGTERM to the script or to the group's leader, leave nothing running and both locks free; one stop tells
the command once, a second during the grace is only noted, and the run ends with the command's status; a
command that ignores SIGTERM keeps the lock held until the grace's or the deadline's SIGKILL; a command
that closes or reopens 9 and 7 and leaves a child running keeps both live locks held, a second run
refused, until that child is gone; a SIGTERM or SIGINT to the script reaches the command as that signal;
the live and the paper locks do not wait for each other; `protect --stream` holds account C's two locks;
a run with `CANON_PAPER_SESSION_DIR` set is refused on each account; an unknown temporary folder, a lock file that
cannot be opened and a clock that gives no time each stop the run; a leading zero is a wrong call, and
`--wait 09` runs nothing while both live locks are held; with the whole-number and leading-zero checks
taken out of a copy, a wait of `08`, of `1.5` and of a word, each without `--stream` and with it, break
the account's lock step and are refused there (the word by the exit's line, which names the step), as is
a broken stream step; the grace; a piped line; on a pseudo-terminal, the descriptors, the child, the two
stops and one Ctrl-C, which reaches the command once.
107 cases on macOS 27.2 under bash 3.2.57, run by hand, 2026-11-14. Failing first: 9 with no script (the
builder's figure, reproduced by a second reader), 38 of the first 82 against the script as first written
(37 in a later reader's run: one count of signals depends on timing), and 16 of the 107 against the script
that exec'd from a terminal. With one guard of clause 6 taken out of a copy: 11 cases fail without the
repeated wait, 1 without the second stop's guard, and 2 without job control, after which a case on the
pseudo-terminal does not end. None fails without the last wait for the group to be gone: that wait is
UNVERIFIED by a case. No case touches a real lock, a keychain or the network. No paper run applies: the
script reaches no account.

## Evidence

Read 2026-11-14 unless a line gives another date.

* `man lockf` on this machine (macOS 27.2) — "BSD-style locking is used, as described in flock(2)"; "lockf
  may also be used to operate on a file descriptor instead of a file"; `-t`: "A timeout of 0 may be given,
  in which case lockf will fail unless it can acquire the lock immediately"; `EX_TEMPFAIL`: "already locked
  by another process"; its example, "Protect a section of a shell script with a lock". Bundled.
* https://man.freebsd.org/cgi/man.cgi?query=lockf&sektion=1&format=ascii — FreeBSD's lockf(1), dated
  February 26, 2026, a later revision of the page macOS's descends from: the descriptor form, the `-t 0`
  sentence and the shell-script example are there in the same words. Fetched; that macOS's lockf behaves
  as FreeBSD's is shown only by the cases above.
* `man bash` on this machine (bash 3.2.57) — SIGNALS: "When job control is not in effect, asynchronous
  commands ignore SIGINT and SIGQUIT"; `-m`: "Background processes run in a separate process group".
  Bundled.
* `man timeout` on this machine — "If the --foreground option is not specified, timeout makes itself the
  leader of a process group that the command inherits and sends the signal to that process group"; "A
  descendant that puts itself into another process group, for example by calling setsid(2), is not
  signalled"; "For any other signals delivered to timeout, it will propagate them to command"; `-k`,
  `-s`. The script does not pass `--foreground`. Bundled.
* `mise.toml` on `main` — the shell paper tasks' lock files, `lock_wait` and `bounded`, and `protect`'s
  account lock. Read.
* `src/canon_trading/session_window.py` and `protector_node.py` on `main` — `paper_start` takes
  `PAPER_ACCOUNT_LOCKS[account]`, then `canon-alpaca-stream-{account}.lock`, for `tests`, `windows` and
  `protect`; `default_log_dir` returns `CANON_PAPER_SESSION_DIR` when it is set; `run_protector` passes
  `default_log_dir().parent` as the lock folder. Read.
* ADR 0069 — clause 3: one lock per account, one stream lock per pair, taken after the account's lock;
  its Confirmation: the stream lock taken last, and the measurement that `lockf -t <secs>` on a descriptor
  spins. Read; the measurement is not repeated here.
* ADR 0110, change (g), the requirement quoted above; ADR 0114, the third paper account, `protect`, which
  says nothing of its locks. Read.
* Second readers, 2026-11-14 (two `evidence-verifier` passes, `/code-review`, a second review seat): this
  record said `protect` had no stream lock on `main`, which `paper_start` contradicts, and a zero-padded
  wait ran the command with no lock. Both are corrected in one round, with clause 5, the checked open,
  the single SIGTERM, the second stop, the kept standard input and the variable's refusal, each on a case
  that failed first. "Account C's 5 before `protect`", a figure no reader could reproduce, is dropped.
* A third reader, 2026-11-14 (a high read of that correction): no case failed with the account step's own
  refusal taken out, since each such case passed `--stream` and the stream step refused instead. Closed
  by the same cases without `--stream`, asked for that step's own line: 4 fail with the refusal removed.
* A fourth reader, 2026-11-14 (a high read): from a terminal the script exec'd the command, so one that
  reopened descriptor 9 freed the live lock and a second live run started (reproduced). Closed by clause
  6's one path, on the lead's three properties: the lock outlives all the command started, a stop reaches
  the command and the run ends with its status, the same with a terminal and without; each on a case that
  failed first.
