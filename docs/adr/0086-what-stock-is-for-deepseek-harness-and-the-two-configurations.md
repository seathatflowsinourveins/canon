---
status: accepted
date: 2026-11-11
decision-makers: the owner (ratified 2026-11-11); master orchestrator (Claude), for the record, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# What "stock" is for DeepSeek Harness, and the review and build configurations that run beside it

## Context and Problem Statement

ADR 0072 leaves two questions open: containment first, and what "stock" is for DeepSeek Harness. On
2026-11-11 a session installed additions into three DeepSeek Harness profiles without an adoption record —
13 packages into `desktop`, 5 into `headless`, and 4 into `dsh-tui` — while `canon/upstreams.json` still
recorded the `dsh-tui` profile with seven community plugins and two hook packages. The machine and the
record disagreed, rule 2's adoption path was bypassed, and three of the additions gave the reviewer arm a
route to state across sessions and to a second model that ADR 0072 rule 6 forbids.

## Decision Drivers

* Rule 2 (adopt through the canon) and rule 5 (no silent fallback): a profile that differs from its record
  is an unrecorded change.
* ADR 0072 rule 6: an addition that keeps state across sessions, or sends the transcript to a second
  model, runs only in the build arm.
* ADR 0045 rule 6: session-log upload and `dsh-memento` are off in the review runtime.
* Rule 10: this is off the trading path, so the smallest documented form — a record, `UNVERIFIED` marks
  and one evidence-verifier pass — is the required weight, not the eval gate.
* Orca launches `dsh-tui .` for agent id `dsh` with no per-task overlay (ADR 0072 rule 5, still
  UNVERIFIED).

## Considered Options

1. Treat the enhanced machine as the new stock and rewrite the record to match.
2. Restore the recorded stock and run every addition as a separate configuration beside it.
3. Record stock as it is already defined, and record two named configurations — review and build — with
   the additions assigned by arm.

## Decision Outcome

Chosen: **option 3**.

1. **Stock is unchanged.** It stays the seven community plugins and two hook packages already recorded for
   the `dsh-tui` profile, at `dsh-v0.2.1-rc.3`. Stock is reproducible only by replaying that list: ADR 0045
   records that the seven plugins were installed by hand, and `scripts/bootstrap --install` writes the two
   `cordis.patch.yml` rows, not the plugins. A profile that differs from the record is a named configuration.
2. **The review configuration** is stock plus one addition, `@deepseek-ai/dsh-experimental-terminal-bundle`
   0.2.1-rc.3. It is chosen because its own code has no network client and its sessions are process-local, though it gives the reviewer a persistent shell, which upstream says carries weaker contracts than one-shot bash;
   whether it opens a channel to another model or to earlier sessions was NOT read in its source
   (`UNVERIFIED`). It carries the `doublecheck` guard at `intensity: remind`: the plugin's `block` mode
   returns a denial and `warn` returns an approval request, which this host would auto-reject, so `remind`
   is the only mode that neither deadlocks an unattended run nor fails silently. Its `tdd` module is off
   so that no red gate applies: at `remind` the guard only emits a reminder and lets the edit proceed, and the
   gate refuses only under `warn` or `block` (read in the 0.9.30 source; the 0.9.29 guard code is identical
   up to the projection registration).
3. **The build configuration** is `dsh --profile headless -`, a one-shot runner carrying `aegis`,
   `dsh-doublecheck` 0.9.30, `dsh-memento` 0.5.30 with `writePolicy: auto`, and
   `dsh-subagent-codex` plus `dsh-subagent-claude-code` 0.2.1-rc.3. It re-enables the `credentials` row
   that `dsh-headless` disables, so a dispatch needs no exported key.
4. **The `desktop` surface** at 0.2.2-rc.1 is interactive and belongs to neither arm. It is **not recorded
   anywhere**: its thirteen packages are unrecorded drift, as are the three additions this session put into
   `dsh-tui` and then removed. Recording them individually is the adoption pass ADR 0072 rule 1 asks for and
   this record does not perform.
5. **The advisor is inert in the review configuration.** It is in stock, and `dsh-advisor` 0.6.4 halts with
   `no adapter registered for provider "deepseek-official"` when given a model. That failure was reported in a
   headless run, though the current `headless` profile no longer carries the advisor. Whether the `dsh-tui` profile configures a model for it was not
   read (`UNVERIFIED`). Rule 6 holds by configuration, not by construction.
6. **ADR 0072 rules 1 and 5 are deferred, not satisfied.** No addition went through `adopt-upstream` with a
   source read for network use and persistence, and each was installed machine-level into the owner's
   profiles rather than a throwaway runtime home. AGENTS.md rule 10 sets the weight for work off the trading
   path, and this record takes that lighter weight deliberately; the deferral is the decision, not a driver.
7. **Nothing here was measured.** No eval run exists for either configuration; both are `UNVERIFIED`.

### Consequences

* Good: the reviewer arm's channels are closed by configuration, which `scripts/bootstrap --check` can keep
  verifying. It is not closed by construction: `dsh-memento` is still in the profile and off only because a
  patch row says so, and its store lives at `$DSH_HOME/dsh-memento/memory.db`, a file the build
  configuration shares.
* Good: stock keeps its meaning, so a `stock` label still points at a list a reader can replay.
* Bad: the build configuration was not dispatched from Orca in any run here. One `orchestration
  worker-start --agent dsh` launched `dsh-tui .` even though Orca's worker descriptor for `dsh` carries
  `buildArgs: ["--profile","headless","-"]`; the chat surface is refused ("this agent has no structured
  session"). Why the descriptor did not win is `UNVERIFIED`, so the build configuration's reachability from
  Orca is open rather than settled.
* Bad: a worker labelled `deepseek-harness` that delegates to `subagent_codex` or `subagent_claude_code`
  answers in another family's words. ADR 0072's driver is that a worker is a runtime, model, effort and
  configuration, so such a result belongs to that other family, not to this one.
* Bad: two configurations on one machine, and the arm a worker belongs to is chosen by how it is started
  rather than by a label Orca reads.

### Confirmation

Run 2026-11-11, both exit 0: `dsh --profile dsh-tui --dump-config` composes 125 rows with no duplicate id
and carries no `subagent-codex`, `subagent-claude-code` or `session-search` row; `dsh --profile headless
--dump-config` composes 106 rows with no duplicate and carries both cross-model providers. These two runs
are the whole support for "the reviewer arm is clean now", so they are repeated under Evidence.

## Evidence

Read and run 2026-11-11.

* Versions and existence of the four additions, from their registry documents:
  https://registry.npmjs.org/@deepseek-ai/dsh-experimental-terminal-bundle/0.2.1-rc.3,
  https://registry.npmjs.org/@deepseek-ai/dsh-subagent-codex/0.2.1-rc.3,
  https://registry.npmjs.org/@deepseek-ai/dsh-subagent-claude-code/0.2.1-rc.3,
  https://registry.npmjs.org/@deepseek-ai/dsh-experimental-session-search/0.2.1-rc.3 (read).
* The engine and its TUI: https://github.com/deepseek-ai/deepseek-harness and
  https://registry.npmjs.org/@deepseek-harness-tui/dsh-tui (read; the TUI's peer range stops at the
  0.2.1 line, which is why every addition here is pinned to 0.2.1-rc.3).
* `canon/upstreams.json`, entry `deepseek-harness` — the seven plugins, two hook packages and the
  `dsh-v0.2.1-rc.3` pin this record preserves (read).
* The build configuration's two newer versions, from their registry documents:
  https://registry.npmjs.org/dsh-doublecheck/0.9.30 and https://registry.npmjs.org/dsh-memento/0.5.30
  (read). Stock's recorded pins stay 0.9.29 and 0.5.28; the build configuration is what carries these.
* ACP profile app `@deepseek-ai/dsh-acp-app` 0.2.1-rc.3:
  https://registry.npmjs.org/@deepseek-ai/dsh-acp-app/0.2.1-rc.3 — registry metadata **UNVERIFIED**:
  the web tool reported this URL inaccessible on 2026-11-11. Its name and version were read in the
  pinned source's `packages/bundle/acp-app/package.json`; the app and EOF/shutdown contract were read
  in [the bundle README at the DSH pin](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/bundle/acp-app/README.md).
* ACP v1 [initialization](https://agentclientprotocol.com/protocol/v1/initialization),
  [prompt turns](https://agentclientprotocol.com/protocol/v1/prompt-turn),
  [tool permissions](https://agentclientprotocol.com/protocol/v1/tool-calls) and
  [transports](https://agentclientprotocol.com/protocol/v1/transports) (read 2026-11-11), and
  [profile bundle resolution at the DSH pin](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/boot/app-boot/src/profile.ts)
  (read: the ordered manifest list and installation-first bundle resolution).
* ACP probe rules rechecked 2026-11-11: [stdio messages](https://agentclientprotocol.com/protocol/v1/transports)
  are individual JSON-RPC objects; the [permission schema](https://agentclientprotocol.com/protocol/v1/schema)
  requires an options array and a string selected `optionId`;
  [prompt turns](https://agentclientprotocol.com/protocol/v1/prompt-turn) gate notices and compaction
  updates on client capabilities; [session setup](https://agentclientprotocol.com/protocol/v1/session-setup)
  gates closing on the agent's `sessionCapabilities.close` and treats `-32002` on close as success (read).
* The ACP manifests and lockfiles also name `dsh-fail-guard` 0.2.1:
  https://registry.npmjs.org/dsh-fail-guard/0.2.1 — published metadata and implementation source
  **UNVERIFIED**: the web tool reported this URL inaccessible on 2026-11-11. Its version below comes
  only from the two profiles' `package.json` and `pnpm-lock.yaml`, not from registry metadata.
* `dsh --profile dsh-tui --dump-config` and `dsh --profile headless --dump-config` (run 2026-11-11, both
  exit 0): 125 and 106 rows, no duplicate id, and the second carries both cross-model providers while the
  first carries neither. This is the only support for the review arm being clean.
* ADR 0072 rules 1–6; ADR 0045 rule 6; `AGENTS.md` rules 2, 5 and 10 (read).
* Orca 1.4.255, `out/preload/index.js` inside `app.asar` — two `dsh` registrations: `launchCmd:
  "dsh-tui ."` for the agent launcher, `buildArgs: ["--profile","headless","-"]` for the worker
  descriptor (read).
* `orca agent launch --agent dsh --surface chat` → `agent_launch_surface_unavailable`, "this agent has no
  structured session" (run).
* `orca orchestration worker-start --agent dsh --spec ...` → a terminal running `dsh-tui .`; the worker
  reported `worker_done` (run).
* `dsh --profile headless -` → `MISSING_CREDENTIAL` before the `credentials` row was re-enabled; `OK`,
  exit 0, after (run).
* `dsh-advisor` 0.6.4 in the headless profile → `no adapter registered for provider "deepseek-official"`
  (run).
* `dsh-doublecheck` 0.9.30, `lib/guard/index.js` — `intensity` defaults to `remind`; `warn` returns
  `{kind:'ask'}`; `block` returns `{kind:'deny'}`; the turn-boundary gates are advisory, "never blocks the
  close" (read).
* Versions from each profile's installed `package.json`; nothing was read beyond them. Licences for the
  four new additions are `UNVERIFIED` — their licence files were not read.
* The eval verdict for either configuration: **UNVERIFIED — no run exists.**

## Amendment — 2026-11-11: ACP profile composition

The `review-acp` and `build-acp` profiles have the direct package composition below. Checked
2026-11-11 against only `package.json` and `pnpm-lock.yaml` in each of
`~/.dsh/profiles/review-acp/` and `~/.dsh/profiles/build-acp/`: each manifest declaration agrees with
its lockfile's root importer, and the table gives the resolved package versions. An em dash means
the package is absent from that profile's declared direct dependencies and root importer.

| Package | `review-acp` | `build-acp` |
|---|---|---|
| `@deepseek-ai/dsh-acp-app` | 0.2.1-rc.3 | 0.2.1-rc.3 |
| `@deepseek-ai/dsh-experimental-terminal-bundle` | 0.2.1-rc.3 | 0.2.1-rc.3 |
| `@deepseek-ai/dsh-hook-protocol` | 0.2.1-rc.3 | 0.2.1-rc.3 |
| `@deepseek-ai/dsh-hooks-claude-code` | 0.2.1-rc.3 | 0.2.1-rc.3 |
| `@deepseek-ai/dsh-subagent-claude-code` | — | 0.2.1-rc.3 |
| `@deepseek-ai/dsh-subagent-codex` | — | 0.2.1-rc.3 |
| `aegis` | 2.15.0 | 2.15.0 |
| `dsh-doublecheck` | 0.9.30 | 0.9.30 |
| `dsh-fail-guard` | 0.2.1 | 0.2.1 |
| `dsh-memento` | — | 0.5.30 |

For `aegis`, both manifests and both lockfile resolutions select git commit
`e5f43c548a06c3f302c521a4c23f258296ccc947`; both lockfiles' package entries report version 2.15.0
for that commit. Peer-dependency suffixes in the root importers are omitted from the package versions
above.

### Bundle selection checked 2026-11-11

Those same two `package.json` files declare the following `dsh.profile.bundles` order. These are
manifest selections; the versions of the direct dependencies remain as recorded above.

| Position | `review-acp` bundle | `build-acp` bundle |
|---|---|---|
| 1 | `@deepseek-ai/dsh-base` | `@deepseek-ai/dsh-base` |
| 2 | `@deepseek-ai/dsh-acp-app` | `@deepseek-ai/dsh-acp-app` |
| 3 | `@deepseek-ai/dsh-experimental-terminal-bundle` | `@deepseek-ai/dsh-experimental-terminal-bundle` |
| 4 | `dsh-doublecheck` | `@deepseek-ai/dsh-subagent-claude-code` |
| 5 | `aegis` | `@deepseek-ai/dsh-subagent-codex` |
| 6 | `dsh-fail-guard` | `aegis` |
| 7 | — | `dsh-doublecheck` |
| 8 | — | `dsh-memento` |
| 9 | — | `dsh-fail-guard` |

The running installation's resolved version of `@deepseek-ai/dsh-base` is **UNVERIFIED** from the
four permitted profile files. The pinned upstream's `profile.ts`, `resolveBundleDir`, resolves each
bundle from the running DSH installation first, then the profile directory. The manifests' declared
order and locked direct dependency versions do not establish which bundle versions a launch loads.

### Adoption gap recorded 2026-11-11

ADR 0095 now registers the ACP v1 standard as `acp-spec` in `canon/upstreams.json` at
`schema-v1.31.0`. Adoption of `@deepseek-ai/dsh-acp-app` and the remaining unregistered profile
components stays **deferred**, under Decision Outcome item 6; the manifest inventory and upstream
contract evidence here do not complete `adopt-upstream`. Registry changes are outside this amendment's
scope. The app and fail-guard npm evidence URLs are above, with registry confirmation explicitly
**UNVERIFIED** after the failed fetches.

Enabled plugin composition, effective configuration, installation state and profile execution remain
**UNVERIFIED**: only the four manifest and lock files were read, and neither profile was executed.
