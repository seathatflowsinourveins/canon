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
| `scripts/bootstrap --install`, `scripts/automation install`/`uninstall`, `git push` | asks | reach outside the checkout; landing is `scripts/land` |

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
* Bad: 59 `ask` rules where there were 14. Each is asserted.

### Confirmation

`scripts/test-hooks` checks 86 command forms: 9 run unprompted, 74 ask and 3 are neither. Its matcher
follows the docs' wildcard rules, in 12 cases: 11 from six rows of their table, one from the `:*` note.
Deleting any `ask` rule or new `allow` rule fails the test, and so does re-adding any removed rule but
`Bash(uv sync)`, which `Bash(uv sync *)` covers; so do the settings as they were. No real `uv add`, `uv
sync`, `uv lock` or `scripts/canon resolve` was run to see a prompt: the real matcher on these forms, on
compound commands, redirections and quoting is UNVERIFIED. A running session reloads `permissions` on
save, from its own primary working directory, so a lane gets these rules once its worktree holds them.

## Evidence

Read 2026-11-10. `fetched`: downloaded whole with `curl` and read, not summarised.

* https://code.claude.com/docs/en/permissions.md — "Rules are evaluated in order: deny, then ask, then
  allow."; "a matching ask rule prompts even when a more specific allow rule also matches the same call";
  "A `*` in a Bash rule matches any text, including spaces"; "A `*` at the end, with a space before it,
  also matches the bare command"; the table of patterns; "Deny and ask rules apply when any subcommand
  matches them"; runners "such as `direnv exec`, `devbox run`, `mise exec`, `npx`, and `docker exec` are
  not in the list"; "isn't a security boundary around the program"; project allow rules wait for "the
  workspace trust dialog"; the two sentences on a mod without managed settings (fetched).
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
