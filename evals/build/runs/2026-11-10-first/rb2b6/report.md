# Report: two execution-client defects in `packages/canon-alpaca/`

## What was wrong

1. **Cancel-all skipped `PENDING_CANCEL`.** `_cancel_all_orders` re-read each selected order and skipped it
   if `order.status == OrderStatus.PENDING_CANCEL`. NautilusTrader marks an order pending cancel when the
   cancel is sent. When that cancel gets no answer (a read timeout: `_cancel` logs it and generates no
   event), the order stays `PENDING_CANCEL` in the node while it may still be working at Alpaca. A later
   cancel-all then skipped exactly that order.
2. **Client order IDs NautilusTrader cannot hold.** `ClientOrderId("")` and `ClientOrderId("订单-1")` raise
   `ValueError` ("invalid string for 'value', was empty" / "... contained a non-ASCII char"; I ran this
   against the installed NautilusTrader v2.0.0rc8). `_root_id` (and `_node_order_id`) built
   `ClientOrderId(alpaca_order.client_order_id)` before `order_status_report` ran. `_order_report` catches
   only `APIError`/`OSError` around the trace, so the `ValueError` escaped. One closed order of that kind
   aborted `_order_status_reports`, and with it the mass status and start-up reconciliation. An open one
   failed without the order's name. A valid replacement of such an order failed too, because the trace
   built `ClientOrderId(prior.client_order_id)` for the unholdable original.

## What I changed, and why that way

`packages/canon-alpaca/src/canon_alpaca/execution.py`:

* **Cancel-all:** I removed the `PENDING_CANCEL` skip. Closed or missing orders are still skipped at their
  turn. A second cancel needs no new handling. A 422 already goes through `_cancel_refused`, which asks
  Alpaca for the order and generates nothing if it shows `canceled` or `pending_cancel`. A 204 waits for
  Alpaca's `canceled` update, as any cancel does. That makes the safety argument for the skip ("a second
  cancel risks a rejection", ADR 0036) moot. Keeping a per-order "no answer" flag instead (option 3 in the
  ADR) would add state to clear on every path that settles a cancel, to save one HTTP call.
* **Client order IDs:** I added `holdable_client_order_id(value) -> ClientOrderId | None`, which returns
  None where NautilusTrader's identifier raises `ValueError`.
  * `_root_id` now returns `ClientOrderId | None`. It starts from the order's own ID if that can be held,
    and it stops the `replaces` trace before an ancestor whose ID cannot be held. Such an ancestor cannot
    be one of the node's orders, so a valid replacement of it is its own root and is reported under its
    own ID. It caches only a non-None root.
  * `_node_order_id` treats an unholdable ID as "not the node's", so a trade update for such an order is
    dropped with the existing "does not hold" log line rather than logged as a failure.
  * When the order's own ID cannot be held and no holdable ancestor exists, `_order_report` passes no
    `client_order_id`. `order_status_report` then builds `ClientOrderId(order.client_order_id)` inside its
    existing `try`. That is the existing "cannot map" path: a closed order returns None and is skipped with
    a warning (whose text now lists "client order ID"); an open one raises `Alpaca order <id> (<symbol>) is
    open with invalid string ...`, which fails the mass status and start-up and names the order. I did not
    add a second check, so the order is named in the same way as every other unmappable open order.
* I corrected the docstrings that the change made untrue: the module header's "Known limits" (cancel-all,
  Reports, start-up failure), `_cancel_all_orders`, `_root_id` and `order_status_report`.

Docs:

* `packages/canon-alpaca/README.md`: Cancel-all and Reports now say what the code does. I added test-table
  entries for the new cases and the new test file.
* `docs/adr/0049-alpaca-cancel-all-includes-pending-cancel.md` (new) records the cancel-all change.
  ADR 0036 is accepted and stated the skip as part of its decision. The repo's rule (`.claude/rules/adr.md`)
  is not to rewrite an accepted decision, so I wrote a new record and added a "Superseded in part by
  ADR 0049" sentence to ADR 0036, following the precedent of ADRs 0014, 0017 and 0035. `scripts/canon lint`
  is clean.

New test file: `packages/canon-alpaca/tests/test_client_order_ids.py`, 6 tests:
* `ClientOrderId` refuses both kinds of ID and the helper returns None for them.
* An ASCII ID is held.
* A trade update's order with an unholdable ID traces to none of the node's orders and is not cached.
* A replacement still traces to the node's order.

I did not edit any existing test file.

## Upstream API or contract followed

* NautilusTrader `ClientOrderId` (v2.0.0rc8 as installed): it raises `ValueError` for an empty or non-ASCII
  value. I verified this by running it, and the helper's docstring names it.
* NautilusTrader's `ExecutionClient` reconciliation hooks, as the module header already names them. An open
  order that cannot be represented fails the report; a closed one is skipped (the module's existing
  contract, which now covers this case).
* `CancelAllOrders` through `_cancel_all_orders`: by ID, from the cache, as ADR 0036 already describes, now
  without the pending-cancel exception.

## What I ran

* The 4 named tests (5 cases, one is parametrised) before the change: 5 failed. After: 5 passed.
* `mise exec -- uv run --locked pytest` (whole workspace): 503 passed, 24 skipped. The skipped ones are the
  paper tests, which need keys; I did not run those.
* `mise exec -- uv run --locked ruff check .`: all checks passed.
* `mise exec -- uv run --locked ruff format --check .`: 113 files already formatted.
* `mise exec -- uv run --locked ty check`: all checks passed.
* `mise exec -- scripts/canon lint`: clean.
* I did not run `scripts/ci`. The brief says this copy lacks files it expects.

## What I am unsure of / did not verify

* **Upstream adapters (UNVERIFIED this session).** I had no network access, so I did not read the claim
  that upstream's Kraken spot, Binance spot, Bybit and Interactive Brokers adapters include `PENDING_CANCEL`
  orders in a cancel-all. It comes from the test's comment and ADR 0036's earlier reading of those files.
  The Rust sources are not in the installed wheel. ADR 0036 recorded that Coinbase does skip such orders.
  ADR 0049 marks all of this UNVERIFIED.
* **Alpaca's answer to a second cancel (UNVERIFIED).** I don't know what Alpaca answers to a cancel of an
  order already `pending_cancel`, 204 or 422; both paths are handled. Nor do I know which characters
  Alpaca accepts in a client order ID. The code and ADR say so.
* **No independent review.** I ran no `/code-review` or `evidence-verifier`. The brief forbids starting
  other agents. Before landing, the repo's ADR 0017 would want `/code-review` on the code and
  `evidence-verifier` on ADR 0049. ADR 0049's `decision-makers` line says the second reader is pending.
* **`_order_report` warning for `replaced` orders.** A closed order whose ID cannot be held, and also a
  `replaced` order (as before), logs a warning that lists several possible reasons, not the specific one.
  I left that generic message as it was, apart from adding "client order ID".
