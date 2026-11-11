# Score sheet: run 2026-11-10-first, wave 1 of 3

The first wave of the build eval's first trial (ADR 0057), scored by the rule in
[`evals/build/README.md`](../../README.md) ("Scoring", "Reading a score"). One run per worker per case;
waves 2 and 3 add the second and third. Each run's folder holds its `grade.json`, `change.patch`,
`report.md` and both readers' mark sheets (`first-reader.md`, `second-reader.md`). No tree is kept.

## The run

* **Cases:** the four `ready` cases: `cancel-again-and-unholdable-id` (fix `f8505c4`, #77),
  `modify-follows-replace` (`52ac69c`, #69), `gate-reads-what-the-run-read` (`ba44c25`, #79) and
  `ledger-remaining-defects` (`6e6fed7`, #72). Every fix was pushed on 2026-11-10.
* **Workers**, one run of each case each, all `stock` (no skills or plugins added):

  | Worker | Model and effort that ran | Runtime version | Memory across sessions |
  |---|---|---|---|
  | `claude/claude-opus-5-5/high/stock` (the reference, ADR 0057 rule 8: the build lane at ADR 0005's effort for a hard change) | Opus 5.5, high | Claude Code 2.1.321 | Auto memory off, through the tree's ignored `.claude/settings.local.json` |
  | `codex/gpt-6.1-sol/max/stock` | GPT-6.1 Sol, max (ADR 0045 rule 7) | Codex CLI 0.167.0 | Memories exist and are "off by default"; the config sets nothing, so off |
  | `muse/default/default/stock` | Started with no model or effort flag, so `muse-spark-1.2` at `high` by ADR 0059's statement (UNVERIFIED for these runs) | UNVERIFIED (not recorded) | File memory, on by default; no opt-out documented, so on (below) |
  | `antigravity/default/default/stock` | Its own default, most likely Gemini 4 Carbon (Medium): UNVERIFIED, inferred from times. The settings file that selects Carbon (High) landed with #138 at 04:29 UTC and was installed later; every run here started before it | UNVERIFIED (not recorded) | No memory feature documented for the CLI ("semantic memory" is named, not explained): UNVERIFIED |

  Muse and Antigravity therefore ran their own defaults, not those ADR 0059 sets.

* **When:** each run started between 03:17 and 04:14 UTC on 2026-11-11, hours after the fixes were
  pushed on 2026-11-10, with 12 to 18 other agent sessions running at its start.
* **How:** each worker ran in its tree as a folder of its own, not a worktree of this repository.
* **Codex and ADR 0045 rule 7:** all four Codex runs are under rule 7 (GPT-6.1 Sol at `max`). None is
  from before it.
* **Broken terms:** none found. In the grades, every run's `checks` and `tests` held and no test file
  was restored. Each run was checked as it finished, before grading: its tree's git had one commit, no
  remote and no stash; the main repository had no stash and no new branch; no process was left running
  from the tree except, for the four Codex runs, the Codex process itself, still open before release.
  Not checked for any run, so UNVERIFIED for all sixteen: the transcripts (reads outside the tree,
  network use beyond `uv`), the clone's git config, and a push through `gh` or another checkout or a
  new scheduled job (README, "Running a trial", step 4).
* **Missing reports:** none; 16 of 16 present.
* **Flags:** only `rd513` has any: 54 files under `.pytest-tmp/`, pytest's own output. Both readers read
  every entry; no test reads them, so neither marked blocker 1. The committed patch has the absolute
  scratch paths inside those files replaced by `<tree>/`; one report link and one report title were
  likewise made relative or dropped. Nothing else in the copied files was changed.

## Training data

A case is not scored for a model whose published cutoff is later than its fix (2026-11-10). None is:

| Model | Published training cutoff | Source (read 2026-11-11) |
|---|---|---|
| Claude Opus 5.5 | Jun 2026 (training data and reliable knowledge) | https://platform.claude.com/docs/en/about-claude/models/overview |
| GPT-6.1 Sol | Apr 30, 2026 | https://developers.openai.com/api/docs/models/gpt-6.1-sol (the API page; no Codex page states one) |
| Gemini 4 Carbon | March 2026 | https://deepmind.google/models/model-cards/gemini-4-carbon/ |
| Muse Spark 1.2, Muse Wonder 1.0 | none published | https://dev.meta.ai/docs/models and each model's page; the Wonder card PDF could not be read (UNVERIFIED) |
| Not run in this wave: Claude Fable 5.2 | Jun 2026 | the Claude models overview above |
| Not run in this wave: DeepSeek Flash (V4.1-Flash-1017) | none published | https://api-docs.deepseek.com/quick_start/pricing, the Hugging Face card and the technical report give none |

## Runs counted apart

* **An earlier read of the fix**, from the second-reader reviews of landed changes (ADR 0045):
  * `cancel-again-and-unholdable-id` (#77): Codex, Muse and Antigravity each reviewed it.
  * `modify-follows-replace` (`52ac69c`): Codex read it (its triage of a later review names the commit).
  * `gate-reads-what-the-run-read` (#79): Codex and Muse.
  * `ledger-remaining-defects` (`6e6fed7`): Codex.
  * So Codex's four runs are all counted apart, Muse's `cancel` and `gate` runs, and Antigravity's
    `cancel` run. No Claude run is, by this rule; but every fix was written and reviewed by Claude Code
    sessions, and the review benchmark (`evals/review/runs/2026-11-10-first/`) had all four runtimes
    read the pre-fix code of two of these modules. Neither is counted, since the README counts only a
    read of the fix itself.
* **Memory left on:** Muse's file memory is on by default with no documented opt-out
  (https://dev.meta.ai/docs/muse-code/configuration, "At the start of a session, Muse Code injects an
  index of your memory", scoped to the project; read through a summary, so the wording is
  UNVERIFIED). With one run per case nothing is counted apart for it in this wave: a later run of a case
  is what memory would make dependent. Whether each tree is a separate project to Muse is UNVERIFIED.
  Codex: https://learn.chatgpt.com/docs/customization/memories, "Local Codex memories are off by
  default".

## Totals per worker

Resolved: the grade's verdict. Marks are the settled marks (below). "Counted in" leaves out the runs
counted apart.

| Worker | Resolved | land | fix | no | Counted apart | Counted in: resolved, land / fix / no |
|---|---|---|---|---|---|---|
| `claude/claude-opus-5-5/high/stock` | 4 / 4 | 0 | 2 | 2 (one disputed) | none | 4 / 4; 0 / 2 / 2 |
| `codex/gpt-6.1-sol/max/stock` | 4 / 4 | 1 | 3 | 0 | all four | none counted in |
| `muse/default/default/stock` | 4 / 4 | 1 | 1 | 2 | `cancel`, `gate` | 2 / 2; 1 / 0 / 1 |
| `antigravity/default/default/stock` | 4 / 4 | 1 | 2 | 1 | `cancel` | 3 / 3; 1 / 1 / 1 |
| **Total** | 16 / 16 | 3 | 8 | 5 | 7 | |

**Cases resolved in every run:** all four, for every worker. With one run per case this is not yet
meaningful; it becomes pass^3 when waves 2 and 3 are in.

## Per case

`no (n)` names the blocker. Where one reader marked `land` and the other `fix`, the settled mark is
`fix`, since one reader listed findings a review round would have fixed. The README sets no rule for
that case; this is the sheet's, and it decides four runs (`r1e4c`, `r9ac6`, `ra977`, `rb2b6`).

| Case | Claude | Codex | Muse | Antigravity |
|---|---|---|---|---|
| `cancel-again-and-unholdable-id` | `rb2b6` fix | `r20c6` land\* | `ra5ef` fix\* | `rf9d4` fix\* |
| `modify-follows-replace` | `r1b0e` **no (4)** | `r9ac6` fix\* | `ra205` **no (1 / 4)** | `rb62a` **no (4)** |
| `gate-reads-what-the-run-read` | `rff8d` fix | `rd513` fix\* | `re3df` **no (4)**\* | `r1e4c` fix |
| `ledger-remaining-defects` | `r5539` **no, disputed** | `ra977` fix\* | `r3124` land | `r7a51` land |

\* counted apart.

What each `no` rests on (each shown by a probe the reader ran; the starting code behaved correctly):

* `r1b0e`: a modify answered HTTP 500 and not applied stays open, and a later modify that succeeds
  through alpaca-py's retry, its repeat refused 422 "order already replaced", gets `OrderUpdated` and
  then a false `OrderModifyRejected`.
* `ra205`: defect 8 stays under another timing. If the first modify's `replaced` arrives during the
  second modify's refusal lookups, the second modify gets no `OrderModifyRejected`. The first reader
  named blocker 1 (only its test is satisfied), the second blocker 4.
* `rb62a`: for an order loaded by start-up reconciliation, a partial fill on the replacement before
  `replaced` leaves the node's order at the old quantity; the replacement's later fill is dropped as an
  overfill and no `OrderUpdated` is sent.
* `re3df`: the readers agree on `no` for different reasons. The first: the ranking now stops on a
  record that failed before its sample number was written, but the unchanged study runner never runs
  that key again. The second: a run over the engine's default cache of 10,000 bars fails (shown with
  10,050 daily bars).
* `r5539`: `open_study` opens the ledger's lock file read-write, so a write-protected ledger that
  opened before now raises `PermissionError`. Both readers reproduced it; they disagree on whether it is
  a blocker (below).

## Reading this wave against ADR 0057 rule 8

* Runs both readers mark `no`: `r1b0e` (Claude), `ra205` and `re3df` (Muse), `rb62a` (Antigravity).
  `r5539` (Claude) is disputed and counted `no`, but is not a `no` both readers give.
* The reference worker has one of them (`r1b0e`). Rule 8 as written says that if the reference fails
  the bar, "the cases or the brief are at fault and no verdict is given". So this wave gives no
  verdict on any worker. A choice of 2026-11-11, whose record is pending, sets the retire levels only
  on the first three rounds together, relative to the Claude Opus worker on the same cases; whether
  the cases or the brief are at fault is judged on all three.
* Four cases from two modules tell only all against none (README, "Reading a score").

## The readers

Both readers are Claude Opus 5.5, each with the patch applied to a fresh starting tree, the case file,
the report and the README's Scoring section, and neither with the other's marks or the table of
workers until every mark was written. The first reader's reading includes `/code-review` at high
effort, the effort the cases' paths get. The second reader's tree is `tree2` in its sheet.

**What happened to the first reading, and what was done.** The scoring session saw the second readers'
marks as they finished. Eight of the sixteen `/code-review` passes had been started from that session
after it had seen them, so they could have been anchored on them. Those eight were run again from fresh
contexts with no sight of the second readers, and a fresh first reader, given only the sixteen clean
reviews, the patches, the case files, the reports and the Scoring section, made all sixteen first
marks. The scoring session made no mark itself. So the first marks below are blind to the second; the
raw agreement can be read as two independent readings.

### Disagreements

| Run | First reader | Second reader | After both reread | Settled |
|---|---|---|---|---|
| `r1b0e` | land | no (4) | both no (4); the first reader reproduced it | no (4) |
| `rb62a` | land | no (4) | both no (4); the first reader reproduced it, with no reconnect | no (4) |
| `r5539` | no (4) | fix | first: fix; second: no (4). Each moved to the other's mark | disputed, counted no |
| `r1e4c` | land | fix (bar cache cap) | not reread: no `no` | fix |
| `r9ac6` | land | fix (an unmatched refusal is silent; an unknown outcome leaves the modify open) | not reread: no `no` | fix |
| `ra977` | land | fix (a DST-fold claim in the ranking header; a stale docstring) | not reread: no `no` | fix |
| `rb2b6` | land | fix (its new ADR marked accepted; a start-up message) | not reread: no `no` | fix |

Agreed on the mark but not on the reason: `ra205` (blocker 1 against blocker 4) and `re3df` (above).

**Raw agreement** on the mark, before any reread: 9 of 16 (56%). After the rereads: 11 of 16.

**How the readers' bars differed.** The first reader held nothing against one run that every run of
the case shares: the engine's default cache of 10,000 bars (the supplied gate test's design needs it),
start-up seeing both a `pending_replace` order and its replacement, and replace state lost across a
client reconnect. The second reader marked the cache limit `no` for `re3df` and `fix` for the other
three gate runs. On `r5539` each reader, rereading, took the other's first view: whether a regression
on an input nothing in the repository uses and nothing promises is a blocker is not settled by the
README's wording.

**Open, found while settling, not counted.** Rereading `r1b0e`, the first reader ran its probe on the
other modify runs: `r9ac6` and `rb62a` give the same false `OrderModifyRejected`, `ra205` does not.
Marks were final by then, and neither reader had marked `r9ac6` `no`, so it stays `fix`; its second
reader had named the open modify left by an unknown outcome as a fix finding. Whether that is a blocker
for `r9ac6` is open.

## The sample for a person's check

Fixed on 2026-11-11 by runtime and case, before the table of workers was seen: one run per runtime and
one per case. Some second-reader marks had been seen by then, but without the table no mark could
steer which run a runtime-and-case choice picked. Claude's `cancel` run (`rb2b6`), Codex's `gate` run (`rd513`), Muse's `modify` run
(`ra205`) and Antigravity's `ledger` run (`r7a51`). It is offered to the owner to check the two
automated readers, and holds nothing up (a choice of 2026-11-11, whose record is pending).
