---
status: accepted
date: 2026-11-11
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Writing the trial ledger: another writer's queued record is retired with its key counted, who wrote a record is read in one place, one writer at a time holds a study, and the build is the check

## Context and Problem Statement

ADR 0035 keeps every backtest as a trial in an Optuna study, and ADR 0054 reads it: the key, N and what
stops the ranking. The store also takes writes the ledger's own writers never make, a trial another
writer queued among them, and those writers ran beside each other. What does the ledger do with another
writer's record, who writes a study at one time, and when does a configuration become a trial? Split
from ADR 0054 on 2026-11-11, with no change of decision; one limit, a forked process, is added that day.

## Decision Drivers

* The remedy for a record that stops a study must never lower N (ADR 0054's Evidence, what a study
  stopped for good does to N).
* ADR 0073 rule 4 and its reachability rule: a family found in two rounds is redesigned as one model,
  and a state only a hand-written record reaches is recorded once, as a class.
* ADR 0001: glue, not invention.

## Considered Options

For the foreign record, on 2026-11-10:

1. A chain of studies, each naming the one before (ADR 0041's option 3, not chosen there).
2. Carrying N by key into a named successor study.
3. A recorded retirement of the foreign record, its key still counted.

The additions of 2026-11-11 answer ADR 0073's rule 4 and its reachability rule; no options are recorded
for them.

## Decision Outcome

Chosen: **option 3**, as ADR 0054's term 7, split from it on 2026-11-11 with no change of decision. The
term keeps its number, so a reference to term 7 still holds; terms 4 to 6 (identity, N, what stops the
ranking) are ADR 0054's and terms 1 to 3 ADR 0035's. Its parts are named as they were: retiring another
writer's queued record, one model of who wrote a record, one writer at a time, the build is the check,
the hand-written class.

7. **Retiring another writer's queued record** (added 2026-11-10). A record with a config key that
   another writer queued in Optuna (`WAITING`) never ran here, and whatever results it holds are that
   writer's. `record_trial` refuses to ask for a trial while one waits, naming it and the remedy:
   `retire_queued(study, n)`, an operator's action on that one record. Retired, a record is never read
   as a run, and its key stays counted in N as a configuration that was tried: the strict side, which
   is the gate's. It is kept, as a record in `not_run_waiting` is (ADR 0053 term 3), but unlike that
   keyless record its key is known, and counted. Its number joins the study user attribute `retired`,
   written under the study's write lock (below; the ledger's lock until 2026-11-11) before the
   record is taken from the queue (`Study.ask`) and told
   `FAIL` with a note, so a retirement cut short reads as retired, never as a run; nothing is deleted,
   and a second retirement is refused. A keyed record taken from the queue with a note before (as
   `record_trial` took one before it refused) is retired by the list alone if it is `FAIL`, since
   Optuna writes nothing on a finished trial, and told `FAIL` after the list if its `tell` was refused
   and it is still `RUNNING`. A retired record is not read in any state: whatever another writer does
   to it later, it gives the ranking no returns, and its key is counted either way. A key tried only
   in retired records stops the ranking until `record_trial` runs it on the current sample; a key no
   configuration `record_trial` runs hashes to therefore stops it for good, the strict side, and so does
   one outside the study's plan (ADR 0048) for as long as the plan is kept. Only another writer's queued record is retired: a record
   `record_trial` wrote is a trial of its key, and only a later record of that key resolves it. Every
   stop on a record `retire_queued` takes names it; the stop on a key tried only in retired records
   names `record_trial`. A `retired` list in another form is refused by `record_trial`, `retire_queued`
   and the study's pass (`canon_trading.variant_study`), and stops the ranking.
   Named limits. A keyed trial queued between `record_trial`'s check and its `ask` is still taken,
   told `FAIL` with `record_trial`'s note, and the ranking stops on it naming `retire_queued`. So is a
   trial `ask` gives, in `record_trial` or in `retire_queued`'s own take, that the store refuses to read
   back: nothing read tells a trial `ask` created from one it took, so it carries the queue's note and
   the read's error is raised naming it (one `ask` created holds no key, and `mark_not_run` clears
   it, as one `record_trial` takes from the queue). If the store refuses that note too, the error
   raised names the trial and the limit (and the refused `tell`, if that is refused as well): for a
   trial read back, the store's error, which names its key too; for one not read back, the read's
   error, since no key was read. What the operator sees is that error. The record is then `FAIL`
   (or `RUNNING`, if that `tell` is refused too) with the other writer's outcome and no note: one
   queued through
   `Study.enqueue_trial` holds the system attribute `fixed_params`, and the ranking stops on it for
   good; one queued with no system attribute reads as a run of its key, as before (ADR 0054's
   Evidence, sixth review). `retire_queued` refuses either, since nothing on it tells it from `record_trial`'s own
   `FAIL`, so the ledger offers no remedy for it. And a writer of the study's attributes other than
   `retire_queued` could put a record of `record_trial`'s in the list, which hides it from every check
   but not its key from N; like the queue, the ledger's writers are this repository's own sessions, so
   this is stated, not defended against.
   **One model of who wrote a record** (2026-11-11; ADR 0073 rule 4, the queued record's family found
   in a second round). It is decided in one place, `canon_trading.ledger.origin`, which `record_trial`,
   `retire_queued`, `mark_not_run`, the ranking and the study's pass all read. By key, state, note and
   attributes, in that order (values first, 2026-11-11): a record told more than one value is
   another writer's; a keyless record that shows a run is another writer's; a `WAITING` one
   is queued; one `FAIL` or `RUNNING` with a queue note this module writes on it was taken from the
   queue here and ran nothing (on a keyless record that is `record_trial`'s note alone, since
   `retire_queued` takes only a keyed one); one with a queue note in another state, a system attribute, a user attribute `record_trial` does
   not write (on a keyless record, any but its error), a parameter or an intermediate value was
   started, told or written by another writer outside the queue's take, and may have run anything;
   anything else is `record_trial`'s own. When: a keyless record leaves N only by its mark, and only
   while it is in the state the mark leaves (`FAIL` in `not_run`; in `not_run_waiting`, `WAITING`, or
   `FAIL` once taken here; one taken here and left `RUNNING`, its `tell` refused, `mark_not_run` tells
   `FAIL`, marked or not). One another writer started is cleared by nothing, a mark made before
   included, so `mark_not_run` refuses it and the ranking stops on it for good. Only a keyed record
   of `record_trial`'s own is read by the pass as its written failure. This narrows ADR 0053 term 3:
   a keyless record ran no backtest only if it is `record_trial`'s own or one taken here. A trial
   number given to `mark_not_run` or `retire_queued` is an `int`, never a `bool` or a float. Named
   limit: a keyless queued trial taken here whose note the store refuses holds `fixed_params` and no
   note, so it reads as another writer's and stops the study for good, the strict side; the error
   raised says so. Named limits (2026-11-11): two keyed records in no form `record_trial` leaves
   read as its own, since `origin` reads who wrote a record from its attributes' names, not their
   values or order. A record cut short holding a `sample` that is no number of the study's, or
   results with no `sample` or `config` written before them, is read as an interrupted record on the
   current sample, which a later run of its key resolves. A record told `FAIL` with outcome `traded`,
   no error and returns whose Sharpe ratio is finite is read as a run and ranked. `record_trial`
   writes a known sample number, its config and every result in that order before its outcome, and
   tells a traded run `FAIL` without an error only where its ratio is not finite; no writer this
   repository names writes either record, so this is stated, not defended against.
   **One writer at a time** (2026-11-11, the seventh round; ADR 0073 rule 4, the interleaving family,
   answered in #172 and #177 and found again: those two members were the queue's, the hand-written
   class below, and the seventh round's is a named writer's, so the lock covers the named writers
   and the queue's guards stay). Each writer the ledger names, `record_trial`,
   `move_sample`, `mark_not_run` and `retire_queued`, holds the study's write lock for everything it
   reads and writes: an exclusive, blocking `fcntl.flock` on a file beside the ledger named for the
   study, percent-encoded, reentrant in one thread. `record_trial` holds it from its first read of
   the study, the sample, to its last write, the `tell` of its run or of its failure, so a move, a
   mark or a retirement lands before a record or after it, never inside, and a second writer waits.
   ADR 0035 term 1's lock keeps first creation only. `open_study`, the ranking and the pass lock
   take no write lock, and a second pass on a study is still refused, not queued. Who failed is read
   by identity: the error `record_trial` lets through carries the number of the trial its attempt
   asked for, or none if it was refused before one, and the study's pass reads only that trial as
   its written failure, where it scanned the study for a failed record of the key.
   The ranking needs no lock because a record is whole at the level of the record. Optuna commits
   each user attribute in a transaction of its own and a `tell`'s state and value in one (Evidence);
   `record_trial` writes the outcome after every result (ADR 0035 term 2); and the ranking reads a
   record as a run only by its outcome. So a ranking read between any two of a record's writes
   stops on that record, naming it, until its outcome is written, and reads the run it was from then
   on. One read is of no single moment: Optuna reads a study's trials in several statements, and a
   `tell` that lands between two gives a record still `RUNNING` with the value told, a state
   `record_trial` never leaves, on which the ranking stops by name, saying a writer may be
   recording it and to rank again (ADR 0054 term 6). A ranking beside a
   writer can therefore stop where a moment later it would report; it never reports from half a
   record. Named limits: the lock is a local `flock`, so it orders writers on the one machine that
   holds the ledger's file (ADR 0035 term 1); a run that never returns holds it until its process
   ends, and a move, a mark or a retirement of that study waits that long, with no time limit
   and nothing printed; on a file system that folds case, two study names differing only in case
   share one lock and take turns; the reentrancy is found by the lock file's path as spelled, so
   a writer nested on one thread through a second spelling of one file would wait for itself, and
   no writer here calls another. `create_ledger_study` opens the lock file before it creates
   anything, so a study whose lock cannot be opened, a name too long for a file name included,
   is refused by name and never created: the open is the check.
   Named limit, added 2026-11-11 (the gate's eighth round): a process forked while one of its
   threads holds a study's write lock inherits the hold and is not excluded; no writer here forks,
   and a writer that must run in a child starts a fresh process. Not excluded is the child of the
   thread that holds the lock. Forked by another thread, the child's writer waits on the thread
   lock it copied, whatever the parent does; and a child of either kind keeps the file lock held,
   after the parent leaves it, for as long as it lives (Evidence). The lead ruled that day that no
   named writer reaches it, so it is stated, not guarded against, and is not counted under ADR 0073.
   **The build is the check** (2026-11-11, the seventh round; ADR 0073 rule 4, the family of a
   configuration refused or changed after its trial was asked for, found before, in a sample
   holding no bar that was recorded where ADR 0041 term 1 refuses it (#182), and again). `record_trial` freezes the configuration once, when the attempt starts: the configs and
   the starting balances as tuples, and one strategy built from each config. The key, the run config
   and the node are made from that copy alone, so a sequence its caller changes afterwards changes
   neither. Before the trial is asked for it reads the sample's bars (ADR 0041 term 1) and builds
   the node with its strategies added (`BacktestNode.build`, `add_strategy`); after, it runs exactly
   that node, and disposes of it on every path. A configuration upstream refuses at the build (no
   starting balance, no definition of an instrument in the catalog or two, a strategy ID given
   twice) is therefore refused before any record exists: it writes nothing, raises no N and cannot
   stop a study. No list of what upstream refuses is kept here. A failure after a build that
   succeeded is a real trial and stays a counted `FAIL`: a trade size finer than the instrument
   takes, refused when the strategy starts, is one. A key whose configuration can never start is
   never resolved (ADR 0054 term 5), so by ADR 0054 term 6 its study stops for good, on purpose: the registered plan
   named a configuration that cannot run, and the strict side is the gate's. Nothing clears the
   failed key in its study (`mark_not_run` refuses a record with a key, and `retire_queued` takes
   only another writer's queued record). The remedy as it exists is a new study under a new name
   (`create_ledger_study`), from a corrected plan pre-registered in a record before its first run,
   naming the configuration that could not start. Its N, counted from its own keys, is one short
   of what was tried: the lowering of N the entry of 2026-11-10 names (ADR 0054's Evidence). That shortfall is
   held only by the pre-registration, not by code; carrying N by key into a successor is not built,
   and the question is not solved. So the limit can be used, the successor's pre-registration
   states the number tried, its own keys plus each configuration of the earlier study that could
   not start, and says that the gate's printed N is short by that many; whoever reads a pass from
   that study reads it against the stated number. The study's pass refuses, by name and before any
   trial, a plan that names one key twice.
   **The hand-written class** (2026-11-11; ADR 0073's reachability rule). The gate trusts the ledger
   as its named writers write it. A state that needs a record written by hand, through Optuna's API
   or the store, is recorded here once, as a class, and is not defended against: no input check
   could prevent a forged record. Its members: a trial put in Optuna's queue (`Study.enqueue_trial`,
   `add_trial`) at any moment, the one between `record_trial`'s check and its `ask` included, which
   #172 and #177 answered and which is reclassified here, since no named writer queues a trial; a
   study attribute (`samples`, `not_run`, `not_run_waiting`, `retired`) written by another hand; and
   the keyed records in no form `record_trial` leaves that the limits above name. The write lock
   does not cover the class. What the ledger does about a queue (`_refuse_a_keyed_queue`,
   `_tell_unread`, `_untold`, `retire_queued`) stays as it was, a guard for this class only, and the
   named limits above about the queue are its members, kept for the operator who meets one.

Who decided. Term 7 was decided by the orchestrator under the owner's delegation of 2026-11-10 (ADR
0010's Evidence), 2026-11-10, over a chain of studies (ADR 0041's option 3, not chosen) and over
carrying N by key into a named successor study, a larger mechanism than one foreign record needs. The
lead decided term 7's three additions of 2026-11-11 (one writer at a time, the build as the check, the
hand-written class) under ADR 0074, and ruled on the fork limit the same day (*One writer at a time*).

### Consequences

* Good: another writer's queued record of a configuration the study runs no longer stops it for good;
  retired, its key still counts, so the remedy never lowers N (term 7).
* Bad: retirement is an operator's action; a retired key no configuration of the study runs stops it
  for good, and the race term 7 names is left to the operator.
* Good (2026-11-11): a configuration upstream refuses to build no longer leaves a failed trial that
  stops its study for good, and no other caller's move, mark or retirement lands inside a record.
* Bad (2026-11-11): writers of one study wait for each other, a run that never returns included, and
  `record_trial` takes only a study the ledger opened. A catalog with no definition of an instrument
  is now a refusal, where it was a failed trial of its key.
* Bad (2026-11-11): a configuration that builds and can never start still stops its study for good
  (ADR 0054 terms 5 and 6), on purpose. The remedy that exists, a new study from a corrected plan, has an N
  one short of what was tried; only its pre-registration, which states the number tried, holds
  that, and no code does (term 7).

### Confirmation

* Term 7, tests that failed before their change: `record_trial` refuses beside a keyed queued record,
  with its note refused too, and the record stays `WAITING`
  (`test_record_trial_refuses_beside_another_writer_s_keyed_queued_trial`); N is the same or higher
  after a retirement, on every path: a retired record alone on its key, then with a later run of its
  key (`test_a_retired_queued_record_keeps_its_key_counted_and_record_trial_goes_on`,
  `test_a_key_tried_only_in_a_retired_record_stays_counted_and_names_record_trial`), and a record the
  code before took from the queue, retired by the list alone
  (`test_a_record_taken_by_the_code_before_is_retired_by_the_list_alone`,
  `test_a_retired_record_is_not_read_and_its_key_stays_counted`); a second retirement is refused and
  `record_trial` goes on afterwards (the first of those); a retirement stopped between the list and the
  take, or with its note refused, reads as retired and never as a run
  (`test_a_retirement_cut_short_reads_as_retired_never_as_a_run`); the queue is retired in its order,
  and every other record is refused (`test_retire_queued_retires_only_another_writer_s_queued_record_in_queue_order`);
  the stops name the remedy (`test_a_waiting_keyed_record_names_retire_queued`, and the tests above);
  a `retired` list in another form stops (`test_a_retired_list_not_in_its_form_stops`); the study's
  pass runs a key whose only record is retired
  (`tests/test_variant_study.py::test_a_key_whose_only_record_on_the_sample_is_retired_is_run`).
  From the review of term 7, tests that failed before their fix: a retirement whose trial another
  process took meanwhile gives the next trial `ask` takes `record_trial`'s note, and that one can be
  retired (`test_a_retirement_that_finds_its_trial_gone_takes_the_next_with_record_trial_s_note`); in
  the race, both refusals reach the error raised
  (`test_a_keyed_trial_queued_after_the_check_names_itself_when_nothing_can_be_written`); a `retired`
  list in another form is refused by `record_trial` and `retire_queued` alike
  (`test_a_retired_list_not_in_its_form_is_refused_by_record_trial_and_retire_queued`); a taken record left `RUNNING` is
  retired and told `FAIL` (`test_a_taken_record_left_running_is_retired_and_told_fail`); a queued
  record that another check stops on still names the remedy
  (`test_a_queued_record_with_another_fault_still_names_retire_queued`). From its second review, tests
  that failed before their fix: a trial retired between `record_trial`'s note and its `tell` lets
  `record_trial` go on
  (`test_a_trial_retired_between_record_trial_s_note_and_its_tell_lets_record_trial_go_on`); a trial
  `retire_queued` takes and cannot read back is still told `FAIL` with its note
  (`test_a_retired_trial_that_cannot_be_read_back_is_still_told_fail`); no remedy is named while the
  `retired` list is not in its form
  (`test_no_remedy_is_named_while_the_retired_list_is_not_in_its_form`). From the gate round after
  term 7's landing, tests that failed before their fix: a raced keyed trial whose read back is refused
  carries the queue's note, stops the ranking and is retired
  (`tests/test_ranking.py::test_a_raced_queued_record_whose_read_back_is_refused_stops_and_is_retired`),
  with its note refused too the error names it and the limit
  (`test_a_raced_trial_not_read_back_with_its_note_refused_names_itself`), and the same refused read in
  `retire_queued`'s take gives the queue's note
  (`test_a_raced_trial_retire_queued_cannot_read_back_carries_the_queue_note`), and one told by
  another process before its read is left as it is
  (`test_a_trial_told_by_another_process_before_its_read_is_left_as_it_is`); and three that fail
  on the mutation of a guard no test held: either queue note on a record not in the `retired` list
  (`test_a_record_with_either_queue_note_not_in_the_retired_list_stops`), an outcome `record_trial`
  never writes on a `FAIL` record
  (`test_a_failed_record_with_an_outcome_the_writer_never_writes_stops_for_good`), and a keyless
  record holding only returns or only session days
  (`test_a_keyless_record_holding_only_returns_or_session_days_shows_a_run`). From the gate's second
  round after term 7, tests that failed before the one model: a keyless record another writer queued
  and started, or wrote an empty key, a key not text or an attribute of its own on, is not cleared,
  under a mark made before either (`test_a_keyless_record_another_writer_started_is_not_cleared`,
  which replaces `test_an_empty_key_is_keyless_and_mark_not_run_clears_it`, whose reading it
  reverses); a trial number that is a `bool` or a float is refused before anything is written
  (`test_a_trial_number_that_is_not_an_int_is_refused`); a raced queued record of the pass's key is
  no failure the pass goes on from, with a second keyed record waiting or the read back refused
  (`tests/test_variant_study.py::test_a_raced_queued_record_of_the_key_is_not_a_failure_the_pass_goes_on_from`).
  A keyless queued record `record_trial` took is still cleared
  (`test_a_keyless_queued_record_record_trial_took_is_still_cleared`), which passed before too. From
  the code review of the model, tests that failed before their fix: a keyless queued trial taken
  without its note names the stop for good
  (`test_a_keyless_queued_trial_taken_without_its_note_says_it_stops_for_good`); a marked `WAITING`
  record written on after its mark is named as such
  (`test_a_marked_waiting_record_written_on_after_its_mark_is_named_as_such`); a marked record taken
  with the retirement's note still stops
  (`test_a_marked_waiting_record_told_fail_with_the_retirement_s_note_still_stops`); and the pass runs
  a key whose record was taken from the queue
  (`tests/test_variant_study.py::test_a_record_taken_from_the_queue_is_no_run_the_pass_skips_its_key_for`).
* From the gate's seventh round (2026-11-11), term 7's two models. Tests that failed before the
  change, in `tests/test_ledger.py` unless named: each named writer waits for the study's write lock
  held by another descriptor (`test_every_named_writer_waits_for_the_study_s_write_lock`); a move
  waits for a record being written (ADR 0041's Confirmation); a study the ledger did not open is
  refused (`test_a_record_is_written_only_on_a_study_the_ledger_opened`); the error names the trial
  asked for, or none (`test_a_raised_record_names_the_trial_it_asked_for_or_none`); another
  caller's failed record is no failure of an attempt refused before its trial
  (`tests/test_variant_study.py::test_another_caller_s_failed_record_is_no_failure_of_an_attempt_refused_before_its_trial`,
  Codex); a configuration with no starting balance, or one strategy ID twice, leaves no trial and
  the ranking as it was (`test_a_configuration_the_node_refuses_to_build_is_refused_before_a_trial`,
  Antigravity); a balance list changed after the key ran 200,000 USD under the key of 100,000 USD,
  and now runs what was keyed (`test_a_balance_changed_after_the_key_does_not_reach_the_run`,
  Codex); the node is disposed however the attempt ends
  (`test_the_node_is_disposed_however_the_attempt_ends`); the build alone refuses what upstream
  refuses (`tests/test_backtest.py::test_what_upstream_refuses_of_a_configuration_is_refused_by_the_build`);
  and a pass naming one key twice is refused before any trial
  (`tests/test_variant_study.py::test_a_pass_whose_configurations_repeat_a_key_is_refused_before_any_trial`,
  Antigravity).
* The ranking beside a writer, tests that passed before and after the change, which is why the
  ranking takes no lock: read before each of a record's eleven writes, it stops on the record by
  name before the outcome is written and is the finished study's report, figure for figure, after
  (`test_rank_beside_a_writer_never_reads_a_half_written_record`); and a read split by a `tell`
  between two of Optuna's statements stops naming the record `RUNNING` with a value, and the next
  read ranks (`test_a_read_split_by_a_tell_stops_the_ranking_and_never_ranks_half_a_record`).
  `open_study`, the ranking and the creation of another study do not wait for the write lock
  (`test_opening_and_ranking_a_study_do_not_take_its_write_lock`), and a writer nested in one
  attempt on one thread goes on (`test_a_writer_nested_in_one_attempt_does_not_wait_for_itself`).
* From the code review of the seventh round's change (2026-11-11): a study named past what a file
  name holds was created and could then not be written, and is now refused at creation, a test
  that failed first (`test_a_study_whose_write_lock_cannot_be_opened_is_not_created`); and the
  test that each named writer waits releases the lock only once the writer has asked for it, where
  a pause alone would have passed a slow writer that takes no lock (run against writers with the
  lock taken out: all four fail).
* The fourth round's tests of who wrote a record (a record told more than one value, a keyless record
  with the retirement's note, a marked `WAITING` record taken whose `tell` was refused) are listed with
  that round's others in ADR 0054's Confirmation.
* The fork limit has no test: nothing guards against it. Evidence holds how it was shown.

## Evidence

* Split from ADR 0054 on 2026-11-11 with no change of decision: term 7, its `Who decided` sentences,
  its consequences, its tests and the lines below are ADR 0054's own, moved, with their dates and how
  each was checked. New here are the title, the Context, the Decision Drivers, the Considered Options
  and the `Chosen` line, which restate the term; the Confirmation's last two lines; and the fork limit
  with its lines at the end. Reworded to read from this record: the pointers that now name ADR 0054
  or ADR 0080, the first words of `Who decided`, and one reference made explicit, "term 1" in *The
  build is the check*, which is ADR 0041's term 1, as the fifth round's entry in ADR 0054 names it.
  `review-by` is ADR 0054's, kept: the term rests on a NautilusTrader release candidate.

Read 2026-11-10 and 2026-11-11. Fetched means read as raw text or source; run means executed in a
checkout.

* Term 7, added 2026-11-10, decides the remedy ADR 0054's Evidence leaves open (what a study stopped
  for good does to N): a recorded retirement of the foreign
  record, its key still counted, and no chain of studies. Optuna 5.0.0, read in the installed package
  (`v5.0.0`, `01ddd17ab9f3`): `Study.ask` takes the oldest `WAITING` trial before it creates one
  (`study/study.py`, `_pop_waiting_trial_id`, over `get_all_trials` in order of trial ID), and no public
  call takes a chosen one; `Study.tell` tells only a `RUNNING` trial (`study/_tell.py`); every write
  of a trial attribute checks `check_trial_is_updatable` (`storages/_rdb/storage.py`,
  `storages/_base.py`), so `Trial.set_user_attr` after `tell` raises `UpdateFinishedTrialError` (run).
  So a record is taken through `ask` in queue order, and the retirement of a finished one is the study
  user attribute (`Study.set_user_attr`), with no private storage call. `record_trial` now refuses
  before `ask`, which closes the known limit of ADR 0054's sixth review on its path: it no longer takes a keyed queued
  record, so no note of its is needed to keep one from reading as a run, and `retire_queued` writes its
  list before it takes one. What is left is the race term 7 names. Reproduced as failing tests on the
  code before this change (Confirmation).
* Review of term 7, 2026-11-10. Code review (`/code-review`, high): ten findings. Fixed, each shown by
  a test that failed first: a retirement's own race took a newer queued trial with a note
  `retire_queued` refused; a refused `tell` replaced the error naming a raced trial; a remedy named for
  a taken record left `RUNNING`, which `retire_queued` refused; a `retired` list in another form read
  three ways; and three cleanups (one take helper for `record_trial` and `retire_queued`, one lookup, one
  helper in the tests). Corrected in this record: a retired key nothing runs stops for good, and a raced
  record holding `fixed_params` stops rather than reading as a run. Not changed, stated in term 7: a
  retired record is read in no state, since checking its state could not tell a retirement cut short
  from `record_trial`'s own failure, and its key is counted either way. Second reader
  (`evidence-verifier`): the Optuna claims hold against `v5.0.0` (the installed files are upstream's,
  run), and so do the records cited; it corrected the race's wording, a stop that did not name the
  remedy when another check fired first, the comparison with `not_run_waiting`, and a test that did not
  measure N before a retirement. A second code review (`/code-review`, high) of those fixes: nine
  findings. Fixed, each shown by a test that failed first: a `RUNNING` record retired while a live
  `record_trial` held it made that `record_trial`'s `tell` raise (now `Study.tell(...,
  skip_if_finished=True)`, Optuna 5.0.0 `study/_tell.py`, and `retire_queued` asks that a `RUNNING`
  record's process be gone, as `mark_not_run` does); a trial `retire_queued` took and could not read
  back stayed `RUNNING`; a remedy was named while the `retired` list was not in its form. Fixed with no
  test: a refused note in a retirement's race now names the trial, a docstring, the pass's refusal of
  such a list, a test fixture's import and a test's pattern. Not changed: two extra reads of the study
  per retirement, which hold the lock a little longer and change nothing read.
* Gate round after term 7's landing, 2026-11-11 (ADR 0073), two outside readers of the gate as
  checked out: one high finding, of the family term 7 names (the race), and three test-only findings.
  The first reader (Codex) found that a refused read of the trial `ask` gave told it `FAIL` with a
  generic error, so a keyed trial another writer queued after the check, with no system attribute,
  lost the only sign it was taken from the queue, and the ranking passed on its returns at N = 3,
  DSR 0.9885, with no backtest run (run in a temporary ledger with an injected refusal). A trial not
  read back now carries the queue's note, through Optuna's public `Trial.set_user_attr` and
  `Study.tell` only, as term 7's other takes do; `retire_queued`'s own take, which noted such a trial
  by the queue it read before `ask`, does the same. The second reader (Muse) found no behaviour fault
  and three guards no test failed on, each shown by a mutation that passed at DSR 0.9885; each now
  has a test that fails on its mutation (run). Code review of the fix (`/code-review`, high): eight
  findings. Fixed, each shown by a test that failed first: a trial another process told before its
  read, which only `mark_not_run` does, was given term 7's limit note (now left as it is, Optuna
  5.0.0's `UpdateFinishedTrialError`, `storages/_base.py`, read); an interrupt while the note was
  written was raised with no note naming the trial. Fixed with no test: this record's wording for the
  unread case, two docstrings, and a test that faked the race by its check rather than at `ask`.
  Not changed: one helper for both notes on a refused note, and one test helper each file keeps.
  Confirmation lists the tests. No change of decision.
* Gate's second round after term 7, 2026-11-11 (ADR 0073), two outside readers of the gate as
  checked out at #172. The first (Codex) found nothing that changes behaviour. The second
  (Antigravity) reproduced three findings, each now a test that failed first (Confirmation). High: a
  keyless record another writer queued (`Study.enqueue_trial`'s `fixed_params`) or wrote attributes
  on, then started and told `FAIL` before any mark, was cleared by `mark_not_run` as `record_trial`'s
  own record lost before its key, and the gate passed at N = 3, DSR 0.9974. High: `mark_not_run` and
  `retire_queued` took `False`, `True` or `0.0` as a trial number (`0 == False == 0.0`) and wrote it
  into lists every reader refuses, which un-marked or un-retired the records before it and stopped
  the study's ranking and recording for good. Medium: the study's pass read a raced queued record of
  its key, taken with the queue's note, as `record_trial`'s written failure, and went on from an
  error that left none. The first is the queued record's family, found in a second round, so it is
  redesigned as one model (term 7) rather than patched; the third is read through that model too.
  Optuna 5.0.0's `FrozenTrial` keeps `user_attrs`, `system_attrs`, `distributions` and
  `intermediate_values`, and `Study.enqueue_trial` writes `fixed_params` (run, in the installed
  package). No change of decision.
  Code review of the model (`/code-review`, high): nine findings. Fixed, each shown by a test that
  failed first: a keyless queued trial taken without its note stopped the study for good with no
  word of it in the error; a marked `WAITING` record written on was said to have been started; the
  rule had widened a marked record's clearing to the retirement's note; the pass skipped a key whose
  record was taken from the queue, where the gate stops on it. Fixed with no test: a check that
  could never fail, the queue notes held in two places, and `origin`'s word on the one reading it
  leaves to `ask`. A NumPy integer stays refused as a trial number, its error naming `int()`. Not
  changed: `origin` read twice for one record's remedy, which changes nothing read.
* The gate's seventh outside round, 2026-11-11 (Codex and Antigravity, on the gate as landed in #185):
  seven findings, Codex 2 and Antigravity 5, each changing behaviour and each reproduced by a test
  that failed first; all reachable from a named writer or an input the gate's check passes, none by
  a hand-written record only (ADR 0073 rule 5 and its reachability rule). Antigravity's first three
  are the numeric family, answered in ADR 0080. The other four are this
  record's: Codex's second, another caller's failed record read as this attempt's, in the
  interleaving family (term 7, one writer at a time); Antigravity's fourth and Codex's first, a
  configuration refused or changed after its trial was asked for, in the family #182 met (term 7,
  the build is the check); Antigravity's fifth, a pass naming one key twice, fixed on
  its own. Two families found again and redesigned as one model each; no new family. No change of
  decision.
* https://raw.githubusercontent.com/optuna/optuna/01ddd17ab9f36c3d2817aed9afe376e1cb047d35/optuna/storages/_rdb/storage.py
  — what makes a record whole at the level of the record. `_create_scoped_session` (line 73)
  commits once on leaving its block (line 80). `set_trial_user_attr` (line 758) is one such block,
  so each attribute is one transaction; `Trial.set_user_attr` calls it once (`trial/_trial.py`
  line 599). `set_trial_state_values` (line 670) writes the values and the state in one block, and
  `Study.tell` calls it once (`study/_tell.py` line 173), so a `tell` is one transaction.
  `_get_trials` (line 858) reads the trials with `selectinload` of their values and attributes
  (lines 874 to 878), a statement each, and `set_study_user_attr` (line 332) replaces an attribute
  whole with no compare-and-set. Read in the local copy at the pin. That a commit lands between two
  of the read's statements on SQLite is observed by the test named above, not read in SQLAlchemy.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/backtest/src/node.rs
  — `BacktestNode::build` (line 103) "creates a `BacktestEngine`, adds venues, and loads instruments
  from the catalog", returning the error when `raise_exception` is on; `run` (line 161) "loads data
  from the catalog and runs the engine" and builds only if no engine exists; `dispose` (line 229),
  after which neither runs. The three refusals: "No instruments found in catalog" (line 342, in the
  build); empty `starting_balances` (`…/config.rs` line 391, `SimulatedVenueConfig::validate`, which
  the build reaches at `node.rs` line 323; the message "starting_balances must not be empty" is
  `crates/common/src/config.rs` line 199); "Strategy … is already registered"
  (`crates/system/src/trader.rs` line 588, on `add_strategy`). Read in the local copy at the pin;
  each refusal run here through `canon_trading.backtest.built_backtest`, 2026-11-11.
* Second reader (evidence-verifier), 2026-11-11, on the seventh round's text: the empty balances
  are refused in `config.rs`, not in `exchange.rs` as first cited; #177 redesigned and did not
  patch; the configuration family's earlier member is named by its pull request, #182, where a
  round number was given that these entries do not bear out; and the remedy for a configuration
  that can never start was written as a new study counting its own N, which the entry of
  2026-11-10 in ADR 0054's Evidence forbids as a way to lower N, so term 7 now states it as a limit (the lead's
  decision of that date). Each corrected in place.
* Code review of the seventh round's change, 2026-11-11, eight findings. Fixed: the study whose
  lock file cannot be named (above); the waiting test's proof; two comments and a docstring that
  still described a race between named writers; this section's `fcntl` entry, which cited a page
  not fetched. Recorded as limits in term 7: the wait has no time limit and prints nothing, and
  the reentrancy goes by the path's spelling. Stated in `record_trial`'s code: the node is disposed
  of after the `tell`, so an error raised there names a trial told as it ran (upstream's `dispose`
  returns nothing, `node.rs` line 229). Not this change's: ADR 0054 term 6's pointer holds once ADR
  0031's addition of this date (now ADR 0080) is on the same commit.
* The standard library's `fcntl.flock`, whose docstring in the pinned Python 3.14.10 reads "Perform
  the lock operation on file descriptor fd. See the Unix manual page for flock(2) for details"
  (`bundled`, read); its page, https://docs.python.org/3/library/fcntl.html#fcntl.flock, was not
  fetched this session and is `UNVERIFIED`. `man 2 flock` on macOS (`bundled` with the system, read): "Requesting a lock on an object that is
  already locked normally causes the caller to be blocked until the lock may be acquired", unless
  `LOCK_NB` is given; "Locks are on files, not file descriptors", a descriptor duplicated by `dup`
  or `fork` being one more reference to the one lock. That a second `open` of the lock file waits
  for a lock held through another descriptor of the same process, which is why the lock is made
  reentrant here, and that closing the holder's descriptor releases it, are run by the tests above.
* The fork limit, 2026-11-11. The gate's eighth outside round, read only, by Codex, on the gate as
  landed in #204: one finding, shown by two scripts kept outside the repository (`fork_move.py`,
  `fork_gate.py`). As reported, each forks from the thread that holds a study's write lock, at a
  scheduling point at `Study.set_user_attr`, and both `move_sample` calls return sample 1: one move
  is lost. The scripts were not read for this record: UNVERIFIED as to their text. The round is not a
  clean one: its other reading, by Antigravity, found five findings a named writer reaches (the
  lead's ruling of 2026-11-11; that reading was not read for this record), answered in a change of
  their own.
* `src/canon_trading/ledger.py` at that commit, read; the split that made this record changes only
  comments and docstrings in the file and moves no line (run: the syntax trees compared).
  `_WRITE_LOCKS` (line 398) keeps, for each lock file, one `threading.RLock` and the open file that
  holds the `flock`; `_write_lock` takes the `RLock` (line 431) and calls `flock` only `if held.file
  is None` (line 432). A forked child gets a copy of that table with the file already set, and its
  one thread is the forking thread, which the copied `RLock` counts as its holder; so in the child
  a writer passes both and takes no lock. `man 2 flock` on macOS (`bundled` with the system, read):
  descriptors duplicated through `fork(2)` "do not result in multiple instances of a lock, but
  rather multiple references to a single lock".
* Run here, 2026-11-11, in a temporary ledger, Python 3.14.10 on macOS, with `move_sample` stopped at
  `Study.set_user_attr` under the lock. A fresh process started there waited (3 s, then to the end)
  and its move returned sample 2. A child forked there by the holding thread opened the study and
  its `move_sample` returned sample 1 without waiting; the parent's returned sample 1, and the study
  kept samples 0, 1 and 2 with the child's range in none. A child forked by another thread while
  the lock was held did not go on: the `RLock` it copied is held by a thread it does not have, and
  it was still waiting 9 s after the parent released the lock, when a timer ended it (the second
  reader's run, repeated here). Whether it would wait for ever was not run: UNVERIFIED. A child
  forked under the lock and left alive kept the file lock held: a fresh process got in only when
  the child exited, 5 s after the parent had released (the same).
* No writer here forks: `git grep` for `os.fork`, `multiprocessing`, `ProcessPool` and `forkserver`
  in `src/`, `packages/` and `scripts/` finds nothing (run, 2026-11-11); the only processes started
  there are `subprocess.run` calls, which run another program. Whether NautilusTrader, Optuna or
  SQLAlchemy fork under the lock was not read: UNVERIFIED. That the backtest queue (ADR 0068) starts
  fresh processes is the lead's ruling of 2026-11-11, not read in code here: UNVERIFIED.
* Second reader of the split and of the fork limit, 2026-11-11 (`evidence-verifier`): every sentence
  of term 7 is in one of the two records, each reworded line only names the record that now holds
  its target, the Optuna line numbers hold in 5.0.0 and every test named exists in its file (run).
  It re-ran both fork runs, and with the code review (`/code-review`, high) found the limit's
  sentence true only of a fork by the holding thread; the two other cases above are its runs, and
  the term now states them. The code review also corrected a line here that called the ledger module
  unchanged, which this split's own comment edits would have made untrue.
