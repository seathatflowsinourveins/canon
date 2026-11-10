---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# The Alpaca adapter's order management: each hook built as upstream's own adapters build it

## Context and Problem Statement

`AlpacaExecutionClient` (ADR 0030) submitted and canceled market and limit orders on paper, and did not
cancel all, take stops or answer an account query (modify is ADR 0042). The adapter guide names the hooks,
not what each must do where Alpaca's endpoint differs from the command. What does each do here, and from
which source?

## Decision Drivers

* ADR 0001, rule 3: each behaviour traces to the guide, an upstream adapter at the pin, or an Alpaca or
  alpaca-py page; upstream is read for shape, not copied. Paper only (ADR 0009). What no page states is not
  guessed into code: it is `UNVERIFIED` and gets a paper test.

## Considered Options

Cancel-all: (1) Alpaca's "Delete All Orders"; (2) the node's open and in-flight orders for the command's
instrument and side, each canceled by ID; (3) Alpaca's open orders for the symbol, from "Get All Orders",
each canceled by ID. Stop orders: send what Alpaca's order page lists, deny the rest before Alpaca. Modify
is ADR 0042, split from this record on 2026-11-10.

## Decision Outcome

### Cancel-all: option 2

Alpaca's endpoint "Attempts to cancel all open orders" of the account; `CancelAllOrders` names one
instrument and maybe a side. Kraken spot, whose venue cancel-all is account-wide too, "select[s] the
matching open orders and cancel[s] them by explicit venue id", in-flight ones included; Binance spot and
Bybit select open orders from the cache for a sided command and call their venue's per-symbol cancel-all
otherwise; Interactive Brokers always selects from the cache; Coinbase lists the venue's orders; Betfair,
and OKX by opt-in, send wider cancels, neither account-wide. So the client cancels by ID, one at a time, the
cache's open and in-flight orders for the instrument and side, every strategy's (the command comes only from
`strategy_only=False`), each re-read at its turn and skipped if closed;one already `PENDING_CANCEL` is
canceled again, after the others, since its first cancel may have had no answer: Kraken spot and Interactive
Brokers select every open order of the instrument, and Binance spot and Bybit every open order of a sided
command, with no status filter, where Coinbase alone filters it out. A 422 is `OrderCancelRejected`
("Cancel, cancel-all, or batch cancel") unless Alpaca shows the order canceled or pending cancel
(alpaca-py's own retry, ADR 0030), or replaced, when the replacement is canceled; an unexpected error is
logged with no event ("parse failures after transmission" leave the outcome unknown). That the rest go on,
in a batch cancel too, is this repo's choice; `_batch_cancel_orders` keeps the guide's default: Alpaca has
no cancel-by-list endpoint.

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

### Account query

The client reported Alpaca's account at connect only. Alpaca's stream sends order updates, not account ones,
and NautilusTrader creates an account from a reported state with fill-driven calculation off
(`AccountAny::from_state_checked` at the pin), so the balance stayed as connect left it. Upstream's adapters
for venues that push no balances, Kraken spot and Coinbase spot, fetch the account on connect and on
`query_account`, as Binance spot and Bybit do on a query too; none refreshes after each fill. So
`_query_account` reads Alpaca's account and reports it as at connect, and a failed read is logged and leaves
the state as it was. Between reads the balance is stale: a strategy that needs it current queries it. This
was the master orchestrator's choice under the owner's delegation of 2026-11-10, on that evidence.

### Consequences

* Good: cancel-all cancels exactly what the command names; stops reach Alpaca only in forms its page lists.
* Bad: the account's balance is as of its last read; one HTTP call per cancel, one at a time; an order
  Alpaca holds that the node does not yet, or one placed after the cache was read, is not canceled. No
  `OrderTriggered`: an election shows only as a fill.

### Confirmation

Paper, 2026-11-10, `mise run paper:test`, all passing: upstream's Execution Testing Spec rows through its
`ExecTester` on the spec's node setup. TC-E41: a limit buy $50 below the bid and a stop buy $50 above the
ask, canceled by `CancelAllOrders` on stop; its sell is left out, since from a flat account Alpaca refuses
it, 403 "cannot open a short sell while a long buy order is open" (observed). The card's
`OrderPendingCancel` does not come: the pinned `Strategy` sends the command without one and the binding
cannot generate it, as Binance spot emits `OrderCanceled` alone. TC-E20 and TC-E22: stop and stop-limit buys
accepted at the prices sent, as Alpaca holds them, and reported back the same. TC-E40 is the existing cancel
test. Not run: sells (TC-E21, E23: a short sale from flat), TC-E42 to E44 (no new hook); not applicable:
TC-E24 to E27. The modify rows are ADR 0042's. Each test places nothing while the account holds an AAPL
order or position, and leaves it as found. This is upstream's practice: the spec's rows through its tester,
and offline stand-ins as alpaca-py's own tests mock its paper endpoints; the offline tests are listed in the
package README.

**Account query.** Offline: a strategy's `query_account` reaches the client and the node's balance takes
Alpaca's new `cash`; a failed read is logged and changes nothing. Paper, 2026-11-10, `mise run paper:test`:
a query through a node, the node's USD balance equal to Alpaca's `cash` as `get_account` gives it.

`UNVERIFIED`, shown by no run: an elected stop and its fill, and what the stream sends on election; any sell
stop.

## Evidence

Read 2026-11-10; "raw": fetched with `curl` and read, not summarised. NautilusTrader files at the pin,
`d53849b216e2`; alpaca-py 0.45.0 and NautilusTrader `v2.0.0rc8` as installed, read with `inspect`.

* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md: the order hooks and the batch
  default, as at the pin but two version lines (fetched, raw; diffed).
  `docs/concepts/execution/policies.md`: the failure events, "parse failures after transmission"; `latest`
  adds `OrderCancelDenied` and `OrderModifyDenied` (fetched, raw; diffed). `docs/concepts/orders/index.md`
  and `stop_market.md`: the trigger types (fetched, raw).
  https://nautilustrader.io/docs/md/latest/developer_guide/spec_exec_testing.md: groups 3 and 5, the TC-E41
  card, `ExecTester`'s setup (fetched, raw).
* `crates/adapters/` at the pin: cancel-all and batch cancel in `kraken/src/execution/spot.rs`,
  `binance/src/spot/execution.rs`, `bybit/src/execution.rs`, `interactive_brokers/src/execution/core.rs`,
  `coinbase/src/execution.rs`, `betfair/src/execution.rs`, `okx/src/execution.rs` and
  `okx/src/websocket/client.rs`; the open orders each selects for cancel-all (Kraken spot `orders_open` and
  `orders_inflight`, Interactive Brokers `cancel_all_targets`, Binance spot and Bybit `orders_open` with the
  side, none filtering by status; Coinbase's status filter without `PendingCancel`); Kraken's trigger
  mapping; Coinbase's `OrderContext`; `query_account` and the account fetch on connect in Kraken spot,
  Coinbase, Binance spot and Bybit (fetched, raw). `crates/trading/src/strategy/mod.rs`: `strategy_only`;
  `crates/testkit/src/testers/exec/strategy.rs`: `on_stop` (fetched, raw).
* https://docs.alpaca.markets/us/: `reference/deleteallorders-1.md` (account-wide, 207 per order),
  `reference/deleteorderbyorderid-1.md` (422 or 204), `reference/patchorderbyorderid-1.md` (`replaced_by`,
  for a cancel that meets a replaced order), `docs/orders-at-alpaca.md` (stop rules and the time-in-force
  tables, read with markup stripped), `docs/websocket-streaming.md` (the events; none for an election),
  `llms.txt` "Orders" (no cancel-by-list) (fetched, raw).
* https://github.com/alpacahq/alpaca-py `v0.45.0`, `tests/trading/trading_client/test_order_routes.py`: the
  paper base URL mocked for orders and replaces (fetched, raw).
* Paper, 2026-11-10, logs kept outside the repo: TC-E41 as written, the sell refused 241 times; five runs
  failed (test code, connect timeouts, open orders on the account); then all passed.
* NautilusTrader `RELEASES.md`: v2.0.0rc9, tagged 2026-11-10, adds `OrderCancelDenied` and
  `OrderModifyDenied` (read by the second reader); the pin stays rc8 until it moves through
  `adopt-upstream`.
* `crates/model/src/accounts/any.rs` (`from_state_checked`), `crates/portfolio/src/portfolio.rs`
  (`calculate_account_state`) at the pin (fetched, raw).
* Second reader (`evidence-verifier`), 2026-11-10, on cancel-all twice and stops once: the decisions stand.
  Corrected above: "never wider than the command"; Binance spot and Bybit select from the cache only for a
  sided command; the retry argument; the 422 exception; who skips `OrderPendingCancel`; Kraken's trigger
  mapping; the run count; sources for the pending-cancel skip and no cancel-by-list; option 3 added.
* Benchmark reviews after landing, 2026-11-10: cancel-all skipped an order already `PENDING_CANCEL`, so one
  whose cancel had no answer was never canceled again, and a closed order with a client order ID
  NautilusTrader cannot hold (empty or not ASCII) aborted start-up reconciliation.Both reproduced with a
  test and are fixed: the first follows the four upstream adapters that include such an order; for the
  second, an ID NautilusTrader cannot hold ends a `replaces` trace, so a closed such order is skipped, a
  valid replacement of one is reported under its own ID, and an open one fails start-up by name, as any open
  order this slice cannot represent does (ADR 0030).
* Modify, its option, outcome, paper rows, `UNVERIFIED` items and their evidence, moved to ADR 0042 on
  2026-11-10 with no change of decision; the source lines that support both are kept in each.