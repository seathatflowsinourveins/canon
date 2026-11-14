# Report: Alpaca execution client — pending-cancel cancel-all and unholdable client order IDs

## What was wrong

Two independent defects in `packages/canon-alpaca/src/canon_alpaca/execution.py`.

1. **Cancel-all skipped an order already `PENDING_CANCEL`.** `_cancel_all_orders` selected the cache's
   open and in-flight orders, but then skipped any whose local status was `PENDING_CANCEL`, on the
   assumption that a cancel was in flight. A cancel that gets no answer (for example a read timeout)
   leaves the node's order `PENDING_CANCEL` while Alpaca still works the order, so a later cancel-all
   never sent the cancel again.

2. **Tracing an order with a client order ID NautilusTrader cannot hold raised too early.**
   `_root_id` and `_node_order_id` constructed `ClientOrderId(alpaca_order.client_order_id)` directly.
   NautilusTrader v2.0.0rc8's `ClientOrderId` rejects an empty, all-whitespace or non-ASCII value with a
   `ValueError`. Alpaca can list an order made outside the node with such an ID. That `ValueError` was
   raised inside tracing, before `order_status_report`'s existing open-versus-closed handling, so:
   - a closed order like that aborted the whole mass status instead of being skipped with a warning;
   - an open order did not fail start-up through the reporting path that names the order;
   - a valid replacement whose original had an unholdable client order ID aborted its trace instead of
     being reported under its own valid ID.

## What changed, and why that way

- **Cancel-all** now re-reads each selected order at its turn and skips it only when it is missing or
  closed. An order in `PENDING_CANCEL` is sent through the same `_cancel` path again. A repeat Alpaca
  refuses is still handled by the existing `_cancel_refused` lookup and retry logic. This fixes the
  behaviour at the cancel-all owner rather than weakening the state machine or special-casing the test.
- **Client order IDs** are now converted through a new `holdable_client_order_id` helper, which returns
  `None` when `ClientOrderId` raises its `ValueError`. `_root_id` and `_node_order_id` use it:
  - an unholdable ID is not adopted as a trace root;
  - tracing continues through that order's `replaces` link;
  - if the chain ends at an unholdable ID, a replacement keeps its own valid ID as the root, while an
    order whose own ID is unholdable has no root.
  The existing `order_status_report` then makes the open/closed decision: an open unrepresentable order
  raises `Alpaca order <id> (<symbol>) is open with invalid string ...`, and a closed one returns `None`,
  which `_order_report` logs as a warning. The warning text now names the client order ID among the
  fields this slice cannot map. This keeps the open/closed policy in the reporting function where it
  already lived, and validates the actual NautilusTrader constraint rather than a list of sample strings.
- Corrected the now-false statements in `execution.py`'s header, the package README, and ADR 0036. The
  ADR now says cancel-all skips an order only when it is gone from the cache or closed, and cancels a
  `PENDING_CANCEL` order again.

No test file was edited. The four named tests (one parametrized twice) are unchanged and now pass.

## Upstream API / contract followed

- `nautilus_trader.live.clients.ExecutionClient`, as named in the module header: the
  `_cancel_all_orders` and `_generate_order_status_reports` hooks.
- `nautilus_trader.model.ClientOrderId` at the pinned v2.0.0rc8. Its own contract is
  "Represents a valid client order ID (assigned by the Nautilus system)"; the pinned class raises
  `ValueError` for empty, all-whitespace and non-ASCII values. `OrderStatusReport` carries that ID, so an
  order whose ID it cannot represent cannot be reported as-is.
- The existing reconciliation policy in this package and NautilusTrader's reconciliation contract: an
  open order this slice cannot represent fails start-up; a closed one is skipped with a warning.
- The cancel-all selection and by-ID cancellation are the behaviour ADR 0036 already records for
  upstream's Kraken spot, Binance spot, Bybit and Interactive Brokers adapters, which include every open
  order. A cancel refused with 422 remains handled under the existing Alpaca "Delete Order by ID"
  contract already cited in the module header: the order is looked up, and nothing is generated when
  Alpaca shows it canceled or pending cancel.

## What I ran, and the results

From the checkout root:

- `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -k "cancel_all_cancels_again_an_order_whose_cancel_had_no_answer or closed_order_with_a_client_id_nautilus_cannot_hold_is_skipped or open_order_with_a_client_id_nautilus_cannot_hold_fails_startup_by_name or replacement_of_an_order_with_an_unholdable_id_is_reported_under_its_own" -q`
  — **5 passed, 135 deselected** (before the change: 5 failed).
- `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -q`
  — **140 passed**.
- `mise exec -- uv run --locked pytest -q`
  — **497 passed, 24 skipped**. No paper test or key was used; the paper tests skipped.
- `mise exec -- uv run --locked ruff check .` — **All checks passed.**
- `mise exec -- uv run --locked ruff format --check .` — **111 files already formatted.**
- `mise exec -- uv run --locked ty check` — **All checks passed.**
- `scripts/canon lint` — **canon: lint clean** (run because ADR 0036 was edited).

I did not run `scripts/ci` or `scripts/land`: this copy is not meant to land, and `scripts/ci` expects
files this checkout leaves out. I did not run the paper tests, use credentials, or reach the network
beyond `uv` running against the existing lock.

I also ran the closed-order test with `-s` and confirmed the warning is emitted:
`Alpaca order <id> (AAPL, canceled) is not reported: its client order ID, type, side, class, quantity or
time in force is not in this slice`.

## What I am unsure of

- **Not verified on paper.** The offline stand-in accepts the second cancel and sends `canceled`. On a
  real paper account Alpaca might answer the repeat with 422 while the first cancel is still pending; the
  existing `_cancel_refused` path would then look the order up and generate nothing if it is canceled or
  pending cancel. If Alpaca still showed the order open after a 422, the client would generate
  `OrderCancelRejected`; that is the existing policy for a refused cancel, not new behaviour. This was not
  exercised against the paper API because paper tests and credentials were out of scope.
- **Unholdable intermediate replacements.** I chose to continue walking `replaces` through an order whose
  client order ID is unholdable, so a valid ancestor can still identify the node's order. The named test
  covers an unholdable *terminal* original and a valid replacement reported under its own ID. If the
  intended rule were to stop at any unholdable link rather than only when the chain has no valid root, the
  tracing branch would need a small adjustment.
- I did not re-fetch Alpaca's docs this session; the Alpaca endpoint claims used here are the ones already
  cited in the module header/README and ADR 0036. The `ClientOrderId` behaviour was checked directly
  against the installed pinned `nautilus-trader==2.0.0rc8`.

## Files changed (left in the working tree, uncommitted)

- `packages/canon-alpaca/src/canon_alpaca/execution.py`
- `packages/canon-alpaca/README.md`
- `docs/adr/0036-alpaca-adapter-order-management.md`
