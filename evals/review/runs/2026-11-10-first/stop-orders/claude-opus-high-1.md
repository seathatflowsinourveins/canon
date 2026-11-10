# Review of 724246e: stop and stop-limit orders for the Alpaca adapter

Scope: `git diff 724246e^ 724246e`, mainly `packages/canon-alpaca/`, with the README and ADR 0036 text it
changes where that text describes the adapter. The offline suite at HEAD passes:
`mise exec -- uv run --locked pytest packages/canon-alpaca -q` gave 177 passed and 6 skipped (the paper
tests). The probes below ran on a copy of `packages/canon-alpaca/tests` in a scratch directory. The
checkout was not edited.

## Defects, most severe first

### 1. A closed order with a client order ID NautilusTrader refuses now aborts start-up reconciliation

* **Where:** `packages/canon-alpaca/src/canon_alpaca/execution.py`, `AlpacaExecutionClient._order_report`
  (about line 1160): `held = self.cache.order(ClientOrderId(order.client_order_id))`.
* **What goes wrong:** this commit builds `ClientOrderId(order.client_order_id)` before
  `order_status_report` is called, so it sits outside that function's `try/except ValueError`. Inside the
  function, a closed order this slice cannot map is skipped (`return None`, logged as "not reported").
  `ClientOrderId` raises `ValueError` for an empty, all-whitespace or non-ASCII value. Before this commit
  that error was raised inside the `try`, so a closed order like that was skipped. It now escapes
  `_order_report` and fails `_generate_order_status_reports`, and with it the whole mass status. The node
  then aborts start-up. This happens for any order Alpaca lists in the window, whatever its type, not
  only stops. An open order with such an ID already failed start-up before the commit, by design, so only
  closed orders regress.
* **How it was shown:** a probe test lists the docs' open limit order next to a `canceled` order with
  `client_order_id="café-1"` and calls `client._generate_order_status_reports` through
  `run_with_client`.
  * At HEAD the node logs `Alpaca order status reports failed: invalid string for 'value' contained a
    non-ASCII char, was 'café-1'`, then `Startup reconciliation failed, aborting startup`, and the run
    raises `RuntimeError: Failed to get mass status from ALPACA`.
  * With the parent commit's `src` (`git archive 724246e^`) on `PYTHONPATH`, the same probe passes and
    returns the open limit order's report alone.
  * `order_status_report(Order(...café-1, canceled...), 0)` returns `None` at HEAD, which confirms the
    skip still works when the call goes through the function.
* **Uncertain:** I did not check whether Alpaca accepts or returns a non-ASCII or blank
  `client_order_id` (no network); that is UNVERIFIED. The code path is a regression either way. The error
  also loses the "Alpaca order … is open with …" context that the function adds.

### 2. The paper test's trigger-type check cannot fail, and it does not check what its docstring says

* **Where:** `packages/canon-alpaca/tests/test_paper_execution.py`,
  `test_paper_stop_buy_is_accepted_with_its_prices`: `report = order_status_report(held, 0)` and
  `assert (report.order_type, report.trigger_type) == (order_type, TriggerType.DEFAULT)`.
* **What goes wrong:** with no `trigger_type` argument, `order_status_report` sets
  `trigger_type=(None or TriggerType.DEFAULT)` for every stop type. So the `TriggerType.DEFAULT` half of
  the assertion holds whatever Alpaca returns or the client does. The docstring says the check is on "the
  client's report of Alpaca's order (what reconciliation reads)". The client's report comes from
  `AlpacaExecutionClient._order_report`, which takes the trigger type from the node's cached order (see 3),
  and the test never calls it. ADR 0036's Confirmation repeats the claim ("Alpaca's order reported back by
  the client with the same type and prices"). The order type and price comparisons in the test are real;
  the trigger-type part and the "client's report" wording are not.
* **How it was shown:** by reading `order_status_report` (lines 444–503). A held `LAST_PRICE` stop going
  through the client's path gives `LAST_PRICE` (probe in 3), which this assertion would never see.

### 3. "An open stop is reported with … trigger type `DEFAULT`" is false for an order the node holds, and that path has no test

* **Where:**
  * `execution.py` module docstring, last "Known limits" bullet: "An open stop is reported with its stop
    price as the trigger price and trigger type `DEFAULT`."
  * `packages/canon-alpaca/README.md`, "Order types" bullet (same sentence).
  * ADR 0036, "Stop and stop-limit orders": "A report carries the stop price as trigger price and trigger
    type `DEFAULT`".
* **What goes wrong:** `_order_report` passes the cached order's `trigger_type`, so a stop the node holds
  is reported with its own trigger type (`LAST_PRICE`, for example). The function docstring of
  `order_status_report` describes this correctly; the three statements above do not. The only offline
  reconciliation test for stops (`test_startup_reconciliation_reports_open_stop_orders`) runs with an
  empty cache. So the patch branch is never exercised: no checked-in test reaches `held.trigger_type`, and
  that test does not assert the stop-limit's trigger type either.
* **How it was shown:** a probe test in which a `Trader` submits a `LAST_PRICE` stop market. The fake
  venue uses the `accept` script, so the order is accepted and not filled. The probe then lists the
  placed order and calls `client._generate_order_status_reports`. It got
  `[(OrderType.STOP_MARKET, TriggerType.LAST_PRICE, Price(200.0000))]`.

### 4. (Unsure) Alpaca may convert buy stops to stop-limits, and the known limits do not say so

* **Where:** the `execution.py` "Known limits" stop bullet, the package README "Order types" bullet, and
  ADR 0036 "Stop and stop-limit orders". These describe a buy stop as sent and elected as Alpaca's stop,
  with "an election shows only as a fill".
* **What may go wrong:** from recollection, not checked this session (UNVERIFIED): the "Orders at
  Alpaca" page says Alpaca converts buy stop orders into stop-limit orders with a limit about 4% (stop
  under $50) or 2.5% (stop at or above $50) above the stop price. If that is so:
  * a `STOP_MARKET` buy can go unfilled after election when the price gaps past that limit, which the
    docs here never state;
  * Alpaca's order may then carry a `limit_price`, or a `stop_limit` type, that reconciliation would
    compare against the node's `STOP_MARKET`.

  ADR 0036 records a passing paper TC-E20 that asserted `order_type == STOP_MARKET` on Alpaca's order, so
  the type field at least stayed `stop` in that run; that run is also UNVERIFIED by me.
* **How to check:** read "Stop Orders" on https://docs.alpaca.markets/us/docs/orders-at-alpaca.md. I did
  not fetch it, because the brief allows no network.

## Not verified (no network, Rust sources not in the installed wheel)

* The quotes from Alpaca's "Orders at Alpaca" page: the "No" entries for IOC, FOK, OPG and CLS on stops,
  the extended-hours "No", "Sub-penny increments for stop orders" and "elected on the consolidated print".
* Kraken spot's trigger mapping and Coinbase's report patching in NautilusTrader's `crates/adapters/`.
* That `ExecTester` places a stop buy relative to the ask and a stop-limit's limit above its trigger.
* The paper runs ADR 0036 reports as passing.

`ExecTesterConfig()` defaults were read from the installed package: `stop_trigger_type=DEFAULT`,
`use_post_only=False` and `modify_stop_orders_to_maintain_offset=False`. They fit the paper tests'
assumptions.
