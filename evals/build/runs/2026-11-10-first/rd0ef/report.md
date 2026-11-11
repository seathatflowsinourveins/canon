# Execution Client Defect Report (`packages/canon-alpaca`)

## What Was Wrong

Two defects existed in `packages/canon-alpaca/src/canon_alpaca/execution.py`:

1. **`_cancel_all_orders` skipped orders in `OrderStatus.PENDING_CANCEL`:**
   - When a strategy cancels an order, NautilusTrader transitions the cached order to `OrderStatus.PENDING_CANCEL` (`is_open=True`, `is_inflight=True`) before `_cancel_order` runs.
   - Under NautilusTrader's execution policies ("Command outcomes") and ADR 0039, a cancel that gets no definitive answer (such as a read timeout or connection drop after sending, or a non-422 HTTP error) leaves the venue outcome unknown and leaves the node's order in `OrderStatus.PENDING_CANCEL` while the order may still be working at Alpaca.
   - `_cancel_all_orders` selected open and in-flight orders from `self.cache.orders_open` and `self.cache.orders_inflight`, re-read each order at its turn, and skipped it on `order.status == OrderStatus.PENDING_CANCEL` (`execution.py:1093`). Consequently, a later cancel-all never sent another cancel for an order whose earlier cancel had received no answer.

2. **Tracing `replaces` chains constructed `ClientOrderId` outside report error handling:**
   - Orders placed outside the node (such as from Alpaca's dashboard or another API client) can carry a `client_order_id` (`str` in `alpaca.trading.models.Order`) that `nautilus_trader.model.ClientOrderId` refuses with `ValueError` (empty `""`, whitespace-only, or containing non-ASCII characters).
   - `order_status_report` already wrapped `ClientOrderId(...)` inside its `try ... except ValueError` block—returning `None` for closed orders and raising `ValueError(f"Alpaca order {order.id} ({order.symbol}) is open with {e}")` for open orders—but `_order_report` called `_root_id` *before* `order_status_report`.
   - Both `_root_id` and `_node_order_id` called `ClientOrderId(alpaca_order.client_order_id)` and `ClientOrderId(prior.client_order_id)` unconditionally, outside `order_status_report`'s `ValueError` handling:
     - A closed order with an empty or non-ASCII `client_order_id` raised an uncaught `ValueError` during `_generate_order_status_reports`, aborting the mass status and start-up reconciliation instead of being skipped with a warning.
     - An open order with an unholdable `client_order_id` failed inside `_root_id` with the raw `ClientOrderId` validation message instead of `order_status_report`'s message naming the Alpaca order ID and symbol.
     - An open replacement with a holdable Alpaca-generated `client_order_id` whose `replaces` link pointed to an order with an unholdable `client_order_id` raised `ValueError` when `_root_id` walked onto the original order, instead of stopping before the unholdable ancestor and reporting the replacement under its own holdable ID.
     - On the trade updates stream, `_node_order_id` likewise raised `ValueError` (logged as `[ERROR]`) instead of returning `None` so `_apply` drops updates for unknown external orders cleanly.

## What Changed, and Why That Way

* **`packages/canon-alpaca/src/canon_alpaca/execution.py`:**
  1. **Include `PENDING_CANCEL` in `_cancel_all_orders`:** Removed `or order.status == OrderStatus.PENDING_CANCEL` from `_cancel_all_orders`, re-checking only `order is None or order.is_closed` at each order's turn. If an earlier cancel never reached Alpaca, cancel-all now cancels the working order; if the earlier cancel *did* reach Alpaca and Alpaca answers the repeat with HTTP 422 (`NOT_CANCELABLE`), the existing `_cancel_refused` lookup (`get_order_by_id`) sees `AlpacaOrderStatus.CANCELED` or `AlpacaOrderStatus.PENDING_CANCEL` and emits no `OrderCancelRejected`.
  2. **Convert venue client order IDs via `holdable_client_order_id`:** Added `holdable_client_order_id(value: str | None) -> ClientOrderId | None`, which delegates validation directly to `ClientOrderId(value)` (rather than inventing a separate ASCII/emptiness rule) and returns `None` on `ValueError` (or non-`str`).
  3. **Bound `_root_id` and `_node_order_id` to holdable IDs:**
     - `_node_order_id` uses `holdable_client_order_id(alpaca_order.client_order_id)` and handles `root is None`, returning `None` for external orders with unholdable IDs so trade updates drop cleanly.
     - `_root_id` now returns `ClientOrderId | None`. It initializes `root = holdable_client_order_id(alpaca_order.client_order_id)` and, at each `replaces` hop, checks `prior_id = holdable_client_order_id(prior.client_order_id)`, breaking out of the walk when `prior_id is None`. Thus a holdable replacement of an unholdable original keeps its own earliest holdable `ClientOrderId`, while an order whose own ID and chain have no holdable ID returns `None` to `_order_report`, where `order_status_report` skips it (with `_order_report`'s warning) when closed or raises `Alpaca order <id> (<symbol>) is open with ...` when open.
     - Guarded writes to `self._replacements` (`if root is not None and alpaca_order.client_order_id:`, and similarly in `_modify_order` and `_adopt`) so `self._replacements` remains `dict[str, ClientOrderId]` and never aliases unrelated orders under an empty `""` key.
  4. **Updated comments and warning text** in `execution.py` where they previously stated that cancel-all skipped `PENDING_CANCEL` or listed the unmapped report fields without `client_order_id`.

* **Documentation and records updated to stay true:**
  - `packages/canon-alpaca/README.md`: Updated the **Modify**, **Cancel-all**, **Reports**, and **Tests** sections to reflect `PENDING_CANCEL` inclusion in cancel-all, `replaces` trace stopping before unholdable client order IDs, start-up failure / closed-order skip on unholdable client order IDs, and the added regression test module.
  - `docs/adr/0036-alpaca-adapter-order-management.md`: Corrected the cancel-all outcome line that previously stated `PENDING_CANCEL` orders were skipped.
  - `docs/adr/0042-alpaca-adapter-modify.md`: Clarified in the decision outcome that `replaces` tracing stops before an order whose `client_order_id` `ClientOrderId` refuses so a holdable replacement is reported under its own ID.

* **Additional offline tests (`packages/canon-alpaca/tests/test_execution_defects.py`):**
  - Added 10 offline tests (without modifying any existing test file) covering `holdable_client_order_id`, trade updates on the stream for orders with empty or non-ASCII `client_order_id`, multi-step replacement chains (`unholdable -> holdable-1 -> holdable-2`) reconciling under `holdable-1`, single-order `_generate_order_status_report` on closed vs. open unholdable orders, and `_cancel_all_orders` absorbing HTTP 422 via `_cancel_refused` when an earlier `PENDING_CANCEL` had already reached Alpaca.

## Upstream APIs and Contracts Followed

* **NautilusTrader (`v2.0.0rc8`, pinned at `d53849b216e2`):**
  - `nautilus_trader.live.clients.ExecutionClient` (`_cancel_all_orders`, `_generate_order_status_report`, `_generate_order_status_reports`; Python adapter guide, `developer_guide/python_adapters.md`).
  - Execution policies (`concepts/execution/policies.md`, "Command outcomes" and "Cancel, cancel-all, or batch cancel"): unknown cancel outcomes stay `PENDING_CANCEL`; cancel-all selects open and in-flight orders from `ClientCache.orders_open` and `ClientCache.orders_inflight` without skipping `PENDING_CANCEL`, matching upstream's Kraken spot, Binance spot, Bybit, and Interactive Brokers adapters (`ADR 0036`).
  - Reconciliation (`concepts/execution/reconciliation.md`): mass status skips closed orders outside the adapter's slice with a warning and fails start-up rather than leaving an open venue order unrepresented.
  - `nautilus_trader.model.ClientOrderId`: authoritative constructor validation for holdable client order IDs (non-empty, non-whitespace ASCII).
* **Alpaca Trading API & `alpaca-py` (`0.45.0`):**
  - `alpaca.trading.models.Order` (`client_order_id: str`, `replaces`, `replaced_by`, `status`).
  - "Delete Order by ID" (`DELETE /v2/orders/{order_id}`: 204 or 422 when no longer cancelable, paired with `get_order_by_id` to absorb repeats when already `canceled` or `pending_cancel`).
  - "Replace Order by ID" (`PATCH /v2/orders/{order_id}`, `ADR 0042`).

## Verification Run and Results

All offline checks and tests were run in the workspace via `mise exec -- uv run --locked`:

1. **Four reproducing tests (`pytest`):**
   - `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py::test_cancel_all_cancels_again_an_order_whose_cancel_had_no_answer packages/canon-alpaca/tests/test_execution_client.py::test_closed_order_with_a_client_id_nautilus_cannot_hold_is_skipped packages/canon-alpaca/tests/test_execution_client.py::test_open_order_with_a_client_id_nautilus_cannot_hold_fails_startup_by_name packages/canon-alpaca/tests/test_execution_client.py::test_replacement_of_an_order_with_an_unholdable_id_is_reported_under_its_own`
   - Result before change: **5 failed** (parametrized `""` and `"\u8ba2\u5355-1"` count as 2 items).
   - Result after change: **5 passed**.
2. **Full workspace test suite (`pytest`):**
   - `mise exec -- uv run --locked pytest` (before adding `test_execution_defects.py`): **497 passed, 24 skipped** (paper tests skipped without credentials).
   - `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_defects.py`: **10 passed**.
3. **Code and canon checks:**
   - `mise exec -- uv run --locked ruff check .`: **All checks passed!**
   - `mise exec -- uv run --locked ruff format --check .`: **112 files already formatted.**
   - `mise exec -- uv run --locked ty check`: **All checks passed!**
   - `./scripts/canon lint`: **canon: lint clean.**

## Uncertainties and What Could Not Be Verified

* **Live/paper Alpaca behavior (`UNVERIFIED` in this session):** Per task constraints, no network calls or paper API tests (`mise run paper:test`) were run. Specifically, whether Alpaca's live/paper API itself ever returns an empty `client_order_id` (versus non-ASCII user-supplied `client_order_id`s) and what HTTP error detail Alpaca returns on a second `DELETE /v2/orders/{order_id}` while an order is `pending_cancel` were not checked against the live paper endpoint in this session (`UNVERIFIED`).
* **Full `scripts/ci`:** Not run as a whole because this task checkout omits repository metadata/files `scripts/ci` expects; the graded checks (`pytest`, `ruff check .`, `ruff format --check .`, `ty check`) and `scripts/canon lint` all ran and passed.
