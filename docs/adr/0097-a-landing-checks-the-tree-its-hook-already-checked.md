---
status: proposed
date: 2026-11-11
decision-makers: the lead, under the owner's delegation (ADR 0074), 2026-11-11
review-by: 2027-02-09
---

# A landing runs every check again on the tree its commit hook has just checked; reusing that pass safely needs what no pinned upstream provides, so the gap is recorded and nothing is built

## Context and Problem Statement

A lane's final tree is checked twice: by `.githooks/pre-commit` (`scripts/ci --staged`) when it is
committed, and again by `scripts/land` (`scripts/ci --landing`) before it is pushed. With several lanes
committing and landing at once, the second run competes with every hook for the machine's CI slots. On
2026-11-11 a Codex builder wrote a passed-tree cache for `scripts/ci`. A landing would reuse the hook's pass
when a key over the tree, scripts, locks, tools and environment matched. The landing-first slot order from
the same work landed alone. The cache did not, for the reasons below.

## Decision Drivers

* The gate's answer must mean that the checks ran on these bytes (ADR 0013, ADR 0017).
* Glue, not invention (AGENTS.md rule 3): a trust mechanism in the gate needs an upstream that documents it.

## Considered Options

1. Keep checking twice; landings take a slot first (ADR 0013, rule 1).
2. The builder's cache: a local record of a pass, keyed by recomputable inputs.

## Decision Outcome

Chosen: **option 1**, until an upstream provides what option 2 lacks. A `/code-review` at high effort on
option 2 found that a safe answer needs two things it did not have:

* **A key that covers everything the checks read.** The tree alone does not. `git diff <tree>` skips
  index entries marked skip-worktree or assume-unchanged, and compares through clean filters set in
  untracked config (skip-worktree and the clean filter reproduced by the review; all three re-run by the
  second reader on git 2.54.0). Exported bash functions do not appear in `compgen -e`; `BASH_ENV` and
  library preloads do, but the key skipped them. Ignored files the checks load were not keyed: `.pth`
  files and `sitecustomize.py` in `.venv`, and `__pycache__` (read, not run: UNVERIFIED). Nor was the
  user's git config.
* **A pass record that cannot be forged.** A file under `~/.cache` can be written by any process of the same
  user. ADR 0034 sandboxes Codex and Muse, but Antigravity and DeepSeek Harness still start on bypass
  flags, as do Claude lanes. A record would need to be
  written only by a run that did the checks, for example signed by a key no agent holds.

A session's start writes the ignored `.canon/` into its worktree, which made the draft decline reuse
there; reuse was not observed in use.

The landing-first slot order that did land is not a trust mechanism: it changes only which of ADR 0013's
existing `lockf`/`flock` slots a run tries first, and a landing still runs every check.

No upstream in `canon/upstreams.json` provides either piece. `gh-signoff` marks a commit green after a local
run and reuses nothing. `mise-action` keys a cache of installed tools, not of check results. Outside
the registry, Turborepo signs its remote cache with an HMAC key that every machine using it holds, so it
would not meet the second need either (seen as a search summary only: UNVERIFIED). The gap stays
open; nothing is built until an upstream that documents such a cache is adopted (`adopt-upstream`).

### Consequences

* Good: the gate's answer still means the checks ran on the landed bytes.
* Bad: each landing repeats the hook's run, minutes per landing under load; slot order softens the wait.

### Confirmation

* `scripts/ci` keeps no record of a pass, and `scripts/land` runs every check through `scripts/ci --landing`.
* This record closes when an adopted upstream's cache meets both needs above, or is superseded.

## Evidence

Read 2026-11-11.

* The builder's draft, `d5ba912` plus uncommitted fixes on `cx-ci-tree-reuse`, and the `/code-review` at
  high effort on it, 2026-11-11: ten findings. Skip-worktree and the clean filter were reproduced in a
  scratch repository, `compgen -e` was run, and `.canon/` was found in a session's worktree. The rest was
  read, not run: UNVERIFIED. Its tenth finding, that the slot split was home-grown too, is answered above.
* An Antigravity reader's eight findings on the same draft, answered by the builder before the review above.
* `canon/upstreams.json` — no entry names a cache of check results; `gh-signoff`'s README mentions none, and
  `mise-action`'s pinned `src/index.ts` (`restoreMiseCache`) caches the installed tools' folder. Resolved.
* https://git-scm.com/docs/git-update-index — assume-unchanged tells git to stop checking the working tree
  file; skip-worktree treats a missing file as unchanged, and "not all git commands will pay attention to
  this bit". Fetched. That `git diff <tree>` hides a skip-worktree edit is from the runs above.
* evidence-verifier, 2026-11-11, on this record: the decision and the facts about this repository held;
  eight statements were corrected above (`compgen -e`, the sandboxes of ADR 0034, `.canon/`, the review's
  scope, the git page, the pinned mise-action source).
