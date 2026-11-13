# Score sheet: run 2026-11-13-dsh-flash

This sheet records one run of each of the four `ready` cases by DeepSeek Harness on DeepSeek Flash. The worker ran
in the configuration Orca's DeepSeek workers use (ADR 0136). The lead ordered the run on 2026-11-13, as a graded
run of DeepSeek Flash through the harness. The runs are scored by the rule in
[`evals/build/README.md`](../../README.md), under "Scoring" and "Reading a score".

Each run's folder holds:

* `grade.json`, `change.patch` and `report.md`;
* `code-review.md`, the `/code-review` findings the first reader was given;
* `first-reader.md` and `second-reader.md`, the two readers' mark sheets;
* for a run both readers mark `no`, the probe each reader ran to show it, saved as `.py.txt`. `r56d4`'s first
  reader appended its probes to a copy of the starting tree's `test_execution_client.py`, so only the appended
  functions are kept, under a three-line header that says so.

No tree or transcript is kept. Paths on the machine that ran the trial are replaced by `<score>` and `<checkout>`.
Some of what follows rests only on the coordinator's own log, kept outside the repository: the effort on each
tab's first screen, mise's prompt, the state after each run, the readers' final messages and mise's trust list.

## The run

* **Cases:** the four `ready` cases, as in the first trial:

  | Case | Fix | Pull request |
  |---|---|---|
  | `cancel-again-and-unholdable-id` | `f8505c4` | #77 |
  | `modify-follows-replace` | `52ac69c` | #69 |
  | `gate-reads-what-the-run-read` | `ba44c25` | #79 |
  | `ledger-remaining-defects` | `6e6fed7` | #72 |

  Every fix was pushed on 2026-11-10. The cases, the brief and the harness are unchanged since `e0f7db09`
  (2026-11-10), so they are the first trial's. ADR 0079 found, on that trial's three rounds together, that the
  reference fails the bar, so the cases or the brief are at fault. It left open what in them changes before a new
  trial. This run changes neither.
* **Worker:**

  | Worker | Model and effort that ran | Runtime version | Memory across sessions |
  |---|---|---|---|
  | `dsh/deepseek-flash/max/orca-worker` | `deepseek-flash` through DeepSeek's own provider, at Max effort, as the coordinator read on each tab's first screen (`deepseek-flash · Max effort`). DeepSeek's docs serve that name as DeepSeek-V4.1-Flash-1017 | DeepSeek Harness 0.2.1-rc.3, dsh-TUI 0.17.0 | Off. The profile's patch sets `memento` and `session-log-deepseek` to `enabled: false` (ADR 0045 rule 6) |

  **Configuration.** The worker ran the `dsh-tui` profile that Orca's DeepSeek workers start with, as it was on
  2026-11-13. Its patch file was last written at 10:13 New York, before the runs. The profile held:

  * first-party packages at 0.2.1-rc.3: `dsh-experimental-terminal-bundle`, `dsh-hook-protocol` and
    `dsh-hooks-claude-code`;
  * dsh-TUI 0.17.0;
  * community packages: `aegis` at commit `e5f43c54`, `dsh-advisor` 0.6.4, `dsh-doublecheck` 0.9.29,
    `dsh-fail-guard` 0.2.1, `dsh-github-skills` 0.2.1, `dsh-llm-fallbacks` 0.6.12, `dsh-memento` 0.5.28 (off)
    and `dsh-tui-find` 0.5.2.

  The profile's patch sets approval to `never`. Each worker's transcript names its file sandbox as
  `workspace-write`, when it plans where to write its report (ADR 0136).

  **Against the record.** Read against ADR 0086 and the registry entry `deepseek-harness`, this is ADR 0086's
  review configuration (stock and `dsh-experimental-terminal-bundle`) with two differences:

  * `dsh-fail-guard` 0.2.1, which has no registry entry;
  * `aegis` at `e5f43c54`, where the entry records `0461c211e4d8`.

  Neither was adopted through `adopt-upstream` before the run, as the README's "A worker" asks of a named
  configuration. So `orca-worker` names the configuration as it ran, not one the registry holds. No run of stock
  was made beside it, as ADR 0072 rule 2 (`proposed`) would have.

  **Which model answered.** Each exported transcript names one model, `deepseek-flash`. `dsh-llm-fallbacks` was
  loaded, and its README says "nothing happens until you configure content". Nothing configures it:

  * the profile's patch has no row for it;
  * the dsh home has no home-level patch and no `settings.yaml` (read at 16:27 New York);
  * a copy of the profile's `package.json`, workspace file and patch, installed into a throwaway dsh home at 15:04
    and composed by the same engine with `--dump-config`, gives it an empty config.

  The live profile's composed config was not dumped. So that no turn went to another model is an inference:
  UNVERIFIED.
* **When:** each run started between 14:59:00 and 14:59:02 New York (19:59 UTC) on 2026-11-13, three days after
  the fixes were pushed. At the start, the load average was 26.75 and other agent sessions were running. Reports
  were written 8 to 16 minutes after the start. The time limit, 90 minutes a run, was set before any result.
* **How each run was started:**
  * Each run had a tree of its own (`harness.py tree`), in a folder under the host's temp area.
  * It ran in an Orca terminal tab, started by a script that runs dsh-TUI in the tree with the task as its first
    prompt.
  * The script set `UV_CACHE_DIR` in the trial folder, a root the sandbox may write (ADR 0136).
  * Each tab first stopped at mise's prompt to trust the tree's `mise.toml`. The coordinator trusted each tree and
    answered the prompt before any worker turn.
* **Broken terms:** none found.
  * **What was read.** The first trial read only the reports. This trial read every tool call in each run's
    exported transcript: 61, 90, 113 and 109 calls for `rf5bf`, `r6160`, `rbd30` and `r56d4`.
  * **What the calls did and did not do.** Every edit and write was inside the tree, except each run's report.
    No call:
    * wrote with git;
    * used the network beyond `mise exec -- uv run --locked`;
    * read a credential;
    * ran a paper test;
    * started an agent or a scheduled job.

    No client web tool was used.
  * **Server-side search.** On 2026-11-13, the day of the runs, DeepSeek announced server-side web search and URL
    reading for `deepseek-flash`, with no time of day given (https://api-docs.deepseek.com/news/news261113).
    * Its docs describe each as a tool a request declares, `deepseek_web_search` or `deepseek_read_url`
      (https://api-docs.deepseek.com/api/create-chat-completion). They do not say what a request that declares
      neither gets.
    * Neither name appears in the engine or dsh-TUI as installed, in the profile's packages, or in the engine's
      source at its pin.
    * No exported transcript shows a citation or a server-side tool event. Whether an export would show one is
      not known.
    * Of the 63 URLs in the four transcripts, 61 are in the repository's files. The other two are a SQLAlchemy
      error link in a test's output and a Python documentation link in `r6160`'s own reasoning.

    That no server-side search ran is UNVERIFIED.
  * **Reads outside the tree.** Each is listed here, by call:
    * Every run tested, listed or read its run folder or its own report, which is where the brief has the report
      written:
      * `rf5bf`: `ls -ld` on the folder; `test -f`, `wc -l` and `test -s` on the report, and two reads of it
        while it edited it;
      * `r6160`: `test -e` and `wc -l` on the report;
      * `rbd30`: `find` in the folder for `.venv`, `ls -la` on it, and `test -f` on the report;
      * `r56d4`: `ls -l` and `head` on the report.
    * `rbd30` read, in four calls with `grep`, `sed` and `wc -l`, the file under the temp area where dsh had saved
      the output of its own earlier pytest command.
    * `rf5bf`, `rbd30` and `r56d4` each loaded Aegis's `systematic-debugging` skill with dsh's `skill` tool,
      which reads it from the profile's installed packages.

    None of these reads a copy of the repository, its history, `evals/`, the case file or another run. So none
    is counted as a breach. Read literally, the README's "a read outside the tree" would count every one of
    them. This sheet reads that sentence by the purpose the README gives for it: other checkouts are on the
    machine, and the repository is public. That reading is this sheet's own.
  * **State after each run.** The coordinator checked each run when it ended, with git in the tree and in the
    shared repository, and with the process list. The tree had one commit, no remote, no stash and one branch,
    and no process named it. The shared repository gained new branches, all of them other sessions'.
* **Missing reports:** none; 4 of 4.
* **Flags:** none in any grade.

## Training data

A case is not scored for a model whose published cutoff is later than its fix (2026-11-10). This model is not:

| Model | Published training cutoff | Source (read 2026-11-13) |
|---|---|---|
| DeepSeek-V4.1-Flash-1017 (`deepseek-flash`) | None published. Its release is dated 2026-10-17, before every fix | https://api-docs.deepseek.com/quick_start/pricing (name and version), https://api-docs.deepseek.com/news/news261017 (release), https://api-docs.deepseek.com/ |

## Runs counted apart

* **An earlier read of the fix:** none found.
  * No comment or review on #77, #69, #79 or #72 names DeepSeek or dsh (`gh pr view`, 2026-11-13).
  * The first trial's sheet lists no DeepSeek read.
  * The `Confirms:` trailers on `main` that name a DeepSeek review answer 14 commits, made from 2026-11-11 to
    2026-11-13. None of them is a case's fix. Two change a file a fix also changed:
    * `65143368` (#270) changes `packages/canon-alpaca/README.md`, which `f8505c4` changed, and
      `packages/canon-alpaca/tests/conftest.py`, which `52ac69c` changed;
    * `04e2d9f3` (#367) changes that README.

    Neither touches the lines a fix changed (`git show -U0`). In the README, the nearest hunk is some 180 lines
    (#270) and 350 lines (#367) from the fix's line. In conftest, #270's hunks are in the fake historical client,
    the socket and the fixtures, not the fake orders client `52ac69c` changed. A reader of those changes may
    still have read the fixed files. The README counts apart a runtime that was given a case's fix to review, and
    neither commit is a fix. So this sheet counts no run apart for them. That reading is this sheet's own.

  A review that no commit answered leaves no trace in those places. That none happened is therefore UNVERIFIED.
* **Memory left on:** none (above).

## Totals

Resolved is the grade's verdict. The marks are the settled marks, below.

| Worker | Resolved | land | fix | no | Counted apart | Counted in: resolved, land / fix / no |
|---|---|---|---|---|---|---|
| `dsh/deepseek-flash/max/orca-worker` | 4 / 4 | 0 | 2 | 2 | none | 4 / 4; 0 / 2 / 2 |

**Cases resolved in every run:** all four. There was one run of each, so this is pass^1, not the three runs the
README sets by default.

**A counted round.** By ADR 0079 rule 1, a round is one run of every ready case by every worker in a trial, graded
by `harness.py` and marked by two readers apart. It counts when the first marks were written before the first
reader could see the second's.

* This run is that for its one worker, so it is that worker's first counted round.
* Its first marks were written blind to the second readers' marks (below). They were not blind to the worker
  (below).
* No reference worker ran in it.

## Per case

| Case | Run | First reader | Second reader | Settled |
|---|---|---|---|---|
| `cancel-again-and-unholdable-id` | `rf5bf` | fix | fix | fix |
| `modify-follows-replace` | `r56d4` | no (4) | no (4) | **no (4)** |
| `gate-reads-what-the-run-read` | `rbd30` | no (4) | no (4) | **no (4)** |
| `ledger-remaining-defects` | `r6160` | fix | fix | fix |

### What each `no` rests on

Each blocker 4 was shown by a probe the reader ran. On each probe's input the starting code behaved correctly. The
case's own fix was handled differently in the two runs:

* for `r56d4`, it passed the same probes;
* for `rbd30`, both readers read it and neither ran it on their inputs. It sizes the cache through `keep_bars`.

* **`r56d4`, blocker 4.** After one modify's replacement has been adopted, the change ignores a `canceled` or
  `expired` update for any other order as a "superseded order" (`execution.py:1520-1527`). That check runs before
  the step that follows the replace chain forward. So when the newest replacement is canceled or expires before
  its `replaced` arrives, its close is dropped and the node's order stays open at the older venue order.

  Both readers showed it with two modifies in a row and the second's `replaced` held back or never sent:

  | Reader | Second replacement expires or is canceled | `replaced` arrives late | Node's own cancel, refused 422 on the first replacement and sent on to the second |
  |---|---|---|---|
  | First | No `OrderExpired` or `OrderCanceled`; the node's order stays `PENDING_UPDATE` | The node's order is moved onto the ended order and left `ACCEPTED` | It stays `PENDING_CANCEL` |
  | Second | The order never closes | The order is moved onto the ended order and left open | The order never closes |

  The statement treats a missed `replaced`, and a replacement's update before its `replaced`, as events that
  happen. Beyond this, each reader names other findings it marks blocker:

  * Both: `_replacement_took_over` swallows a failed REST read (`except APIError, OSError: return False`) and logs
    nothing. A reconciliation that meets one failed read then loses the node's filled replacement with no message.
    The second reader names the same pattern in `_replaces_chain_contains` as well.
  * The first reader: the report says a replacement's cancel or expiry before `replaced` "adopts the replacement's
    terms first", which the change does not do for these updates.
  * The second reader: a second modify gets no `OrderModifyRejected` when its PATCH is refused 422 while the first
    modify's `replaced` is being applied.
    * When the second modify asks for a price the first replacement does not hold, the starting code gives one.
    * On the statement's own terms the starting code gives none, which is defect 8 itself.
    * The case's own fix gives none in either.
* **`rbd30`, blocker 4.** The change reads the run's bars back from the engine's cache and requires one cached
  bar per iteration. It leaves that cache at NautilusTrader's default `bar_capacity` of 10,000 bars per bar
  type. So every trial with more than 10,000 bars of one bar type is told `FAIL`, where the starting code
  recorded it.

  Each reader showed this with its own probe, kept in the run's folder:

  | Reader | Sample | Starting tree | Change |
  |---|---|---|---|
  | First | 26 regular sessions of one-minute bars (10,140 bars) | `COMPLETE` | `FAIL` |
  | Second | 27 regular sessions of one-minute bars (10,530 bars) | `COMPLETE` | `FAIL` |
* **`rbd30`, blocker 2.** Both readers also named this one. The change finds the uncovered parts of the sample
  with interval arithmetic of its own. NautilusTrader's `ParquetDataCatalog.get_missing_intervals_for_request`
  answers the same question (AGENTS.md rule 3). `canon_trading.bars_to_catalog` already calls it, and so does the
  case's own fix.

### What the `fix` marks rest on

In both `fix` runs, both readers mark every defect in the statement removed. Two of those marks have a limit:

* `rf5bf`'s first reader marks defect 4 removed for the statement's case;
* both of `r6160`'s readers mark defect 2 removed except in a fall-back hour.

* **`rf5bf`.**
  * Both readers found that the text and the code disagree on which ID an order is reported under, when its
    `replaces` chain mixes IDs NautilusTrader can and cannot hold. The README and the module header give the
    order's own ID. `_root_id` walks up the chain to the first ID that can be held. Each reader showed this with
    a probe, and the case's own fix keeps the replacement's own ID.
  * The first reader also found two more:
    * A trace that finds no holdable ID is not cached, so it is walked again on each update for that order. Each
      walk costs one REST lookup per link, under the client's lock.
    * A comment is stale.
* **`r6160`.**
  * Both readers found a false claim in the new docstrings. They say `astimezone(UTC)` "converts back" Optuna's
    naive local start. In a fall-back hour this fails: the fold is lost and the earlier instant is taken. The
    limit is stated only in the worker's report.
  * The first reader also showed that `open_study` takes no part in the new creation lock. So a process that
    opens a study while another is creating it gets `LedgerNotFound`. The second reader set this aside: it is not
    one of the six defects, and its own probe showed no failure in 10 rounds.

## Against ADR 0057 rule 8

* **One wave.** As amended by ADR 0079, the levels are set after three counted rounds, relative to the Claude Opus
  worker on the same cases, and no runtime is retired or promoted on one wave. A broken term still retires at
  once, and none was broken.
* **Runs both readers mark `no`:** `rbd30` and `r56d4`. In the first trial, these two cases are also where the
  reference worker has runs both readers mark `no` (that trial's `scores.md` and `scores-waves-2-3.md`):
  * `gate-reads-what-the-run-read`: the same cache limit was a `no` for the reference worker's `r7da4`, Codex's
    `rd814` and `r3971`, Muse's `ra964` and Antigravity's `r552f`.
  * `modify-follows-replace`:
    * the reference worker's `r1b0e` gave a false `OrderModifyRejected`;
    * Codex's `r7312`, and Muse's `r2a55` and `r86bb`, each dropped a close or a refusal the starting tree emits;
    * Muse's `ra205` left defect 8 under another timing;
    * Antigravity's `rb62a` dropped a replacement's fill.

  Rule 8 as first written gives no verdict when the reference fails the bar, since then the cases or the brief
  are at fault. The first trial's reference worker failed it in every wave.
* **A case the reference worker resolves in every run and this worker does not:** none. Every run here is
  resolved.
* **What four cases can show.** Four cases from two modules tell only all against none (README, "Reading a
  score").
* **What came of it.** The lead ruled on these marks on 2026-11-13, at 16:13. DeepSeek workers do not build on the
  order or stop path. They read, write text and blind tests, and build off the trading path. The order and stop path
  stays with the builders ADR 0058 names, reproduced by a Claude lane. The lesson goes to the lessons journal
  (`docs/handbook/journal.md`) in a change of its own.

## The readers

* **Who and what they had.** Both readers are Claude Opus 5.5 subagents. Each had:
  * the patch applied to a fresh starting tree of its own;
  * the case file;
  * the report;
  * the README's Scoring section.

  Neither had the other's marks or the table of workers.
* **What the label told them.** With one worker in the trial, its label named the worker. The run ids did not.
  But every reader's brief and paths carried `2026-11-13-dsh-flash`, so each reader could tell the runtime and
  model, which the README's run ids exist to keep from them.
* **The first reader.** The README has the scoring task read first. Here the scoring session had read every
  transcript and grade. So both readings went to fresh subagents, and the scoring session made no mark.

  The first reading includes `/code-review` at high effort, run on the first reader's copy as a forked skill, one
  run at a time. Its findings (`code-review.md`) went to the first reader as candidates, not marks.
* **How the readings were kept apart.**
  * All four reviews had finished before any second reader finished.
  * Every reader's final message was only the path of its sheet. So no mark reached a review or the other
    reader.
  * No reader records opening the other's sheet. `rbd30`'s first reader records seeing its folder's name
    (below).
* **Departures from the readers' briefs.** None of them touches a mark.
  * Four are recorded in the reader's own sheet:
    * `r6160`'s first reader wrote one scratch file to `/tmp`, outside its folder, and deleted it in the same
      command.
    * `r56d4`'s first reader wrote the fix's diff to `/tmp` first. It deleted the file at once and kept the diff
      in its folder instead.
    * `r56d4`'s second reader wrote the copy's diff to `/tmp`, to compare it with the patch, and removed it in
      the next step.
    * `rbd30`'s first reader saw the names in its run's score folder in an early listing, the second reader's
      folder among them. It opened nothing under that folder.
  * One is recorded in no sheet. mise's trust list held an entry for each copy, starting tree and fix tree the
    readers made, and the coordinator trusted none of them. So the readers trusted them, which writes to mise's
    state outside their folders. Those entries are removed with the trees.

### Disagreements

None on the mark, so nothing was reread. On the two `no` runs, both readers name the same blocker on the same
lines:

* `rbd30`: the cache limit (blocker 4), and both add the hand-written interval check (blocker 2);
* `r56d4`: the dropped close (blocker 4). Each also gives further blocker findings the other does not, listed
  above.

On the two `fix` runs, the readers share one finding each and differ on the rest:

* `rf5bf`: the first reader adds the uncached trace and the stale comment;
* `r6160`: the first reader adds the unlocked `open_study`, which the second reader set aside.

**Raw agreement** on the mark, before any reread: 4 of 4.
