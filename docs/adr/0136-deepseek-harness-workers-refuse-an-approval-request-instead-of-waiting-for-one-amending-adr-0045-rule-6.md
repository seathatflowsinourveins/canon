---
status: accepted
date: 2026-11-13
decision-makers: the owner, approving on 2026-11-12 a plan to make DeepSeek Harness dependable runtime workers for the lead; a build lane (Claude Opus 5.5) chose the means under the owner's delegation (ADR 0074)
review-by: 2026-12-13
---

# DeepSeek Harness workers refuse an approval request instead of waiting for one, amending ADR 0045 rule 6

## Context and Problem Statement

Orca starts every DeepSeek Harness worker as `dsh-tui .`, with no flag or environment of its own (ADR 0043, ADR
0045). Each fresh session then takes the sandbox `workspace-write` and the approval policy `ask` from the profile's
bundles. A worker's first write outside its worktree therefore asks for wider access, and nobody answers a prompt in
an Orca terminal. In ADR 0043's builder trial, `git add` reached the worktree's index under the main repository's git
directory, outside the sandbox root, and the worker waited. The owner's direction of 2026-11-12 is that these
workers be dependable: a worker finishes or fails, and never waits unseen.

## Decision Drivers

* An unattended worker must not stall on a prompt (ADR 0043, ADR 0045).
* Keep the sandbox: `workspace-write` holds writes to the workspace and the temp areas, so a builder cannot commit
  (ADR 0079) or write outside its tree.
* One profile serves every dsh-tui launch, and Orca's `dsh-tui .` takes no per-task overlay (ADR 0045 rule 6, ADR
  0086), so whatever is chosen binds every dsh-tui launch on this machine.
* Use the upstream's documented settings, set the way ADR 0045 rule 6 already sets two rows.

## Considered Options

1. **Leave approval at `ask`.** Workers keep stalling on the first escalation.
2. **`DSH_PERMISSION_MODE=danger-full-access` in Orca's environment for dsh.** The bundle then sets approval to
   `never` itself, but the sandbox is gone and the worker's terms are text only.
3. **An `approval` row with policy `never`, alone.** The base bundle's permission presets infer their default from
   the composed sandbox and approval, and no stock preset is `workspace-write` with `never`. Run here, the presets
   plugin did not load.
4. **That row, plus a `permission` row in which each of the three stock presets keeps its sandbox and refuses.**
   dsh-TUI seeds each later fresh session with the last preset a `/permission` pick or a Shift+Tab mode reached, so
   one move to `danger-full-access` in any dsh-tui terminal would lift the sandbox from every later worker.
5. **That row, plus a `permission` row with only the two sandboxed stock presets, each refusing.**
6. **That row, plus a fourth preset (`workspace-write`, `never`) as `defaultPreset`, with the stock presets still
   asking.** One remembered pick of `workspace-write` in any tab would bring the stall back to every worker.
7. **The experimental auto-review answerer.** Its preset pairs `danger-full-access` with `ask`, so it gives up the
   sandbox, and a call it refuses still goes to a person as a prompt.

## Decision Outcome

Chosen: **option 5**. The plan assumed one `approval` row (option 3); running it showed the presets plugin then
refuses to load. This record's first draft took option 4, until the code review found the remembered path to
`danger-full-access`.

1. **The rows.** `home/dsh/review.patch.yml` gains two rows inside the canon block. `approval` takes `policy:
   never`, which rejects every request before any answerer and does not auto-approve. `permission` repeats the base
   bundle's `read-only` and `workspace-write` presets with approval `never`, leaves out `danger-full-access`, and
   sets no `defaultPreset`. A user-patch row replaces a bundle row's config as a whole. From the two rows the
   presets plugin infers `workspace-write` and pins it, with approval `never`, into each fresh session.
2. **No pick lifts the sandbox or brings a prompt back.** dsh-TUI seeds each fresh session with the last preset a
   `/permission` pick or Shift+Tab reached (`~/.dsh-tui/permission.json`) but skips one the table does not offer,
   and refuses a Shift+Tab mode that matches no preset: `full` (`danger-full-access`, `never`) and the two that ask.
   The exception is the opt-in auto-review plugin, whose Auto preset (`danger-full-access`, `ask`) lasts one session.
3. **`DSH_PERMISSION_MODE`** still selects the sandbox; approval is `never` under every value. `read-only` and
   `workspace-write` match a preset. `danger-full-access` matches none, so a launch with it runs without the sandbox
   and without the presets plugin, which fails to load with a warning. Orca sets none.
4. **Bootstrap.** `scripts/bootstrap` treats `approval` and `permission` as it treats the other two ids. A row for
   either outside the canon block is a conflict, merged by hand. `--check` names a canon block that differs from
   the repository's, and `--install` replaces it where it stands.
5. **Reach.** The rows bind every dsh-tui launch, Orca workers and the owner's own tabs alike; a running session
   keeps the rows it started with. No other profile changes: the shipped `headless` profile asks (rehearsed), and
   whether ADR 0086's build configuration on it waits or fails on a request is `UNVERIFIED`.
   2026-11-13: at `dsh-v0.2.2-rc.2`, `dsh-base` composes `fs-observation-policy` with `disabled: true` (its
   `cordis.patch.yml` L292-294), so `headless`, `review-acp` and `build-acp` run without read-before-write while
   `tool-fs` stays on (each profile's `--dump-config` as `home/dsh/profiles.json` records it, rehearsed
   2026-11-13). None is dispatched, and a row restores the guard before any lane dispatches one.
6. **Briefs.** A brief to a dsh worker that runs `uv` sets `UV_CACHE_DIR` under the temp area, since uv's default
   cache is outside the sandbox, unless the smoke test below shows uv runs without it.
7. **AGENTS.md is unchanged.** Its condition, `--check` showing the review patch ok, covers these rows; between a
   landing that changes the block and the owner's `--install` the row reads NOT SET, and no dsh worker starts.

This amends ADR 0045 rule 6, which named two conditions; refused approvals are a third. It also amends ADR 0034
rule 4, which left DeepSeek Harness at `ask` and named this patch row as untried. It decides nothing about plugins,
the engine version or which profile Orca starts.

### Consequences

* Good: an escalation fails at once, with a tool error the worker can report. `git add` and `git commit` in a
  linked worktree are refused, so a builder's no-commit term holds by construction.
* Bad: a command that writes outside the worktree and the temp areas fails, uv's default cache among them.
  Amended 2026-11-13: a worker's brief therefore names each tool by its literal path in the tree's own `.venv`,
  with no uv, which the sandbox refuses at its cache under the home folder; mise is not needed once each tool is
  named by path. The forms, each run from the tree's root as `scripts/ci` runs its own, are
  `<tree>/.venv/bin/pytest`, `<tree>/.venv/bin/ruff check .`, `<tree>/.venv/bin/ruff format --check .` and
  `<tree>/.venv/bin/ty check`. For a project kept apart, such as `filings/` (ADR 0108), its `ty` and `pytest` run
  from that folder with its own `.venv`, as `scripts/ci` runs them. pytest keeps its default temporary folder under
  `$TMPDIR`, which the sandbox lets it write, so no test's folder lies inside a checkout (`docs/architecture.md`,
  "A git checkout above a temp folder") and a reader's tree shows no change. The coordinator builds each `.venv`
  before the worker starts, since the worker cannot. The shell tool makes an approval request only when the model
  passes `sandbox_permissions` with a `justification`, as the hint after a refusal invites, and a hook's `ask`
  decision goes to the same service; `never` rejects both before any answerer. Grants are one-shot and no row
  allows a command shape, so this adds no row. The profile also refuses to run `/bin/ps`, which is setuid root, so
  two cases of the whole suite fail inside it and pass outside it (in the Evidence); a brief that has a worker run
  the whole suite names them as the sandbox's, not the worker's.
* Bad: the owner's dsh-tui tabs lose approval prompts too, and from the source neither `/permission` nor Shift+Tab
  brings them back (not run). A tab that needs more is relaunched with `DSH_PERMISSION_MODE=danger-full-access`.
* Bad: a remembered `read-only` pick makes every later worker read-only, and a builder then fails at its first
  write. An owner's `sandbox-policy` row in the profile's patch sets the sandbox the presets are matched against,
  so one set to `danger-full-access` runs as rule 3 describes. A home-level `$DSH_HOME/cordis.patch.yml` would
  outrank these rows in every profile. None of the three is on this machine. Since 2026-11-13 the
  `dsh-tui worker defaults` row of `scripts/bootstrap --check` names each of them, and a remembered `/model`,
  `/effort`, `/preset` or `/kernel` pick: a `/kernel` pick other than `dsh` would start every worker on another
  backend, since Orca passes no `--backend`. It also names `DSH_PERMISSION_MODE`, `DSH_TUI_PRESET` or
  `DSH_TUI_BACKEND` set in the environment it runs in, each of which outranks the remembered pick. It cannot read
  Orca's own environment, which the workers take.
* Bad: the presets table is replaced as a whole. A preset that a later base bundle adds is not offered until this
  block follows it, and the next engine move re-reads the base rows.
* Bad: `scripts/bootstrap` finds an owner's row for one of the four ids by its whole `id` key, in block or flow
  style, with the key and the value each quoted or not; a key that only ends in `id`, such as `grid`, is another key.
  An id spelled another way YAML allows (a tag, an anchor or alias, an explicit `?` key, an escape, or the value on
  the next line) is not found. Such a row could restore `ask` while `--check` reads ok. The other way round, a row in
  a comment, or a key named `id` inside a row's `config`, reads as a conflict: `--install` then leaves the file alone
  and writes its block beside it, for the owner to merge.
* Unchanged: the sandbox holds writes only. The macOS profile allows by default and denies file writes, so it does
  not hold reads, the network, or plugins running in-process. The brief and the transcript check remain the
  controls there (ADR 0034).

### Confirmation

* `scripts/bootstrap --check` shows the dsh-tui review patch ok, with approval requests refused.
* `scripts/test-lanes`:
  * the current block after another row is ok;
  * outside the block, each of these is a conflict: an `approval` row, a flow-style `permission` row, either written
    with a quoted key, and a key with a space before the colon;
  * a row whose `config` has a `grid` or `uid` key set to `approval` or `permission`, quoted or not, is no conflict:
    beside the block it reads ok, and with no block `--install` appends the block after it;
  * the older two-row block is named as differing;
  * a patch with no block names the missing rows;
  * `--install` replaces the older block between the owner's rows, keeps a copy, and leaves the result alone on a
    second run;
  * with a quoted-key `approval` row and no block, `--install` leaves the file as it is and writes the block beside
    it as `review.patch.yml.canon-proposed`.
* A worker's brief gives each command in the literal forms above, from the tree's root, and the coordinator builds
  each `.venv` they name before the worker starts (amended 2026-11-13).
* The rehearsals on the installed engine, in the Evidence.
* **The smoke test.** After `--install`, the lead's deputy starts one Orca `--agent dsh` worker in a new worktree on
  this lane's brief. It must run a test file in the literal form above (amended 2026-11-13; it first said with
  `uv`), have `git add` refused with no prompt pending, have a write to `~/` refused, and reach `worker_done` within
  15 minutes. It is `UNVERIFIED` until run; its result goes below.

## Evidence

Read and run 2026-11-12 and 2026-11-13. Pinned sources were read with `scripts/canon src` at the locked commits:
dsh-TUI v0.17.0 (registry `dsh-tui`) and DeepSeek Harness `dsh-v0.2.1-rc.3` (registry `deepseek-harness`).

* https://github.com/ccch1mneyyy/dsh-TUI/blob/103834cb1000a84569a71646fec7162f18a058c8/cordis.patch.yml#L172-L186
  The `sandbox-policy` row (`DSH_PERMISSION_MODE`, else `workspace-write`, rooted at the start folder) and the
  `approval` row (`never` under `danger-full-access` or on Windows, else `ask`). Resolved.
* https://github.com/ccch1mneyyy/dsh-TUI/blob/103834cb1000a84569a71646fec7162f18a058c8/docs/cli.en.md#L27
  "DSH `approval: never` rejects approval requests; it does not auto-approve." Resolved.
* https://github.com/ccch1mneyyy/dsh-TUI/blob/103834cb1000a84569a71646fec7162f18a058c8/docs/configuration.en.md#L23-L27
  "A user configuration normally overrides an existing row by `id`", its "`config` block is replaced as a whole".
  Resolved.
* https://github.com/ccch1mneyyy/dsh-TUI/blob/103834cb1000a84569a71646fec7162f18a058c8/src/permissionPrefs.ts#L1-L14
  The stored preset is "the last permission preset the user switched to through `/permission` (picker or typed) or
  the Shift+Tab mode cycle"; "A value the running preset roster no longer offers is skipped, and an explicit
  `DSH_PERMISSION_MODE` deployment pin outranks the file." Resolved.
* https://github.com/ccch1mneyyy/dsh-TUI/blob/103834cb1000a84569a71646fec7162f18a058c8/src/dsh-adapter/channel/mode-permission-actions.ts#L388-L439
  The composition pins `permission/preset`, `sandbox/mode` and `approval/policy` into every fresh session; a
  remembered preset seeds one unless `DSH_PERMISSION_MODE` is set (L409), and is skipped when not on the mounted
  roster (L431-L437). L205-L215 and L274-L286: a `/permission` switch and a Shift+Tab mode each store the preset
  reached, never `custom` or `auto`. Resolved.
* https://github.com/ccch1mneyyy/dsh-TUI/blob/103834cb1000a84569a71646fec7162f18a058c8/src/dsh-adapter/channel/mode-roster.ts#L48-L55
  With no presets service mounted there is no roster, so a remembered preset is skipped then too. Resolved.
* https://github.com/ccch1mneyyy/dsh-TUI/blob/103834cb1000a84569a71646fec7162f18a058c8/src/sessionModes.ts#L18-L20
  The Shift+Tab modes `default` (`workspace-write`, `ask`), `plan` (`read-only`, `ask`) and `full`
  (`danger-full-access`, `never`); L54-L74, a table that names atoms maps a mode only to a preset with the same
  atoms. Resolved.
* https://github.com/ccch1mneyyy/dsh-TUI/blob/103834cb1000a84569a71646fec7162f18a058c8/src/dsh-adapter/channel/mode-permission.ts#L233-L255
  A static mode with no matching preset is refused in a session that holds a preset ("cannot safely clear
  permission identity"). Resolved.
* https://github.com/ccch1mneyyy/dsh-TUI/blob/103834cb1000a84569a71646fec7162f18a058c8/docs/auto-review-preset-adaptation.md#L80
  In Chinese, summarised here: the Auto preset binds `danger-full-access` with `ask`, and a refused call becomes an
  approval request to the user. Resolved.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/bundle/base/cordis.patch.yml#L259-L276
  The base `approval` row, and the `permission` row whose `read-only` and `workspace-write` presets ask, with no
  `defaultPreset`. Resolved; the installed engine's copy holds the same rows (bundled).
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/interaction/permission-presets/README.md#L30-L64
  `defaultPreset` is "required when composition defaults match no preset", and session creation applies the default
  preset's sandbox and approval. Resolved.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/interaction/permission-presets/src/index.ts#L222-L226
  The default is inferred from the composed sandbox and approval (`derive`, lines 348-360) and the constructor
  throws when none matches; `pinInitialPermission` (lines 428-443) sets both on a fresh session. Resolved; the
  installed build holds the same check (bundled).
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/interaction/user-approval/README.md#L36
  "`never` rejects every request deterministically before interactive dispatch — the strict headless stance for CI
  and unattended runs"; line 77, enforced inside the service before any answerer. Resolved.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/fs/fs-sandbox/README.md#L46
  "`workspace-write` allows a mutation only when the target canonicalizes under the workspace root or a platform
  temp area". Resolved.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/sandbox/sandbox-local/README.md#L79
  The Seatbelt profile "is allow-default with `(deny file-write*)` plus write allow-lists". Resolved.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/apps/cli/src/profile-boot.ts#L212-L217
  The home-level `$DSH_HOME/cordis.patch.yml` "outranks the per-profile layer". Resolved.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/bundle/headless/README.md#L55
  The shipped headless profile "disables the managed credential store"; "API-key routes use the launch
  environment, then `<invocation cwd>/.env`, then `$DSH_HOME/.env`". Resolved.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/shell/bash-sandbox/README.md#L78
  The executor "awaits confinement of each command's exact `['bash', '-c', command]` argv through
  `ctx.sandbox.confine()`, spawning the returned argv directly". Resolved; read 2026-11-13 for the amendment.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/sandbox/sandbox/src/roots.ts#L52-L55
  Under `workspace-write` the writable roots are the workspace root, `/tmp` and `tmpdir()`, each canonical. With
  `packages/sandbox/sandbox-local/src/profiles.ts` L51-58, the macOS profile is `(allow default)`,
  `(deny file-write*)`, and file writes allowed under those roots and to `/dev/null`. Resolved; read 2026-11-13 for
  the amendment.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/shell/tool-bash/src/index.ts#L482-L484
  The shell tool asks for approval only when the model passes both `sandbox_permissions` and `justification`, on
  any call, not only after a refusal. `packages/sandbox/sandbox/src/escalation.ts` L84-85 tells the model after a
  refusal that it may "retry this exact ${subject} once with sandbox_permissions"; L188-195 ask "Allow this
  operation with ${mode} permissions"; L203 answers a rejection with "it stays denied, so stop and explain instead
  of working around it". `packages/core/tools/src/index.ts` L1735-1760 sends a hook's `ask` decision, for any
  tool, to the same approval service. Resolved; read 2026-11-13 for the amendment.
* https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/interaction/user-approval/README.md#L154
  "Only one-shot grants exist": there is no `allow-always`, remembered rule or grant store, so no row can allow a
  command shape. `src/index.ts` L73 is what a `never` session tells the model ("do not request sandbox
  escalation"), and L275 returns `rejected` before any answerer. Resolved; read 2026-11-13 for the amendment.
* https://github.com/deepseek-ai/deepseek-harness/blob/38a6a5c965aa99ab525161efb7cc5cf1fe62fced/packages/sandbox/sandbox-policy/src/index.ts#L142
  Counter-evidence, from `dsh-v0.2.2-rc.3` (a pre-release published 2026-11-13, newer than the pin): a sandbox
  setting, `additionalWritableRoots`, adds writable roots (`packages/sandbox/sandbox/src/roots.ts` L89), so a
  profile row could hold uv's cache in place of the literal forms. It is not at the pin; a pin move weighs it.
  Fetched 2026-11-13 through GitHub's contents API at the tag's commit.
* The rehearsal (run 2026-11-12; this record's block again 2026-11-13): the installed engine (`dsh` 0.2.1-rc.3), a
  new `DSH_HOME` under the temp area, the shipped `headless` profile, no API key in the environment or either
  `.env`, task "Reply with OK". Each run stopped at `MISSING_CREDENTIAL` before any model call, and the session log
  it wrote was read.
  - Stock: `permission/preset workspace-write`, `sandbox/mode workspace-write`, `approval/policy ask`.
  - An `approval: never` row alone, as `--patch`: "warning: 1 entry did not activate", "permission
    (@deepseek-ai/dsh-permission-presets): Error: permission: composed sandbox and approval defaults match no
    preset; configure defaultPreset explicitly", and no permission event.
  - This record's block: every entry activated; `workspace-write`, `workspace-write`, `never`. With
    `DSH_PERMISSION_MODE=read-only`: `read-only`, `read-only`, `never`. With `danger-full-access`: the warning and
    error above, and no permission event.
* `dsh --profile dsh-tui --dump-config` (run, exit 0) on a copy of this machine's dsh-tui profile packages with this
  block as the whole user patch: against the older block only the `approval` row (`policy: never`) and the presets
  differ (both refuse; `danger-full-access` gone). This composes the plugins without loading them; the
  evidence-verifier's run of that copy did not finish within 120 s, so activation in the dsh-tui profile is
  `UNVERIFIED`. Without the plugin the two rows still give `workspace-write` and `never`, and no roster seeds a
  remembered preset.
* The literal forms' rehearsal (run 2026-11-13, for the amendment), with no model and no dsh. Each command ran as
  the engine runs one, `sandbox-exec -p <profile> -- bash -c <command>`
  (`packages/sandbox/sandbox-local/src/index.ts` L339 and L351), under the profile above for `workspace-write`,
  written out by hand from those lines, from the root of a Canon worktree whose `.venv` was built:
  * `mise exec -- uv run --locked pytest` on `tests/test_build_cases.py`: exit 2. uv could not take its lock:
    "Operation not permitted" at its cache under the home folder. `uv run` with no mise failed the same way, and
    `mise exec -- true` exited 0.
  * `<tree>/.venv/bin/pytest` on the same file, with pytest's default temporary folder: 31 passed. The locked
    pytest, 9.2.0, puts that folder under `tempfile.gettempdir()`, here `$TMPDIR`, unless `PYTEST_DEBUG_TEMPROOT`
    is set (`_pytest/tmpdir.py` L201-206, read in the tree's `.venv`).
  * `ruff check .`, `ruff format --check .` and `ty check` from the tree's `.venv`, over the whole tree: each
    exited 0, with no refusal.
  * The whole suite in the test form, as `scripts/ci` runs it: 5800 passed, 2 failed, 61 skipped (the paper tests,
    with no key in the environment) and 1 expected failure, in 15 minutes at a load average near 27. The two
    failures are the `37` cases of
    `tests/test_session_window.py::test_bounded_passes_a_stop_on_as_terminate_and_waits_its_grace_only_when_given_one`.
    Run again, they failed inside the profile and passed outside it. Inside it, `/bin/ps`, which is setuid root,
    is refused ("Operation not permitted", exit 126), and `bounded()` reads the empty answer as its command having
    ended, so it kills at once instead of waiting out the grace.
  * A write to a file in the home folder: refused, "Operation not permitted".
  * `git status --porcelain` read the same before and after.

  The run shows the sandbox's side only. What a model does after a refusal is held by `never`, through the
  lines above.
* ADR 0043, line 161 (Orca's dsh definition launches `dsh-tui .`) and lines 172-176 (`git add` refused outside the
  sandbox root, at a prompt nobody answers). Read.
* This machine: `~/.dsh/profiles/dsh-tui/cordis.patch.yml`, read for row ids only, has no `approval`, `permission`
  or `sandbox-policy` row; there is no `~/.dsh-tui/permission.json` and no `~/.dsh/cordis.patch.yml` (checked
  2026-11-13). Read.
* Second readers, 2026-11-13. The evidence-verifier (Claude Opus 5.5) re-ran the rehearsal and held every cited
  line. It corrected the driver's citation, what `DSH_PERMISSION_MODE` keeps, the Auto exception, and where plugin
  activation was shown. `/code-review` at high effort found the remembered path to `danger-full-access` (option 4
  to option 5), the older-block and no-block cases sharing one message, and the brief term. A second round added an
  owner's `sandbox-policy` row to the limits, the caveat in `docs/architecture.md`, and these dates.
  A pre-landing reader (Codex; its screen showed GPT-6-Astra at max) re-read all 17 upstream citations, and all held.
  It found that a quoted `id` key escaped the conflict check, so a later `ask` row read as ok. The check now reads a
  quoted key, with cases, and the other spellings are a limit. A delta reader (Codex, the same model and effort on its
  screen) found that the wider match also took a key that only ends in `id`, such as a quoted `grid` in a row's
  `config`, for the `id` key. The key is now matched whole, with cases beside the block and with no block. The text
  above carries each.
  For the amendment, the evidence-verifier (Claude Opus 5.5) re-ran the rehearsal. It found that any call with
  `sandbox_permissions` asks, not only a retry, and that no run showed mise refused, and it found the
  counter-evidence above. `/code-review` (medium) found pytest's temporary folder put inside the tree, against the
  hazard `docs/architecture.md` names, and asked for a literal path on every form, a working folder, a form for a
  project kept apart and the coordinator's step in the Confirmation. The text above carries each.
* The smoke test: **UNVERIFIED — not yet run.**
* 2026-11-13, the pins move to dsh-TUI `v0.20.0` → `8958f08e3ba0` and DeepSeek Harness `dsh-v0.2.2-rc.2` →
  `ddac96760b6e` (resolved; ADR 0045's Evidence). Each citation above was re-read at the new commits. The cited
  dsh-TUI lines are unchanged and where they were. In the engine, the base bundle's rows, `profile-boot.ts`
  L212-217, the presets' `derive` and `bash-sandbox`'s README hold in place; the rest hold at moved lines:
  `user-approval`'s README L78 and L165 and its `src/index.ts` L74 and L301, `permission-presets/src/index.ts`
  L218-222 and L429-444, `sandbox-local`'s README L81 and its `src/index.ts` L357 and L369, `profiles.ts` L53-60,
  the `headless` README L56, `tool-bash/src/index.ts` L506-508, `escalation.ts` L121-122, L235-242 (the prompt's
  text now in `locales.ts` L5) and L249, and the hook `ask` path in `packages/core/tools/src/pipeline.ts`
  L440-465. Two texts changed: the presets README now says agent creation applies the default preset to the
  session, and `fs-sandbox`'s README L48 and `roots.ts` L87-106 add explicit additional writable roots to
  `workspace-write`. That is the counter-evidence above, now at the pin: `additionalWritableRoots` is a
  `sandbox-policy` setting (`src/index.ts` L142, README L49), empty by default, so the writable set stays as
  recorded until a row sets it. Two more paths widen it without a user row: a plugin may call `sandbox-policy`'s
  `registerWritableRoot(session, directory)` for a live session (README L55-57, `src/index.ts` L244), which no
  package in the four recorded worker profiles calls (a grep of their rehearsed installs; in the engine, outside
  `sandbox-policy` itself, it appears only as an API catalog string in `dsh-tool-cordis`, which no worker
  composes), and the shipped `headless` row fills `additionalWritableRoots` from `--add-dir` (the `headless`
  README L90), which no lane passes. The rehearsal (run 2026-11-13) composed this block in the `dsh-tui` profile on
  the new pins with `approval` `never` and both presets `never`, and with the new `fs-observation-policy` row,
  which `scripts/bootstrap` now counts among the block's ids. The smoke test stays `UNVERIFIED`.
