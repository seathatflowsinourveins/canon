---
status: accepted
date: 2026-11-11
decision-makers: lead orchestrator (Claude) under ADR 0074 (the owner's delegation of 2026-11-11), where ADR 0058 rule 4 had left it to the owner; the owner is told in the lead's report and may reverse it
review-by: 2026-12-11
---

# Build workers of other runtimes: Claude keeps the trading paths and keep-or-retire levels wait for three blind-scored rounds; the checking lane commits a builder's work

## Context and Problem Statement

ADR 0057 rule 8 has the build eval's first run set the levels that keep or retire a worker, and ADR
0058 rule 4 left to the owner which paths stay with Claude. The first trial's first wave is now scored
(`evals/build/runs/2026-11-10-first/scores.md`): sixteen runs, all resolved, settled marks 3 `land`,
8 `fix` and 5 `no` (one disputed), with the two readers agreeing on 9 of 16 raw marks. Waves 2 and 3
have since been scored on their re-marked sheets (`scores-waves-2-3.md`, joined in `scores.md`). ADR 0034 took the commit from a Codex builder only. What counts, what
is set when, which paths stay with Claude, and who commits a builder's work?

## Decision Drivers

* Two readers who agree on 9 of 16 marks give no single wave enough to keep or retire a runtime on.
* ADR 0074 rule 4 names the trading path: the adapter's order and data paths, the session window and
  the gate.
* One rule for every runtime, before and after a runtime's sandbox profile is in force (ADR 0034).
* "Every commit runs `scripts/ci`" (`AGENTS.md`, Automation); a gate that skips a case is quietly
  narrower than it says.

## Considered Options

1. Keep ADR 0057 rule 8 and ADR 0058 rule 4 as written: wave 1 sets the levels; the paths stay unsaid.
2. Set fixed levels from wave 1 now.
3. Set levels after three blind-scored rounds, relative to the reference; keep the trading paths with
   Claude until then; the checking lane commits every builder's work.

## Decision Outcome

Chosen: **option 3**, decided by the lead on 2026-11-11.

1. **A round** is one run of every ready case by every worker in a trial, graded by `harness.py` and
   marked by two readers apart. It counts only when the first reader's marks were written before that
   reader could see the second reader's; the README's settling, both rereading a `no`, comes after and
   is part of the round. Wave 1 counts: its first marks were made again by a reader with no sight of the
   second, and the reference's `no` from both readers came from that settling. Waves 2 and 3 count on
   their re-marked sheets, not on sheets marked after such a sight. The three-round mark is met when
   both of those sheets are on `main`.
2. **Levels** (supersedes ADR 0057 rule 8's "the first run sets them"). No runtime is retired or
   promoted on one wave. After three counted rounds this record states each level relative to the
   Claude Opus worker on the same cases; rule 8's conditions (a case the reference resolves in every run
   and the worker does not, a run both readers mark `no`, two of five changes abandoned or taking a third
   round) are weighed then. Rule 8's clause that a reference failing the bar puts the cases or the brief
   at fault is judged on the three rounds together; wave 1's reference has a `no` from both readers.
   Wave 1 stands as scored, with no worker named ahead. The rest of rule 8 stands: a broken term retires
   at once (ADR 0058 rule 3), work is not gated, a failed trial is not repeated unchanged, a new model,
   effort, configuration or runtime version is a new worker, the question is asked again at each
   `review-by`, and readers are pruned as ADR 0046 will state.
3. **Paths that stay with Claude** (supersedes ADR 0058 rule 4). Until three rounds are counted: the
   adapter's order and connection paths, the gate, anything that handles a credential, the paper runner,
   the session window and the node. Rule 4's own exclusions (dot-directories, `AGENTS.md`,
   `CLAUDE.md`) are kept. This is wider than rule 4's recommendation, which named the order path, the
   gate, credential handling and the paper runner: the session window and the node carry the same order
   path. ADR 0074's trading path also names the adapter's data path, which is not kept here; a change to
   it keeps ADR 0074's evidence tier for the trading path, whoever builds it. Everything else may go to
   a worker of another runtime now, under ADR 0058's terms. ADR 0074 rule 1 keeps risk limits with the
   owner; the lead reads this choice as one of means, which the owner may reverse.
   2026-11-11, evening: the owner directed that every open gap be built by workers of the other runtimes
   at maximum parallel; the lead reads this as reversing rule 3 for building, with Claude keeping the high
   review, the paper firing, the landing and credential-handling code; reversed again by one word from
   the owner or by a build on these paths that a Claude lane has to redo.
4. **Widening.** After three counted rounds, moving a trading path to another runtime is decided by the
   lead on the scored evidence and reported to the owner before the first such lane starts.
5. **A builder of another runtime does not commit** (supersedes ADR 0058 rule 2's "It commits on its
   branch"; extends to every runtime what ADR 0034 rule 1 set for Codex). It leaves its work in its
   workspace's tree, runs the offline checks its task names (ruff and the tests of the files it wrote),
   and reports. The Claude lane that reproduces its tests reads the diff and commits it, so the hook runs
   all of `scripts/ci` on the runtime that is allowed near the keychain, and lands it. A further switch that made
   `scripts/ci` skip a case for builders would narrow the gate (the one named skip,
   `CANON_TEST_NO_KEYCHAIN`, is for the hosted runner, and ADR 0082 fails any skip not named), and a routine `--no-verify` contradicts
   "every commit runs `scripts/ci`". `scripts/ci`'s `paper:store` case writes and removes throwaway
   keychain items and reads no credential, so a builder whose commit ran it would break no term; but a
   builder has no reason to touch the login keychain, and under this rule it never does.

### Consequences

* Good: no keep-or-retire call rests on one wave of unsteady marks; the trading paths wait for evidence.
* Good: one commit rule for every runtime, and `scripts/ci` runs whole on every commit.
* Bad: until three rounds are counted, other runtimes get no trading-path work, and a Claude lane
  spends time committing each builder's change.
* Bad: the levels are still to be written; this record says when, not what.
* Leaving: mark this superseded; ADR 0057 rule 8 and ADR 0058 rules 2 and 4 read as written.

### Confirmation

The lead read this record and accepted it on 2026-11-11; it lands with the dated pointers in ADR 0057
rule 8 and ADR 0058 rules 2 and 4 and the matching line in `AGENTS.md`. Each round's score
sheet says whether its first marks were written blind; wave 1's does. A builder's report names no
commit of its own. Two further choices of 2026-11-11, by the same decision-maker:

* (b) No sandbox is added beyond ADR 0034's profiles for now. The containment is ADR 0058's terms (its
  own workspace, no credential, no landing, no push, no paper run) and the check after each run that the
  tree and the clone's git config are untouched outside the workspace. It is revisited with the build
  eval's evidence of how each runtime behaves when it edits. No wave's check covered the clone's git
  config, and wave 1's did not cover the transcripts, network use, a push from elsewhere or a new
  scheduled job (UNVERIFIED for those runs). Waves 2 and 3 found five writes outside the tree by
  reading the reports, a lower bound; that evidence is not yet weighed here.
* (d) A person checks a sample once. After the first scoring the owner is offered a small sample of
  sheets to check the two automated readers; it holds nothing up. The sample is four runs, one per
  runtime and one per case, fixed by runtime and case before the table of workers was seen; some second
  marks had been seen, but no mark could steer the pick. Wave 1's sheet names it.

**The three rounds, 2026-11-11.** Both sheets of waves 2 and 3 are on `main`, so rule 1's mark is met
for Claude, Codex and Muse. Antigravity ran two models across wave 2, so neither of its workers has
three rounds. Of waves 2 and 3's first readings, 24 were redone blind; the 8 `modify-follows-replace`
runs were first-read by the scoring task, final before it read the second reader's. Wave 1's settled
marks stand, though read by waves 2 and 3's rule its reports would add two Claude broken terms. On the
three rounds together the reference fails the bar (runs both readers mark `no`, and broken terms), so
by rule 8's clause, kept in rule 2, the cases or the brief are at fault and no level is stated yet.
Rule 3's paths stay with Claude until the lead decides under rule 4. Open, for the lead: whether the
8 runs meet rule 1; whether the broken terms (Muse 3, the reference 2) retire under rule 2 or fall to
the brief under rule 8; and what in the cases or the brief changes before a new trial.

## Evidence

Read 2026-11-11.

* The lead's decision of 2026-11-11, restated above (a decision, not a source; not checkable here).
* ADR 0057 rules 6 and 8, ADR 0058 rules 2, 3 and 4, ADR 0034 rule 1 ("a builder cannot commit ...
  for a Codex build worker. It leaves the change in the working tree, and the lane that checks it
  commits it"), ADR 0074 rules 1 and 4 (read).
* `AGENTS.md`, Automation: "Every commit runs `scripts/ci` through `.githooks/pre-commit`" (read).
* `scripts/test-lanes`, header: "the paper:store case writes to the real login keychain, under
  throwaway services only, and deletes what it wrote on exit" (read).
* `evals/build/runs/2026-11-10-first/scores.md` — wave 1's marks, agreement and the blind re-marking
  (read). Waves 2 and 3: `scores-waves-2-3.md`, `marks-wave2.md`, `marks-wave3.md` and the joined
  table in `scores.md`, on `main` (read 2026-11-11). Their sheet says 24 first readings were redone by
  a fresh reader who had seen no marks, and the other 8 were final before the second reader's were read.
* https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents — "Because model outputs vary
  between runs, we run multiple trials to produce more consistent results"; "LLM-as-judge graders
  should be closely calibrated with human experts"; "Once the system is robust, it's sufficient to use
  human review only occasionally" (fetched; the second reader checked the quotes word for word). It
  sets no minimum number of trials.
* Second reader, 2026-11-11 (`evidence-verifier`, two passes): the quotes and wave-1 figures held. It
  found the session window missing from the kept paths; rule 8's reference clause and first condition
  misstated; the first wave's date, the containment list and the dropped UNVERIFIED on the cockpit's
  start wrong; the blind rule's relation to the reread unsaid; and `AGENTS.md` still saying a builder
  commits. All corrected.
* Third pass, 2026-11-11 (`evidence-verifier`), after waves 2 and 3 reached `main`: 18 claims held. It
  found the three-round mark met but unacted on, Antigravity without three rounds, the broken terms and
  the 8 modify runs unreconciled, (b)'s gap stated for wave 1 only, and rule 5 silent on the named
  keychain skip. Stated in Confirmation or corrected; the open points are the lead's.
