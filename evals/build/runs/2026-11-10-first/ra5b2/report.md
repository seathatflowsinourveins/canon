# Report: two defects in the Alpaca execution client

## What was wrong

1. **Cancel-all skipped `PENDING_CANCEL`.** In
   [execution.py](<tree>/packages/canon-alpaca/src/canon_alpaca/execution.py)
   `_cancel_all_orders` re-read each selected order and continued when
   `order.status == OrderStatus.PENDING_CANCEL`. A first cancel that got no answer — for
   example a read timeout, an unknown outcome under ADR 0039 — leaves the node's order
   `PENDING_CANCEL` (NautilusTrader generates `OrderPendingCancel` when the command is
   sent) while the order still works at Alpaca. The later cancel-all then never sent it
   again, so the order kept working. Reproduced before the fix:
   `test_cancel_all_cancels_again_an_order_whose_cancel_had_no_answer` saw 1 cancel
   where 2 were required.

2. **An unholdable client order ID raised during tracing, outside the skip/fail
   handling.** NautilusTrader's `ClientOrderId` (`nautilus_trader.model`) is a validated
   string: with the pin it raises `ValueError: invalid string for 'value', was empty`
   for `""` and `ValueError: invalid string ... contained a non-ASCII char` for
   `"订单-1"` (verified this session with `ClientOrderId(...)` against the installed
   pin). Alpaca can list such an ID on an order made outside the node.
   `order_status_report` already encodes the intended rule — raise
   `Alpaca order {id} ({symbol}) is open with {reason}` for an open order, return
   `None` (skip with a warning in the caller) for a closed one — but `_root_id` and
   `_node_order_id` constructed `ClientOrderId(order.client_order_id)` and
   `ClientOrderId(prior.client_order_id)` directly. That raise happened before
   `order_status_report` was reached, so one closed order with such an ID aborted
   the whole mass status (`Failed to get mass status from ALPACA`), an open one
   failed without the naming prefix, and tracing a valid replacement back to an
   unholdable original aborted instead of reporting the replacement under its own
   ID. Reproduced before the fix: all three reconciliation tests failed with
   `Alpaca order status reports failed: invalid string ...` and no
   `is open with invalid string` prefix.

## What was changed, and why that way

All changes are in the working tree, uncommitted, in three files. No dependency
was added and no test file was edited.

### Cancel-all includes `PENDING_CANCEL`

* `_cancel_all_orders`: the re-read guard is now `if order is None or order.is_closed:`
  only. The selection (cache `orders_open` + `orders_inflight` for the command's
  instrument/side, each canceled by ID through `_cancel_logged`/`_cancel`) is
  unchanged, so a `PENDING_CANCEL` order is sent again through the same path as any
  other cancel.
* Why this is safe: if the first cancel did reach Alpaca, the second is answered
  422 and `_cancel_refused` already generates nothing when Alpaca shows the order
  `canceled` or `pending_cancel` (the alpaca-py retry case, ADR 0030/0036). If the
  first never took effect, the second is the cancel that works. A genuine refusal
  still produces `OrderCancelRejected` and the rest still go on.

### Unholdable IDs never raise during tracing

* New helper `holdable_client_order_id(value) -> ClientOrderId | None`: returns
  `ClientOrderId(value)`, or `None` when it raises `ValueError`. Its docstring names
  the contract it follows (below).
* `_root_id` now returns `ClientOrderId | None`. The order's own unholdable ID
  gives `None` without caching; a prior's unholdable ID stops the trace (`break`),
  keeping the last holdable ID — so a valid replacement is reported under its own
  ID. Cache, `MAX_REPLACE_CHAIN` walk and 404 handling are otherwise unchanged.
* `_node_order_id` returns `None` for an unholdable own ID (the node cannot hold
  the order, so a trade update for it is dropped with the existing
  "which this node does not hold" log instead of an apply error), and handles a
  `None` root.
* `_order_report` needed no new branch: a `None` root now falls through to
  `order_status_report`, which applies the open/closed rule — open raises
  `Alpaca order {id} (AAPL) is open with invalid string ...` (which
  `_generate_order_status_reports` logs via `logged_failure` and the node turns
  into `Failed to get mass status`), closed returns `None` and `_order_report`
  logs the "is not reported" warning. The warning text now lists the client order
  ID among the unmappable fields so it stays true for this case.
* `order_status_report` itself is unchanged in behaviour; its docstring now states
  the client-ID case explicitly.

### Records the change made untrue, corrected

* `execution.py` header: the cancel-all bullet now says "including an order
  already pending cancel"; the Reports bullet and the start-up bullet now name
  the unholdable client order ID (open fails, closed skipped with a warning,
  valid replacement reported under its own ID).
* [README.md](<tree>/packages/canon-alpaca/README.md):
  the Cancel-all and Reports bullets in "Known limits of the execution slice" say
  the same.
* [ADR 0036](<tree>/docs/adr/0036-alpaca-adapter-order-management.md):
  the cancel-all decision now reads "skipped only if closed" with the reason
  above, replacing "skipped if closed or `PENDING_CANCEL` (as Coinbase...)".
  The Evidence section's historical second-reader note was left as the record of
  that review.

No new maintained test file was added: the four reproducing tests already live in
`packages/canon-alpaca/tests/test_execution_client.py`, the task forbids editing
test files in this checkout, and the grader restores them — they are the durable
coverage for both defects. A throwaway probe (below) covered the one variant the
named tests do not (open non-ASCII) without touching the repo.

## Which upstream API or contract the change follows

* **Cancel-all:** NautilusTrader's execution policies ("Command outcomes",
  `OrderCancelRejected` for "Cancel, cancel-all, or batch cancel") via the same
  single-cancel path, and upstream's cache-selection practice at the pin as
  recorded in ADR 0036's Evidence (`kraken/src/execution/spot.rs`,
  `binance/src/spot/execution.rs`, `bybit/src/execution.rs`,
  `interactive_brokers/src/execution/core.rs`): Kraken spot, Binance spot, Bybit
  and Interactive Brokers include every open order. Alpaca's account-wide
  "Delete All Orders" is still not called. The `_cancel_all_orders` docstring
  and `execution.py` header name this, as the surrounding code does.
* **Unholdable IDs:** NautilusTrader's `ClientOrderId` validated-string contract
  (`nautilus_trader.model`, v2.0.0rc8 pin; empty/non-ASCII refused, verified
  against the installed package this session) and the reconciliation contract
  already in `order_status_report` (open unrepresentable order fails the report
  by name; closed is skipped with a warning). `holdable_client_order_id`'s
  docstring names `ClientOrderId` and the observed `invalid string` refusal.
  No Alpaca or alpaca-py API changed: tracing still follows `replaces` venue
  IDs through alpaca-py's `get_order_by_id` / the fetched `known` map.

## What was run, with its result

All offline, from the checkout root, no credentials, no network beyond `uv`
installing the pinned `uv.lock` into `.venv`:

* Before the fix, the four named tests (5 cases with the closed parametrisation):
  **5 failed** — cancel-all saw 1 of 2 cancels; closed raised
  `Failed to get mass status`; open lacked the
  `Alpaca order ... is open with invalid string` log line; replacement aborted
  the mass status.
* After the fix, the same four tests: **5 passed**.
* `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -q`:
  **140 passed**.
* `mise exec -- uv run --locked pytest -q` (whole workspace; paper tests skip
  without keys): **497 passed, 24 skipped**.
* `mise exec -- uv run --locked ruff check .`: **All checks passed**.
* `mise exec -- uv run --locked ruff format --check .`: **111 files already formatted**.
* `mise exec -- uv run --locked ty check`: **All checks passed**.
* Throwaway probe `/tmp/probe_unholdable.py` (not in the repo): the helper maps
  `""`/`"订单-1"` to `None`; `order_status_report` raises
  `Alpaca order 7b08df51-... (AAPL) is open with invalid string ...` for an open
  non-ASCII order (the named open test uses `""` only) and returns `None` for
  closed empty/non-ASCII. **All probes passed.**

`scripts/ci` was not run: the task states this copy leaves out files it expects,
so its failure here may not be attributable; the graded gates above were run
instead.

## Anything unsure of

* A `replaces` chain longer than the tested one where an *intermediate* (not the
  original) carries an unholdable ID: the trace stops there and the live order is
  reported under its own last holdable ID rather than walking past the
  unholdable link by venue ID to an earlier node order. That matches the task's
  "the trace stops at the unholdable original" for the covered case; intermediates
  are Alpaca-made replacement IDs (observed holdable), so the distinction is
  UNVERIFIED against a real chain and untested beyond the named tests.
* The closed-order skip warning is the caller's generic "is not reported: its
  client order ID, type, side, ..." line; it does not quote the underlying
  `invalid string` reason. The named tests require a warning and an empty cache,
  both of which hold, but a more specific reason would be nicer.
* ADR 0036's Evidence bullet still records the earlier second-reader correction
  mentioning the `PENDING_CANCEL` skip; it was left as history of that review
  rather than rewritten. If the repo prefers ADRs never to be edited in place,
  this decision change should instead be a superseding ADR — flagged here rather
  than silently creating one, since the task asked for the defect fix and
  correcting records it made untrue.
* Paper behaviour (a real second cancel against Alpaca, a real dashboard order
  with an unholdable ID) is UNVERIFIED: no paper test was run, per the task.
