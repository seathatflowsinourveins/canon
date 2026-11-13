---
status: accepted
date: 2026-11-13
decision-makers: repo owner (an advisor in every session, Opus for routine work and Fable 5.2 only at the final landing, and the retry watchdog, 2026-11-13); build session (Claude Opus 5.5) for the means and the record
review-by: 2026-12-13
---

# Every Claude Code session takes Opus as its advisor and waits out a usage limit, and a lane landing a trading-path change takes Fable

## Context and Problem Statement

Claude Code's advisor tool, experimental by its docs, lets a session's main model consult a second model at
decision points. On 2026-11-13 the owner required an advisor in every Claude Code session, Opus for routine
work and Fable 5.2 only at the final landing of essential work, and the retry watchdog for sessions that run
unattended. The advisor needs the feature-flag fetch ADR 0139 turned on, and sessions here already
carried an advisor equal to their main model with none set, which the docs (unset is off) do not explain.

## Decision Drivers

* The owner's requirement of 2026-11-13, above.
* ADR 0005 left advisor pairings out on the vendor's measurements, and that evidence has not changed.
* One place every session reads: the user settings `scripts/bootstrap --user-config` installs (ADR 0065).
* Orca starts a Claude worker with `--model` and `--effort` only, and no environment variable names an
  advisor, so only a settings file reaches an Orca lane.
* A Fable 5.2 main model accepts only a Fable 5.2 advisor; an Opus 5 or 5.5 one accepts Fable or Opus.

## Considered Options

1. No setting: leave the sessions as they run now.
2. Fable for every session (`advisorModel` `"fable"`).
3. `advisorModel` `"opus"` and `env.CLAUDE_CODE_RETRY_WATCHDOG` `"1"` in the user settings, and
   `claude-fable-5-2` in the `.claude/settings.local.json` at the root of the worktree where a Claude lane
   lands a trading-path change.
4. As 3, with the landing lane's Fable advisor given by `claude --advisor` at its start.
5. As 3, with the routine advisor in canon's project settings (`.claude/settings.json`).

## Decision Outcome

Chosen: **option 3**, because it gives Opus to routine sessions and Fable to the one lane with files each
session already reads; a local value outranks the user one (managed, command line, project local, shared
project, user). The alias `opus` follows the build lane's `opus` main model as both advance (ADR 0059 pins
no alias), so the pairing stays valid. Option 2 prices every consultation at Fable's rate, against "only at
the final landing"; option 4 needs a flag Orca does not pass; option 5 misses sessions started outside a
canon checkout. `.claude/rules/dispatch-practices.md` rule 8 holds the landing step: the builder stops at
its report, and a Claude lane started for the landing reviews and lands it.

Where the evidence disagrees with the request or with itself:

* A builder's workspace (`--worktree new-top-level`) is a git worktree, where Claude Code keeps the local
  settings file at the main checkout's root, which every canon session reads. The docs say it still reads
  "a file an earlier version left" in the starting directory, the root's value winning; that it reads one
  written by hand rests on one non-interactive probe, and a later version may drop the read. A Fable
  advisor also goes unsent, with no error, where a plan bills Fable to usage credits before its one-time
  consent. Rule 8's check of the first request shows each, and the lane then takes the routine advisor.
  The key never goes in the main checkout's file.
* Anthropic's cost guide measured Opus 5.5 with a Fable 5.1 advisor at 1.7 points over Opus alone on a
  coding benchmark, "at the edge of run-to-run noise", for about 2.1 times the cost, and pairings whose
  executor rarely consulted within noise or lower. Opus advising Opus was not measured; the Claude Code
  docs offer it as "a second Opus reviews the first". ADR 0005's case against stands; this record adopts
  the advisor on the owner's word, and its first check is the consult rate.
* The Fable lead gets no advisor: Opus ranks below Fable 5.2, so Claude Code does not attach it, and says
  so in a notification. The lead had run with a Fable advisor no setting gave it. Its Opus, Sonnet and
  Haiku subagents take the Opus advisor.
* The owner asked for the watchdog for workers. Every session reads the one user `env`, and the docs name
  it for unattended sessions, so the owner's own sessions get it too.

### Consequences

* Good: a session may consult Opus before committing to an approach, on a recurring error and before
  declaring a task done, and a trading-path change gets a Fable read at its landing.
* Good: a session waits out an overload, a rate limit or a usage limit instead of failing, and retries
  other transient errors up to 300 times, about three hours; a spend limit or exhausted usage credits
  still fail at once.
* Bad: the tool's definition adds about 1,000 prompt tokens to each request, cached after the first; each
  consultation reads the whole transcript uncached and counts toward a plan's limits, a Fable one billing
  to usage credits where Fable does. The landing lane's subagents, its review among them, inherit Fable.
* Bad: a waiting session keeps its turn open, counting down to the next retry, with no cap
  (`CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS` and `_MAX_USAGE_LIMIT_WAIT_MS` unset), and `scripts/lanes` and
  the lane watchdog report it quiet, as a stall. Only rule 8's last step removes the landing key; a lane
  started in that workspace before then takes Fable too.
* Neutral: `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` in the user `env` turns the advisor off, and `--check`
  names it OWNER. What `/advisor off` writes is UNVERIFIED; if it removes the key or sets null,
  `--user-config` adds `"opus"` back.

### Confirmation

* `scripts/bootstrap --user-config` adds both settings where a file lacks them or sets null, with a `NOTE`
  each, and keeps and names another value, such as a pick `/advisor` saved. `--check` has rows `advisor
  model` and `retry watchdog`: ok as the template sets them; `NOT SET` (absent or null) fails; `OWNER` for
  another value, or for `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` set other than 0 in the file or environment;
  `MISSING` for a file `--user-config` leaves as it is; `UNKNOWN` for an unreadable template.
  `scripts/test-lanes` covers each, and holds the template's advisor to `canon/models.json`'s
  `claude_code.advisor.routine` and its `final_landing` to the orchestrator lane's model.
* Rule 8: the coordinator reads the advisor model the landing lane's first request carried, from its
  transcript's request-shape entry; the `advisorModel` field shows what is set, not what is attached.
* Forward checks, not yet run: after its next restart or compaction the lead's requests carry no advisor;
  the first landing lane's carry `claude-fable-5-2`; the consult rate per session over the review period,
  against the guide's 1.4 per attempt. How a transcript marks a consultation, and whether `scripts/lanes
  --rates` counts an advisor call's tokens under the main model, are UNVERIFIED.

## Evidence

Read 2026-11-13. `fetched`: downloaded whole with `curl` and read in the parts named.

* https://code.claude.com/docs/en/advisor.md: "experimental", behavior, pricing and availability "may
  change"; the pairing table (Opus 5 or 5.5: "Fable, Opus 5 or later"; Fable 5.2: Fable 5.2); a lower
  advisor is not attached, with a notification; subagents inherit it; each call "processes the full
  transcript anew"; billing; the flag fetch; `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` ignores `advisorModel`
  (`fetched`).
* https://code.claude.com/docs/en/env-vars.md: `CLAUDE_CODE_RETRY_WATCHDOG`, "for unattended sessions",
  its retries, waits and immediate failures, and `_MAX_WAIT_MS` unset meaning no limit (`fetched`).
* https://code.claude.com/docs/en/errors.md: "Tune retry behavior": the `Retrying in Ns · attempt x/y`
  countdown, and `CLAUDE_CODE_RETRY_WATCHDOG_MAX_USAGE_LIMIT_WAIT_MS`, "no cap" by default (`fetched`).
* https://code.claude.com/docs/en/settings-reference.md: `advisorModel`, any file, an alias or full ID,
  "unset, so the advisor is off"; `/advisor` saves to `~/.claude/settings.json`; `--advisor` (`fetched`).
* https://code.claude.com/docs/en/settings.md: "Settings precedence"; for the local file, "In a worktree,
  it uses the file at the main checkout's root", and Claude Code "still reads a file an earlier version
  left there alongside the root file", the root's value winning (`fetched`).
* https://code.claude.com/docs/en/model-config.md: `opus` is Opus 5.5 on the Anthropic API (`fetched`).
* https://code.claude.com/docs/en/permission-modes.md: "Protected paths": `bypassPermissions` allows a
  write under `.claude` (`fetched`).
* https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md:
  "Advisor strategy: escalate hard decisions", the figures above and the definition's 1,000 tokens (`fetched`).
* This machine, 2026-11-13: `orca agent launch --help` shows `--model` and `--effort` and no other agent
  flag; `.gitignore` ignores `.claude/settings.local.json`; the main checkout's file sets no advisor.
* This machine, 2026-11-13, Claude Code 2.1.323, one non-interactive probe: `claude -p --model
  claude-opus-5-5` in a plain git worktree whose own local file set `claude-fable-5-2` sent its request with
  the advisor tool on `claude-fable-5-2` (its request-shape entry, that field alone; worktree and transcript
  removed). An interactive session, and Orca's chat surface, were not probed (UNVERIFIED).
* This machine, 2026-11-13, transcripts read one field at a time: this session's requests carried the
  advisor tool on `claude-opus-5-5`, and the Fable lead recorded `claude-fable-5-2`, with no settings file
  setting `advisorModel`. Where that default comes from is UNVERIFIED, as are what `/advisor off` writes
  and whether a settings change mid-session attaches an advisor, which is why rule 8 starts a fresh lane.
* Second readers, 2026-11-13: the `evidence-verifier` found the worktree file stated as applying "in that
  workspace alone", the watchdog overstated, `/advisor off` unsourced and the `advisorModel` field taken
  as attachment; `/code-review` (high) found null and other kill-switch values misread by `--check`, the
  key's removal and the subagents unstated, and a wait that looks like a stall. A second pass found the
  consent unnamed, the worktree read and the probe stated more widely than shown, and "every Orca
  workspace" too broad. Each is corrected above.
