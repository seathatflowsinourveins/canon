---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# The Alpaca adapter's order management: each hook built as upstream's own adapters build it

## Context and Problem Statement

`AlpacaExecutionClient` (ADR 0030) submits and cancels market and limit orders on paper. It does not
cancel all, take stop orders, modify, or refresh its account after fills. NautilusTrader's adapter guide
names the hooks, but not what each must do when Alpaca's endpoint differs from the command. What does
each do here, and from which source? A section is added as each part is built; until then ADR 0030 and
the package README describe that part.

## Decision Drivers

* ADR 0001, rule 3: each behaviour traces to the guide, to an upstream adapter at the pinned tag, or to
  an Alpaca or alpaca-py page. Upstream's adapters are read for shape, not copied.
* Paper only (ADR 0009); nothing here weakens the client's paper-only check.
* What a page does not state is not guessed into code; it is `UNVERIFIED` and given a paper test.

## Considered Options

Cancel-all: (1) Alpaca's "Delete All Orders", `DELETE /v2/orders`; (2) the node's open and in-flight
orders for the command's instrument and side, each canceled by ID; (3) Alpaca's open orders for the
symbol, listed with "Get All Orders" and each canceled by ID. Stop orders: send Alpaca's stop and stop-limit
for what Alpaca takes, and deny the rest before Alpaca.

## Decision Outcome

### Cancel-all: option 2

Alpaca's endpoint "Attempts to cancel all open orders" of the account; `CancelAllOrders` names one
instrument and maybe a side, so option 1 cancels what it does not name. At `v2.0.0rc8`, Kraken spot, whose
venue cancel-all is account-wide too, "select[s] the matching open orders and cancel[s] them by explicit
venue id", in-flight ones included ("the venue can have accepted an order the cache still records as
`Submitted`"). Binance spot and Bybit select from the cache when the command names a side, their cancel-all
having "no side parameter", and otherwise call their venue's cancel-all for that one symbol; Interactive
Brokers selects from the cache. Coinbase alone lists the venue's orders (option 3). Betfair, and OKX with an
opt-in, send wider venue cancels, neither account-wide. So `_cancel_all_orders` takes the cache's open and
in-flight orders for the instrument and side, every strategy's (a strategy sends the command only with
`strategy_only=False`), skips those already `PENDING_CANCEL` (Coinbase does too: a second cancel risks a
rejection), and cancels each through `_cancel_order`'s path, one at a time, each re-read from the cache at
its turn. A 422 gets `OrderCancelRejected`, the pinned policies' event for "Cancel, cancel-all, or batch
cancel", unless Alpaca shows the order canceled or pending cancel (alpaca-py's own retry, ADR 0030). An
unexpected error, such as an unparsable answer, is logged with no event, the policies' "unknown live
outcome" for "parse failures after transmission"; either way the rest go on, this repo's choice, in a batch
cancel too. `_batch_cancel_orders` keeps the guide's default, "ordered calls of the corresponding
single-order hook": Alpaca has no cancel-by-list endpoint.

### Stop and stop-limit orders

Alpaca's whole-share table gives stop and stop-limit orders `GTC` and `DAY` "Yes" and IOC, FOK, OPG and CLS
"No"; its extended-hours table gives them "No" throughout; a stop is "elected on the consolidated print", a
trade at or through the stop price not outside the NBBO; a stop price follows the limit price's sub-penny
rule. So `_submit_order` sends alpaca-py's `StopOrderRequest` and `StopLimitOrderRequest`, `DAY` or `GTC`,
never `extended_hours`, and denies before Alpaca another time in force, a sub-penny stop or limit price, and
a trigger type other than `DEFAULT` ("the venue's default") or `LAST_PRICE` ("the last traded price"), as
Kraken spot maps those two to its last-trade trigger and refuses what its venue has no trigger for. Alpaca
documents no status or update for a stop's election, so no `OrderTriggered` is sent and an elected
stop-limit stays `ACCEPTED`. A report carries the stop price as trigger price and trigger type `DEFAULT`,
Alpaca's order having no trigger type field (Coinbase's adapter notes that a report without them makes the
reconciler clear the local prices). Trailing stops stay denied: their stop price moves with Alpaca's
high-water mark and no update reports it.

### Consequences

* Good: cancel-all cancels exactly what the command names; stops reach Alpaca only in forms its page lists.
* Bad: one HTTP call per cancel, one at a time; an order Alpaca holds that the node does not yet, or one
  placed after the cache was read, is not canceled. No `OrderTriggered`: an election shows only as a fill.

### Confirmation

Paper, 2026-11-10, `mise run paper:test` (all passed): upstream's Execution Testing Spec rows through its
`ExecTester` with the spec's node setup. TC-E41 ("Cancel all on stop"): a limit buy $50 below the bid and a
stop buy $50 above the ask, both canceled by `CancelAllOrders` on stop. TC-E20 and TC-E22 ("StopMarket BUY",
"StopLimit BUY"): accepted at the prices sent, as Alpaca holds them, and Alpaca's order reported back by the
client with the same type and prices. TC-E40: the existing cancel test. Not run: TC-E41's sell, which from a
flat account Alpaca refuses with 403 "cannot open a short sell while a long buy order is open" (observed);
TC-E21 and TC-E23, since a sell stop from flat is a short sale; TC-E44. Not applicable: TC-E24 to TC-E27 (no
such order type), TC-E42 and TC-E43 (no new hook). The card's `OrderPendingCancel` before `OrderCanceled`
does not come for a cancel-all: the pinned `Strategy` sends the command without one, and the binding has no
`generate_*` for it, as Binance spot emits `OrderCanceled` alone. Each test places nothing while the account
holds an open AAPL order or position, and leaves it as found. These follow upstream's practice: the spec's
rows through its tester, and offline stand-ins for the venue as alpaca-py's own tests mock its paper
endpoints. `UNVERIFIED`: an elected stop and its fill (the market must move $50), what Alpaca's stream sends
on election, and any sell stop or stop-limit, sent like a buy but shown by no paper run. The offline tests
are listed in the package README.

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read, not summarised. NautilusTrader files are at the pin,
`d53849b216e2`.

* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md, order command hooks and the
  batch default; the same at the pin but for two version lines (fetched, raw; diffed).
* `docs/concepts/execution/policies.md`: failure events, "parse failures after transmission"; `latest`
  adds `OrderCancelDenied` and `OrderModifyDenied`, absent from the pinned binding (fetched, raw; diffed).
* `crates/adapters/`: `kraken/src/execution/spot.rs`, `binance/src/spot/execution.rs`,
  `bybit/src/execution.rs`, `interactive_brokers/src/execution/core.rs`, `coinbase/src/execution.rs`,
  `betfair/src/execution.rs`, `okx/src/execution.rs`, `okx/src/websocket/client.rs`: cancel-all, batch
  cancel, Kraken's trigger mapping,
  Coinbase's `OrderContext` (fetched, raw).
* `crates/trading/src/strategy/mod.rs` (`cancel_all_orders`, `strategy_only`) and
  `crates/testkit/src/testers/exec/strategy.rs` (`on_stop`) (fetched, raw).
* `docs/concepts/orders/index.md` ("Trigger type"), `docs/concepts/orders/stop_market.md`, and
  https://nautilustrader.io/docs/md/latest/developer_guide/spec_exec_testing.md, groups 3 to 5 (fetched, raw).
* NautilusTrader `v2.0.0rc8` and alpaca-py 0.45.0 as installed: hook and `generate_*` signatures,
  `CancelAllOrders`, `ClientCache.orders_open` and `orders_inflight`, `StopOrderRequest`,
  `StopLimitOrderRequest` (read with `inspect`).
* https://docs.alpaca.markets/us/reference/deleteallorders-1.md (account-wide, 207 per order),
  `deleteorderbyorderid-1.md` (422 or 204), https://docs.alpaca.markets/us/llms.txt "Orders" (no
  cancel-by-list) and https://docs.alpaca.markets/us/docs/orders-at-alpaca.md: stop and stop-limit orders,
  their sub-penny rule, the whole-share and extended-hours tables, "Order Lifecycle", "Order Handling
  Standards" (fetched, raw; tables read with markup stripped).
* https://github.com/alpacahq/alpaca-py `v0.45.0`, `tests/trading/trading_client/test_order_routes.py`: the
  paper base URL mocked for orders and replaces (fetched, raw).
* Paper runs, logs kept outside the repo: TC-E41 as written, the sell refused 241 times with the 403 above;
  of the five runs that failed after it, two on the test's own code, one on connect timeouts to Alpaca (it
  answered again at once), two refused to start on open AAPL orders already on the account; then all
  passed.
* NautilusTrader `RELEASES.md`: v2.0.0rc9, tagged 2026-11-10, adds `OrderCancelDenied` and
  `OrderModifyDenied` (read by the second reader). The pin stays rc8 until it moves through `adopt-upstream`.
* Second reader, 2026-11-10 (`evidence-verifier`), twice: the cancel-all decision stands. Corrected above:
  "never wider than the command"; Binance spot and Bybit select from the cache only for a sided command; the
  retry argument against option 1; the 422 exception; who skips `OrderPendingCancel`; sources for the
  pending-cancel skip and for no cancel-by-list endpoint; option 3 added.
