---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-09
---

# Model routing: one model per conversation, routed by delegation, re-measured every release

## Context and Problem Statement

The owner required each task to run on a model suited to it, from Claude Code sessions down to runtime
workers, using the latest models. Which model and effort does each lane use, and how is that kept current?

## Decision Drivers

* Cost per completed task, not price per token.
* Prompt caches are per model: switching models inside a conversation discards the cache.
* The measured results available today are on other people's workloads, not ours.

## Considered Options

1. One top model everywhere.
2. Cheapest model that seems adequate per task, switching freely.
3. A routing table by lane, applied through subagents and sessions, treated as a starting point to measure.

## Decision Outcome

Chosen: **option 3**. The table lives in `canon/models.json`.

| Lane | Model | Effort | Basis |
|---|---|---|---|
| Orchestrator: cross-lane planning, ADR decisions, long-horizon work | `fable` (`claude-fable-5-2`) | high | Vendor: step up to Fable 5.2 for demanding reasoning and long-horizon work, or when Opus 5.5 at higher effort still falls short |
| Default build session: implementing against a clear plan | `opus` (`claude-opus-5-5`) | medium; high for hard changes; the orchestrator's model for design-heavy work (rule 7) | Vendor "start here" model at its default effort |
| Bulk, well-specified sub-tasks run in parallel | `sonnet` (`claude-sonnet-5-5`) | medium | Opus orchestrator with Sonnet 5.5 subagents matched Opus alone at `medium` for 31% less (defect search); 1.9 points below Opus alone at `high` |
| Read-heavy scouting and web research | `sonnet` | default | Judgement needed to weigh sources |
| Mechanical, checkable sub-tasks; high-volume runtime work | `haiku` (`claude-haiku-5-5`) | default | Vendor: Haiku 5.5 first for high-volume work with checkable outputs; Batch API (50% off) when nobody is waiting |
| Evidence verification and review | `opus` | high | Judgement call; no primary source says a reviewer must match the author's tier |

Rules:

1. **One model per conversation.** Route by delegating to a subagent or a separate session.
2. **Model choice is the last lever** (ADR 0006). Sweep effort first and compare cost per completed task.
3. **For checkable work, run low and re-run failures high.** Opus 5.5 on SWE-bench Pro: `low` with failures
   re-run at `high` solved about 97% at about $0.17, against 95.3% at $0.29 for everything at `high`.
4. **Haiku is not for coding loops.** Haiku subagents under Opus cut cost per attempt 32% and pass rate about
   4 points on coding; on defect search they scored 0.677 against 0.827.
5. **Re-check at every model release**; this ADR expires in 30 days regardless.
6. **The aliases are for the Anthropic API.** `opus`, `sonnet`, `haiku` and `fable` resolve to the models
   above there. On Bedrock, Foundry and other providers they resolve to older models; use full IDs there.
7. **A build session may be raised to the orchestrator lane's model** (the owner's instruction,
   2026-11-10: Claude workers may also run on that model, its use kept about level with the other
   models' overall). The build row of the table has two raises. `raise_to` names an effort on the same
   model, for a hard change. `raise_model_to` names a lane, `orchestrator`, whose model and effort the
   session takes instead; it names the lane, not a model, so the two cannot drift apart. Limiting the
   raise to design-heavy work is this record's narrowing of the owner's instruction: a decision record, or a
   state machine on which review rounds keep finding the same family of defect. That is the case in
   hand. On Opus 5.5, modify by replace took two changes (#55, #69) and two more that fixed replace
   defects among others (#99, #107); a session window's account check took four (#97, #105, #110, #114)
   after two on its stop and close (#83, #101). Each landed after its review, and each but the last of
   its group was followed by another fixing defects of the same kind.
   The evidence supports less than the instruction. The vendor names the step, to Fable 5.2 "for
   demanding reasoning and long-horizon agentic work, or when your evals on Claude Opus 5.5 at higher
   effort still fall short", but after an effort sweep: on its SWE-bench Pro subset Opus 5.5 at `xhigh`
   was level with Fable 5.2 at `high` for a little over half the cost. No eval of ours shows a
   shortfall, the effort those ten changes were built at was not recorded, and whether the raise ends
   such rounds is not measured. So rule 2 still comes first, a raise in effort before a raise in model;
   the model is picked when the session starts (rule 1); and the raise stays occasional, as the owner's
   condition asks.

Not adopted:

* **Advisor pairings.** For the current-generation pairs measured (Opus 5.5 with a Fable 5.1 advisor; Haiku
  or Sonnet 5.5 with an Opus 5.5 advisor) the gain was at the edge of noise or negative, at about twice the
  cost. A weak executor can gain a lot (a low-effort Sonnet 5 gained 23 points), but no lane here is one.
  ADR 0141 supersedes this bullet: on the owner's requirement of 2026-11-13 it adopts an advisor, Opus for
  every session and Fable 5.2 for a lane landing a trading-path change. The measurement above stands, and
  that record states it as the case against.
* **A second model provider for runtime workers.** Artificial Analysis shows GPT-6.1 Sol (xhigh) at index 52
  for $0.76 per task and Gemini 4 Carbon (high) at index 60. A second provider adds a second SDK, cache
  namespace and set of failure modes; it is adopted only after an eval on our own tasks shows it wins.
  A coding runtime is another matter: ADR 0058 lets an adopted one build on the owner's word, and ADR
  0057's eval, which has not run, keeps or retires it.

Where this corrects the request: a cheaper model is often not cheaper per task. Opus 5.5 at `low` cost about
7% more per solved task than Sonnet 5.5 at its default, and replacing Opus 5.5 with Fable 5.2 as orchestrator
cost 19% more for about the same score. Artificial Analysis points the other way on its own index: Fable 5.2
at `low` scores 54 for $3.10 per task against Opus 5.5 at `high` scoring 52 for $3.46. The vendor's own
defect-search runs also show Fable 5.2 alone scoring 1.6 to 5.9 points above Opus 5.5 at `high`, for 1.5 to
2.4 times the cost. The sources disagree because the workloads differ, which is why the table is a starting
point.

ADR 0059 sets the default for a session started with no model named to the build lane's `opus` (ADR 0022
had set `fable`), pins no alias, so each follows the vendor and a lane's model is what its alias resolved to
on the table's `read_at`, and holds the picker to the four families with Fable at the orchestrator lane's ID.
The table's `claude_code` section holds those settings, with the advisor ADR 0141 adds under `advisor`, and
its `runtimes` section the other runtimes' defaults. The lanes above are unchanged.

### Codex service tier (2026-11-12)

The owner ordered that the Codex lane run in Fast mode for the tasks that suit full speed, held in this
repository rather than set by hand; this replaces a setting made by hand for one night on 2026-11-11.
On 2026-11-12 the owner's instruction became Fast "if needed", and the lead ordered standard as the
default with Fast stated per dispatch. `home/.codex/defaults.toml` sets the top-level key
`service_tier = "default"`, and the table's codex row records it as `service_tier`; a dispatch that needs
Fast states it as below.

* **The spelling.** The config reference: "Use `fast` or another tier advertised by the active model; `fast`
  maps to the request value `priority`". At the pin (codex-cli `rust-v0.167.1`), the schema lists
  "`default`, `priority`, or `flex`; legacy `fast` also works" (`codex-rs/core/config.schema.json:8588`);
  Codex's own config editor writes `"fast"` for that tier, with the comment "Runtime values use `priority`,
  but config.toml continues to store it as `fast`" (`codex-rs/core/src/config/edit.rs:236-238`); its tests
  pass `"fast"` as an override (`codex-rs/core/src/config/config_tests.rs:10656`) and set it in config
  (`:10721`, `config_toml_legacy_fast_service_tier_uses_priority_request_value`).
  Loading turns either spelling into
  the request value `priority` (`codex-rs/core/src/config/mod.rs:4205`). A dispatch that asks for Fast writes
  `fast`, the spelling Codex itself stores and the vendor's speed page gives. The standard tier is the
  value `default`, Codex's sentinel for "the user intentionally selected no service tier, so model
  catalog defaults should not apply" (`SERVICE_TIER_DEFAULT_REQUEST_VALUE`,
  `codex-rs/protocol/src/config_types.rs:533-537`). It passes without a catalog check (`get_service_tier`,
  `codex-rs/core/src/session/mod.rs:1231-1247`) and is then left out of the request
  (`service_tier_for_request`, `codex-rs/protocol/src/openai_models.rs:1125-1130`, tested at `:2228`), so
  standard is asked for and a catalog's default tier does not apply.
* **When Codex sends it.** Only while the `fast_mode` feature is on (stable, on by default,
  `codex-rs/features/src/lib.rs:2207-2210`) and the model's catalog entry advertises the tier. With the
  feature off, loading drops the tier with no warning (`codex-rs/core/src/config/mod.rs:4206-4208`); with
  it on and the tier not advertised, Codex omits it and warns (`get_service_tier` and
  `unsupported_service_tier_warning`, `codex-rs/core/src/session/mod.rs:1231-1262`). The template sets no
  `[features]` and relies on `fast_mode` being on by default. The catalog bundled at the pin
  (`codex-rs/models-manager/models.json`) gives `gpt-6.1-sol` one tier: `priority`, "Fast", "2x speed,
  increased usage". The catalog the server sends this account was not read (`UNVERIFIED`).
* **Its cost.** The speed page: "For supported models, Fast mode uses included subscription limits at 2.5x
  the Standard rate. Purchased credits and Enterprise pay-as-you-go usage are billed at 2x the Standard
  rate." GPT-6.1 Sol is among the supported models it names. On setting it: "You can also persist the
  default with `service_tier = "fast"` plus `[features].fast_mode = true` in `config.toml`".
  The speed gain was not measured here (`UNVERIFIED`).
* **Which tasks suit it (the lead's rule, recorded as intent).** Fast when a Claude lane, a fix round or a
  firing waits on the worker's result: test-first builds and fix rounds on the trading path, and a read a
  lane is blocked on. Standard when nothing waits: replays, tapes, inventories, registry fragments, second
  reads after a landing, and work bound by local compute or by CI slots.
* **How one dispatch states its tier.** Orca's `worker-start` takes `--model` and `--effort` and no other
  agent option (`src/cli/specs/orchestration-worker-specs.ts` at the Orca pin). Orca has a Codex "Speed"
  option that would pass `-c service_tier=...` (`CODEX_SERVICE_TIER`,
  `src/shared/agent-session/options/agent-session-option-catalog-claude-codex.ts:199-209`), but nothing in
  the orchestration CLI reaches it, and a `--profile` cannot be named either. Codex itself reads a project
  layer, `.codex/config.toml` at the checkout's top, above the user config
  (`codex-rs/config/src/loader/README.md`, "Layering model", item 4 above items 5 and 6), and
  `service_tier` is not among the keys a project layer may not set (`PROJECT_LOCAL_CONFIG_DENYLIST`,
  `codex-rs/config/src/loader/mod.rs:90-103`). The layer applies only in a trusted project. Codex looks up
  trust for the folder itself first, then its project root, then the main checkout of a linked worktree
  (`decision_for_dir`, `codex-rs/config/src/loader/mod.rs:1083-1120`; `resolve_root_git_project_for_trust`,
  tested at `codex-rs/core/src/worktree_trust_tests.rs:86`); on 2026-11-12 `~/.codex/config.toml` listed
  this repository's main checkout and every Orca workspace entry in it as trusted, and Orca's managed home
  lists the main checkout too, mirrored from `~/.codex` (`codex-config-mirror-merge.ts:17-72` at the Orca
  pin). A `-c service_tier=...` given at launch outranks the project file (README item 3); Orca's Speed
  option is such a flag, and it is not sent by a dispatch. So a dispatch that needs Fast is two steps: the coordinator creates the worker's workspace,
  writes `service_tier = "fast"` into its `.codex/config.toml` (ignored by git, so it is never
  committed), and starts the worker in that workspace with `--worktree`; its brief names the tier, the
  worker's first line repeats it, and the session's `thread_settings_applied` record shows the tier
  requested (`ThreadSettingsSnapshot`, `codex-rs/protocol/src/protocol.rs:2451`; it is the requested tier,
  filtered only later, `step_settings.rs:29-30` and `:48-49`), not proof of the tier that ran; a tier the
  model does not offer shows only as a warning (`unsupported_service_tier_warning`,
  `codex-rs/core/src/session/mod.rs:1249-1262`), and `turn_context` has no tier field (`TurnContextItem`,
  `codex-rs/protocol/src/protocol.rs:3594`). Every other dispatch runs standard. A dispatched session
  applying the project layer has not been observed (`UNVERIFIED`); the first such dispatch's record
  settles it.
* **How it reaches each Codex home.** `scripts/bootstrap --user-config` writes the settings of
  `home/.codex/defaults.toml` that `${CODEX_HOME:-~/.codex}/config.toml` lacks, leaves a value set
  otherwise as it is and names it; `--check` reports it in the row `codex service tier`, and calls any other
  value than canon's `default` MISMATCH, `fast` and `priority` included. The maintenance agent
  (ADR 0065) runs `--user-config` from its own job, whose environment `scripts/automation` gives no
  `CODEX_HOME`, so it writes `~/.codex`. An Orca-started Codex reads its own `CODEX_HOME` under Orca's
  application support folder, and at the Orca pin Orca fills that home from `~/.codex`: before each
  Codex launch it promotes settings changed in the managed home back into `~/.codex`, then mirrors
  `~/.codex/config.toml` into the managed home (`src/main/codex-accounts/runtime-home-service-launch.ts:72`,
  `syncSystemConfigIntoManagedCodexHome` in `src/main/codex/configuration/settings/codex-config-mirror.ts`),
  and does the same for every managed home when its account service starts
  (`src/main/codex-accounts/service.ts:142`). So the tier written to `~/.codex` reaches Orca's Codex at its
  next launch. The mirror rebuilds the managed config from `~/.codex`'s settings and promotes back only an
  allowlist (`model`, `model_reasoning_effort`, `approval_policy`, `sandbox_mode` and a few `tui` and
  `features` keys, `src/main/codex/configuration/toml/config-toml-promoted-setting-values.ts:13-23`), which
  has no `service_tier`; a tier set only in the managed home, by hand or with `/fast`, is therefore replaced
  at the next launch, and `~/.codex` is where it holds. That the installed Orca does the same was not
  observed (`UNVERIFIED`). `--user-config` run from an Orca-started session writes the managed home, and
  the next launch replaces that copy the same way.

### Codex model: GPT-6 Astra at `max`, and the rate-limit prompt off (2026-11-12)

The owner's standing instruction (2026-11-12): the GPT lane runs the most capable current model at a high
reasoning effort, with the fast tier where it is needed. The table's codex row records this as
`model_rule`, and the row and `home/.codex/defaults.toml` move from `gpt-6.1-sol` to `gpt-6-astra`, at
`max`, which ADR 0059 rule 4 and ADR 0045 rule 7 now name.

* **Why Astra.** The vendor's models page calls Astra "Our most capable model for complex work across code,
  apps, and research" and Sol "Near-Astra performance for complex work at a lower cost than Astra" (quoted
  in ADR 0059 rule 4). The catalog bundled at the pin (`codex-rs/models-manager/models.json`) gives
  `gpt-6-astra` the efforts `low` through `ultra`, default `low`, and the tier `priority`, "Fast", "2x
  speed, increased usage", the same as `gpt-6.1-sol`. `max` stays the effort for the reason ADR 0045 rule 7
  gives against `ultra`.
* **Probe.** One read-only Codex worker was started on 2026-11-12 with `--model gpt-6-astra --effort max`.
  Its session's own record showed `gpt-6-astra`, effort `max`, tier `priority` and no reroute warning;
  then it stopped at the account's usage limit. Its output rate was not measured (`UNVERIFIED`), and so
  the 2x speed is still the catalog's word.
* **Why workers ran on `gpt-6-luna`.** At the pin, when a rate-limit snapshot from a Codex limit shows the
  primary or the secondary window at 90% used or more (`RATE_LIMIT_SWITCH_PROMPT_THRESHOLD = 90.0`,
  `codex-rs/tui/src/chatwidget/rate_limits.rs:16`; the condition at `:353-375`), the account has no
  workspace credits and the session is not on Luna, the TUI marks a prompt pending; it opens unless a
  backend banner is showing (`maybe_show_pending_rate_limit_prompt`, `:441-447`). The prompt reads
  "Approaching rate limits", "Switch to gpt-6-luna for lower credit usage?" (`NUDGE_MODEL_SLUG`, `:15`).
  Its first item, "Switch to gpt-6-luna", moves the session to Luna at Luna's default effort
  (`open_rate_limit_switch_prompt`, `:465-537`; Luna's default is `medium` in the bundled catalog), and
  it opens selected: the list selects the first enabled item when nothing else was chosen
  (`first_enabled_visible_idx`, `codex-rs/tui/src/bottom_pane/list_selection_view.rs:861`), as the
  prompt's snapshot shows (`codex-rs/tui/src/chatwidget/snapshots/codex_tui__chatwidget__tests__rate_limit_switch_prompt_popup.snap`).
  So an Enter meant for the worker accepts it. On 2026-11-12, eleven Codex session records on this machine
  each crossed 90% primary usage and then carried `gpt-6-luna` at `medium`, with tier `priority` kept.
  The prompt is not raised while `notice.hide_rate_limit_model_nudge` is true
  (`rate_limit_switch_prompt_hidden`, `:434-438`, read at `:367` and `:444`; schema
  `codex-rs/core/config.schema.json:3684`; the prompt's own "never show again" item writes the same key,
  `codex-rs/core/src/config/edit.rs:250`). The template sets it, and `--user-config` writes it where the
  config lacks it, into a `[notice]` table the file already declares. Orca's mirror treats the key like the tier: `[notice]` is
  not among the settings it promotes back
  (`src/main/codex/configuration/toml/config-toml-promoted-setting-values.ts:13-23`), and the managed
  home keeps only its own hook-trust and project sections when it is rebuilt from `~/.codex`
  (`isRuntimePreservedTomlSection`, `src/main/codex/configuration/toml/config-toml-runtime-owned-sections.ts:101-109`).
  So the key set in a managed home by hand lasts until the next launch, and the copy in `~/.codex` is
  the one that holds.
* **A second path off Astra, with no setting to stop it.** When the backend sends a usage-limit banner
  that names the current model as blocked, the TUI switches the session by itself to the banner's first
  fallback model the picker shows, keeping the current effort, or, for the `luna_reserve` banner, to
  `gpt-reserve` (`backend_banner_fallback`, `codex-rs/tui/src/chatwidget/backend_banners.rs:82-172`); it
  applies the switch and says "Automatically switched to ... due to usage limits"
  (`codex-rs/tui/src/app/backend_banner_fallback.rs:104`), and switches back once usage recovers. Its only
  gates are a ChatGPT sign-in and OpenAI authentication (`backend_banners.rs:83-85`); the config schema at
  the pin has no key for it, and the nudge key does not reach it. This is an upstream limit. A third path
  is a server that answers on another model, which raises a reroute warning for that turn
  (`codex-rs/core/src/session/mod.rs:4422-4462`). codex-cli `rust-v0.168.0` was out on 2026-11-12 with
  the same prompt logic (by the second reader; the pin is not moved here).
* **The control is a per-worker check over the whole run.** Because a session can leave Astra after its
  first line, and a single-turn builder can be switched mid-turn, the first `turn_context` line proves
  nothing about the rest. The coordinator reads every model a worker's session record shows; a build
  any part of which ran on another model is labelled so, and its reviewer is told. `scripts/lanes
  --rates` marks a Codex conversation whose events in its window (`--window`, ten minutes by default) name another model than the
  codex row OFF-MODEL; a turn on another model before the window is not seen.
* **Not yet measured.** ADR 0057's build eval has measured `gpt-6.1-sol` only; Astra is unmeasured by
  this repository's own eval. The eval runs once on Astra when the lane is back, and its result keeps or
  reverses this default.
* **Holding it.** `scripts/bootstrap --check` labels MISMATCH, and fails, a Codex `model`,
  `model_reasoning_effort`, nudge key or `service_tier` that differs from the template or is set by a
  profile, and NOT SET when one is missing. It does not read `features.fast_mode`: with canon's standard
  tier there is nothing for it to drop, and a dispatch that asks for Fast in its own workspace config is
  read back from its rollout instead (ADR 0132 unit 3; whether that record shows a dropped tier is UNVERIFIED).
  `--user-config` leaves such a value as it is and names it MISMATCH, so moving it is a step taken on
  purpose.

### Consequences

* Good: caches survive; lanes are cheap where output is checkable; the table has an expiry date.
* Bad: until this repo has its own eval, every row rests on external measurements.
* Bad: Astra at `max`, on a dispatch that asks for Fast, draws on the allowance fastest of what this
  account's catalog offers below `ultra`, and the probe (run on Fast) stopped at the account's usage limit four minutes after it started.
  The speed page also lists Ultrafast for Astra, "8x the Standard rate" of included limits, on Pro $500
  and eligible Enterprise and Edu plans; the catalog bundled at the pin has no such tier for it, and it is
  not used.
* Bad: Fast is two steps for the coordinator, not a flag, until Orca's dispatch passes a tier; a
  workspace it forgets to write runs standard, and the session record is where that shows.
* Bad: `--check` reports the Codex home of the session it runs in, so a check run outside Orca says
  nothing about the managed home Orca's mirror fills.

### Confirmation

Subagent frontmatter pins `model` and `effort`. A raised build session (rule 7) is started with the
orchestrator lane's model by its full ID, and `scripts/lanes` shows the model of each session that drew
a status line; nothing yet measures the owner's condition on use. The status line records a model change inside a session to
the event ledger, and `scripts/lanes` flags that session. The machine settings pin no alias (ADR 0059).
`scripts/bootstrap --check` labels an `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` in the installed settings PINNED, or
MISMATCH when it names another model than `canon/models.json`, and an unreadable table UNKNOWN; it labels
MISMATCH a top-level `model` that `canon/models.json` does not name, and checks the allowlist and picker row
against the template. `scripts/test-lanes` fails when the template's model keys differ from the table's
`claude_code` section, so the two move together. The picker's "Fable 5.1" row was an extra option whose
value was `claude-fable-5-1[1m]`; ADR 0059 shows the allowlist hiding it.
`scripts/test-lanes` fails when the template's `service_tier` differs from the table's codex row, and holds
the `codex service tier` and `codex defaults` rows' ok, NOT SET and MISMATCH cases, the last two failing the
check, and `--user-config` adding the keys. `scripts/test-lanes` holds the OFF-MODEL mark on a Codex
conversation whose model differs from the codex row, and its absence on one that matches.

## Evidence

Read 2026-11-09.

* https://platform.claude.com/docs/en/about-claude/models/overview.md — current lineup, IDs, prices
  ($8/$40 Fable 5.2, $4/$20 Opus 5.5, $2/$10 Sonnet 5.5, from $0.10/$0.50 Haiku 5.5 per MTok), "start with
  Claude Opus 5.5 for most workloads" (fetched by the orchestrator).
* https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md — starting
  point, step-down and step-up guidance, orchestrator and advisor pairings, effort curve, re-run-failures
  result (fetched by the orchestrator, first 100k characters; then read in full by the second reader).
* https://platform.claude.com/docs/en/about-claude/pricing — Haiku 5.5 costs more above 100K-token prompts.
* Second reader, 2026-11-09 (`evidence-verifier`): 17 of 18 claims supported. The advisor reason was wrong
  as first written and is restated above; rows 1 and 3 of the table and rules 4 and 6 gained their limits.
* https://code.claude.com/docs/en/model-config.md — aliases `opus`, `sonnet`, `haiku`, `fable` and what they
  resolve to; Claude Code's effort default is `medium` on Fable 5.2 and the 5.5 models, and `high` on Opus 5,
  Fable 5 and Fable 5.1 (fetched). Read again 2026-11-10: "Unless you set
  `ANTHROPIC_DEFAULT_FABLE_MODEL`, the `fable` alias resolves to Fable 5 in Claude apps gateway sessions and
  to Fable 5.2 everywhere else"; "To pin to a specific version, use the full model name ... or set the
  corresponding environment variable"; the page's own example sets one in a settings file's `env` (fetched).
* https://code.claude.com/docs/en/costs.md — `model: haiku` for simple subagents (fetched). The same page
  says to use Sonnet by default, which conflicts with the platform docs' Opus 5.5 starting point; this ADR
  follows the platform docs because they carry the measurements.
* https://artificialanalysis.ai/leaderboards/models — Intelligence Index snapshot; every figure quoted here
  and in `canon/models.json` was re-checked against the page by the second reader. The version label
  (v5.0.1) comes from Artificial Analysis's methodology pages, not the leaderboard page.
* Rule 7, 2026-11-10. The owner's instruction of that day, as rule 7 gives it. The cost guide above, read
  again that day: "Use Claude Fable 5.2 for demanding reasoning and long-horizon agentic work, or when
  your evals on Claude Opus 5.5 at higher effort still fall short"; Opus 5.5 "at `xhigh` 96.5% for $0.75
  USD, level with Fable 5.2 at `high` for a little over half the cost" (fetched, searched in full). This
  repository's history: the subjects and `Co-Authored-By` trailers of the ten commits (read, `git log`).
* Second reader of rule 7, 2026-11-10 (`evidence-verifier`): the quotation and the trailers held, and
  nothing that reads the table breaks. It regrouped the ten changes by what each fixed, and had the
  guide's effort sweep, the unrecorded effort and the narrowing of the owner's instruction stated; all above.
* 2026-11-10, with ADR 0059: the machine default, the alias pins and the picker settings above, and the
  table's `claude_code` and `runtimes` sections; the evidence is in that record.
* Cost-optimisation guide bundled with Claude Code 2.1.321 (`claude-api` skill, `shared/cost-optimization.md`)
  — model selection comes after every lever that does not lower the intelligence ceiling (bundled).
* Codex service tier, 2026-11-12. https://learn.chatgpt.com/docs/agent-configuration/speed — the cost
  multipliers, the supported models, Ultrafast, and "persist the default with `service_tier = "fast"`"
  (fetched, and read through a summarising fetch tool, so the quotations are as it returned them).
  https://learn.chatgpt.com/docs/models — "Start with High for Luna or Light for Astra" and "Astra's Light
  setting is low" (fetched the same way).
  https://learn.chatgpt.com/docs/config-file/config-reference — `service_tier` and `features.fast_mode`
  ("Availability still depends on the model and account"); the page states no cost (fetched, first 100k
  characters). The codex-cli source at its pin, at the lines cited above (read, `scripts/canon src
  codex-cli`). `scripts/automation`, whose installed jobs set no `CODEX_HOME` (read). The Orca source at its
  pin, at the lines cited above (read, `scripts/canon src orca`). `~/.codex/config.toml`'s
  `[projects]` headers and trust levels, 2026-11-12: this repository's main checkout and every Orca
  workspace entry trusted (read, those lines only).
* Second reader of the Codex service tier, 2026-11-12 (`evidence-verifier`): every codex-cli line held but
  one test citation, now restated; it found Orca's config mirror, which replaced a claim that the managed
  home is reached only from an Orca session, and had the silent drop with `fast_mode` off and the speed
  page's whole sentence stated. `/code-review` the same day: `priority` then read as canon's tier (dropped
  once the default became `default`), and the comment `--user-config` writes into a Codex config names
  this record.
* Second reader of the Astra amendment, 2026-11-12 (`evidence-verifier`): the decision held; it found the
  automatic usage-limit switch, the second usage window, the preselected item, Ultrafast and the
  departure from "Light for Astra", all stated above. Second reader of the per-dispatch tier, the same day:
  the decision held; `default` is left out of the request, `thread_settings_applied` shows the tier
  requested, a launch `-c` outranks the project file, and the trust lookup order, all stated above.
  `/code-review` of each round: the nudge key now joins an existing `[notice]` table (CRLF and a last
  header line included) and is written beside a `[permissions]` table it cannot join, `--check` fails on
  an unreadable config and `--user-config` names a strict value MISMATCH, and `scripts/lanes` marks a
  conversation any turn of which ran on another model.
