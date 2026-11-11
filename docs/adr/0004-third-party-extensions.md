---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-09
---

# Third-party skills, plugins and workflow frameworks: pinned, read, then enabled

## Context and Problem Statement

Skills and plugins are instructions and scripts that run with the agent's permissions. Community collections
are large and popular, and the owner wants upstream skills used "seamlessly". Which sources may be enabled,
and under what conditions?

## Decision Drivers

* A skill can exfiltrate data or run commands exactly as the agent can.
* No signing or review standard for skills was found (UNVERIFIED: no source either way).
* Popularity is an adoption signal, not a safety signal.

## Considered Options

1. Install popular collections wholesale and auto-update.
2. Write all skills in-house.
3. Tiered sources, each pinned to a commit and read before enabling.

## Decision Outcome

Chosen: **option 3**.

Source tiers, in order of preference:

1. Claude Code built-ins.
2. Anthropic-maintained plugins (`anthropics/claude-plugins-official`, `/plugins` directory).
3. Vendor-official extensions for an upstream already in the canon (for example `alpacahq/agentic`,
   `alpacahq/alpaca-skills`).
4. Community collections (for example `obra/superpowers`): `reference` role. Read for patterns; enable a
   specific skill only after tier-3 treatment.

Conditions for enabling anything from tiers 2 to 4:

* pinned to a commit SHA that this repo holds, with no auto-update. Two mechanisms give that: copying the
  skill from a pinned commit into `.claude/skills/` as a committed project skill, or a marketplace this repo
  controls whose plugin entries set `sha`. Anthropic's official marketplace does neither: it cannot be
  pinned as a whole (`ref` takes a branch or tag), serves one SHA per plugin that Anthropic chooses, and
  auto-updates by default, so anything installed from it also needs `"autoUpdate": false`;
* every file of the skill or plugin read in full first, including scripts and hook definitions;
* recorded in `canon/upstreams.json` with role `source`;
* project-scoped by default, so it is version-controlled and reviewable.

Spec-driven workflow frameworks (Spec Kit, BMAD) are **not adopted**: the field has not converged on a shared
format. Planning uses Claude Code's built-in plan mode. `spec-kit` is tracked with role `watch`.

Reviewed so far: Alpaca's four trading skills, at the pinned commit of `alpacahq/alpaca-skills`
(2026-11-09; the `SKILL.md` files only, not their `reference.md`). **None is enabled.** They install tools
unpinned (`go install …@latest`, `brew install`, `uvx alpaca-mcp-server`); the CLI and MCP paths switch to
live trading through an environment variable; every safeguard is an instruction rather than an enforced
limit; and the backtest skill has the agent hand-write a script per run instead of using NautilusTrader.

ADR 0021 makes an exception to the four conditions above for the two skills that Orca installs. ADR 0023
makes one for the plugin Orca loads into each session, which meets none of them.

### Consequences

* Good: upstream skills are usable without inheriting an unreviewed supply chain.
* Bad: enabling a skill costs a review; pinned skills lag upstream until refreshed.

### Confirmation

`scripts/canon lint` requires a registry entry and ADR for every `source`. The `adopt-upstream` skill carries
the review checklist.

## Evidence

Read 2026-11-09 (fetched unless noted).

* https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/ — scan of 3,984 skills on ClawHub and
  skills.sh (not Claude plugin marketplaces), as of 2026-02-05: 36.8% had an issue, 13.4% a critical one, 76
  confirmed malicious payloads. Vendor research with no replication found, and one independent critique:
  https://www.flyingpenguin.com/toxicskills-revisit-loch-ness-levels-of-mythical-ai-risk/comment-page-1/
  says the pattern layer over-reports.
* https://github.com/anthropics/claude-plugins-official — README says Anthropic "does not control what"
  external plugins include and "cannot verify that they will work as intended or that they won't change";
  external plugins must meet quality and security standards for approval.
* https://code.claude.com/docs/en/plugins/marketplace-reference.md — plugin sources (`github`, `url`,
  `git-subdir`) accept a 40-character `sha`, which wins over `ref`; a marketplace source accepts only `ref`.
  https://code.claude.com/docs/en/plugins/loading.md — the official marketplace auto-updates by default.
* https://code.claude.com/docs/en/skills — project skills live in `.claude/skills/<name>/SKILL.md` and are
  committed; a project skill's `allowed-tools` apply even in a folder never trusted, which is one more
  reason to read every file first.
* https://github.com/alpacahq/alpaca-skills at `0e7c35163ae4` — the four trading `SKILL.md` files (read by a
  research lane; the live-switch variables and unpinned installs re-read by the orchestrator).
* https://github.com/anthropics/skills — README describes the skills as demonstration quality.
* https://github.com/obra/superpowers — MIT, v7.0.2 (tag resolved to `22f0b463e7a5`). It is listed in
  Anthropic's official marketplace, pinned there to the v7.0.0 commit (confirmed by the second reader).
* Second reader, 2026-11-09 (`evidence-verifier`): 6 of 10 claims held as written, 3 were too loose and 1
  had no source; all are corrected above.
* https://github.com/github/spec-kit and https://github.com/bmad-code-org/BMAD-METHOD — both widely starred,
  no shared spec format between them.
* https://github.com/github/spec-kit/releases/tag/v1.4.0 — published 2026-11-10; read 2026-11-10 (`gh`):
  HTTPS required for catalog sources, workflow pipe filters, reasoning-effort mapping for Copilot, Claude Code and
  Codex, fixes; no breaking change named. `spec-kit` stays `watch` and is not installed; its pin moved
  from `tag:v1.3.3` to `tag:v1.4.0` → `0019fa14d12f` (resolved).
* Star counts across this field are very high and could not be checked for inflation: UNVERIFIED as a quality
  signal.
