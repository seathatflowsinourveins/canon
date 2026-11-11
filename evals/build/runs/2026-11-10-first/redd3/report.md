# Report: two execution-client defects in `packages/canon-alpaca/`

## What was wrong

1. **Cancel-all skipped `PENDING_CANCEL` orders.** `AlpacaExecutionClient._cancel_all_orders` read each
   selected order again and `continue`d if it was closed **or `PENDING_CANCEL`**. That skip was on purpose:
   ADR 0036 took it from Coinbase's adapter, "since a second cancel risks a rejection". NautilusTrader puts
   an order in `PENDING_CANCEL` when the cancel is sent. If that cancel gets no answer (a read timeout,
   `ConnectionError`), `_cancel` logs it and leaves the order as it is, so the order stays `PENDING_CANCEL`
   in the node while it may still be working at Alpaca. After that, no cancel-all would send its cancel again.
2. **Client order IDs that NautilusTrader cannot hold aborted reconciliation.** `_root_id` and
   `_node_order_id` called `ClientOrderId(alpaca_order.client_order_id)` directly, both for the order's
   own ID and for each order in its `replaces` chain. They did this before `order_status_report`, whose
   `try` is what separates "open: fail with the order named" from "closed: skip". An empty or non-ASCII ID
   raised `ValueError` outside that handling. The error went up through `logged_failure` as
   "order status reports failed: invalid string for 'value', was empty", so the whole mass status failed,
   and start-up with it, even when the order was closed. A trade update for such an order failed in the
   same way, inside `_apply`.

## What I changed, and why that way

`packages/canon-alpaca/src/canon_alpaca/execution.py`:

* `_cancel_all_orders` now skips an order only when it is gone or closed. A `PENDING_CANCEL` order is
  sent its cancel again. This is safe because the client already handles the answer: when Alpaca had
  taken the first cancel, "Delete Order by ID" answers 422, and `_cancel_refused` looks the order up,
  finds it `pending_cancel` or `canceled`, and generates nothing. The existing
  `test_refused_repeat_of_a_cancel_alpaca_took_is_not_a_rejection` covers that path. I chose this over
  asking Alpaca about each pending order first: the 422 path already does that lookup when it is needed,
  and a lookup made first would race the cancel. I updated the docstring and the header's known-limits line.
* New `held_client_order_id(value) -> ClientOrderId | None`. It wraps the `ClientOrderId` constructor and
  returns None on the `ValueError` it raises for an empty, all-whitespace or non-ASCII string.
  * `_node_order_id`: an ID it cannot hold is never treated as a node order. A trade update for such an
    order is then dropped through the existing "this node does not hold" path.
  * `_root_id` (now returns `ClientOrderId | None`): when a link in the `replaces` chain has an ID it
    cannot hold, the walk passes over that link and keeps going. The result is the earliest ID in the chain
    that can be held. If none earlier can, the result is the order's own ID, or None if that cannot be held
    either. A None result is not cached in `_replacements`. I chose to pass over rather than stop so that a
    node order replaced outside the node under such an ID still traces back to the node's order. For the
    task's case (the unholdable order is the original), passing over and stopping give the same result:
    the replacement keeps its own ID.
  * `_order_report`: when no ID can be held, `order_status_report` builds `ClientOrderId(...)` inside its
    existing `try`. That reuses the documented split: an open order raises "Alpaca order `<id>` (AAPL) is
    open with invalid string …", which fails start-up and names the order; a closed one returns None. The
    warning for a closed one now gives the actual reason ("NautilusTrader cannot hold its client order ID
    '…'"), with the venue text cut by the existing `bounded`. Before, it would have said "type, side,
    class…", which is wrong for this case.

Records corrected so that none of them is now untrue:

* `packages/canon-alpaca/README.md`: the cancel-all bullet, the "Reports" bullet (unholdable client order
  IDs), the `test_execution_client.py` row, and a new row for the new test file.
* `docs/adr/0049-cancel-all-cancels-again-an-order-pending-cancel.md` (new, `accepted`). The repo rule
  (`.claude/rules/adr.md`) says not to rewrite an accepted ADR's decision, so the change is recorded
  here. `docs/adr/0036-…md` keeps its text, with an inline note and one Evidence line saying that only
  its `PENDING_CANCEL` skip is superseded by ADR 0049. `scripts/canon lint` reports it clean.

Tests added in `packages/canon-alpaca/tests/test_client_order_ids.py`, a new file; no existing test file
was edited:

* `held_client_order_id` for `""`, `"   "` and `"订单-1"`, and for a valid ID.
* `order_status_report` on such IDs: a closed order is not reported, an open one raises with its name,
  and it is reported under a traced ID when one is given.
* The `_root_id` trace passes over an unholdable middle link to the node's order.
* A chain with no ID that can be held returns None and is not cached.

## Upstream API or contract followed

* `nautilus_trader.live.clients.ExecutionClient`'s `_cancel_all_orders` and the reconciliation hooks
  (`_generate_order_status_report(s)`), as the module header already names them. The behaviour "an open
  order the client cannot represent fails start-up; a closed one is skipped with a warning" is the
  module's own documented contract, now applied to client order IDs as well.
* NautilusTrader's `ClientOrderId(value)` constructor (v2.0.0rc8 as installed). I checked it this
  session by calling it: `""` raises `ValueError: invalid string for 'value', was empty`, `"   "` raises
  "was all whitespace", `"订单-1"` raises "contained a non-ASCII char". The new tests assert this, so a pin
  move that changes it shows up.
* Alpaca's "Delete Order by ID" 422 for an order that can no longer be canceled, as the client's
  `NOT_CANCELABLE` constant quotes it, and its existing `_cancel_refused` handling.
* No dependency was added.

## What I ran, and the result

* `mise exec -- uv run --locked pytest`: before the change, the four named tests failed (seen for the
  closed-order case: "order status reports failed: invalid string for 'value', was empty"). After: **510
  passed, 24 skipped**. The skips are the paper tests, which need keys; I did not run them.
* The four named tests, `-k "cannot_hold or unholdable or no_answer"`: 5 passed (one is parametrized twice).
* `mise exec -- uv run --locked ruff check .`: All checks passed.
* `mise exec -- uv run --locked ruff format --check .`: 113 files already formatted.
* `mise exec -- uv run --locked ty check`: All checks passed.
* `scripts/canon lint`: clean.
* `scripts/ci`: exit 0, "ci: all checks passed". That includes actionlint and gitleaks on the history
  and the working tree; the paper tests were skipped because no credentials were in the environment.

## What I am unsure of

* That Kraken spot, Binance spot, Bybit and Interactive Brokers include pending-cancel orders in their
  cancel-all comes from the comment in the defect's test. I did not re-read their sources (no network), so
  it is marked `UNVERIFIED` in ADR 0049.
* No test covers the combined case: a cancel-all repeating a cancel that Alpaca had in fact taken, then
  answered 422 and handled with no event. Each half is tested separately. I could not write that test
  without editing `test_execution_client.py`, where the node helpers live, and I was not allowed to edit it.
* Passing over, rather than stopping at, an unholdable link in the middle of a `replaces` chain is my
  choice. The named tests do not decide between the two.
* ADR 0049 was written by me as a worker and has not had the `evidence-verifier` review the repo requires
  before a new record lands. `/code-review` was not run either: the brief rules out other agents. Both are
  needed before this lands.
