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
`adopt-upstream` skill, as decided, stopped at such a prompt and waited for a person. The owner
decided on 2026-11-10 that every lane runs seamlessly, without repeated permission prompts, after asking
that the state-of-the-art landscape resolution proceed automatically. Which prompts stay?

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
| `scripts/bootstrap --install` or `--user-config`, `scripts/automation install`/`uninstall`, `git push`, a direct `gh` write (Confirmation) | asks | reach outside the checkout or publish; landing is `scripts/land` |

Where the rules differ from what was decided:

* **`uv sync --locked` still asks.** Rules go "deny, then ask, then allow", and "a matching ask rule
  prompts even when a more specific allow rule also matches". A pattern has only `*`, so no `ask` rule
  matches `uv sync` without `--locked` and misses `uv sync --locked`. A lane rarely needs it: `uv run
  --locked`, allowed for ruff, ty and pytest, syncs the environment from the lock (inexactly) first.
* **Not every unpinned `uv add` asks.** A bare name holds nothing to match: alone it is neither allowed
  nor asked (Manual mode prompts, bypass mode runs it), and next to an exact pin, as `uv add a==1 b`, it
  is allowed; so is a local path. A strict `>` is not matched, since `*>*` would match `2>&1`, nor
  `==1.*`, since `*` is always a wildcard. `-U` and `-P` ask only as their own word, so a name such as
  `types-PyYAML` or `Flask-UUID` is allowed, and so are `-qU` and `-qP`, a flag bundled after another;
  `-r`, `-i` and `-f` ask only with a space before their value, and `--requirement` and
  `-c`/`--constraints` match no rule, so beside an exact pin each of those is allowed.
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
* Bad: 302 `ask` rules where there were 14. Each is asserted.

### Confirmation

`scripts/test-hooks` checks 440 command forms: 21 run unprompted, 338 ask, 67 are neither, 13 are
spellings no rule matches and 1 is a bundled flag the `allow` rule takes (below). Its matcher follows the
docs' wildcard rules, in 12 cases: 11 from six rows of their table, one from the `:*` note. Deleting any
`ask` rule fails the test, as does deleting any `allow` rule for a one-pin resolve, a pinned `uv add` or
`scripts/land` but the bare `Bash(scripts/land)` pair, which `Bash(scripts/land *)` covers. The settings
before the `gh` rules fail 229 cases: 222 `gh` forms, 6 exact pins whose name holds `-U`, `-P` or
`-index`, and `uv lock -P Flask-UUID` (below); before the previous change's rules, 28 under that change's
test: 25 for want of the rules for `scripts/bootstrap --user-config` and the `mise exec --` forms of a
full resolve, the installers and the push, 3 for want of the ` *` on `scripts/automation
install`/`uninstall`. A running session reloads `permissions` on save, from its own primary working
directory, so a lane gets these rules once its worktree holds them.

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

Direct `gh` writes publish, as a push does, so they ask, bare and under `mise exec --` (2026-11-10): `gh
pr` merge, create, close, review, comment, edit, ready, reopen, lock, unlock, revert and update-branch;
`gh issue` create, close, lock, comment, edit, reopen, delete, transfer, pin, unpin, unlock and develop,
`artifact` create, edit and delete; `gh repo` edit, create, delete, archive, unarchive, rename, fork and
sync, `deploy-key` add and delete, `autolink` create and delete; `gh release` create, edit, delete,
delete-asset and upload; `gh label` create, edit, delete and clone; `gh secret` and `gh variable` set and
delete; `gh workflow` run, enable and disable; `gh run` rerun, cancel and delete; `gh gist` create, edit,
delete and rename; `gh cache delete`; the built-in aliases of these (`new` for create in `pr`, `issue`,
`repo`, `autolink`, `release` and `gist`, `remove` for delete in `secret` and `variable`); and `gh api`
with `-X` or `--method` naming a method that starts with P or D in either case, written `-X M`, `-XM`,
`-X=M`, `--method M` or `--method=M`: gh upper-cases the method, so this covers POST, PUT, PATCH and
DELETE in any case. So do `gh alias set` and `import` (an alias is a way round every rule), `gh extension
install` and `upgrade`, also as `extensions` and `ext` (they download and run code, as the installers do),
`gh auth login`, `logout`, `refresh` and `setup-git` (they change credential state), and `gh auth token`
and `gh auth status` with `-t` or `--show-token`, which print the GitHub token into an agent's context,
against AGENTS.md's "use no credentials" (ADR 0018). Bare `gh auth status`, which `scripts/land` and lanes
use to check the sign-in, does not ask. The reads of those groups (list, view, get, download, checks,
diff) match none. `gh issue develop --list`, a read, asks with its group, as does a `GET` whose field
value holds `-X P` or the like, and `gh auth status --template`. Runs with the same setup, and an empty gh
config folder so that nothing that ran could reach GitHub:

* Under the `gh` rules alone, in `bypassPermissions`, one command for each rule that only it matches, but
  for nine whose siblings ran (`gh extensions install`, `gh ext upgrade`, `gh auth logout`, `gh auth
  setup-git` and five `mise exec --` forms of the same kind), and three with `2>&1`, `> /dev/null` or `cd
  . &&`: each was refused, in three runs as the rules were added and a fourth for the eight lowercase `gh
  api` forms. Under the settings before them, `gh pr merge`, `gh api -X POST`, `mise exec -- gh release
  create` and `gh label create` ran and stopped at gh's sign-in.
* Quotes: `-X 'POST'`, `-X "POST"`, `-X ''POST`, `--method 'delete'`, `-X='PATCH'`, a quoted
  method under `mise exec --` and `gh 'pr' merge` were refused: Claude Code matched them as the shell
  reads them, which the docs do not state. This test's matcher reads quotes as text.
* Not caught, run: `gh run view` and `list`, `gh pr view`, `list`, `checks` and `diff`, `gh api` with no
  method and with `-X GET`, `--method GET` and `-X get`, `gh issue list` and `view`, `gh auth status`
  bare, with `--json hosts` and with `-a`, `gh release list` and `view`, `gh label`, `secret`,
  `variable`, `workflow`, `cache`, `gist`, `alias` and `extension list`, `gh variable get` and `gh repo
  view`, with `mise exec --` before eight of them, ran with no prompt.
* `scripts/land` itself, in a throwaway git repository with no remote and a stub `scripts/ci`, under the
  whole `permissions` object: bare (refused for want of a note), `--reviewed "..."` (landed locally) and
  `--pr 5` (refused: no origin) ran with no prompt in `default` and `bypassPermissions`, and
  `./scripts/land --reviewed` and `--pr` in `default`. A script that runs `mise exec -- gh pr merge`
  inside ran with no prompt: a rule reads the command Claude writes, not what it starts. No script here
  calls `gh auth token`.
* Stated limits, run: `gh api` with `-f`, `-F` or `--input` and no method POSTs, and no rule tells it from
  `gh api -X GET ... -f` or `gh api graphql -f query=...`, which `scripts/drift` runs as reads; an `allow`
  cannot carve them out (above). A flag before the subcommand, `gh -R o/r pr merge 1` or `gh pr -R o/r
  merge 1` (gh parsed both, given `pr view` in a shell), a bundled shorthand, `gh api -iX POST` or `gh
  auth status -at`, `mise x -- gh` and `sh -c 'gh ...'` ran too, as would an alias already set. A rule
  with text before `gh`, such as `Bash(*gh *pr merge*)`, would reach some of these and ask for any command
  that holds the words, a commit message included; not taken. The groups not named above that write
  (`discussion`, `project`, `codespace`, `ssh-key`, `gpg-key`, `skill`, `agent-task` and others) have no
  rule, nor do `gh issue artifact new`, `gh auth switch` and the hidden `gh auth git-credential`, which
  prints the token to git as its credential helper (so does `git credential fill`), and `gh copilot`,
  which runs the Copilot CLI.

The ask rules are a convenience against accidents, not a security boundary: the stated limits above get
past them.

Considered and not taken (2026-11-10): a guard inside the scripts that publish or install, refusing a
run that is not the owner's. A TTY check or an owner variable would change the installers for the owner
and for the self-tests; any variable a test can set, a lane can set; the owner decided on 2026-11-10
that nothing in the workflow blocks by failing (ADR 0020: "Workflow and quality come before added layers
of defence"); and the ask rules are there to stop an accident. The spellings they cannot hold
are recorded here, for a lane's brief to name as forbidden, and AGENTS.md says that a lane stopped by a
prompt does not look for another route.

Flags and names (2026-11-10). `*-U*`, `*-P*` and `*-index*` asked for an exact pin whose name holds the
text: `uv add types-PyYAML==6.0.12`, its `mise exec --` form, `Flask-UUID==0.2` and
`sphinx-index-plugin==1.0` were refused under the settings as they were, in `default`, while
`httpx==0.27.2` ran. A flag is its own word and a name cannot start with `-`, so `-U` and `-P` now ask
first or after a space (`uv add -U*`, `uv add * -U*`, the same for `-P` and for `uv lock -U`), and the
index rules name the long flags (`--index`, which covers `--index-url` and `--index-strategy`,
`--default-index` and `--extra-index`). Run in `default` under the whole `permissions` object: those four
pins, `--package` before one and two such pins under `mise exec --` ran with no prompt; `-U` and `-P idna`
after the pin, `-Pidna`, `--extra-index-url` and `--index-strategy` were refused. Under the uv rules
alone, in `bypassPermissions`: `-U` and `-P` first, `--index`, `--default-index`, `uv lock -U` and `uv
lock --offline -U` were refused; `uv lock -P Flask-UUID` ran, as did `uv add -qU httpx==0.27.2` (and `-qP`
would), a stated limit: a short flag bundled after another is not its own word, so that upgrade is allowed
and runs unasked.

Still UNVERIFIED: the real `uv add`, `uv sync`, `uv lock` and `scripts/canon resolve` forms not named
here (stubs and an absent `pyproject.toml` stood in for the commands), quoting beyond the forms above, and
an Orca session, where its plugin can approve a call (above).

### Standing pre-approval of 2026-11-11: end-to-end automation of what is adopted

The owner's standing instruction of 2026-11-11 pre-approves, as a class, end-to-end automation of what the
repository has already adopted:

* reinstalling the machine layer and applying settings from main;
* updating the adopted runtimes;
* paper end-to-end runs on both paper accounts, at any hour, by a lane, a coordinator or a scheduled
  agent;
* simulation runs.

As before, it does not cover:

* entering or creating a credential or an account;
* anything billed;
* a tool not yet adopted;
* repository settings and visibility;
* anything that touches live trading.

It is honoured without loosening what protects main:

* **The maintenance agent installs only from main** (ADR 0065, addition of 2026-11-11). It runs an
  installer only when the main checkout is at origin/main with no tracked change. So what it puts on the
  machine has passed main's checks, independent review and sign-off.
* **A coordinator in the main checkout may run the installers and the settings step directly**
  (`scripts/automation install`, `scripts/bootstrap --user-config`), when that checkout is on main at
  origin/main. That is the same code the agent would install at its next round. It still meets the
  installers' prompt, which stays the owner's to answer and is not routed around; the pre-approval
  says the owner expects to answer it yes there.
* **A worker in its own workspace still does not run an installer from its branch.** Its branch has not
  passed main's gate, so an install from it would put code no one has reviewed on the machine, under
  every session's settings. That is why the `ask` rules on the installers stay. Paper runs meet no
  prompt: `mise run paper:test` and `mise run paper:session` match no `ask` rule.

No permission rule changes. The prompts that remain each guard a step that should not run unasked:

* a re-resolve of every pin, because a pin moves one reviewed change at a time;
* `uv sync`, because it can rewrite `uv.lock`;
* the installers, which would run a worker's branch on the machine;
* a direct push or `gh` write, because landing goes through `scripts/land`, which runs the gate.

A rule cannot tell main's checkout from a worker's (`*` only, see above), so both meet the same prompt.

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
  not the target" (fetched). Re-read 2026-11-10: "A deny or ask rule matches past any leading
  assignment"; the page says nothing of quotes in rule matching (fetched).
* https://github.com/cli/cli at `v2.103.3`, the `gh` pinned here — `pkg/cmd/api/api.go`: `--method`/`-X`
  defaults to `GET`, and `method = "POST"` when the method was not passed and there are fields or an
  input file; `pkg/cmd/api/http.go`: `http.NewRequest(strings.ToUpper(method), ...)` (fetched). `gh api
  --help`: "The default HTTP request method is `GET` normally and `POST` if any parameters were added",
  and the subcommands of each group from `gh help` (read, gh 2.103.3). `pkg/cmd/auth/status/status.go`:
  `-t`/`--show-token` "Display the auth token", and the token is left out otherwise (fetched).
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
* Independent code review, 2026-11-10 (run): it found an empty id followed by a word, the short and index
  flags, `uv lock --upgrade` and the redirect `*>*` caught; a second pass, `-r`. Second reader, the same
  day (`evidence-verifier`): every quotation held; it corrected what `uv run --locked` is allowed for and
  the count of the docs' table, named the unstated gaps (a URL without `@`, `uv lock -P`, `Flask-UUID`, a
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
  this record then left open; the `gh` rules above close it.
* `gh` rules, 2026-11-10: that they ask, and that no guard goes inside the scripts, was the coordinator's
  decision under this record's rule for what publishes. Code review (high, run): it found the built-in
  aliases, `gh issue artifact`, `gh api -iX`, `gh alias set`, `gh extension install`, the `gh auth` token
  and sign-in commands and the AGENTS.md sentence unaddressed, and the brief sentence unsupported; each is
  a rule, a stated limit or a correction above. Second reader (`evidence-verifier`, the same day): the
  counts, the gh source and the docs quotes held; it found eight lowercase `gh api` rules with no command
  only they matched, a quote claim resting on a form that holds `-X P` as text, a read list and the ADR
  0020 reason wider than the runs and the record, and the same gaps; all are fixed above. Its second pass
  found the 229 failures misattributed, `issue artifact new`, `auth switch` and `auth git-credential`
  uncovered, `--requirement`, `-c` and attached values unstated, the ADR 0009 and ADR 0020 citations and
  the AGENTS.md sentence too wide, and three run claims wider than the runs; each is corrected or stated
  above, with no rule added. A later reading of the rules found `types-PyYAML==6.0.12` asked for (above).
* https://docs.astral.sh/uv/reference/cli/ — the `uv add` flags, among them `-U`/`--upgrade`,
  `-P`/`--upgrade-package`, `--index`, `--index-url`/`-i`, `--default-index`, `--extra-index-url` and
  `--index-strategy` (fetched 2026-11-10; uv 0.13.7 here).
