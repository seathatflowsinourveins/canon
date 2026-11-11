# Report: `canon-alpaca` Execution Client Defects

## What Was Wrong

Two defects were present in `packages/canon-alpaca/src/canon_alpaca/execution.py`:

1. **`_cancel_all_orders` skipped orders in `OrderStatus.PENDING_CANCEL`.**
   When a strategy cancels an order, NautilusTrader moves the local order into `OrderStatus.PENDING_CANCEL` before `_cancel_order` receives an HTTP response. If the cancel request gets no definitive answer (for example, a read timeout under ADR 0039 or a non-422 transport/server error), `_cancel` logs the error and leaves the order in `OrderStatus.PENDING_CANCEL` locally while it can still be working at Alpaca. Later, `_cancel_all_orders` selected open and in-flight orders from `self.cache.orders_open` and `self.cache.orders_inflight`, re-read each order, and explicitly skipped `order.status == OrderStatus.PENDING_CANCEL` (`execution.py:1093`), never sending another cancel for it.

2. **Tracing `client_order_id`s that NautilusTrader's `ClientOrderId` refuses raised `ValueError` outside report error handling.**
   Alpaca's `Order.client_order_id` (`alpaca-py` 0.45.0) is an unconstrained `str`, so orders created outside the node (such as from Alpaca's dashboard or another API client) can carry an empty (`""`), whitespace-only (`"   "`), or non-ASCII (`"\u8ba2\u5355-1"`) `client_order_id`. Pinned `nautilus_trader.model.ClientOrderId` (`v2.0.0rc8`) rejects empty, all-whitespace, and non-ASCII strings with `ValueError`. While `order_status_report` (`execution.py:668-683`) already caught `ValueError` to skip closed unmappable orders with `None` and fail open ones with `ValueError(f"Alpaca order {order.id} ({order.symbol}) is open with {e}")`, `_order_report` called `_root_id` first (`execution.py:2072`). Both `_root_id` (`execution.py:1484, 1505`) and `_node_order_id` (`execution.py:1464`) constructed `ClientOrderId(...)` unconditionally:
   - A closed external order with an empty or non-ASCII `client_order_id` raised `ValueError` inside `_root_id`, aborting `_generate_order_status_reports` / `_generate_mass_status` and start-up reconciliation instead of being skipped with a warning.
   - An open external order with an unholdable `client_order_id` raised the bare `ClientOrderId` `ValueError` inside `_root_id` instead of reaching `order_status_report`, where the error names the Alpaca order ID and symbol.
   - A valid open or closed replacement (`client_order_id="alpaca-made-1"`) whose `replaces` link pointed to an original order with an unholdable `client_order_id` raised `ValueError` at `ClientOrderId(prior.client_order_id)` inside `_root_id`, instead of keeping its own holdable `ClientOrderId`.
   - On the live trade updates stream, `_node_order_id` raised `ValueError` on updates for external orders with unholdable `client_order_id`s instead of returning `None` so `_apply` drops them cleanly at `INFO`.

## What Changed and Why

* **`packages/canon-alpaca/src/canon_alpaca/execution.py`**:
  * **Cancel-all (`_cancel_all_orders`, `_cancel_refused`)**: Removed `or order.status == OrderStatus.PENDING_CANCEL` from the per-order skip guard in `_cancel_all_orders` so only `None` or `order.is_closed` orders are skipped when their turn arrives. If the earlier cancel had in fact reached Alpaca, `_cancel_refused` already inspects `get_order_by_id` on HTTP 422 and emits nothing when Alpaca holds the order in `canceled` or `pending_cancel` (or cancels `replaced_by` if replaced), avoiding a spurious `OrderCancelRejected`.
  * **Holdable `ClientOrderId` check (`holdable_client_id`, `_node_order_id`, `_root_id`, `_modify_order`, `_adopt`, `_order_report`)**:
    * Added `holdable_client_id(value: str) -> ClientOrderId | None`, which returns `ClientOrderId(value)` or `None` on `ValueError`, delegating the exact identifier validity rules to NautilusTrader's `ClientOrderId` constructor rather than duplicating character checks.
    * Updated `_root_id` to return `ClientOrderId | None`. It initializes `root = holdable_client_id(alpaca_order.client_order_id)`, skips any ancestor in the `replaces` chain whose `client_order_id` is unholdable (`prior_id is None`), and caches in `self._replacements` only when both `own` and `root` are holdable (preserving `dict[str, ClientOrderId]` typing and preventing empty-string key collisions across unrelated external orders). Consequently:
      * A standalone order with an unholdable `client_order_id` yields `root = None` and reaches `order_status_report`, which returns `None` when closed (logged as a warning by `_order_report`) and raises `ValueError(f"Alpaca order {order.id} ({order.symbol}) is open with {e}")` when open.
      * A holdable replacement of an order with an unholdable `client_order_id` keeps its earliest holdable `ClientOrderId` in the `replaces` chain (its own ID for a direct replacement) and is reconciled under it.
    * Updated `_node_order_id` to use `holdable_client_id` and handle `root is None`, returning `None` for external unholdable orders on the trade updates stream.
    * Guarded `self._replacements[...]` writes in `_modify_order` and `_adopt` with `holdable_client_id(...) is not None` and updated module/method docstrings and `_order_report`'s warning text to match.
* **`packages/canon-alpaca/README.md`, `docs/adr/0036-alpaca-adapter-order-management.md`, `docs/adr/0042-alpaca-adapter-modify.md`**:
  * Corrected statements that said cancel-all skips `PENDING_CANCEL` orders and documented how unholdable `client_order_id`s behave in reports and `replaces` tracing.
* **`packages/canon-alpaca/tests/test_execution_regressions.py`**:
  * Added offline regression tests covering whitespace-only `client_order_id`s (closed skipped with warning, open failing start-up by name), multi-step `replaces` chains with unholdable originals or intermediate links (including `_generate_order_status_report`), live stream trade updates on empty and non-ASCII `client_order_id`s, and `_cancel_all_orders` on a `PENDING_CANCEL` order already held `pending_cancel` at Alpaca. Existing test files were left untouched.

## Upstream API and Contracts Followed

* **NautilusTrader `v2.0.0rc8` (`d53849b216e2`)**:
  * `nautilus_trader.live.clients.ExecutionClient` (`_cancel_all_orders`, `_generate_order_status_report`, `_generate_order_status_reports`, `_generate_mass_status`) and execution policies ("Command outcomes", `docs/concepts/execution/policies.md`).
  * Cancel-all cache selection including `PENDING_CANCEL` open/in-flight orders matches upstream's Kraken spot (`crates/adapters/kraken/src/execution/spot.rs`), Binance spot (`crates/adapters/binance/src/spot/execution.rs`), Bybit (`crates/adapters/bybit/src/execution.rs`), and Interactive Brokers (`crates/adapters/interactive_brokers/src/execution/core.rs`) adapters cited in ADR 0036.
  * `nautilus_trader.model.ClientOrderId`: native identifier validation raising `ValueError` on empty, all-whitespace, or non-ASCII values; start-up reconciliation (`docs/concepts/execution/reconciliation.md`) failing mass status when an open venue order cannot be represented.
* **Alpaca Trading API / `alpaca-py` `0.45.0`**:
  * `alpaca.trading.models.Order` (`id`, `symbol`, `status`, `client_order_id: str`, `replaces`, `replaced_by`), "Delete Order by ID" (`DELETE /v2/orders/{order_id}`, HTTP 204 / 422), "Get All Orders" (`GET /v2/orders`), and "Replace Order by ID" (`PATCH /v2/orders/{order_id}`, ADR 0042).

## Verification Run and Results

All offline checks and tests were run in `<tree>`:

1. **Targeted reproduction tests**:
   `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -k "test_cancel_all_cancels_again_an_order_whose_cancel_had_no_answer or test_closed_order_with_a_client_id_nautilus_cannot_hold_is_skipped or test_open_order_with_a_client_id_nautilus_cannot_hold_fails_startup_by_name or test_replacement_of_an_order_with_an_unholdable_id_is_reported_under_its_own" -v`
   -> **Result**: `5 passed, 135 deselected in 1.60s` (exit 0).
2. **Added regression tests**:
   `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_regressions.py -v`
   -> **Result**: `7 passed in 1.42s` (exit 0).
3. **Full offline workspace test suite**:
   `mise exec -- uv run --locked pytest`
   -> **Result**: `504 passed, 24 skipped in 83.82s` (exit 0; the 24 skipped tests are paper-only tests requiring live paper keys).
4. **Ruff lint**:
   `mise exec -- uv run --locked ruff check .`
   -> **Result**: `All checks passed!` (exit 0).
5. **Ruff format check**:
   `mise exec -- uv run --locked ruff format --check .`
   -> **Result**: `112 files already formatted` (exit 0).
6. **Type check (`ty`)**:
   `mise exec -- uv run --locked ty check`
   -> **Result**: `All checks passed!` (exit 0).
7. **Canon ADR / registry / lock lint**:
   `./scripts/canon lint`
   -> **Result**: `canon: lint clean` (exit 0).

## Uncertainties and `UNVERIFIED` Items

* **Live paper behavior on non-ASCII / empty `client_order_id`s (`UNVERIFIED` on paper)**: No paper API calls were run in this session (per task rules). Which exact `client_order_id` forms Alpaca's dashboard or API produces or accepts on live paper endpoints, and whether Alpaca allows multiple external orders with `client_order_id == ""` simultaneously, was not tested against paper here (`UNVERIFIED`); avoiding `_replacements` cache entries when `holdable_client_id` returns `None` prevents collisions regardless.
* **Paper test suite**: Paper tests (`mise run paper:test`) were not run, as required by the task instructions.
