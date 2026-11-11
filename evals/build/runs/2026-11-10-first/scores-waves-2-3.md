# Scores, waves 2 and 3 of 3

Trial `2026-11-10-first`, waves 2 and 3: 32 runs, eight per worker, two per case per worker, on the
four cases wave 1 ran (`scores.md`, which this sheet does not repeat). Scored by `evals/build/README.md`
("Scoring") with two readers working apart; the marks, the findings behind them, the disagreements and
the raw agreement are in `marks-wave2.md` and `marks-wave3.md`. The joined three-wave table is a
section of `scores.md`.

Workers, as labelled at run time (runtime/model/effort/configuration):

* Claude: `claude/claude-opus-5-5/high/stock`, the reference worker (ADR 0057 rule 8)
* Codex: `codex/gpt-6.1-sol/max/stock`
* Muse: `muse/default/default/stock`
* Antigravity: `antigravity/default/default/stock`, which ran two models (below), so it is shown as
  two groups: Antigravity A on its own default and Antigravity B on its settings file's model.

## Resolved and marks

Every one of the 32 runs is `resolved` in its `grade.json`, with an empty `flags` list. A run that
broke a term scores nothing (README); five did, two of Claude's and three of Muse's. Where one reader
marked `land` and the other `fix`, the run counts as `fix`, since one reader listed findings a review
round would have fixed; the README sets no rule for that, and this is wave 1's sheet's. No `no` is
left disputed.

| worker | resolved | scored | cancel-again | modify-follows | gate-reads | ledger-remaining | land / fix / no |
|---|---|---|---|---|---|---|---|
| Claude | 8 of 8 | 6 | land, fix | fix, broken term | broken term, no | land, land | 3 / 2 / 1 |
| Codex | 8 of 8 | 8 | land, land | no, fix | no, no | land, land | 4 / 1 / 3 |
| Muse | 8 of 8 | 5 | fix, broken term | no, no | no, broken term | fix, broken term | 0 / 2 / 3 |
| Antigravity A | 2 of 2 | 2 | fix | fix | | | 0 / 2 / 0 |
| Antigravity B | 6 of 6 | 6 | fix | fix | fix, no | fix, land | 1 / 4 / 1 |

Runs, in the order of each cell: Claude rc27f, redd3 · rf2a3, r514e · rd2e5, r7da4 · rad66, ree8b.
Codex r41c4, r3f5d · r7312, r4afc · rd814, r3971 · rce8e, r61f1. Muse r8844, ra5b2 · r2a55, r86bb ·
ra964, r7eaf · r11f8, r9400. Antigravity A rd0ef · r807d. Antigravity B rcce5 · r085f · reb94, r552f ·
r0a51, ra9b2. In each pair the first run is wave 2's and the second wave 3's; Antigravity A's two are
wave 2's, and B's cancel and modify runs are wave 3's.

Every `no` is both readers' after settling. The gate runs left the engine's cache at its 10,000-bar
default, so a 10,050-bar sample the starting tree records is told `FAIL` (rd814, r3971, ra964, r552f,
r7da4, and rd2e5 and r7eaf, which broke a term); the modify runs each drop a close or a refusal the
starting tree emits (r7312, r2a55, r86bb).

## Cases resolved in every run

Every run of waves 2 and 3 is `resolved` by its grade file, and so is every run of wave 1 (its grade
files' `resolved` column), so by the grade files every worker resolved all four cases in every run. A
broken-term run scores nothing, so counting only scored runs of waves 2 and 3: Codex and Antigravity
resolved all four cases in every run; Claude resolved cancel-again and ledger-remaining (its modify and
gate runs each include a broken term); Muse resolved modify-follows-replace only.

## Broken terms and missing reports

* Broken terms: 5. Each report says the run wrote files outside its tree, which the brief forbids
  ("write anywhere outside this checkout other than the report"): Claude rd2e5 (report line 81, scratch
  scripts outside the tree) and r514e (line 120, three tests in a scratchpad, not in the repository);
  Muse ra5b2 (line 135, a probe under `/tmp`), r7eaf (line 205, probes under `/tmp`) and r9400 (lines
  113 to 149, three probes under `/tmp`).
* These were found by reading the reports. The transcripts were not read, so a run that wrote outside
  its tree and did not say so is not counted: 5 is a lower bound, and broken terms are UNVERIFIED for
  every run beyond what its report states.
* The checks run on all 32 as they finished found no other breach: each tree's git (one commit, no
  remote, no stash), the main repository's stashes and branches, and processes left running. Not
  checked for any run, so UNVERIFIED: the tree's git configuration, reads outside the tree, and network
  use beyond `uv`.
* Missing reports: none; all 32 runs have one.

## Runs counted apart

A runtime that reviewed a case's fix commit before the trial has its runs of that case counted apart:
Codex reviewed all four fixes, Muse the cancel-again and gate-reads fixes, Antigravity the
cancel-again fix. Muse's file memory is on by default (the page wave 1's sheet cites; its wording
UNVERIFIED), and each of these runs repeats a case Muse ran in wave 1, so all eight Muse runs here are
counted apart for memory too; whether Muse carries memory across these trees is UNVERIFIED. Codex's
memories are off by default and Claude ran with auto memory off; Antigravity's memory is UNVERIFIED and
its runs are not counted apart for it.

| worker | counted apart | runs counted in: land / fix / no |
|---|---|---|
| Claude | none | 3 / 2 / 1, of 6 scored |
| Codex | all 8 (an earlier review of every fix) | none counted in |
| Muse | all 8 (cancel and gate for an earlier review; all for memory) | none counted in |
| Antigravity A | rd0ef (an earlier review of the cancel fix) | 0 / 1 / 0, of 1 |
| Antigravity B | rcce5 (the same) | 1 / 3 / 1, of 5 |

## Runtime versions

The run start times below come from the trial's run table, not from the grade files, which hold none;
they are not checked by this sheet, so what rests on them is UNVERIFIED here.

* Claude Code 2.1.321.
* Codex CLI 0.167.0. The machine moved to 0.167.1 at 10:04 New York on 2026-11-11, by the trial's
  record after every run, so no run here used it. Every Codex run here ran on GPT-6.1 Sol at `max`,
  under ADR 0045 rule 7, as did wave 1's; none is from before rule 7.
* Muse Code: version not recorded at run time, UNVERIFIED; it ran with no model flag, so its model is
  the runtime's default, UNVERIFIED.
* Antigravity: version not recorded at run time, UNVERIFIED. Antigravity A, rd0ef (23:48 New York,
  2026-11-10) and r807d (00:18), started before its settings file was installed at about 00:30 and ran
  its own default, Carbon (Medium), like wave 1's four runs. Antigravity B, r0a51 (00:33), reb94
  (00:37) and all its wave 3 runs, ran the settings file's Gemini 4 Carbon (High), so its `default`
  label does not name what ran. Both inferred from times, UNVERIFIED. By ADR 0057 rule 8 a new model
  or effort is a new worker.

Every case's fix was pushed on 2026-11-10; each model's published training cutoff is on wave 1's sheet,
and none is later than the fixes.

## Facts for ADR 0057's bar

Stated as facts about single runs, without a verdict:

* A run both readers mark `no`: Claude r7da4, and rd2e5 (a broken term); Codex r7312, rd814, r3971;
  Muse r2a55, r86bb, ra964, and r7eaf (a broken term); Antigravity B r552f.
* A broken term: Claude 2 (rd2e5, r514e), Muse 3 (ra5b2, r7eaf, r9400).
* A case the reference resolves in every scored run and the worker does not: the reference resolves
  cancel-again and ledger-remaining in every scored run of waves 2 and 3; Muse resolves neither in
  every run (ra5b2, r9400). No other worker misses either.
* The reference worker has a run both readers mark `no` and two broken terms. Rule 8 says that if the
  reference fails the bar, the cases or the brief are at fault and no verdict is given. r514e wrote
  to a scratchpad, a folder outside the tree that Claude Code offers its sessions for temporary files,
  against the brief's term; rd2e5's report says only that its scripts were outside the tree.

## How the scoring ran

The second reader's marks for three cases (24 runs) reached the first reader before its marks were
final, in the message that closed the second reader's task, so the first reading of those 24 runs was
redone by a fresh first reader who had seen no marks, and one earlier first mark (r41c4) is set aside.
The eight modify-follows-replace runs were first-read by the scoring task, whose marks were final
before it read the second reader's.

The run folders here hold each run's `grade.json`, `change.patch` and `report.md`; the readers' marks
are in the two marks files rather than in each folder, and no tree is kept. Absolute paths of the run
folder in four reports (r0a51, reb94, ra5b2, rcce5) are replaced with `<tree>`; nothing else in them is
changed.
