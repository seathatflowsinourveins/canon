---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); coordinator (Claude), by the owner's delegation
review-by: 2026-12-10
---

# Native defaults for every runtime: Claude aliases unpinned, the legacy Fable row hidden, each runtime's model set where its docs put it

## Context and Problem Statement

The `/model` picker on this machine showed a row "Fable · Fable 5.1". The owner asked that every runtime,
started natively with no flags, already run the right model at the right effort: "it is important for our
practice be auto default into sota as llm native, such as env set, cli update etc". Where is each default
set, to what, and how is it checked? How the runtimes update themselves is ADR 0065.

## Decision Drivers

* The owner's instruction above, and the lead session always on Fable 5.2.
* Check what a request actually ran on, not a label (rule 4); no silent fallback (rule 5).
* Only settings a runtime's own docs or pinned source document.

## Considered Options

1. Pin every Claude alias to `canon/models.json`'s ID, as ADR 0005 pinned `fable`.
2. Leave every alias unpinned; hide the legacy row with an allowlist and add a picker row by ID; set each
   other runtime's default where its docs put it.
3. As 2, but with `modelPicker`'s `replaceBuiltInOptions` lineup in place of the allowlist.

## Decision Outcome

Chosen: **option 2**.

1. **No alias is pinned.** An alias "point[s] to the recommended version for your provider and update[s]
   over time"; a pin names one ID and does not follow it. The owner's instruction points to following, so the
   template pins nothing, replacing ADR 0005's fable pin. A lane's model in `canon/models.json` is what
   its alias resolved to on `read_at`.
2. **An allowlist hides the legacy row.** The picker's Fable row was not the `fable` alias but an extra
   option whose value was `claude-fable-5-1[1m]` (Claude Code's state file; its keys are undocumented).
   The picker hides a model the allowlist excludes, and the allowlist "applies everywhere a user can
   specify a model"; an entry naming a model "disables that family's wildcard entry". So
   `availableModels` is `["opus", "sonnet", "haiku", "claude-fable-5-2"]`, and `modelPicker` adds a row
   for `claude-fable-5-2`, since Claude Code adds a row of its own only "for Opus, Sonnet, and Haiku
   versions"; it titles the row Fable 5.2 itself. Both were shown (Confirmation). The cost: Fable alone is
   held, so a newer Fable is replaced by 5.2 "and shows a notice" until the list moves with
   `canon/models.json`. `deniedModels`, which could block 5.1 alone, is read "from managed settings only".
   Option 3 would "show only these rows, **Default**, and a row for the model the session is already
   using", so it hides the row too, but it restricts nothing: without an allowlist,
   `--model claude-fable-5-1` ran 5.1.
3. **The lead and every Fable session start by full ID**, `claude --model claude-fable-5-2`, never from a
   picker row; `/status` then shows `Model: claude-fable-5-2`. New sessions default to the build lane's
   `opus`, as the owner's settings hold. This supersedes ADR 0022 rule 5 (`fable`).
4. **Codex: `gpt-6.1-sol` at `max`** in `config.toml` (`home/.codex/defaults.toml`), so a session with no
   flags runs as ADR 0045 rule 7 starts a review. This is the owner's choice and departs from OpenAI's
   page three ways: Astra is "Our most capable model for complex work across code, apps, and research";
   Sol is "Near-Astra performance for complex work at a lower cost than Astra"; and "For GPT-6.1 Sol,
   start with the reasoning effort available by default in your client", since "Most tasks do not need Max
   or Ultra"; Max is for "the hardest problems, when depth matters more than speed or usage". Naming
   `model` also stops Codex choosing "a recommended model" itself.
5. **Antigravity: Gemini 4 Carbon (High)**, Google's "strongest reasoning model, built for complex software
   engineering", in `~/.gemini/antigravity-cli/settings.json`'s `model`, "saved by its label". High is the
   highest level `agy models` and the models page list for it; `agy --effort` also takes `xhigh` and
   `max`, and what those do on Carbon is not documented, so they are not used. The label was shown to be
   the model agy selects (Confirmation); its own default is Carbon (Medium).
6. **Muse Code: `muse-wonder-1.0` at `max`**, "Meta's most capable model, built for complex software
   engineering and long-horizon agentic work" (preview), at "the deepest reasoning level". Muse Code's own
   default is `high`, and Meta says "For agentic coding, start with "high""; on the API "the default is
   "max"" for Wonder, and `max` follows the owner's choice for Codex. Muse Wonder Full, "the higher-capability variant", is "available only in Muse Code
   with a subscription" on two plans; its ID there is not documented. The settings file holds "model
   defaults" under no key the docs name, so the default is set at launch: natively
   `muse --model muse-wonder-1.0 --reasoning-effort max`, and from the cockpit
   `--agent muse --model muse-wonder-1.0 --effort max` ("`--effort` requires `--model`"). A Muse worker
   started without those flags runs `muse-spark-1.2` at `high`, not this default.
7. **DeepSeek Harness stays on the bundle's `deepseek-flash` at `max`.** DeepSeek's page lists
   `deepseek-flash` (V4.1-Flash-1017) and `deepseek-v4-pro` (V4-Pro-0813) and names neither as better for
   coding, so nothing here overrides the bundle.
8. **Orca** documents no default model or effort per agent: `--model` "Overrides apply to that launch
   only". Its per-agent launch defaults hold a "default Command and Arguments (and environment)"; whether
   they may carry a model flag is not documented.

Rules 4 to 7 replace ADR 0045 rule 7's "the other three stay on their defaults".

| Runtime | Model | Effort | Where it is set | How it is checked |
|---|---|---|---|---|
| Claude Code, new session | `opus` (Opus 5.5) | model default, `medium` | settings `model` | `--check` row default model |
| Claude Code, lead | `claude-fable-5-2` | per lane | `--model` at launch | `/status` |
| Claude Code, picker | three families and Fable 5.2 | — | `availableModels`, `modelPicker` | `--check` rows model allowlist, picker rows |
| Codex | `gpt-6.1-sol` | `max` | `config.toml` `model`, `model_reasoning_effort` | `--check` row codex defaults |
| Antigravity | Gemini 4 Carbon (High) | in the label | `settings.json` `model` | `--check` row agy default model |
| Muse Code | `muse-wonder-1.0` | `max` | launch flags | the session's model line |
| DeepSeek Harness | `deepseek-flash` | `max` | dsh-tui bundle | `dsh --profile dsh-tui --dump-config` |

### Consequences

* Good: Claude Code, Codex and Antigravity started natively run the model chosen here, the vendor's most
  capable where it names one and the owner's choice for Codex; Muse does when launched with the flags,
  and DeepSeek Harness unless a saved `/model` choice comes first; the legacy row can be neither picked
  nor typed; `--check` names every default a file holds.
* Bad: Fable is held at 5.2 until the list moves. Codex no longer follows OpenAI's recommended model or
  effort. Muse's default exists only where a launch passes the flags.

### Confirmation

On this machine, 2026-11-10, Claude Code 2.1.321, signed in with the owner's own login (the CLI's keychain;
no credential read). Every allowlist test passed the keys with `--settings`; no settings file changed.
`claude -p ... --output-format json`: `--model 'claude-fable-5-1[1m]'` and `--model claude-fable-5-1`
ran `claude-opus-5-5` by init model and `modelUsage`, with nothing on stderr; `fable` ran
`claude-fable-5-2`; without the allowlist, `claude-fable-5-1[1m]` ran `claude-fable-5-1`. Interactively,
`/model` listed Default, Opus, Fable ("Fable 5.1 · Most capable…"), Sonnet, Haiku without it; with it the
Fable row was gone; with the `modelPicker` row a fifth row read "Fable 5.2", described "Custom model
(claude-fable-5-2)". A start with `--model claude-fable-5-1` printed `Model "claude-fable-5-1" is
restricted by your organization's settings. Using claude-opus-5-5 instead.` `/status` in a
`--model claude-fable-5-2` session gave `Model:claude-fable-5-2`. `agy -p` with `--settings` naming the
model: the label "Gemini 4 Carbon (High)" was logged as the selected model; the slug
`gemini-4-carbon-high` and an unknown label were logged as "Gemini 4 Carbon (Medium)", the default, with
no warning in print mode. `scripts/test-lanes` exercises every new `--check` row and `--user-config` step
against a temporary HOME.

UNVERIFIED: why the server offers `claude-fable-5-1[1m]` (the 2.1.321 changelog moves saved 5.1 picks to
`fable` and offers `/model claude-fable-5-1` "to switch back"; Fable models have the 1M window "by
default"); whether this account may use Muse Wonder or Wonder Full; whether Orca's per-agent arguments
can carry a model.

The picker with these keys in user settings, closed on 2026-11-11: the owner observed, in a new session on
Claude Code 2.1.322 with the allowlist and the picker row in user settings, that `/model` shows Default,
Opus, Sonnet, Haiku and "Fable 5.2 · Custom model (claude-fable-5-2)", and no Fable 5.1 row (the owner's
observation, reported to this record's author, not re-run).

Applied on 2026-11-11, at the owner's request, from the main checkout at commit 229110a:
`scripts/bootstrap --user-config` added `availableModels`, `modelPicker` and the `latest` channel, retired
the `fable` alias pin, set Codex's model and effort and Antigravity's default model, and kept a copy of
each file it changed; `scripts/bootstrap --install` removed the Antigravity no-update line; `claude update`
moved the CLI from 2.1.321 to 2.1.322. `scripts/bootstrap --check` ended "All present". A request for
`claude-fable-5-1` then started and billed on `claude-opus-5-5`, confirming the allowlist and the vendor's
fallback.

Confirmation, 2026-11-11, the lead applied #174 on this machine at commit 0cb6d1a. The claude-hud plugin v0.15.0
was installed at user scope. `scripts/bootstrap --user-config` installed the status-line script and the HUD
config. `scripts/bootstrap --check` showed "auto-compact window ok 300000 tokens" and the status line ok. A
render with example input gave two lines with no address or account name, and a grep of it for identifiers
matched 0 lines.

**Auto-compaction (added 2026-11-11, the lead's decision).** The template sets `"autoCompactWindow": 300000`
for every session, and `scripts/bootstrap --user-config` adds it to a settings file that lacks it. The
documented default for a model with a native 1M window, which includes the Fable models, is to compact at
about 967K tokens. `autoCompactWindow` takes 100,000 to 1,000,000 in any settings file. A model's
`modelSettings.<model>.autoCompactWindow`, which `/autocompact` writes, wins over it for that model in the
same settings file. `--autocompact` wins for one launch. `CLAUDE_CODE_AUTO_COMPACT_WINDOW` wins over all
three unless Claude Code ignores its value. 300,000 is
canon's choice, not a documented recommendation: the docs give no figure. A wake's cost grows with the
context it carries, and a session's state lives in records, memory and digest files rather than in its
context. The figure is tuned from the lanes Usage row (ADR 0007) once readings exist. `scripts/bootstrap
--check` has an "auto-compact window" row. It reads ok at the template's value, and OWNER, without failing,
at another one. It reads NOT SET, failing, with none, and it names any per-model or environment override.
Claude HUD's `claude-hud.json` carries the same 300000 for its context bar, and the row names a difference
between the two. Standing compaction instructions use two of the documented ways. The repository's `CLAUDE.md` has a
`# Compact instructions` section that keeps the session's role and who it reports to, decisions and who
made them, open obligations with times, running lanes' dispatch IDs, and the paths of state files and
reports. It keeps the owner's rules by reference to `AGENTS.md` and the records. `.claude/settings.json` has a
`SessionStart` hook with the `compact` matcher, `.claude/hooks/compact-brief.sh`, that prints one line pointing a coordinator at its state
file. It runs after a compaction, not before a tool call, so ADR 0020 does not bear on it. The repository
fixes no state-file path, so the line points at the one the summary keeps. A third documented way, a
`PreCompact` hook whose output "becomes instructions for the compaction summary" (hooks.md), is not used:
the CLAUDE.md section already carries them. UNVERIFIED: how a 300,000 window
changes summary quality against the default.

## Evidence

Read 2026-11-10. `fetched`: downloaded and searched here; `run`: a command run here.

* https://code.claude.com/docs/en/model-config.md — the alias sentence; "Claude Code hides excluded models
  from the `/model` picker"; "applies everywhere a user can specify a model"; the substitution notice; the
  merge rule; the built-in rows "for Opus, Sonnet, and Haiku versions"; `deniedModels` read "from managed
  settings only"; Fable's 1M window (fetched).
* https://code.claude.com/docs/en/settings-reference.md — `availableModels` scope "Any file";
  `modelPicker`, "show only these rows, **Default**, and a row for the model the session is already
  using", "A label changes what the picker shows, not which model Claude Code runs" (fetched).
* https://code.claude.com/docs/en/changelog.md — 2.1.321, "Changed saved Fable 5.1 model picks to move to
  `fable`" (fetched).
* `~/.claude.json` — `additionalModelOptionsCache`, value `claude-fable-5-1[1m]`, label "Fable" (the
  coordinator's observation; keys undocumented; account data not read).
* The Claude runs and screens in Confirmation (run; screens captured with `expect`).
* https://learn.chatgpt.com/docs/config-file/config-reference — `model` "Model to use (e.g.,
  gpt-6.1-sol)", `model_reasoning_effort` (fetched).
* https://learn.chatgpt.com/docs/models — the Astra, Sol and effort sentences; with no model the CLI "uses
  a recommended model" (fetched).
* `~/.codex/models_cache.json`, Codex 0.167.0's catalog for this account — `gpt-6.1-sol` default `low`,
  levels `low` to `ultra`, so `max` is offered (run; model rows only).
* https://aistudio.google.com/docs/models — Gemini 4 Carbon, `gemini-4-carbon`, the quoted sentence
  (fetched).
* https://antigravity.google/docs/settings — the file, `model` "saved by its label", and "If that model is
  no longer available, the CLI uses the default model and shows a warning" (fetched).
* https://antigravity.google/docs/models — Carbon's levels High, Low, Medium (fetched).
* `agy models` — the slugs and labels, High the highest for Carbon (run).
* `agy --help` — `--effort (low|medium|high|xhigh|max)`, `--settings` (run).
* `agy -p --settings '{"model": ...}'` and `~/.gemini/antigravity-cli/log/` — the model each run
  selected, "Propagating selected model override" (run).
* https://dev.meta.ai/docs/models — "Muse Wonder is Meta's most capable model…" (fetched).
* https://dev.meta.ai/docs/muse-wonder — Wonder Full's availability; "the default is "max""; "For agentic
  coding, start with "high"" (fetched).
* https://dev.meta.ai/docs/muse-code/configuration — "The default model is `muse-spark-1.2`", the
  `--reasoning-effort` levels with `high (default)`, "max is the deepest reasoning level", the settings
  file and its "model defaults" (fetched).
* https://api-docs.deepseek.com/quick_start/pricing — `deepseek-flash`, `deepseek-v4-pro` and the models
  behind them (fetched).
* https://raw.githubusercontent.com/ccch1mneyyy/dsh-TUI/v0.17.0/docs/configuration.en.md — the `dsh-tui`
  row's `model` (Harness `agentDefaultModel`) and `effort` ("normally `max` in the bundle") (fetched).
* `dsh --profile dsh-tui --dump-config` — the `agent-default-model` row `deepseek-flash`, `effort: max`
  (run; model lines only).
* https://www.onorca.dev/docs/cli/orchestration — `--model` per launch; "`--effort` requires `--model`"
  (fetched).
* https://www.onorca.dev/docs/settings — "Per-agent launch defaults" (fetched).
* The machine before this change: settings `model` `opus` and the fable pin; Codex's config sets neither
  key; Antigravity's settings no `model`; Muse's no model key (key names read).
* Second reader, again 2026-11-10: the quotes held character for character; it added Wonder's API default,
  OpenAI's Max sentence, and the narrower Good bullet, and had "selected" said for Antigravity; above.
* Second reader, 2026-11-10 (`evidence-verifier`): most quotes held. It had the Sol and Astra quotes made
  verbatim, OpenAI's effort guidance, Muse Code's own default, Wonder Full, the pro model and `agy
  --effort` stated, option 3's rejection argued from its docs, the Orca sentence and the Consequences
  corrected, the update policy moved to ADR 0065, and the Evidence put one source to a line; all above.
* Auto-compaction, read 2026-11-11 (`fetched`, raw): https://code.claude.com/docs/en/model-config.md
  ("Sessions auto-compact at about 967K tokens by default"; the command, setting, flag and variable and
  their precedence; 100,000 to 1,000,000), https://code.claude.com/docs/en/settings-reference.md
  (`autoCompactWindow`: "Any file", "Default: unset, so Claude Code picks a window tuned for your model";
  `modelSettings`), https://code.claude.com/docs/en/context-window.md (`/autocompact`; `SessionStart` hooks
  that match the `compact` source run and "adds their output to the compacted context"),
  https://code.claude.com/docs/en/costs.md (the `# Compact instructions` section of CLAUDE.md) and
  https://code.claude.com/docs/en/hooks.md (`SessionStart` matcher `compact`).
