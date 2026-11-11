# Execution Client Defect Fix Report

## What Was Wrong

Two defects were present in `packages/canon-alpaca/src/canon_alpaca/execution.py` (`AlpacaExecutionClient`):

1. **Cancel-all skipped `PENDING_CANCEL` orders.**
   `_cancel_all_orders` re-read each selected order from the cache and skipped it when `order.status == OrderStatus.PENDING_CANCEL`. NautilusTrader moves an order to `PENDING_CANCEL` locally as soon as a cancel command is sent. When an earlier single-order cancel got no answer (for instance, a read timeout or connection error under ADR 0039), the node's order remained `PENDING_CANCEL` while still working at Alpaca, and a subsequent `CancelAllOrders` skipped it instead of canceling it again.

2. **Orders with client order IDs NautilusTrader cannot hold aborted tracing and mass status.**
   Orders placed outside the node (such as from Alpaca's dashboard) can carry an empty (`""`), whitespace-only, or non-ASCII (e.g. `"\u8ba2\u5355-1"`) `client_order_id`, all of which `nautilus_trader.model.ClientOrderId` refuses with `ValueError`. Both `_order_report` (during reconciliation) and `_apply` (on stream trade updates) called `_root_id` / `_node_order_id` before `order_status_report`, and `_root_id` / `_node_order_id` called `ClientOrderId(...)` unconditionally on the order and on every `replaces` link:
   - A closed order with an unholdable `client_order_id` raised `ValueError` outside `order_status_report`'s `try ... except ValueError` block, aborting `_generate_order_status_reports` and startup mass-status reconciliation instead of skipping the closed order with a warning.
   - An open order with an unholdable `client_order_id` failed inside `_root_id` with a bare `ClientOrderId` `ValueError` rather than reaching `order_status_report`'s open-order error (`Alpaca order <id> (<symbol>) is open with ...`), failing without naming the order.
   - A valid order with a holdable `client_order_id` that replaced an order with an unholdable `client_order_id` walked `replaces` to the original and raised `ValueError` instead of reporting under its own holdable ID.
   - Stream updates for external orders with unholdable `client_order_id`s raised inside `_node_order_id` instead of being dropped as unknown orders, and empty `client_order_id`s were not guarded when indexing `self._replacements`.

## What Changed and Why

* **`packages/canon-alpaca/src/canon_alpaca/execution.py`**
  * **`_cancel_all_orders`**: Removed `or order.status == OrderStatus.PENDING_CANCEL`, skipping only `order is None or order.is_closed`. `PENDING_CANCEL` orders remain deduplicated across `self.cache.orders_open` and `self.cache.orders_inflight` via `selected: dict[ClientOrderId, None]`. If the earlier cancel had in fact reached Alpaca, Alpaca answers the repeat `DELETE /v2/orders/{order_id}` with HTTP 422 (`NOT_CANCELABLE`), and existing `_cancel_refused` looks up the order by ID, sees `CANCELED` or `PENDING_CANCEL`, and emits no `OrderCancelRejected`.
  * **`holdable_client_order_id`**: Added a small helper around `ClientOrderId(value)` returning `ClientOrderId | None` (`None` on `None` or `ValueError`), delegating the identifier validity rule directly to NautilusTrader's `ClientOrderId` rather than inventing separate string checks.
  * **`_root_id`, `_node_order_id`, and `_replacements` guards**: Changed `_root_id` to return `ClientOrderId | None`, starting from `holdable_client_order_id(alpaca_order.client_order_id)` and updating `root` along `replaces` only when a prior order's `client_order_id` is holdable (stopping early when the node's cache already holds `root` or `self._replacements` already maps it). Standalone unholdable orders yield `None`; valid replacements of an unholdable external order keep the earliest holdable `ClientOrderId` on their chain. Guarded `self._replacements` lookups and writes against empty `client_order_id` strings in `_root_id`, `_modify_order`, `_finish`, and `_adopt`, and guarded `_root_id`'s loop-exhaustion warning with `if replaces is not None:`.
  * **`order_status_report` and `_order_report`**: Updated `order_status_report` to evaluate `client_order_id if client_order_id is not None else ClientOrderId(order.client_order_id or "")` inside its existing `try ... except ValueError` block. Closed orders with unholdable client order IDs now return `None` and are skipped with a warning in `_order_report`; open orders raise `ValueError(f"Alpaca order {order.id} ({order.symbol}) is open with {e}")`, failing startup reconciliation with the order ID, symbol, and `ClientOrderId` reason logged. Updated module and method docstrings, inline comments, and `_order_report`'s warning text.
* **`packages/canon-alpaca/README.md`, `docs/adr/0036-alpaca-adapter-order-management.md`, `docs/adr/0042-alpaca-adapter-modify.md`**
  * Corrected statements made untrue by the change: cancel-all includes `PENDING_CANCEL` orders rather than skipping them (ADR 0036, `README.md`), and `replaces` tracing / order status reporting keeps the earliest holdable `ClientOrderId`, skips closed unholdable-ID orders with a warning, and fails startup on open ones (ADR 0042, `README.md`). Added dated defect-fix notes in the `## Evidence` sections of ADR 0036 and ADR 0042.
* **`packages/canon-alpaca/tests/test_execution_unholdable_and_cancel_all.py`**
  * Added 9 supplemental offline tests covering `holdable_client_order_id`, multiple closed empty/whitespace/non-ASCII orders alongside an open valid order, open whitespace/non-ASCII orders failing startup by name, multi-hop replacements from an unholdable original across bulk (`open_only=True`) and single-order status reports, trade stream updates for external unholdable-ID orders, and cancel-all on a `PENDING_CANCEL` order already `pending_cancel` at Alpaca emitting no rejection. No existing test file was modified.

## Upstream APIs and Contracts Followed

* **NautilusTrader `v2.0.0rc8` (`d53849b216e2`)**:
  * `nautilus_trader.live.clients.ExecutionClient` (`_cancel_all_orders`, `_generate_order_status_report`, `_generate_order_status_reports`), Python adapter guide (`developer_guide/python_adapters.md`), execution policies (`concepts/execution/policies.md`, "Command outcomes"), and reconciliation (`concepts/execution/reconciliation.md`).
  * Upstream adapters at `d53849b216e2`: `crates/adapters/kraken/src/execution/spot.rs`, `crates/adapters/binance/src/spot/execution.rs`, `crates/adapters/bybit/src/execution.rs`, and `crates/adapters/interactive_brokers/src/execution/core.rs`, which select open/in-flight orders from the cache in cancel-all without skipping `PENDING_CANCEL`.
  * `nautilus_trader.model.ClientOrderId`, whose constructor defines what client order IDs NautilusTrader can hold (non-empty, non-whitespace ASCII) and raises `ValueError("invalid string for 'value'...")` otherwise.
* **Alpaca Trading API & `alpaca-py` `0.45.0`**:
  * `TradingClient.cancel_order_by_id` / "Delete Order by ID" (`DELETE /v2/orders/{order_id}`, 204 or 422) and `TradingClient.replace_order_by_id` / `TradingClient.get_order_by_id` / `TradingClient.get_orders` ("Replace Order by ID", `replaces` and `replaced_by` links, ADR 0036 and ADR 0042).

## Verification Run and Results

All offline checks were executed from the repository root without credentials or network access:

1. Reproducing tests (failed before the change, passed after):
   `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -k "test_cancel_all_cancels_again_an_order_whose_cancel_had_no_answer or test_closed_order_with_a_client_id_nautilus_cannot_hold_is_skipped or test_open_order_with_a_client_id_nautilus_cannot_hold_fails_startup_by_name or test_replacement_of_an_order_with_an_unholdable_id_is_reported_under_its_own"` -> **5 passed**.
2. Full workspace test suite:
   `mise exec -- uv run --locked pytest` -> **506 passed, 24 skipped in 119.25s** (the 24 skipped are the paper-only tests gated on `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`).
3. Ruff lint:
   `mise exec -- uv run --locked ruff check .` -> **All checks passed!** (exit 0).
4. Ruff format check:
   `mise exec -- uv run --locked ruff format --check .` -> **112 files already formatted** (exit 0).
5. Type check:
   `mise exec -- uv run --locked ty check` -> **All checks passed!** (exit 0).
6. Canon ADR and registry lint:
   `mise exec -- ./scripts/canon lint` -> **`canon: lint clean`** (exit 0).

## Uncertainties (`UNVERIFIED`)

* **Paper environment execution (`UNVERIFIED`)**: Per task instructions, no paper tests (`mise run paper:test`) or live Alpaca API calls were run in this session.
* **Venue formatting of external `client_order_id` values (`UNVERIFIED`)**: Which non-ASCII or whitespace `client_order_id` forms Alpaca's paper/live endpoints accept on external order creation or replacement was not queried online in this offline session; delegating directly to `nautilus_trader.model.ClientOrderId` ensures exact agreement with every string NautilusTrader itself accepts or rejects.
