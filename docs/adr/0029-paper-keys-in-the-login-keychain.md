---
status: accepted
date: 2026-11-10
decision-makers: repo owner (request, 2026-11-10); master orchestrator (Claude)
review-by: 2027-02-08
---

# The Alpaca paper key pair: the login keychain, read by one mise task when it runs

## Context and Problem Statement

The adapter reads a paper key pair from `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (ADR 0030), and the two
paper tests are skipped until both are set. Nothing in the repository said how the pair gets into a
process's environment. The owner asked (2026-11-10) for an interactive key store for the paper account,
entered once. How `paper:store` asks for the pair and checks that it is paper is ADR 0033.

## Decision Drivers

* A value is never in a file, a command line or a conversation (ADR 0009; AGENTS.md rule 8).
* It reaches the process that needs it and no other: not the owner's shell, not `scripts/ci`, not an agent's
  `mise env`.
* Hosted CI and a machine without the pair are unaffected: no error, no prompt, the paper tests skipped.
* No new upstream (ADR 0001); the same in this checkout, a new worktree (Orca's included) and a terminal.

## Considered Options

1. A `.env` file, loaded by mise's `_.file`.
2. `export` lines in the shell profile.
3. Claude Code's `env` settings.
4. Alpaca's CLI profiles.
5. fnox, through mise's secrets support.
6. The login keychain, read by an `exec()` template in `mise.toml`'s top-level `[env]`.
7. The login keychain, read by an `exec()` template in the `env` of one mise task.

## Decision Outcome

Chosen: **option 7**. Each value is a generic password item in the login keychain, written by Apple's
`security`, service `canon-alpaca-paper`, account the variable's name. Four tasks in `mise.toml`:

| Command | Does |
|---|---|
| `mise run paper:store` | Asks for both values at a hidden prompt, stores them only if Alpaca's paper API accepts the pair |
| `mise run paper:check` | Says whether each item exists, looking it up without `-w` or `-g`, so no value is read |
| `mise run paper:remove` | Deletes both items |
| `mise run paper:test` | Runs the paper tests under a lock; its `env` reads both items when the task starts |

1. **Only `paper:test` puts the items in an environment** (`paper:store` reads them back once to confirm the
   write). mise renders a task's `env` when that task runs, and in the trial not for `mise env`, `mise exec`,
   `mise tasks` or an activated shell. Both values are marked `redact = true`, so the task's output shows
   `[redacted]`. With an item missing it exits 1, naming the `security` call that failed; with one empty,
   which would skip both tests and pass, it refuses.
   This clause is superseded by ADR 0044 (2026-11-10): `paper:session` also reads the items into its own
   `env`, the same way, and runs under the same lock.
2. **The template is in tracked `mise.toml` and holds no value.** A new worktree has it, and is trusted
   through the main checkout. No `mise.local.toml`, global config or shell profile line.

Two departures from the first design (option 6, with `security`'s own prompt), both from the trial. One
follows; the other, the prompt, is in ADR 0033 with paper only at entry:

* **Option 6 fails the second driver.** A top-level `[env]` template rendered on `mise env`, which
  `scripts/ci` evaluates, on every `mise exec`, and in every interactive shell entering the checkout
  (`~/.zshrc` activates mise); `mise env` printed the value, as its docs say it does "on purpose".

Options 1 to 3 put a value in a file or in every process of a shell. Option 4: the CLI is a `watch` entry and
its profiles do not set these variables. Option 5 is mise's recommended route, but is experimental and needs
fnox, which is not in the registry. **Watch fnox** until mise's secrets leave experimental.

### Consequences

* Good: entered once, in no file, read by one child process per run. CI, a Linux machine and a Mac without the
  items are unchanged. Rotation is `paper:remove`, then `paper:store`.
* Bad: the keychain does not keep agents out. The creating program "is trusted to access its data without
  warning", and that is `security`, so any process of this user running `security find-generic-password -w`
  reads a value with no dialog. "Agents never read a credential" stays an instruction. `-T ""` would make each
  read ask the owner: untested for a process with no window, and whether "Always Allow" puts `security` back
  on the item's list is UNVERIFIED.
* An agent may run `paper:test`, whose tests place paper orders and cancel each: within ADR 0009. The `ask`
  rule for it that this left open is closed, not wanted (2026-11-10). Open, the owner's: a `deny` rule for
  `security find-generic-password` (`.claude/settings.json`), a text matcher ADR 0020 calls fragile.
* Once ADR 0025's sandbox is on, an agent's `paper:test` reads the keychain and reaches Alpaca from inside it,
  which is untried (UNVERIFIED); there `gh` failed on a certificate check. The owner's own terminal is outside
  the sandbox.
* Bad: macOS only (`paper:store` needing the network is in ADR 0033). Task scripts are Tera templates (no
  `{{`, `{%` or `{#`; `${#x}` broke one), and `scripts/ci`'s shellcheck reads files with a shebang only.

### Confirmation

`scripts/bootstrap --check` prints a `paper key pair` row, `ok` or `absent`, that never fails the check;
`scripts/test-lanes` covers it with a `security` stub, asserts it never passes `-w` or `-g`, and fails if
`mise.toml` names a keychain service other than the bootstrap's. `scripts/ci` passed with no item present, the
paper tests (two at the time) skipped. The four task bodies, written out with a bash shebang, passed shellcheck 0.11.0 (run
once, by hand).

The trial, 2026-11-10, mise 2026.11.5 and macOS 27.2, with made-up values under a throwaway service:

* `security add-generic-password -U -a ALPACA_API_KEY -s <throwaway> -w` on a pty, typed by `expect`:
  "password data for new item:", "retype password for new item:", nothing echoed, exit 0. A `-w` read returned
  at once, no dialog: length 20, sha256 `096b0bf83314`, the made-up value's.
* A template that also appended to a marker file. In top-level `[env]`: a render each for `mise env -s bash`,
  `mise exec -- true` and `zsh -ic` entering the checkout; `mise env` listed the variable. In a task's `env`:
  none for those, `mise tasks ls` or `mise tasks info`; one for `mise run`, whose child saw length 20 and the
  same hash, and whose `echo` of it printed `[redacted]`. The same in a new `git worktree` with stdin not a
  terminal (trusted already, no prompt), and from `env -i` plus `zsh -il` on a pty, where the shell lacked the
  variable and the task's child had it.
* The four tasks, only the service name changed (`paper:store`'s refusals are in ADR 0033). `paper:check`:
  both absent, exit 1. Two items placed as `paper:store` places them: `paper:check` exit 0; `paper:test` ran
  both tests, which failed on Alpaca's `{"message": "unauthorized."}` with neither value in the output.
  `paper:remove` removed both (`find-generic-password` exit 44 each), a second said "was not there", and
  `paper:test` exited 1 naming the failed `security` call. After review: with two empty items, `paper:test`
  refused, "a keychain item is empty", exit 1.
* UNVERIFIED in this trial: `paper:test` with a real paper pair (none was used; it has run with one since,
  ADR 0030 and ADR 0036); a keychain dialog after a macOS update or an item moved between Macs.

## Evidence

Read 2026-11-10. mise's pages were read whole as source at the pinned tag, with `curl` from
https://raw.githubusercontent.com/jdx/mise/v2026.11.5/: `fetched`. The Alpaca and `man curl` lines moved to ADR
0033, split from this record on 2026-11-10 with no change of decision; the `man security` and review lines
support both and are kept in each; the review line covers the record before the split, so "All fixed above"
means the two together. The mise base URL gained its `https://` scheme so the lint finds it.

* ADR 0044, 2026-11-10: supersedes clause 1, "Only `paper:test` puts the items in an environment"; the
  rest of this record stands.
* The owner's standing instruction, 2026-11-10: a paper run needs no ask, at any hour, as long as the tests
  follow the practice of Alpaca's own repositories and NautilusTrader (how ADR 0036's do is stated there);
  the `ask` rule above is closed on it.
  `paper:test` now runs every `test_paper_*.py` under `lockf(1)` (read with `man lockf`) (ADR 0036).
* `man security` on this machine — `-w`: "Put at end of command to be prompted (recommended)"; `-U`; `-T`; "By
  default, the application which creates an item is trusted to access its data without warning. You can remove
  this default access by explicitly specifying an empty app pathname: -T """; `find-generic-password` `-g` and
  `-w` display the password; `delete-generic-password`. It does not say that `-w` reads stdin (read).
* mise `docs/environments/index.md` — "Where variables apply": an activated shell receives `[env]`;
  `docs/tasks/task-configuration.md` — a task's `env` is "for this task's commands", in `[env]`'s forms;
  `docs/templates.md` — `exec()` returns a command's output, needs trust, and "`[vars]` and `[env]` are still
  rendered when the config loads", while task fields render "When the task runs" (fetched).
* mise `docs/environments/secrets/index.md` — fnox "(experimental, recommended)"; `redact = true` gives
  `[redacted]` in task output; `mise env` "prints real values on purpose". `docs/cli/trust.md` — trust in the
  main checkout "also trusts the same path in its git worktrees"; `settings.toml` — `env_cache` off by
  default, never holding a `redact = true` config unless `env_cache_redacted` is on (fetched).
* The trial (run).
* Reviews, 2026-11-10. `/code-review` (high): nine findings; five fixed (an empty item passing `paper:test`,
  no terminal, a network failure blamed on the pair, the service name unchecked, the README overstating who
  can read the item), the two permission rules recorded as open, two left as they are. Second reader
  (`evidence-verifier`): every quote held and it re-ran the trials; it corrected "only `paper:test` reads the
  items", found the PK/AK hint (now an entry refusal) and had "Always Allow" labelled. All fixed above.
* Second reader (`evidence-verifier`) of the split, 2026-11-10: nothing missing or changed in meaning, no
  citation wrong; it corrected the departures sentence and the review line's scope.
