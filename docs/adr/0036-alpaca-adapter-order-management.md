---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# The Alpaca adapter's order management: each hook built as upstream's own adapters build it

## Context and Problem Statement

`AlpacaExecutionClient` (ADR 0030) submitted and canceled market and limit orders on paper, and did not
cancel all, take stops, modify, or answer an account query. The adapter guide names the hooks, not what each
must do where Alpaca's endpoint differs from the command. What does each do here, and from which source?

## Decision Drivers

* ADR 0001, rule 3: each behaviour traces to the guide, an upstream adapter at the pin, or an Alpaca or
  alpaca-py page; upstream is read for shape, not copied. Paper only (ADR 0009). What no page states is not
  guessed into code: it is `UNVERIFIED` and gets a paper test.

## Considered Options

Cancel-all: (1) Alpaca's "Delete All Orders"; (2) the node's open and in-flight orders for the command's
instrument and side, each canceled by ID; (3) Alpaca's open orders for the symbol, from "Get All Orders",
each canceled by ID. Stop orders: send what Alpaca's order page lists, deny the rest before Alpaca. Modify:
(a) `OrderUpdated` from the PATCH answer, as Binance spot's HTTP fallback does; (b) `OrderUpdated` on
Alpaca's `replaced`, with the replacement's values, as Binance spot's WebSocket path waits for the venue's
stream; (c) not implemented, with a paper test to settle what the pages leave open.

## Decision Outcome

### Cancel-all: option 2

Alpaca's endpoint "Attempts to cancel all open orders" of the account; `CancelAllOrders` names one
instrument and maybe a side. Kraken spot, whose venue cancel-all is account-wide too, "select[s] the
matching open orders and cancel[s] them by explicit venue id", in-flight ones included; Binance spot and
Bybit select open orders from the cache for a sided command and call their venue's per-symbol cancel-all
otherwise; Interactive Brokers always selects from the cache; Coinbase lists the venue's orders; Betfair,
and OKX by opt-in, send wider cancels, neither account-wide. So the client cancels by ID, one at a time, the
cache's open and in-flight orders for the instrument and side, every strategy's (the command comes only from
`strategy_only=False`), each re-read at its turn and skipped if closed or `PENDING_CANCEL` (as Coinbase,
since a second cancel risks a rejection). A 422 is `OrderCancelRejected` ("Cancel, cancel-all, or batch
cancel") unless Alpaca shows the order canceled or pending cancel (alpaca-py's own retry, ADR 0030), or
replaced, when the replacement is canceled; an unexpected error is logged with no event ("parse failures
after transmission" leave the outcome unknown). That the rest go on, in a batch cancel too, is this repo's
choice; `_batch_cancel_orders` keeps the guide's default: Alpaca has no cancel-by-list endpoint.

### Stop and stop-limit orders

Alpaca's whole-share table gives stop and stop-limit orders `GTC` and `DAY` "Yes" and the rest "No", its
extended-hours table "No" throughout; a stop is "elected on the consolidated print", a trade at or through
the stop price not outside the NBBO; a stop price follows the sub-penny rule. So the client sends
`StopOrderRequest` and `StopLimitOrderRequest`, `DAY` or `GTC`, never `extended_hours`, and denies before
Alpaca another time in force, a sub-penny stop or limit price, and a trigger type other than `DEFAULT` ("the
venue's default") or `LAST_PRICE` ("the last traded price"), as Kraken spot maps those two to its last-trade
trigger. Alpaca documents no status or update for a stop's election, so no `OrderTriggered` is sent. Reports
carry the stop price as trigger price, and the node's trigger type, else `DEFAULT` (Coinbase's adapter notes
a report without them makes the reconciler clear the local prices). Trailing stops stay denied: their stop
price moves with Alpaca's high-water mark and no update reports it.

### Modify: option (b)

Alpaca's "Replace Order by ID" answers "The new Order object with the new order ID", says a success "does
NOT guarantee the existing open order has been replaced", and gives the replacement's client order ID as "A
unique identifier for the new order. Automatically generated if not sent." It leaves unstated which order
`replaced` carries, whether the original ID may be kept, and any error status. A paper observation on
2026-11-10, 3 of 3 runs, settled those: a kept ID is refused, 422 "client_order_id must be unique"; the
answer has a new ID, an Alpaca-made client order ID and `replaces`; `replaced` carries the old order with
`replaced_by`; a second replace of the old order is refused, 422 "order already replaced"; no replacement
sent `new`. On that, the master orchestrator chose (b) under the owner's delegation of 2026-11-10.
`_modify_order` sends no client order ID; `OrderUpdated` comes only with `replaced`, its values from the
replacement as the answer or a read of `replaced_by` gives it, nothing if neither does. Updates and reports
of a replacement are traced to the node's order through `replaces`, at most 16 links, from the orders
already fetched first, cached, misses too, and walked again after a restart. A 422 is `OrderModifyRejected`
unless Alpaca shows the order replaced as asked (alpaca-py's own retry); any other status leaves the outcome
unknown. `order_replace_rejected` and the `rejected` of a replacement that has not taken over are
`OrderModifyRejected` too. Denied before Alpaca: no venue order ID, a fractional quantity, a sub-penny
price, a price the order type lacks.

### Consequences

* Good: cancel-all cancels exactly what the command names; stops reach Alpaca only in forms its page lists;
  `OrderUpdated` waits for Alpaca's own confirmation.
* Bad: one HTTP call per cancel, one at a time; an order Alpaca holds that the node does not yet, or one
  placed after the cache was read, is not canceled. No `OrderTriggered`: an election shows only as a fill.

### Confirmation

Paper, 2026-11-10, `mise run paper:test`, all passing: upstream's Execution Testing Spec rows through its
`ExecTester` on the spec's node setup. TC-E41: a limit buy $50 below the bid and a stop buy $50 above the
ask, canceled by `CancelAllOrders` on stop; its sell is left out, since from a flat account Alpaca refuses
it, 403 "cannot open a short sell while a long buy order is open" (observed). The card's
`OrderPendingCancel` does not come: the pinned `Strategy` sends the command without one and the binding
cannot generate it, as Binance spot emits `OrderCanceled` alone. TC-E20 and TC-E22: stop and stop-limit buys
accepted at the prices sent, as Alpaca holds them, and reported back the same. TC-E30: the tester amends
"one tick more passive", 0.0001 here, so the client denies it as sub-penny, `OrderModifyRejected`: the row's
pass criterion, `OrderUpdated`, fails by design, as the card's "different valid price" is not valid at
Alpaca. TC-E40 is the existing cancel test. Modify through a node, 6 of 6 runs: $1.00 to $1.01,
`OrderUpdated` under the replacement's ID and price, the cancel reaching the replacement;
`test_paper_replace.py` asserts the four observed answers. Not run: sells (TC-E21, E23, E31, E33: a short
sale from flat), TC-E32, E35 and E42 to E44 (no new hook), TC-E34 (offline only); not applicable: TC-E24 to
E27 and E36. Each test places nothing while the account holds an AAPL order or position, and leaves it as
found. This is upstream's practice: the spec's rows through its tester, and offline stand-ins as alpaca-py's
own tests mock its paper endpoints; the offline tests are listed in the package README.

`UNVERIFIED`, shown by no run: an elected stop and its fill, and what the stream sends on election; any sell
stop; `order_replace_rejected`; `pending_replace`; a `new` for a replacement later than ~200 ms; a status on
a replace other than 422; a modify of a partially filled order (what `qty` means then); a fill on the old
order racing a replace, where the old order's fill applies as usual and the replacement's `rejected` becomes
`OrderModifyRejected`, not `OrderRejected`.

## Evidence

Read 2026-11-10; "raw": fetched with `curl` and read, not summarised. NautilusTrader files at the pin,
`d53849b216e2`; alpaca-py 0.45.0 and NautilusTrader `v2.0.0rc8` as installed, read with `inspect`.

* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md: the order hooks and the batch
  default, as at the pin but two version lines (fetched, raw; diffed).
  `docs/concepts/execution/policies.md`: the failure events, "parse failures after transmission"; `latest`
  adds `OrderCancelDenied` and `OrderModifyDenied` (fetched, raw; diffed). `docs/concepts/orders/index.md`
  and `stop_market.md`: the trigger types (fetched, raw).
  https://nautilustrader.io/docs/md/latest/developer_guide/spec_exec_testing.md: groups 3 to 5, the TC-E30
  and TC-E41 cards, `ExecTester`'s setup (fetched, raw).
* `crates/adapters/` at the pin: cancel-all and batch cancel in `kraken/src/execution/spot.rs`,
  `binance/src/spot/execution.rs`, `bybit/src/execution.rs`, `interactive_brokers/src/execution/core.rs`,
  `coinbase/src/execution.rs`, `betfair/src/execution.rs`, `okx/src/execution.rs` and
  `okx/src/websocket/client.rs`; Kraken's trigger mapping; Coinbase's `OrderContext`; Binance's
  `modify_order`, both paths (fetched, raw). `crates/trading/src/strategy/mod.rs`: `strategy_only`;
  `crates/testkit/src/testers/exec/strategy.rs`: `on_stop` (fetched, raw).
* https://docs.alpaca.markets/us/: `reference/deleteallorders-1.md` (account-wide, 207 per order),
  `reference/deleteorderbyorderid-1.md` (422 or 204), `reference/patchorderbyorderid-1.md` (the body, the
  answer, no error status), `docs/orders-at-alpaca.md` (stop rules and the time-in-force tables, read with
  markup stripped), `docs/websocket-streaming.md` (the events; none for an election), `llms.txt` "Orders"
  (no cancel-by-list) (fetched, raw).
* https://github.com/alpacahq/alpaca-py `v0.45.0`, `tests/trading/trading_client/test_order_routes.py`: the
  paper base URL mocked for orders and replaces (fetched, raw).
* Paper, 2026-11-10, logs kept outside the repo: the replace observation, 18:52 UTC, 3 runs through
  alpaca-py with `raw_data=True`; TC-E41 as written, the sell refused 241 times; five failed runs, two on the
  test's code, one on connect timeouts, two refusing to start on open orders on the account; then all passed.
* Second reader after landing, 2026-11-10: an independent review of the modify change found three
  defects, each reproduced through the stand-in venue and a real node, and fixed with a regression test
  each: a filled replacement whose `replaced` was missed went unreported, so its fill was lost; a replace
  refused on the HTTP answer and on the stream gave two `OrderModifyRejected`; a `replaced` delivered
  twice gave two `OrderUpdated` (whether Alpaca repeats it is UNVERIFIED). Two more independent reviews the
  same day added: a trace missed in the start-up window kept for the session; a fill on a replacement
  before its `replaced` applied to the old terms (an overfill dropped); a late cancel of the replaced order
  closing the replacement; `pending_replace` failing start-up; a replacement replaced again before its
  `replaced` was read; a refused second modify taken for a retry; answers kept for the session; a chain
  past the limit walked again each time. Each reproduced and is fixed with a test, following upstream's
  Binance spot `dispatch.rs` (`record_order_update`, `record_order_update_from_fill`,
  `defer_replace_cancel`). Not fixed, UNVERIFIED: whether a replacement's `filled_qty` counts the fills of
  the order it replaced. A rejected replacement after a fill now leaves the fill as the outcome, since
  NautilusTrader drops a modify rejection for a filled order (observed).
* NautilusTrader `RELEASES.md`: v2.0.0rc9, tagged 2026-11-10, adds `OrderCancelDenied` and
  `OrderModifyDenied` (read by the second reader); the pin stays rc8 until it moves through
  `adopt-upstream`.
* Second reader (`evidence-verifier`), 2026-11-10, on cancel-all twice, stops once and modify once: the
  decisions stand. Corrected above: "never wider than the command"; Binance spot and Bybit select from the
  cache only for a sided command; the retry argument; the 422 exception; who skips `OrderPendingCancel`;
  Kraken's trigger mapping; the run count; sources for the pending-cancel skip and no cancel-by-list; option
  3 added; the documented client order ID; no source for a 403 on a replace; TC-E30's criterion; Binance's
  two paths.