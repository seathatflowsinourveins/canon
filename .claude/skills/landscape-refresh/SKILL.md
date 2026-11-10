---
name: landscape-refresh
description: Re-verify the canon against the live landscape, covering newer upstream releases, re-pointed tags, ADRs past their review date, new model releases and new Claude Code features. Use weekly, after a major model or tool release, or when asked whether this repo is still current.
---

# Refresh the landscape

The output is a short report of what moved and what it changes. This skill reports; it never moves a pin,
changes an ADR's status, commits or pushes. Those wait for the owner or orchestrator to approve the report.

## 1. Gate: three free checks first

Run these before reading anything on the web:

1. `scripts/canon check`. `RETAGGED` means a pinned release tag now points at a different commit: a
   supply-chain warning. `advanced` means a tracked branch has new commits: normal. A failure to reach an
   upstream (exit 1) is a finding; report it. `STALE` (exit 4) means the registry names another repo or
   track than the lock, so that pin was not compared; report the ids and the fix, `scripts/canon resolve
   <id>`, which this skill does not run: the report waits for approval (below).
2. `scripts/canon outdated`. `NEWER` and `STABLE-AVAILABLE` rows are the upstreams with something to read.
3. ADRs whose `review-by` is before today (the session-start lines name them).

**If `scripts/canon check` exited 0, nothing is newer and no ADR has expired, say so in three lines and
stop.** Any other exit (1, 3 or 4) is a finding to report, never the "all current" case. Do not
research for the sake of it.

## 2. Read only what the gate flagged

* **Retagged:** stop and report it first. Do not re-resolve. Look for an upstream explanation (a release
  note, an issue) and say whether one exists.
* **Newer release of a `foundation`, `platform` or `tool`:** read the release notes between the pinned tag
  and the new one. Note breaking changes, deprecations and security fixes. NautilusTrader v2.0.0 stable is
  the one that reopens ADR 0009 and ADR 0030.
* **Advanced branch of a `source`:** the skills or plugins there changed. Say that a re-read is due before
  re-pinning (ADR 0004); do not re-read everything now unless asked.
* **`watch` entries:** only check whether their status changed: an official adapter, a leaving-alpha release,
  a converged standard.

## 3. Expired ADRs

For each one, run the `evidence-verifier` subagent on it. If the ADR is about models or prices (ADR 0005),
re-read `https://platform.claude.com/docs/en/about-claude/models/overview.md` and
`https://artificialanalysis.ai/leaderboards/models` and compare with `canon/models.json`.

## 4. Harness changes

Only if `claude-code` shows a newer release: read `https://code.claude.com/docs/en/changelog.md` from the
pinned version forward and note anything that touches `.claude/` or `home/`.

Use one `upstream-scout` per independent field when there is a lot to read; otherwise read directly.

## Report

* A table of what moved: upstream, pinned, new, what it affects.
* The ADRs that need a decision, with the verifier's verdicts.
* One recommended action per item.

Then stop and wait for approval before `scripts/canon resolve` or any ADR change.
