# Alpaca execution client: cancel-all and unholdable client order IDs

## What was wrong

1. **Cancel-all skipped `PENDING_CANCEL`.** In `packages/canon-alpaca/src/canon_alpaca/execution.py`,
   `_cancel_all_orders` re-read each selected order and skipped it when
   `order.status == OrderStatus.PENDING_CANCEL`. NautilusTrader generates `OrderPendingCancel` itself
   when a cancel command is sent, so a single cancel that got no answer (for example a read timeout,
   ADR 0039) left the node's order in `PENDING_CANCEL` while the order still worked at Alpaca. A later
   cancel-all then sent nothing for it, and no later cancel-all would ever reach it.

2. **Tracing an order with a client order ID NautilusTrader cannot hold raised outside the skip/fail
   handling.** Alpaca's orders endpoint can list an order placed outside the node whose
   `client_order_id` is empty or non-ASCII. `ClientOrderId` (`nautilus_trader.model`, pinned
   NautilusTrader v2.0.0rc8) refuses both — verified this session against the installed pinned package:
   `ClientOrderId("")` raises `ValueError: invalid string for 'value', was empty` and
   `ClientOrderId("订单-1")` raises `ValueError: invalid string ... contained a non-ASCII char`.
   `order_status_report` already handled that `ValueError` correctly (return `None` for a closed order,
   raise `Alpaca order {id} ({symbol}) is open with {e}` for an open one), but `_root_id` constructed
   `ClientOrderId(alpaca_order.client_order_id)` and `ClientOrderId(prior.client_order_id)` before
   `order_status_report` was reached, and `_order_report` caught only `APIError`/`OSError` around the
   trace. One closed order with such an ID therefore aborted the whole mass status
   (`Alpaca order status reports failed: invalid string ...` → `Failed to get mass status from ALPACA`)
   and with it start-up reconciliation; an open one failed start-up but without the order's name in the
   reason; and a valid replacement was lost when its `replaces` chain reached the unholdable original.

## What I changed, and why that way

All changes are in the working tree, uncommitted, in three files. No dependency was added and no test
file was edited.

### `packages/canon-alpaca/src/canon_alpaca/execution.py`

- **Cancel-all now includes `PENDING_CANCEL`.** The re-read skip is now only
  `if order is None or order.is_closed: continue`. The docstring, the module header's cancel-all
  paragraph and Known-limits bullet, and the re-read comment were updated to match. Why this way: the
  defect is the skip itself, not the test's timing. A repeat cancel is safe through the existing path —
  `_cancel` calls Alpaca's "Delete Order by ID" by venue ID, and a 422 for a cancel Alpaca already took
  is settled by `_cancel_refused`, which looks the order up and generates nothing when Alpaca shows it
  canceled or pending cancel. Removing the skip therefore re-cancels the stranded order without
  inventing a new outcome for the already-taken case.

- **Tracing stops at an unholdable ID instead of raising.** New helper
  `holdable_client_order_id(value) -> ClientOrderId | None` returns `None` on the `ValueError` from
  `ClientOrderId`. `_root_id` now returns `ClientOrderId | None`: it starts from the holdable form of
  the order's own ID, and in the `replaces` walk a prior whose ID is unholdable breaks the walk and
  keeps the last holdable ID. It caches only a non-`None` result. `_node_order_id` uses the same helper
  for the order's own ID and tolerates a `None` root. Consequences, by case:
  - Closed order, no holdable ID in its chain: `_order_report` passes `renamed=None` to
    `order_status_report`, which returns `None`; `_order_report` logs the existing
    "... is not reported: its type, side, class, quantity, time in force or client order ID is not in
    this slice" warning (wording widened to name client order ID) and mass status continues.
  - Open order, no holdable ID: the same call raises
    `Alpaca order {id} ({symbol}) is open with invalid string ...`, so start-up fails with the order
    named, as the test asserts.
  - Valid replacement of an unholdable original: the walk stops at the original and keeps the
    replacement's own holdable ID, so it is reported under `alpaca-made-1` in the test.
  - Replacement traced to a node order (including one whose own Alpaca ID is unholdable but whose
    `replaces` chain reaches a holdable node ID): reported under the node's ID through the existing
    `renamed` argument, which bypasses the unholdable string in `order_status_report`.
  - Trade updates: an update for an external order with an unholdable ID now resolves to `None` from
    `_node_order_id` and is logged as "which this node does not hold; reconciliation reports it"
    instead of an apply failure. That is the same drop path other external orders take.

  Why this way: it puts the failure back inside the handling the code already had. `order_status_report`
  is the one place that decides open-fails/closed-skips for every other unmappable field (type, side,
  class, status, quantity, time in force); the fix stops the trace from pre-empting it and extends that
  list with client order ID, rather than adding a second open/closed decision in `_order_report`.

- Header bullets for Reports and start-up failure now name the client-order-ID case and the
  trace-stops rule, so the header stays true.

### `packages/canon-alpaca/README.md`

- Cancel-all: "skipping one already pending cancel" → including one already pending cancel, with the
  no-answer reason.
- Reports: the unmappable list now includes client order ID, with the `ClientOrderId` empty/non-ASCII
  note and the replacement-under-its-own-ID rule.

### `docs/adr/0036-alpaca-adapter-order-management.md`

- The cancel-all decision sentence "skipped if closed or `PENDING_CANCEL` (as Coinbase ...)" is
  corrected to skipped-if-closed with `PENDING_CANCEL` canceled again, and an Evidence correction line
  records the reversal, its defect, and that the Kraken/Binance/Bybit/Interactive Brokers evidence was
  read in this record and not re-fetched for the correction. See "Unsure" below for the record-form
  caveat.

## Upstream API or contract the change follows

- **Cancel-all:** NautilusTrader's execution policies ("Command outcomes", pinned v2.0.0rc8, as cited in
  the module header): cancel-all refusals are `OrderCancelRejected` through the single-cancel path.
  Alpaca's "Delete All Orders" is still not called (account-wide, wider than the command). The
  include-every-open-order selection follows upstream's Kraken spot, Binance spot, Bybit and
  Interactive Brokers adapters at the pin as recorded in ADR 0036's Evidence (`crates/adapters/`:
  `kraken/src/execution/spot.rs`, `binance/src/spot/execution.rs`, `bybit/src/execution.rs`,
  `interactive_brokers/src/execution/core.rs`) — read this session via ADR 0036 and the module header,
  not re-fetched (offline task; no network beyond `uv`). The Coinbase skip that ADR 0036 originally
  followed is the reading the confirmed defect overturns.
- **Client order IDs:** `ClientOrderId` from `nautilus_trader.model` at the pin — its "invalid string"
  validation for empty or non-ASCII values, verified this session by constructing it in the pinned
  environment (see above). Alpaca side: orders from Alpaca's orders endpoint (`get_orders`,
  `GetOrdersRequest`) can carry such IDs for orders placed outside the node; the adapter's existing
  reconciliation contract (module header, Reconciliation paragraph) is that an unmappable open order
  fails the report by name and a closed one is skipped with a warning. The `replaces` tracing itself is
  the existing ADR 0042 mechanism (Alpaca's "Replace Order by ID", `replaced_by`/`replaces`).

## What I ran, with its result

All commands via `mise exec -- uv run --locked ...` from the checkout root, after `mise trust` for this
copy. No paper tests, no credentials, no network beyond `uv` installing the pinned `uv.lock` set into
`.venv`.

- Before the fix, the four named tests (5 cases with parametrisation):
  `test_closed_order_with_a_client_id_nautilus_cannot_hold_is_skipped` failed with
  `RuntimeError: Failed to get mass status from ALPACA` (log: `invalid string for 'value', was empty`);
  `test_open_order_with_a_client_id_nautilus_cannot_hold_fails_startup_by_name` failed on the missing
  `Alpaca order 7b08df51-... (AAPL) is open with invalid string` log line;
  `test_replacement_of_an_order_with_an_unholdable_id_is_reported_under_its_own` and
  `test_cancel_all_cancels_again_an_order_whose_cancel_had_no_answer` failed (cancel count stayed 1).
- After the fix, the same four: **5 passed**.
- `pytest packages/canon-alpaca/tests/test_execution_client.py -q`: **140 passed**.
- `pytest -q` (whole workspace): **497 passed, 24 skipped** in 135.92s. The skips are the paper tests
  (no keys in this environment, by design).
- `ruff check .`: **All checks passed!**
- `ruff format --check .`: **111 files already formatted**.
- `ty check`: **All checks passed!**
- `scripts/canon lint`: **canon: lint clean**. (`scripts/ci` was not run as the gate: the task states
  this copy leaves out files it expects, so its result here would not be attributable.)

## Anything I am unsure of

- **ADR form (UNVERIFIED as a landing decision).** ADR 0002/`.claude/rules/adr.md` say not to rewrite an
  accepted ADR to change its decision — write a superseding one. I corrected ADR 0036 in place with a
  dated Evidence correction line because the task instructs correcting a record the change makes untrue,
  forbids starting other agents (a new record needs the `evidence-verifier` reader before landing), and
  states nothing here lands. On landing, the coordinator should instead write a new ADR superseding
  ADR 0036's cancel-all clause and reduce my in-place correction to a "superseded by" pointer. The stop
  and account-query decisions in ADR 0036 are untouched and still true.
- **Upstream adapter readings not re-fetched.** The Kraken/Binance/Bybit/Interactive Brokers
  include-every-open-order claim is taken from ADR 0036's Evidence and the failing test's comment, read
  this session; the pinned crates were not re-read (offline). It is therefore UNVERIFIED first-hand in
  this session, though the regression test asserts the behaviour directly.
- **Paper behaviour UNVERIFIED.** No paper test was run (task forbids keys). In particular, what Alpaca
  answers a second cancel of an order already `pending_cancel` at the venue (204 vs 422) is handled by
  the existing `_cancel_refused` lookup either way, but which one paper returns is UNVERIFIED.
- **No new test file added.** The task forbids editing any test file already in this checkout, and the
  four named tests are the durable regression coverage for both defects (empty and non-ASCII closed,
  empty open failing by name, replacement under its own ID, pending-cancel re-cancel). I added no
  duplicate file. On landing, those tests land with the fix as usual.
- Left in the working tree, uncommitted: `packages/canon-alpaca/src/canon_alpaca/execution.py`,
  `packages/canon-alpaca/README.md`, `docs/adr/0036-alpaca-adapter-order-management.md`. Nothing else
  is left: no other work, no paper run, no leftover process.
