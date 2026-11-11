# Review of `3a00f617fd20` — modify Alpaca orders by replace (`packages/canon-alpaca/`)

Scope: `git diff 3a00f617fd20^ 3a00f617fd20`. Read in full: `execution.py`, the new and changed tests,
`conftest.py`'s stand-in, both READMEs and ADR 0036. Checked against the installed NautilusTrader 2.0.0rc8
(`live/clients.py`: `generate_order_updated` and `generate_order_modify_rejected` signatures, `create_task`)
and alpaca-py 0.45.0 (`ReplaceOrderRequest`, `replace_order_by_id`, `Order.replaces`/`replaced_by`,
`TradingStream._cast`). The calls match those signatures.

How I tested: the offline suite `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py`
passes (64 passed). I ran experiments in a copy of the package made with `git archive HEAD`, outside the
checkout, with `PYTHONPATH` pointed at the copy's `src` (I confirmed the copy was the module that got
imported). The experiments are in a scratch `test_exp.py` that reuses the suite's `venue` fixture, `modifier`
and `run_modify`. I mutated only the copy. The checkout is unchanged (`git status` is clean, HEAD is
unchanged). I ran no paper tests.

---

## 1. Reconciliation never reports a node order whose replacement has closed, unless `OrderUpdated` was already applied (high)

**Where:** `execution.py`, `_order_report`, the early `return None` guarded by
`order.status in CLOSED_STATUSES and held.venue_order_id != VenueOrderId(str(order.id))` (lines 1515–1524).
The same path serves `_order_status_report` (single) and `_order_status_reports` (list).

**What goes wrong:** The guard is meant for "a replacement that closed without taking over (rejected when
the old order filled first)". But it applies to *every* closed status (`filled`, `canceled`, `expired`,
`rejected`). It also applies whenever the node's order still names the old venue order ID. That is exactly
the case the design hands to reconciliation: the stream's `replaced` was missed, or `_replaced` could not
read the replacement ("no OrderUpdated, reconciliation settles it"). In that state:

* the old order is `replaced` and is skipped by `order_status_report`;
* the replacement is closed and its venue ID differs from the node's, so it is skipped too.

So no report for the node's order ever comes back, from either the list or the single-order hook. The order
stays `PENDING_UPDATE`/`ACCEPTED` locally even though it has filled or been canceled on Alpaca. A fill is
never inferred, so the position is wrong. Several texts claim the opposite: the module header ("if neither
yields them, nothing is generated and reconciliation settles the order"), `_replaced`'s docstring and log
line, the ADR ("nothing if neither does"), and the README's TC-E74–78 row ("left to the stream or
reconciliation"). What NautilusTrader's in-flight check then does with a `None` report is `UNVERIFIED`. It
may resolve the order wrongly rather than leave it.

**Shown:** experiment `test_e1_closed_replacement_never_reported_when_update_missed`. I used
`replace_script="silent"`, so the `replaced` update never arrives. The node's order kept the old venue ID.
I set the replacement to `filled` on the stand-in, then cleared `_replacements` and `_foreign` and called
both report hooks:

```
NAMES ['OrderSubmitted', 'OrderAccepted', 'OrderPendingUpdate']
NODE VENUE ID 9876395f-…  old 9876395f-…  new aefef290-…
LIST []
SINGLE None
```

Control `test_e1b_open_replacement_is_reported`: with the replacement still `new`, both hooks report it
under the node's client order ID. Only the closed case is lost. A narrower guard would fix it, for example
"the replacement is `rejected` and the old order is not `replaced`".

## 2. `test_rejected_replacement_is_modify_rejected_not_order_rejected` never reaches the branch it is named for, and the documented behaviour for that sequence is false (medium)

**Where:** `tests/test_execution_client.py::test_rejected_replacement_is_modify_rejected_not_order_rejected`.
`execution.py` `_apply`, the `event == "rejected" and is_replacement and order.venue_order_id != venue_order_id`
branch (line 1243). The module header ("a replacement's `rejected` [is] `OrderModifyRejected` … because
Alpaca rejects the replacement when the old order fills first"). ADR 0036's `UNVERIFIED` paragraph ("the old
order's fill applies as usual and the replacement's `rejected` becomes `OrderModifyRejected`").

**What goes wrong:** The test sends the sequence Alpaca describes: the old order's `fill`, then the
replacement's `rejected`. The fill is terminal, so `_apply` adds the node's order to `_finished`. The
replacement's `rejected` then returns at `if client_order_id in self._finished: return`, before the branch.
For that sequence no `OrderModifyRejected` is ever generated, which contradicts the header and the ADR.
The test asserts only that `OrderRejected` is absent and `OrderFilled` present. Its name promises
`OrderModifyRejected`, but it never asserts it, so it passes whether or not the branch exists.

**Shown:**

* `test_e5_rejected_replacement_names` prints the events of that run:
  `['OrderSubmitted', 'OrderAccepted', 'OrderPendingUpdate', 'OrderFilled']`, with no `OrderModifyRejected`.
* Mutation A: I replaced the branch condition with `if False:` in the copy. The test still passes
  (`1 passed`).
* No other offline test reaches the branch either: it fires only if the replacement's `rejected` arrives
  before the old order's fill.

## 3. `test_replace_rejected_on_the_stream_is_modify_rejected` passes without the `order_replace_rejected` handling, and the stand-in produces two `OrderModifyRejected` for one modify (medium)

**Where:** `tests/test_execution_client.py::test_replace_rejected_on_the_stream_is_modify_rejected`.
`conftest.py` `FakeOrdersClient.replace_order_by_id`, the `"reject"` script. `execution.py` `_apply`, the
`order_replace_rejected` branch.

**What goes wrong:** Under `"reject"` the stand-in sends `order_replace_rejected` *and* raises 422. The 422
path (`_replace_refused`) generates `OrderModifyRejected` by itself, so the test's assertion
(`"OrderModifyRejected" in names`) is met even if the stream event is ignored. The test cannot show that the
stream event is handled. The run also emits two `OrderModifyRejected` events for one `ModifyOrder`. If
Alpaca really answers both ways, which the ADR leaves `UNVERIFIED`, the client double-reports. I am unsure
whether Alpaca does; the duplicate comes from the stand-in's script.

**Shown:**

* Mutation B: I replaced `if event == "order_replace_rejected":` with `if False:`. The test still passes.
* `test_e3_reject_script_counts` prints
  `['OrderSubmitted', 'OrderAccepted', 'OrderPendingUpdate', 'OrderModifyRejected', 'OrderModifyRejected']`.

## 4. PATCH answers are left in `_answers` when the stream's `replaced` beats the HTTP answer (low–medium)

**Where:** `execution.py` `_modify_order` (stores `self._answers[str(answer.id)]` after the PATCH returns),
`_replaced` (pops it), and the `order_replace_rejected` branch (deletes by `replaces`).

**What goes wrong:** The consumer task handles `replaced` as soon as it arrives. If that happens before
`_modify_order` resumes, `_replaced` finds no answer and reads the replacement from Alpaca. `_modify_order`
then stores the answer, and nothing ever removes it until disconnect. The same race defeats the cleanup in
the `order_replace_rejected` branch. The effects are a dict that grows by one entry per modify for the
session, an extra REST read per modify, and the docstring's "The answer is kept for that update's values"
not holding in that ordering. Whether Alpaca's stream usually beats its HTTP answer is `UNVERIFIED`. In the
stand-in it always does.

**Shown:** `test_e4_answers_left_behind`, two modifies in a row: after both `OrderUpdated` events,
`client._answers` still holds both replacement IDs (`ANSWERS LEFT ['94b74dfe-…', 'a7880e9c-…']`).

## 5. Every `replaced` order in a report request is logged with a false reason (low)

**Where:** `execution.py` `_order_report`. `order_status_report` returns `None` for status `replaced` by
design ("its replacement is reported instead"). `_order_report` then logs
"is not reported: its type, side, class, quantity or time in force is not in this slice".

**What goes wrong:** The warning names the wrong cause. It fires on every reconciliation for every replaced
order the window holds, and its own comment says such an order "needs no lookup" and is skipped on purpose.

**Shown:** `test_e6_warning_for_replaced`, after one modify:
`[WARN] … Alpaca order 319fcc3b-… (AAPL, replaced) is not reported: its type, side, class, quantity or time in force is not in this slice`.

## 6. The new paper modify tests do not skip outside the regular session; the node test also modifies before Alpaca can have routed the order (low; unsure, not run)

**Where:** `tests/test_paper_execution.py::test_paper_limit_order_is_modified_through_a_node`
(`AcceptModifyCancel.on_order_event`) and `tests/test_paper_replace.py::test_paper_replace_answers_as_observed`.
README rows for both.

**What goes wrong:**

* `test_paper_replace.py`'s own docstring says it "needs the regular session: an order not yet routed
  (`accepted`) cannot be replaced". Outside the session it does not skip: it fails after 30 s in
  `wait_for(... == "new")`. Its siblings `test_paper_matrix.py` and `test_paper_fill_ids.py` check
  `get_clock()` and skip. The README says "needs the regular session" without saying the test fails rather
  than skips.
* The node test sends the modify on `OrderAccepted`. The client emits that from the submit answer, whatever
  the status (`pending_new`/`accepted`/`new`). The order may therefore not yet be routed. Outside the
  session it certainly is not, so the run should end `OrderModifyRejected` and fail the event-list
  assertion.
* The README's `test_paper_execution.py` row lists no session requirement, and there is no skip.

These tests can fail for a reason that is not the adapter. I could not confirm this without a paper run,
which the brief does not allow.

A smaller point in `test_paper_replace.py`: if Alpaca ever accepts the kept client order ID,
`seen["replace"]` is never set. The final block then dies with `KeyError` before the assertion meant to
report that change.

## 7. Stale or contradictory test-coverage statements (low; unsure which side is right)

* `tests/test_paper_execution.py` module docstring: it was not updated. It still says the tests other than
  the first are TC-E41, TC-E20 and TC-E22. It omits TC-E30 and the modify-through-a-node test this commit
  adds.
* `packages/canon-alpaca/README.md`, execution matrix:
  * The new row "TC-E21, E23, E31, E33 to E35 | Not run: sells …, cancel-replace, and a stop's trigger
    modified" lists TC-E33, which the existing row "TC-E02, E32, E33, E44, E84 to E87 | Not run: a short,
    cancel-replace, cancel of a canceled order …" already lists with a different description.
  * The new ADR 0036 text says "TC-E32, E35 and E42 to E44 (no new hook)" and "TC-E34 (offline only)".
    The README groups E35 with the sells and cancel-replace, and lists TC-E42 as "Passed".
  * The upstream spec is not available offline, so I cannot say which mapping is correct. The two
    documents disagree.
* ADR 0036 says the chain cache keeps "misses too". `_node_order_id` does not add to `_foreign` when the
  16-link limit is reached, so that miss is walked again on every update for the order. This is minor.

---

## Not found to be defective (checked)

* `generate_order_updated(order, venue_order_id, quantity, price, trigger_price, None, ts_event)`: the sixth
  positional argument is `protection_price` in rc8, so `None` is right. `generate_order_modify_rejected`
  accepts a `None` venue order ID.
* `ReplaceOrderRequest(qty=int, limit_price=float, stop_price=float)` matches alpaca-py 0.45.0's types and
  validator. The limit price is sent for limit and stop-limit orders, and the stop price for stop types, as
  alpaca-py's docstring requires.
* `OrderUpdated` moves the node's venue order ID to the replacement: the two-replaces test sends its second
  PATCH to the first replacement.
* `_cancel_refused`'s redirect to `replaced_by` looks right. No offline test exercises it, and the paper
  test cancels only after `OrderUpdated`, so it does not exercise it either.

## Not verified

* What Alpaca actually sends for `order_replace_rejected`, and whether it also answers the PATCH 422
  (bears on #3).
* The relative timing of `replaced` and the PATCH answer (#4).
* NautilusTrader's in-flight resolution when the report is `None` (#1's downstream effect).
* The Execution Testing Spec's row numbering (#7).
* No paper test was run.
