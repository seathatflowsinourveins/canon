---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-08
---

# Review gate scope: every change carries a review note except a named few

## Context and Problem Statement

ADR 0017 rule 2 named the kinds of change that need an independent review, and `scripts/land` held it as a
list of paths. A list of what needs review lets through whatever it does not name: the earlier list
(`src/`, `packages/`, `scripts/`, `home/`, `.claude/hooks/`, `.claude/agents/`, `.claude/settings.json`,
`.githooks/`, `.github/workflows/`, and new records) matched none of `canon/upstreams.json`, `mise.toml`,
`.github/dependabot.yml` or `AGENTS.md`. Which changes need a review note?

## Decision Drivers

* A path added later must be reviewed unless someone decides otherwise, not skipped unless someone notices.
* The gate stays reproducible: a fixed list in the script, and a model never blocks (ADR 0017 rule 1).
* The dependency bot's lockfile-only pull request lands on passing checks (ADR 0016 rule 1).

## Considered Options

1. Keep the list of paths that need review and extend it.
2. Invert it: every change needs a note unless every path it changes is on a short list.
3. Require a note for every change.

## Decision Outcome

Chosen: **option 2**, as `NO_REVIEW` in `scripts/land` has it. A change needs no note only if every path it
changes is one of:

* `README.md`, `packages/*/README.md`, `catalog/README.md`, added, modified or deleted: prose a reader
  corrects.
* An edit to an existing `docs/adr/NNNN-*.md`: also prose a reader corrects. A new record, or one made by
  renaming, needs the `evidence-verifier`.
* An edit to `uv.lock`: the bot's lockfile pull request, which the checks decide (ADR 0016).

`*` does not cross a `/`, renames count as a delete plus an add, and names are read NUL-separated, so a move
or an odd name does not slip past. Option 3 is not taken because it would ask a note of the bot's lockfile
pull requests, which ADR 0016 lands on passing checks.

Unchanged from ADR 0017: `/code-review` reads code and the `evidence-verifier` reads a record; the note is
recorded on the commit as the `review` status; a model never blocks. With no `origin` remote the note is
printed and, as `scripts/land` says, "not recorded anywhere".

GitHub's own pull request rule can require approval by file pattern, from teams or from code owners. Teams
do not exist in a user-owned repository, and the one owner here cannot approve their own pull request
(ADR 0014), so the scope is held in the landing script and the note is a status, not an approval.

Supersedes ADR 0017 rule 2's list of what needs a review and ADR 0014 rule 9 in part. The rest of both stands.

### Consequences

* Good: a new kind of file is reviewed by default; the exempt list is short enough to read in one line.
* Bad: more changes carry a note, instructions and configuration included. A note is a sentence, not a
  second approval: it says a review was claimed, not what it read.

### Confirmation

`scripts/test-land` holds the gate offline, against throwaway repositories and a stub `gh`: `README.md`, a
package README, `uv.lock` alone and an edit to an existing record land without a note; `AGENTS.md`,
`pyproject.toml`, a test, a skill, a rule, `canon/upstreams.json`, `mise.toml`, a README two levels down,
code, a README with code, a new record and a record made by renaming need one; an unreadable file list
stops the run; `--dry-run` without a needed note fails; the local path prints the note it cannot record.

## Evidence

Read 2026-11-10.

* `scripts/land` in this repository — `NO_REVIEW` and `no_review()`, and the no-remote message "It is not
  recorded anywhere, because there is no remote to post it to" (read); `git show cdc247f^:scripts/land` —
  the earlier `REVIEW_PATHS` pattern, matched by hand against the four paths named above (read).
* `scripts/test-land` — the cases listed in Confirmation; 66 passed (read, run).
* `canon/github-practice.json` through `scripts/survey-github report` — "Ruleset requires one or more
  approvals | 11/19", "Approving review on half or more of human-authored pull requests | 12/28",
  "CODEOWNERS | 13/29", all `split` (run). The survey does not measure review required by path.
* https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
  — the pull request rule covers "all changes to the target branch"; approval can be required "when a pull
  request changes certain files or directories", from teams ("not available on user-owned repositories")
  or from code owners (second reader, fetched).
* Second reader, 2026-11-10 (`evidence-verifier`): the Decision matches `NO_REVIEW`, the four Context paths
  and the survey rows hold. It corrected the dependency-bot wording and replaced an unfetched claim about
  GitHub's rule with what the page says. Both are fixed above.
