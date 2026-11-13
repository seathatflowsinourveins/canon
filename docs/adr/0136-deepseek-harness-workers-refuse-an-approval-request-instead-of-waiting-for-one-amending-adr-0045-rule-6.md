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
* Bad: the owner's dsh-tui tabs lose approval prompts too, and from the source neither `/permission` nor Shift+Tab
  brings them back (not run). A tab that needs more is relaunched with `DSH_PERMISSION_MODE=danger-full-access`.
* Bad: a remembered `read-only` pick makes every later worker read-only, and a builder then fails at its first
  write. An owner's `sandbox-policy` row in the profile's patch sets the sandbox the presets are matched against,
  so one set to `danger-full-access` runs as rule 3 describes. A home-level `$DSH_HOME/cordis.patch.yml` would
  outrank these rows in every profile. None of the three is on this machine, and `scripts/bootstrap --check` reads
  none of them.
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
* The rehearsals on the installed engine, in the Evidence.
* **The smoke test.** After `--install`, the lead's deputy starts one Orca `--agent dsh` worker in a new worktree on
  this lane's brief. It must run a test file with `uv`, have `git add` refused with no prompt pending, have a write
  to `~/` refused, and reach `worker_done` within 15 minutes. It is `UNVERIFIED` until run; its result goes below.

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
* The smoke test: **UNVERIFIED — not yet run.**
