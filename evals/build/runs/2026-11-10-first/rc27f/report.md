# Report: two execution-client defects in `packages/canon-alpaca/`

## What was wrong

1. **Cancel-all skipped `PENDING_CANCEL`.** `_cancel_all_orders` re-read each order at its turn and skipped
   it if `order.status == OrderStatus.PENDING_CANCEL` (ADR 0036: "since a second cancel risks a
   rejection"). NautilusTrader sets that status when it *sends* a cancel, not when Alpaca takes it. A cancel
   that got no answer (a read timeout) left the order pending cancel while it still worked at Alpaca, and
   no later cancel-all touched it. Seen on the original code: the given test failed with one cancel sent
   where two were expected.
2. **Unholdable client order IDs aborted reconciliation.** `_root_id` (and `_node_order_id`) built
   `ClientOrderId(alpaca_order.client_order_id)` before any handling. For an empty or non-ASCII ID, the
   constructor raises `ValueError` ("invalid string for 'value', was empty" / "contained a non-ASCII
   char"). `_order_report` catches only `APIError`/`OSError` around the trace, so one such order, even a
   closed one, failed `_order_status_reports`, the mass status and start-up. The same raise would have made
   a trade update for such an order fail with an error, not be dropped as an order the node does not hold.
   The original-chain code also built `ClientOrderId(prior.client_order_id)` for every link, so a
   replacement of a dashboard order with a non-ASCII ID could not be traced at all.

## What I changed, and why that way

`packages/canon-alpaca/src/canon_alpaca/execution.py`:

* **Cancel-all:** dropped the `PENDING_CANCEL` skip; closed orders are still skipped at their turn. A
  second cancel to an order Alpaca is already canceling is safe on the existing path: answered 204, it
  waits for the `canceled` update; refused 422, `_cancel_refused` asks Alpaca for the order and generates
  nothing when it shows it canceled or pending cancel. I chose not to add a lookup before re-sending
  (option 3 in the ADR): the 422 path already covers it.
* **New `holdable_client_order_id(value)`:** returns `ClientOrderId(value)` or `None` when the constructor
  refuses it. NautilusTrader's own check decides; the repo doesn't re-implement it.
* **`_root_id`** now returns `ClientOrderId | None`: the first ID NautilusTrader can hold along the
  `replaces` chain. It passes over a link whose ID can't be held and keeps walking, so a node order →
  unholdable → replacement chain still traces to the node's order. An unholdable original makes its
  replacement its own root, reported under its own ID. With no holdable ID in the chain it returns `None`.
  An unholdable ID is never looked up in, or written to, the `_replacements` cache, since an empty ID need
  not name one order.
* **`_node_order_id`** checks its own ID through the same helper and handles a `None` root, so such a trade
  update is dropped as unknown.
* **`_order_report`** already passes a `None` root through. `order_status_report` builds the
  `ClientOrderId` inside its existing `try`. So a closed order is skipped with the warning, now worded to
  name the client order ID as a reason. An open one raises "Alpaca order <id> (AAPL) is open with invalid
  string …", which the node logs and which fails start-up ("Failed to get mass status").
* The module header's "Cancel-all", "Reports" and "Start-up fails" limits and the touched docstrings are
  updated.

Records and docs made untrue, corrected:

* `packages/canon-alpaca/README.md`: the cancel-all bullet, the reconciliation "Reports" bullet, the
  `test_execution_client.py` row, and a new row for the added test file.
* `docs/adr/0036-alpaca-adapter-order-management.md`: its cancel-all decision named the skip. Per
  `.claude/rules/adr.md` (an accepted ADR's decision isn't rewritten), I added "Superseded in part by
  ADR 0049" to it, as ADR 0020 does for ADR 0028.
* New `docs/adr/0049-cancel-all-resends-to-an-order-pending-cancel.md` records the reversed clause. The
  canon lint is clean.

Added test file `packages/canon-alpaca/tests/test_client_order_ids.py`, 9 tests, unit-level with a stand-in
for the client's cache, replacement map and order lookup. It covers:

* the helper;
* a trade update for an unholdable order not being the node's;
* an unholdable middle link passed over to the node's order;
* an unholdable replacement traced but not cached;
* a chain with no holdable ID having no root.

No existing test file was edited.

## Upstream API or contract followed

* NautilusTrader v2.0.0rc8 `ClientOrderId` constructor. I ran it here: it raises `ValueError` for `""`,
  all-whitespace and non-ASCII values.
* `ExecutionClient` reconciliation hooks, as the module header already names them: `_generate_order_status_reports`
  feeding the native mass status.
* Alpaca's "Delete Order by ID" 204/422 contract, as quoted in the module.

The test comment's claim that upstream's Kraken spot, Binance spot, Bybit and Interactive Brokers adapters
cancel every open order is **UNVERIFIED** this session. The installed wheel has no Rust adapter source, and
I didn't use the network. ADR 0049 says so.

## What I ran

* Before the change: the 4 named tests (5 cases) failed. The cancel test showed `assert 1 == 2`; the
  others showed "invalid string for 'value', was empty".
* `mise exec -- uv run --locked pytest`: **506 passed, 24 skipped**. All 24 skips are paper tests, which
  need a key.
* `ruff check .`: all passed. `ruff format --check .`: 113 files already formatted. `ty check`: all
  passed.
* `scripts/canon lint`: clean.
* Against the original source, the new test file fails at import, since `holdable_client_order_id`
  doesn't exist there. I didn't separately show each of its tracing tests failing against the original
  code.
* Not run: `scripts/ci` (the task says this copy lacks files it needs), the paper tests, `scripts/land`.

## Unsure / left

* I couldn't start the repo's required review lanes, `/code-review` for the code and `evidence-verifier`
  for the new ADR 0049, because the task forbids subagents. Both are owed before this lands.
* ADR 0049's status is `accepted` on the strength of the confirmed defect. A reviewer may prefer
  `proposed` until the upstream adapter claim is re-read at the pin.
* Whether Alpaca answers a repeated cancel on a pending-cancel order with 204 or 422 isn't on a page read
  (UNVERIFIED); both are handled.
* Passing over an unholdable link mid-chain, rather than stopping there, is my reading of "the trace
  stops at the unholdable original". The named test covers only the original being the chain's first
  order.
* No paper run of either fix.
