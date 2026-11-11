---
status: accepted
date: 2026-11-10
decision-makers: repo owner (instruction, 2026-11-10); coordinator (Claude)
review-by: 2027-02-08
---

# Permission prompts: a lane's own steps run without one; what rewrites everything or leaves the checkout still asks

## Context and Problem Statement

ADR 0020 rule 4 put `ask` rules in front of a pin move, an install, a push and the two installers, and
called each prompt the owner's to answer. An `ask` rule prompts in every permission mode, and the
sessions here run in parallel, mostly with no one watching. A worker adopting a library through the
`adopt-upstream` skill, as decided, stopped at such a prompt and waited for a person. The owner's
instruction, 2026-11-10: "make sure every lanes are seamless without consistant premission frictions",
after "please proceed with the sota landscape clean resolution automatically". Which prompts stay?

## Decision Drivers

* A lane that follows a skill as written does not wait for a person (owner, 2026-11-10).
* A rule is held where it cannot be skipped, not by a prompt someone may not see (ADR 0017, ADR 0020).
* What rewrites every pin or the whole lock, or reaches outside the checkout, still asks.
* No hook runs before a tool call (ADR 0020 rule 1); the vendor's rule syntax is used as documented.

## Considered Options

1. Leave the prompts.
2. Remove every `ask` rule.
3. Remove the prompts a lane hits in its normal work and keep the rest.
4. Hold the rules with the sandbox or auto mode instead.

## Decision Outcome

Chosen: **option 3**, in `.claude/settings.json`. Each form is listed bare and, where the repo runs it
so, with `./scripts/` or `mise exec --` in front.

| Command form | Now | Why, and what holds it |
|---|---|---|
| `scripts/canon resolve <id>` | allowed | `scripts/canon lint` (lock against registry), the review before landing (ADR 0017, ADR 0019), the ruleset on `main` |
| `uv add name==version`, with `--package` or more pins | allowed | `uv lock --check` in `scripts/ci`, the review (a `pyproject.toml` change needs a note), the ruleset |
| `scripts/canon resolve` with no id, a flag, an empty quoted id or a `$` variable | asks | re-resolves every pin |
| `uv add` with `>=`, `<`, `~=`, `!=`, `@`, `://`, an upgrade flag, an index, `--find-links`, a requirements file, `--project` or `--directory` | asks | not one exact pin in this checkout, or moves other pins |
| `uv lock` with `--upgrade` or `-U` | asks | rewrites every pin |
| every `uv sync`, `--locked` included | asks | can rewrite `uv.lock`; see below |
| `scripts/bootstrap --install` or `--user-config`, `scripts/automation install`/`uninstall`, `git push` | asks | reach outside the checkout; landing is `scripts/land` |

Where the rules differ from what was decided:

* **`uv sync --locked` still asks.** Rules go "deny, then ask, then allow", and "a matching ask rule
  prompts even when a more specific allow rule also matches". A pattern has only `*`, so no `ask` rule
  matches `uv sync` without `--locked` and misses `uv sync --locked`. A lane rarely needs it: `uv run
  --locked`, allowed for ruff, ty and pytest, syncs the environment from the lock (inexactly) first.
* **Not every unpinned `uv add` asks.** A bare name holds nothing to match: alone it is neither allowed
  nor asked (Manual mode prompts, bypass mode runs it), and next to an exact pin, as `uv add a==1 b`, it
  is allowed; so is a local path. A strict `>` is not matched, since `*>*` would match `2>&1`, nor
  `==1.*`, since `*` is always a wildcard. `*-U*` also asks for a name such as `Flask-UUID`.
* **Stricter than before:** the `mise exec --` forms (`mise exec` is not a wrapper Claude Code strips, so
  `mise exec -- uv sync` matched no rule), `uv lock --upgrade` and the extra `uv add` flags now ask.
  `uv lock -P name`, which moves one pin, matches nothing, as before.

Other spellings get past, as with every rule (an absolute path, `sh -c`, `mise x`, an id from a command
substitution): a rule "isn't a security boundary around the program". Allow rules here wait for folder
trust and do nothing in `bypassPermissions`, Orca's mode (ADR 0018), where only `ask` rules decide.

Supersedes ADR 0020 rule 4 in part: a single pin move and an exactly pinned `uv add` no longer ask; the
rest of ADR 0020 stands, "do not route around a prompt" included. Option 1 is what stopped the worker.
Option 2 would let a full re-resolve, a lock rewrite, the installers and a direct push run unasked.
Option 4 is ADR 0025's question; this record leaves it there. ADR 0025 expected `uv sync --locked`
to be freed here; it is not (above).

### Consequences

* Good: `adopt-upstream` runs from registry entry to landing with no one at the keyboard.
* Bad: a pin move or a new dependency is caught after it runs, by the checks, the review and the ruleset,
  not before. A mistaken one changes a working tree; it cannot reach `main` unreviewed.
* Bad: the weekly task (ADR 0026) is saved in bypass mode, so a one-pin resolve and a pinned `uv add` run
  there unasked; "never move a pin" rests on its prompt alone for those, as for an edit or a commit.
* Bad: an empty id is caught only as typed. `scripts/canon` reads one as "resolve all"; refusing it there
  would hold every spelling, and is a change to that script, not to these rules.
* Bad: ADR 0023 found that this machine has none of the setups that load Claude Code's built-in guard,
  so a mod "can approve a call that a deny rule refuses", with no prompt "when the mod approves a call
  that an ask rule ... would prompt for". The `ask` rules hold in Orca sessions only while Orca's plugin
  handles no tool call. This record does not change that.
* Bad: 82 `ask` rules where there were 14. Each is asserted.

### Confirmation

`scripts/test-hooks` checks 124 command forms: 15 run unprompted, 102 ask, 4 are neither and 3 are
spellings no rule matches (below). Its matcher follows the docs' wildcard rules, in 12 cases: 11 from six
rows of their table, one from the `:*` note. Deleting any `ask` rule fails the test, as does deleting any
`allow` rule for a one-pin resolve, a pinned `uv add` or `scripts/land` but the bare `Bash(scripts/land)`
pair, which `Bash(scripts/land *)` covers. The settings as they were fail 28 cases: 25 for want of the
rules for `scripts/bootstrap --user-config` and the `mise exec --` forms of a full resolve, the
installers and the push, 3 for want of the ` *` on `scripts/automation install`/`uninstall`. A running
session reloads `permissions` on save, from its own primary working directory, so a lane gets these rules
once its worktree holds them.

The real matcher, 2026-11-10: Claude Code 2.1.321, `claude -p` in a temporary git folder with stub
scripts and a local bare remote, `--setting-sources local` and a `--settings` file holding only the rules
under test (one command's forms, or two subcommands of one script), in `bypassPermissions`; a prompt
shows as an entry in the run's `permission_denials`, and the transcript showed each command run as
written.

* With no rule, `scripts/bootstrap --user-config`, which writes the owner's Claude and Codex config, ran;
  `Bash(scripts/bootstrap --user-config *)` asks for it, its `./` form and its `2>&1` form.
* `Bash(scripts/automation install)` asked for the command followed by `2>&1`, `2>/dev/null` or
  `> install.log`, each of which ran with no rule: Claude Code matches past a redirection, which this
  test's matcher, reading it as text, does not. It let `scripts/automation install now` run, which
  installs, since the script reads only its first word; the ` *` form asks for it.
* `Bash(git push *)` let `mise exec -- git push origin main` run and push; `Bash(mise exec -- git push *)`
  asks. So with `mise exec --` before the installers: each ran under the bare rule and asks under its own;
  and under the whole set `mise exec -- scripts/canon resolve` and `mise exec -- ./scripts/canon resolve
  --prune` ask, while `mise exec -- scripts/canon resolve alpaca-py` runs in `bypassPermissions` (no
  rule allows it, so `default` would prompt). The empty-id and `$` forms were not run there.
* `cd . && scripts/automation install` asked, as "any subcommand" says.
* Stated limits, run: `mise x -- git push origin main`, `sh -c 'scripts/automation install'` and
  `git -C . push origin main` each ran under the rules above. Only a rule naming each spelling would
  reach them, and the spellings are open-ended; they stay limits, as "Other spellings get past" says.
* With the whole `permissions` object, in `bypassPermissions` and in `default`: `scripts/canon resolve
  alpaca-py` and its `./` form, `uv add httpx==0.27.2`, `mise exec -- uv add`, with and without
  `--package`, `scripts/land`, `scripts/land --reviewed "..."` and its `./` form and `scripts/land --pr 5`
  ran with no prompt (the `mise exec -- uv add` runs then stopped at mise, with no `uv` set in that
  folder); in `default` an unlisted `scripts/automation bogus` was refused.

Still UNVERIFIED: the real `uv add`, `uv sync`, `uv lock` and `scripts/canon resolve` forms not named
here (stubs and an absent `pyproject.toml` stood in for the commands), quoting, and an Orca session, where
its plugin can approve a call (above).

## Evidence

Read 2026-11-10. `fetched`: downloaded whole with `curl` and read, not summarised.

* https://code.claude.com/docs/en/permissions.md — "Rules are evaluated in order: deny, then ask, then
  allow."; "a matching ask rule prompts even when a more specific allow rule also matches the same call";
  "A `*` in a Bash rule matches any text, including spaces"; "A `*` at the end, with a space before it,
  also matches the bare command"; the table of patterns; "Deny and ask rules apply when any subcommand
  matches them"; runners "such as `direnv exec`, `devbox run`, `mise exec`, `npx`, and `docker exec` are
  not in the list"; "isn't a security boundary around the program"; project allow rules wait for "the
  workspace trust dialog"; the two sentences on a mod without managed settings (fetched). Re-read
  2026-11-10: "A Bash rule matches the command text Claude writes, after Claude Code splits compound
  commands and strips wrappers"; on redirects, "A rule such as `Bash(git commit *)` allows the command,
  not the target" (fetched).
* https://code.claude.com/docs/en/cli-reference.md — `--setting-sources`, `--settings` and
  `--permission-mode`; https://code.claude.com/docs/en/headless.md — "the final result message lists them
  in `permission_denials`" (fetched). The `claude -p` runs above (run).
* https://code.claude.com/docs/en/settings.md — "Claude Code watches your settings files and reloads them
  when they change, so it applies most edits to the running session without a restart, including edits
  to `permissions`"; "Claude Code reads the shared `.claude/settings.json` from the session's primary
  working directory" (fetched).
* https://code.claude.com/docs/en/permission-modes.md — "Allow rules have no effect in
  `bypassPermissions`"; "Tools matched by an explicit ask rule" under "Actions no mode auto-approves"
  (fetched; those two sections read).
* https://docs.astral.sh/uv/concepts/projects/sync/ — "when `uv run` is used, the project is locked and
  synced before invoking the requested command"; with `--locked`, "If the lockfile is not up-to-date, uv
  will raise an error instead of updating the lockfile"; `--no-sync` skips the sync (fetched).
  https://docs.astral.sh/uv/reference/cli/ — the `uv add` and `uv lock` flags named above (fetched).
* `scripts/canon` — `resolve <id>` merges one pin; no id, `--prune` or an empty id re-resolves all.
  `scripts/land` — no-note paths exclude `pyproject.toml` and `canon/upstreams.lock.json`. `orca.yaml` —
  `uv sync --locked` through `mise exec`. ADR 0018, 0020, 0023 and 0026 rule 2 ("The weekly task was
  saved in bypass-permissions mode") (read). `scripts/test-hooks` and the deletion run (run).
* The owner's instruction, as given to this session on 2026-11-10, and the worker session that stopped
  at the prompt — not re-observable (UNVERIFIED).
* Independent code review, 2026-11-10 (run): it found an empty id followed by a word, the short and
  index flags, `uv lock --upgrade` and the redirect `*>*` caught; a second pass, `-r`. Second reader, the same day
  (`evidence-verifier`): every quotation held; it corrected what `uv run --locked` is allowed for and the
  count of the docs' table, named the unstated gaps (a URL without `@`, `uv lock -P`, `Flask-UUID`, a
  command substitution), and asked for the weekly run's consequence and ADR 0025's status. All are fixed
  above or stated where a rule cannot express them.
* Second reader's sweep, 2026-11-10 (run, with this test's matcher): `scripts/bootstrap --user-config`,
  `scripts/automation install 2>&1` and `mise exec -- git push origin main` matched no rule. The first and
  third held on the real matcher and now ask; the second did not hold there (above). That
  `scripts/bootstrap --user-config` and the `mise exec --` forms of the push and the installers ask was
  the coordinator's decision, 2026-11-10, under this record's rule for what leaves the checkout.
* Second reader (`evidence-verifier`), 2026-11-10: every quotation and count held; it found two run
  claims wider than the runs and the 16 failures misattributed. The missing runs (`2>/dev/null` with no
  rule, `mise exec --` before the bootstrap forms and `uninstall` under the bare rules) were made and
  ran unasked; the attribution is fixed above. Code review, the same day (run): it found the `mise exec
  --` form of a full resolve unasked, now asked, and named `gh` writing to GitHub with no rule, which
  this record does not decide and leaves open.
