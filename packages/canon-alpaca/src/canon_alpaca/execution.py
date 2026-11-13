"""Execution client for US equities on Alpaca's paper Trading API, with opt-in venue trailing stops.

`submit_trailing_stops`, off by default, enables SELL TRAILING_STOP_MARKET with a PRICE or BASIS_POINTS
offset, DEFAULT trigger, no activation or initial trigger price, DAY or GTC. The contract is Alpaca's
"Trailing Stop Orders" (https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, read 2026-11-12):
`TrailingStopOrderRequest` at alpaca-py c42add7c0a70 requests.py:624-670 takes exactly
one trail_price or trail_percent; BASIS_POINTS / 100 gives percent (Nautilus d53849b216e2
docs/concepts/orders/index.md:235-236). BUY, other offset/trigger kinds and extra price fields are denied
before transport. It is also an eligible explicit sell_to_close_order_types member, retaining the
default scope of three proven types: TrailingStopOrderRequest inherits OrderRequest.position_intent
(requests.py:388, :624, :644). Enforcement on the market order the trigger creates is UNVERIFIED.
Before submission TradingClient.get_clock (client.py:422-436, Clock models.py:410-424) must say the
regular session is open, with time remaining to next_close. One answer is reused until the boundary it
names, its age the longer of the monotonic and the wall clock's, so a machine's sleep cannot carry an
open answer past its close; a read that fails denies by name, with no submit, and is not kept. ADR 0129.
At/after the sampled close it denies, without extended_hours. A close during the subsequent request
can still queue an order until the next session (the same page, "Orders Submitted Outside of Eligible
Trading Hours": "queued up for release the next trading day"); there is no atomic clock-and-submit
endpoint. That boundary and paper election/fill behavior remain UNVERIFIED until paper proof.

Trail modifications use ModifyOrder.params['trailing_offset'] in the existing order's units, the
pinned Interactive Brokers adapter's convention (docs/integrations/interactive_brokers.md:446-450,
execution/commands.rs:340-363). Alpaca PATCH takes ReplaceOrderRequest.trail (requests.py:307-335),
keeping PRICE versus BASIS_POINTS; it cannot change kind (Orders page:329-333). Quantity replacement
keeps the existing path and its reduce-only increase refusal; a limit/trigger price or extra parameter
is refused. A trail replacement checks the regular-session clock too; a quantity-only replacement and a
cancel do not, and only the trail change needs `submit_trailing_stops`. The native
Strategy.modify_order requires a quantity/price/trigger change even with params (trading/src/strategy/
mod.rs:359-404): offset-only works at the adapter hook, while the public strategy path can combine a
quantity change with the offset. No meaningless trigger/quantity change is injected to bypass it.

Native report/event limits at the pin: OrderStatusReport carries stop_price as trigger_price and the
trail as trailing_offset/type; it has no hwm field (model/__init__.pyi:4862-4898). OrderUpdated updates
only quantity and trigger, not the cached trailing_offset (model/src/orders/trailing_stop_market.rs:
532-539), so the status report is the venue's current offset. There is no invented hwm or offset event.
TRAILING_STOP_MARKET rejects OrderTriggered (model/src/orders/mod.rs:116-124, :874-878), so election
arrives as OrderFilled. Alpaca does not document which ID the order a trigger creates carries. Two
forms are covered, both by the existing paths: the order's own venue ID, and a new one that names the
old in `replaces`. A new venue ID with no link is not adopted (its fills are counted by the existing
rule for an unknown leg); that form is UNVERIFIED and is built only if a paper read shows it.

Unanswered submissions (ADR 0044), pin read 2026-11-13: `crates/execution/src/engine/mod.rs:1533-1572`
matches a fill report by client order ID before venue order ID. `_unanswered_named` uses the client ID
from Alpaca's order read only while the cached order still awaits its submit answer; the fill retains
Alpaca's trade ID. This follows OKX's `parse_fill_report` at `crates/adapters/okx/src/common/parse.rs:1157`.
An unread order or ancestor withholds only its own reports for the next request, counting each failed
attempt: an unnamed report would be booked as external and hide the discrepancy that asks again.
A refused cancel that
reads an unacknowledged order FILLED sends that evidence through `_send_report`: retention stops its
automatic queries (`crates/live/src/execution/submission.rs:23-27`), but later venue reports resolve it.
If its activities lag, `_catch_up_later` keeps reading until the fills arrive or the node stops.

Contract: `nautilus_trader.live.clients.ExecutionClient`, NautilusTrader v2.0.0rc8 (Python adapter guide,
"Execution client hooks", https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md). The
client is constructed with the factory's name, config, cache, clock and trader ID and with a venue, account
ID, account type and OMS type. It overrides `_submit_order`, `_modify_order`, `_cancel_order`, `_query_account`,
`_query_order`, `_cancel_all_orders` and the four reconciliation hooks, emits account, order and fill events through the
base class's `generate_*`
methods, and leaves `_batch_cancel_orders` to its default, "ordered calls of the corresponding
single-order hook" (Alpaca has no endpoint that cancels a list of orders by ID), and `_generate_mass_status`
to the native composition of the bulk report hooks. Nothing is started in the constructor; the alpaca-py
clients and the task that applies trade updates in order are made in `_connect`, and the stream thread is
joined and the task ended in `_disconnect`, each wait bounded by a deadline on the node's loop
(`data.join_within`, `TASK_GRACE_SECS` and `TASK_ABORT_SECS`), as upstream's Coinbase adapter bounds its
task drain at disconnect; a wait that runs out fails the disconnect, logged with its reason. Hooks not overridden here
raise `NotImplementedError`.

An acceptance the stream brings carries the order's `submitted_at`, as the HTTP answer's does: the
field is required on alpaca-py's `Order` (c42add7c0a70, trading/models.py:226,269), which a trade update
holds beside its own `timestamp` (:778-779), and NautilusTrader keeps `OrderAccepted.ts_event` as the
order's `ts_accepted` (d53849b216e2, crates/model/src/orders/mod.rs:1042-1046). Fills and endings keep
the update's own time. So an acceptance can be dated before an event already applied (the node's own
submitted time when its clock runs ahead of Alpaca's, or a refused cancel that came first), as the
engine's own reconciliation dates one from the venue's report (crates/execution/src/engine/mod.rs:1751).
Two routes are not covered. A fill that arrives while a cancel or modify is pending and before any
acknowledgement generates the fill alone, and the engine then dates the acceptance with the fill
(orders/mod.rs:1413-1415). After a replace, the acceptance carries the replacement's `submitted_at`;
whether Alpaca gives a replacement its own or its predecessor's is UNVERIFIED.

Instruments at connect: the provider's instruments are handed to the node unless its cache already holds
them, which it does for whatever the data client loaded, since the node connects data clients and fills
the cache first (`crates/live/src/node/mod.rs:35-48` at `nautilus-trader@d53849b216e2`). Each instrument
handed over is one notification on this client's command queue of 1,024 (the guide, "Process commands in
order", `docs/developer_guide/python_adapters.md:168-170`, and `:408`), and upstream refuses those past
its capacity at a whole-market start whatever this client does; the README states that limit ("Instrument
load and the command queue") and `tests/test_instrument_load.py` counts it.

Reduce-only follows the send-or-reject contract in `docs/concepts/adapters.md` at
`nautilus-trader@d53849b216e2`: an equivalent enforcing venue instruction may replace the literal flag.
`reduce_only_sells` is false by default: every reduce-only order is denied before transport with
`reduce-only is not supported`. With it true, a reduce-only SELL sends
`position_intent=PositionIntent.SELL_TO_CLOSE`; a reduce-only BUY is denied before transport. This
field is declared by the pinned alpaca-py's `OrderRequest` (`alpaca/trading/requests.py:388`) and inherited
by every supported request type; the enum value is in `alpaca/trading/enums.py:598`. Alpaca's order
schema describes position_intent (https://docs.alpaca.markets/us/reference/postorder.md). No account
configuration is read or retained. `sell_to_close_on_long_exits`, off by default, also sets that field
for an ordinary SELL when the cache's net position for this account and instrument is long
(`ClientCache.positions_open`, `Position.signed_qty`, in the pinned model/live stubs). It changes no
Nautilus reduce-only flag or strategy default. Both settings act only on the order types in
`sell_to_close_order_types`, by default a limit, a stop and a stop-limit: the types for which a paper run
shows Alpaca refusing the field when the sell would open a short (ADR 0111's Confirmation). A market
order is not among them until that is shown for it: with `reduce_only_sells` a reduce-only market SELL is
denied before transport, and with `sell_to_close_on_long_exits` a market SELL of a long stays a plain sell.

Account metadata contract: alpaca-py c42add7c `alpaca/trading/models.py:626` declares
`TradeAccount.non_marginable_buying_power`; NautilusTrader d53849b2
`crates/model/src/events/account/state.rs:29-32,63-65` preserves venue-specific fields in
`AccountState.info`. The account report carries this optional value unchanged beside buying_power.

Modify (ADR 0042) rests on Alpaca's "Replace Order by ID"
(https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md) and on a paper observation of
2026-11-10, 3 of 3 runs, for what that page does not state. The PATCH answer is "The new Order object with
the new order ID", but a success "does NOT guarantee the existing open order has been replaced", so
`OrderUpdated` is generated only when Alpaca's stream shows the replace took effect: its `replaced` for an
order of the node's chain, which carries the old order with `replaced_by` (observed), with the replacement's
values from the answer or, failing that, from Alpaca; if neither yields them, nothing is generated and
reconciliation settles the order. The replacement is sent a new client order ID of the order's own form
(`replacement_client_order_id`): one kept is refused (observed), and the reference gives the replace a
`client_order_id`, "A unique identifier for the new order. Automatically generated if not sent." Alpaca
makes the ID where the order's own has no numeric last field to build on. A 422 refusal is `OrderModifyRejected` unless Alpaca shows the order already replaced as asked (a
second replace is refused "order already replaced", observed);
`order_replace_rejected` is `OrderModifyRejected` too. Denied before Alpaca: no venue order ID, a fractional
quantity, a quantity or price that is not positive, and a sub-penny price; what only Alpaca knows (an order in
`accepted` "cannot be replaced") is left to its answer. Each modify sent gets one outcome, though Alpaca can
refuse it on the HTTP answer and on the stream.

The replace chain is one model, after upstream's Binance spot adapter, the one pinned adapter that modifies
by cancel-replace (`crates/adapters/binance/src/common/dispatch.rs` at the pin: `OrderBinding`,
`record_order_update`, `record_order_update_from_fill`, `begin_replace`, `reject_replace`, `restore_order`,
`cleanup_terminal`, read raw): one record per node order (`OrderRecord`) holds the Alpaca orders it has been,
the one it is on now and the ones it has left, the replaces awaiting an outcome, and the client's own count of
its fills and their trade IDs. It is built from the node's cache when first needed (the order's venue order
IDs, current and before, its fills and trade IDs), as `restore_order` rebuilds a binding, so a reconnect or a
new client loses nothing of it, and it is dropped when the order ends. Every trade update and every report is
judged through it:

* An Alpaca order is the node's by its own client order ID, the client's index, the node's cache
  (`ClientCache.client_order_id`) or, only on a miss, a walk of `replaces` (`_root_id`), which reads at most
  `MAX_REPLACE_CHAIN` orders and fails by name past that or on an order Alpaca does not find.
* A fill is applied once, on any order of the chain, and the order ends when the record's count reaches its
  quantity, so a late fill of an order since replaced still counts. A fill is known by Alpaca's trade ID
  alone, from the stream, a fill report and the client's own read of the fill activities (`_count_fill`,
  ADR 0075). The engine never infers a fill for an order the node holds: an Alpaca read showing more filled
  than the node holds first brings the missing fills by their trade IDs (`_catch_up`), and a status report
  reaches the engine only once the node's order has applied them (`_settled_report`). A `canceled` or
  `expired` showing more filled than the node holds ends the order after those fills (`_end_after_fills`).
* A replacement is adopted once: by `replaced`, by its own update once the record shows the order it replaces
  replaced by it, or by a fill, which proves it live (`record_order_update_from_fill`), unless Alpaca shows
  it replaced; its `new` alone does not, since it may still be rejected. A late update of an order the record has left is ignored, but for its
  fills (`record_order_update` returns nothing for a superseded venue order ID).
* A replacement's `rejected` took over only if the order it replaces is replaced by it, as the record shows or
  one read of that order does; then nothing works at Alpaca, and the node's order ends: rejected, or, partly
  filled, canceled with its fills, as `reject_replace` emits the old order's withheld `OrderCanceled` and since
  NautilusTrader has no transition from partly filled to rejected. A replacement rejected without taking over
  is the modify's rejection; one a later replace left behind changes nothing; one rejected after the order
  filled leaves the fill as the outcome (NautilusTrader drops a modify rejection for a filled order, observed).
* Reports apply the same rules to Alpaca's reads: a replacement is reported under the client order ID its chain
  starts from, a rejected one only if it took over, and then as the order's end; an open replacement whose
  chain cannot be read fails the report, a closed one is skipped. `pending_replace` is open, at the order's own
  terms ("waiting to be replaced").

What a replacement's `qty` and `filled_qty`, and its fill activities' `cum_qty`, count once the order before it
had fills is not on Alpaca's pages; on paper, overnight, 2 of 2 runs (ADR 0042), they counted the whole
chain: `FILLED_READING` is that reading, `CHAIN`, and `chain_filled` and `chain_quantity` are the only places
the accounting turns on it; the regular session is UNVERIFIED.

Order events follow NautilusTrader's execution policies ("Command outcomes",
https://nautilustrader.io/docs/md/latest/concepts/execution/policies.md): an order this slice cannot send is
denied before it is submitted (`OrderDenied`, no `OrderSubmitted`); one Alpaca refuses with a documented
status (403 or 422 on "Create an Order", https://docs.alpaca.markets/us/reference/postorder.md) is
rejected; a transport error or any other status leaves the outcome unknown and the order `SUBMITTED`, for
the trade updates stream or reconciliation to resolve. A cancel answered 204 waits for Alpaca's `canceled`
trade update; one answered 422 ("Delete Order by ID", https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md)
is a cancel rejection, unless Alpaca shows the order pending cancel (its `canceled` update follows) or
canceled, when the order is canceled from that read if Alpaca shows no more filled than the node holds,
as upstream's Bybit adapter emits `OrderCanceled` from an order it reads canceled: a dropped stream would
never send the update. Cancel-all cancels the node's open and in-flight orders for the command's instrument,
and side if it names one, each by ID through the same path, so a refused one gets `OrderCancelRejected`
("Cancel, cancel-all, or batch cancel": `OrderCancelRejected`, same policies page, at the pinned commit).
Alpaca's "Delete All Orders" (https://docs.alpaca.markets/us/reference/deleteallorders-1.md) "Attempts to
cancel all open orders" of the account, wider than the command, so it is not called; upstream's Kraken spot
adapter, whose venue cancel-all is account-wide too, selects and cancels by ID the same way
(`crates/adapters/kraken/src/execution/spot.rs` at the pinned commit). Fills come only from the stream's
`fill` and `partial_fill` events, whose `qty` and
`price` are "the number of shares" and "the price per share" for that fill event, not cumulative
(https://docs.alpaca.markets/us/docs/websocket-streaming.md).

Reconciliation (https://nautilustrader.io/docs/md/latest/concepts/execution/reconciliation.md): order status
reports come from Alpaca's orders endpoint and position reports from its positions endpoint. Fill reports
come from Alpaca's account activities of type `FILL`, `GET /v2/account/activities/FILL` with the paper
server listed (https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md), called
through alpaca-py's generic `RESTClient.get` ("Performs a single GET request"), since 0.45.0's
`TradingClient` has no method for it (ADR 0024). Each report's trade ID is the UUID after `::` in the
activity's `id`, which a paper test matched to the stream's `execution_id` for every fill, so the engine's
duplicate fill detection sees one fill once ("Duplicate fill detection", `docs/concepts/execution/index.md`
at the pinned commit). With fill reports, the engine no longer "Infers `OrderFilled` events for missing
trade reports" for orders whose fills Alpaca lists. An activity listed twice is one fill, counted and
reported once, as NautilusTrader's order "enforces one quantity-bearing fill per `trade_id`" ("Duplicate
fill detection"). An order's reports are kept only when its activities,
or failing that its order (`filled_qty`), show they are all its fills; otherwise, and when the history
cannot be read at all, none are, with a warning, and the engine infers them as before, for orders the node
does not hold. An open order the node does not hold that shows fills has no exit to inference: its chain's
fills are reported by trade ID at any age, outside the engine's window too, when they add up to what its
status report counts, and are not judged again; otherwise the fill reports fail by name after
`FILL_READ_TRIES` reads, so start-up fails rather than infer fills its later updates could not be matched
to (`_open_order_fills`, ADR 0075). Upstream's OKX
and Kraken spot adapters (`crates/adapters/okx/src/execution.rs`, `kraken/src/execution/spot.rs` at the
pinned commit) instead track whether their fill history is complete and mark the mass status so
(`set_report_window`; reconciliation.md: an adapter "marks the mass status incomplete to record the
missing history"). NautilusTrader's Python `ExecutionMassStatus` at the pin has no setter for
`reports_complete`, so this client drops the fills it cannot show complete: a departure (ADR 0024).
A fill Alpaca lists under an order since replaced is reported on the order that replaced it, which the
order reports carry under the node's client order ID, as the stream applies a fill on a superseded order
to the node's order. The chain's fills are judged together, every order of it read (`_replaced_fills`):
kept when the live order's `filled_qty`, and its newest listed activity's `cum_qty`, show they are all of
them under `FILLED_READING`, the reading observed on paper (ADR 0042); when they show it counts only its
own, which paper did not show, its own fills are kept and the earlier ones left to the engine.

With `report_external_trailing_stops`, off by default, an outside Alpaca `trailing_stop` maps to a
NautilusTrader `TRAILING_STOP_MARKET` report; submission needs the independent setting above. With it off
an open one fails start-up by name, as any open order this slice does not map does: an outside order is
never taken on unasked. Alpaca's trailing-stop fields (`trail_price`, `trail_percent`,
`stop_price`, https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Trailing Stop Orders") give its
offset and current trigger. `trail_price` is a PRICE offset; `trail_percent` is multiplied by 100 for
BASIS_POINTS (NautilusTrader's pinned `docs/concepts/orders/index.md`, "Trailing offset type": 100 is
1%). `OrderStatusReport`'s trailing_offset, trailing_offset_type, trigger_price and trigger_type are
documented in `python/nautilus_trader/model/__init__.pyi` at the pin. A missing or unrepresentable
advisory stop_price stays None (one warning per order for an unrepresentable value): the trailing model
permits a trigger not materialized yet; no activation price or high-water mark is
required (`docs/concepts/orders/trailing_stop_market.md`). Exactly one positive finite trailing offset
is required, else the report fails by name, as any unmapped open order does. This mapping has no local
trailing logic and no stream reports of a changing high-water mark.
Own cached trailing stops use it regardless of the outside-report setting. At a start with no cache
nothing tells this node's open trailing stop from another's (a generated client ID carries the
trader's tag, the same on every node here), so it is mapped only with the outside-report setting, and
assigned to a strategy by Nautilus's external instrument claim, configured by the node, not inferred
from a lost strategy name (execution/src/engine/mod.rs:1219, :1418-1423). Without that setting an
uncached open trailing stop still fails start-up by name.

Venue side, alpaca-py 0.45.0:

* `TradingClient(api_key, secret_key, paper=True)` for `submit_order`, `cancel_order_by_id`,
  `replace_order_by_id`, `get_order_by_id`, `get_order_by_client_id`, `get_orders`, `get_all_positions`,
  `get_account`, `get_clock` and `get` (fill activities only). The
  client is always built with `paper=True` and never with `url_override`, whatever the config holds; the
  config refuses both (`AlpacaExecutionClientConfig`). alpaca-py's REST client is synchronous, so each call
  runs in a worker thread, one at a time on its one `requests.Session`, as in `data.py`.
* `TradingStream(api_key, secret_key, paper=True)` for the trade updates stream. Like the market data
  stream, its `run()` drives its own asyncio loop, so it runs on a thread this client owns, started in
  `_connect` after the handler is subscribed (before `run()`, `subscribe_trade_updates` only records the
  handler) and stopped and joined in `_disconnect`. Updates are handed to the node's loop, where every
  event is generated; the handler holds the client weakly (`data.weak_coroutine`).

Known limits:

* Market, limit, stop and stop-limit, plus the opt-in trailing-stop-market SELL above; `DAY` or `GTC`, whole shares, simple orders
  only. Alpaca marks IOC, FOK, OPG and CLS for whole-share market and limit orders "Yes*", and "Please
  contact the sales team for any TIF marked with a *", and for stop and stop-limit orders "No" ("Orders at
  Alpaca", https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Order Types vs Supported Time in
  Force"), so they are denied.
* Stop and stop-limit orders: a stop "is elected on the consolidated print" (same page, "Order Handling
  Standards"), the last trade, so only `DEFAULT` and `LAST_PRICE` trigger types are sent (both as Alpaca's
  stop; Alpaca has no trigger type field), and any other is denied. Upstream's Kraken spot adapter maps
  `DEFAULT` and `LAST_PRICE` to its last-trade trigger too, and refuses what its venue has no trigger for.
  Alpaca's extended hours table
  gives stop and stop-limit orders "No" in every time in force; a stop marked eligible is denied (below,
  "Extended hours"). The stop price and a stop-limit's limit price follow the sub-penny rule ("Sub-penny
  increments for stop orders"). Alpaca documents no status or trade update for a stop's election, so no
  `OrderTriggered` is generated: an elected stop-limit stays `ACCEPTED` until it fills or ends. A venue
  trailing stop's moving stop price is read in reports; Alpaca's stream documents no hwm update.
* Reduce-only orders are denied by default. Opting into `reduce_only_sells` sends a reduce-only SELL
  with `position_intent=sell_to_close`; BUY is always denied. No account configuration is read. The
  independent optional long-exit intent setting adds the same field to ordinary closing SELLs without
  setting their Nautilus reduce-only flag (sources and cache definition above).
* Post-only, quote-quantity, display quantity (on any order type, a stop-market's
  included), contingent orders and order lists are denied or not implemented.
* `QueryOrder`, which NautilusTrader's in-flight and open-order checks send, is answered with the order's
  status report, as upstream's Binance spot, Bybit, Coinbase, Kraken spot and OKX adapters answer it
  (`_query_order`); an order not found or a failed read sends nothing, and the engine asks again.
* Extended hours (ADR 0038): `submit_order(order, params={"extended_hours": True})` sends a limit order,
  `DAY` or `GTC`, with Alpaca's `extended_hours` set; any other order so marked, an unknown params key and
  a non-boolean value are denied before Alpaca ("Only limit orders with `time_in_force` set to `day` or
  `gtc` orders are accepted as extended hours eligible", "Orders at Alpaca", "Extended Hours Trading").
  Its trade updates are mapped as any order's. A `day` order placed overnight stays active through the next
  trading day and "If unfilled, the order is canceled at 8:00 PM ET" (Alpaca's 24/5 page); which update
  that cancel sends is not documented.
* Cancel-all sends one cancel per order, one at a time, in no specified order, an order already pending
  cancel included; an order placed after the command was read from the cache is not canceled by it.
* A limit price at or above $1.00 with more than 2 decimals, or below $1.00 with more than 4, which Alpaca
  rejects ("Sub-penny increments for limit orders", same page), is sent on that increment instead, a buy
  rounded up and a sell down, so never less marketable (`venue_limit_price`), on a submit and a modify
  alike. NautilusTrader's emulator releases a triggered stop-limit or trailing stop-limit at the
  instrument's 0.0001 increment, so a strategy cannot keep its released limit in cents (ADR 0030). When
  Alpaca acknowledges the order, `OrderUpdated` with the price sent comes just before `OrderAccepted`, so
  the node's order and Alpaca's agree. A modify whose price rounds to the order's own, with nothing else
  changed, is refused before Alpaca: the replace would change nothing. A stop price finer than that increment is still denied: which way to round a trigger is the
  strategy's choice, not the venue's.
* The account is a NautilusTrader cash account holding Alpaca's `cash` as its USD balance, all of it free,
  read at connect and on `QueryAccount` (a strategy's `query_account`), as upstream's Kraken spot,
  Coinbase, Binance spot and Bybit adapters fetch their venue's account on a query; Alpaca's stream pushes
  no account update, and NautilusTrader keeps a reported cash account's balance as reported
  (`calculate_account_state` off), so the balance is stale between reads (ADR 0036). Cash finer than a
  cent is reported to the cent with a warning. An answer without `cash` or `currency`, both optional in
  alpaca-py's `TradeAccount`, is refused naming the field: a connect fails, a query leaves the state.
  Buying power, margin and shorting are not modelled.
* No commission: Alpaca's trade updates carry none, so fills pass `None` and NautilusTrader's own fallback
  applies. No liquidity side: Alpaca reports none, so fills are `NO_LIQUIDITY_SIDE`.
* A fill's trade ID is Alpaca's `execution_id`, which the stream page shows in its example but does not
  define. A fill without one is not emitted; it is logged, and the client's read of the order's fill
  activities brings it (`_catch_up`).
* A fill price is kept exactly, at whatever precision Alpaca sends; the paper example has seven decimals.
* Trade updates for orders this node does not hold are logged and dropped by default, without the venue
  order ID. With `report_external_orders=True`, `_send_report` sends mapped status and companion fills
  through the adapter guide's `_handle_report(report, fills)` contract. The runtime creates the order,
  assigns its external instrument claim and applies real fills before any residual inference
  (`crates/execution/src/engine/mod.rs:1679`, at the pin). Every fill keeps its trade ID; a snapshot is
  sent only when all its filled shares are accounted for by those IDs, so no residual is inferred.
  Updates queued before creation retain their fills by trade ID. Once the cache holds the order,
  updates use the normal event path. Unmapped orders and unloaded symbols are warned about and not reported. No additional
  read is made beyond `_send_report`'s own mapping: a simple hand-placed market fill costs zero REST
  requests. Incomplete or unreadable stream fills leave the order to reconciliation, logged once with
  their stored fills dropped; later outside updates cannot infer the missing fill. Complete closed
  reports release their fills immediately; pending replace-chain fills move off superseded legs.
  Companion fills stay bundled even if the order materializes during mapping's await: the pinned
  `reconcile_order_with_fills` handles both existing and new orders without a second REST fill read.
* An Alpaca order event this slice does not map (`pending_cancel`, `done_for_day`, `stopped`,
  `calculated`, `suspended`, `held`, `restated`) is logged as a warning; `pending_replace` is ignored.
  NautilusTrader generates `OrderPendingCancel` and `OrderPendingUpdate` itself when a command is sent.
* The stream's subscription is not acknowledged through alpaca-py's interface, so trade updates sent before
  it is listening are missed. alpaca-py reconnects the stream by itself after a websocket or connection
  error, with backoff, and says so only through Python `logging` (`TradingStream._drive_run`); updates
  sent while it was down are not replayed, and none of upstream's adapters at the pin catches up on a
  reconnect (Kraken spot and Binance spot log it, Coinbase reads its account again). What settles such an
  order is NautilusTrader's in-flight check, on by default, for an order `SUBMITTED`, `PENDING_CANCEL` or
  `PENDING_UPDATE`, and its open-order check, off by default (`open_check_interval_secs`), for an
  accepted one; both send `QueryOrder`. Otherwise it is settled at the next start. If `run()` returns
  (the stream ends by itself) the client logs an error and does not start it again, as in `data.py`.
* Reports: every open order, up to 500, read first, on its own, with no window, plus the orders
  submitted in the engine's window, since Alpaca filters on submission time and a read of every status
  returns only the newest; at most 500 orders per request (`GetOrdersRequest.limit`, "max is 500"), the newest first; a full page is
  logged as possibly truncated. An order Alpaca reports with a type, side, class, status, quantity, time in
  force or client order ID this slice does not map, or a replacement whose chain cannot be read, fails the
  report when it is open and is skipped with a warning naming why when it is closed.
* Order commands are sent once (ADR 0039). alpaca-py 0.45.0 sends a request answered 429 or 504 up to 3
  more times (`RESTClient._request`), a submit, replace or cancel included, and `TradingClient` takes no
  retry setting. NautilusTrader's policies send a submit, modify or cancel "Once, unless the safety test
  holds" ("Delivery and retry limits", `docs/concepts/execution/policies.md` at the pinned commit), and
  upstream's Binance, Coinbase, Derive, Hyperliquid and Lighter adapters send them once ("Retry gate
  conformance", `docs/developer_guide/adapters.md`); Alpaca's "Working with Orders" FAQ says of an order
  that timed out "You should not attempt to resend the order". So `send_order_commands_once` adds a
  requests response hook to the client's session at connect, and `without_resend` has it end an order
  command at Alpaca's first error answer with alpaca-py's own `APIError`; reads keep alpaca-py's retry,
  a repeat that is safe ("Read or other idempotent request": "On a transient failure"). A 504 or 429 is
  an unknown outcome ("An HTTP 5xx without definitive command evidence is ambiguous", adapters.md; "rate
  limits are definitive only when venue-specific semantics prove that the command was not accepted",
  policies; Alpaca's pages read document none for the Trading API): the order stays as it was,
  `SUBMITTED`, `PENDING_UPDATE` or `PENDING_CANCEL`, for a trade update, the engine's in-flight check (`QueryOrder`,
  read by client order ID for an order without a venue order ID) or reconciliation. Whether a 504 is the
  FAQ's "timeout message" is not stated on Alpaca's pages read (UNVERIFIED).
* A submit refused with 403 or 422 is rejected only once Alpaca shows it does not hold the order: Alpaca
  refuses a repeated `client_order_id`, 422 "client_order_id must be unique" (paper, 2026-11-10), and so
  holds an order under it. A cancel refused with 422 is rejected only once Alpaca shows the order neither
  canceled nor pending cancel; one it shows canceled is canceled.
* alpaca-py sets no HTTP timeout; the client's `TradingClient` gets one, (3.05, 27) s, from a transport
  adapter on alpaca-py's private `_session` (`providers.with_http_timeout`, ADR 0039). A connect timeout
  on a call's only attempt proves nothing was sent, so a submit is rejected, a modify modify-rejected and
  a cancel cancel-rejected, each "not sent" (NautilusTrader's policies, "Command outcomes"); any other
  timeout is an unknown outcome, as above.
* Positions in an instrument the client has not loaded are not reported: NautilusTrader fails start-up on
  one ("instrument missing from cache", observed), and upstream's Coinbase adapter keeps its reports to
  the instruments it loaded. A position in another symbol is not read for a report asked for one.
* Start-up fails, by design, on an open order or position this slice cannot represent: a fractional
  position in a loaded instrument, or an open order that is not a simple whole-share US equity market,
  limit, stop, stop-limit or trailing stop order with mapped fields. An open stop is reported with its
  stop price as the trigger price and the node's trigger type, else `DEFAULT`; trailing reports also
  carry their offset and type, as above.
* Timestamps lose sub-microsecond precision: alpaca-py parses them into `datetime`.
* A trading client given to the constructor is the caller's: every connect uses it, a disconnect does not
  drop it, and nothing is built from the environment in its place (the adapter guide closes in
  `_disconnect` what the adapter creates in `_connect`). A given stream serves one connect, since
  alpaca-py's stream keeps its handlers and websocket between runs; a second connect fails, naming why.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import re
import threading
import time
from datetime import datetime
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal
from enum import Enum
from time import monotonic
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import Protocol
from typing import cast
from typing import override
from urllib.parse import urlsplit

import requests
from alpaca.common.enums import BaseURL
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass
from alpaca.trading.enums import OrderClass as AlpacaOrderClass
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.enums import OrderType as AlpacaOrderType
from alpaca.trading.enums import PositionIntent
from alpaca.trading.enums import PositionSide as AlpacaPositionSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Clock as AlpacaClock
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import TradeAccount
from alpaca.trading.models import TradeUpdate
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.requests import ReplaceOrderRequest
from alpaca.trading.requests import StopLimitOrderRequest
from alpaca.trading.requests import StopOrderRequest
from alpaca.trading.requests import TrailingStopOrderRequest
from alpaca.trading.stream import TradingStream
from nautilus_trader.common import Logger
from nautilus_trader.config import ExecutionClientConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.live.clients import ExecutionClient
from nautilus_trader.model import AccountBalance
from nautilus_trader.model import AccountType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import Equity
from nautilus_trader.model import FillReport
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import LiquiditySide
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import OrderStatusReport
from nautilus_trader.model import OrderType
from nautilus_trader.model import PositionSide
from nautilus_trader.model import PositionStatusReport
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import Symbol
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TradeId
from nautilus_trader.model import TrailingOffsetType
from nautilus_trader.model import TriggerType
from nautilus_trader.model import VenueOrderId

from .constants import ACCOUNT_ID
from .constants import PRICE_PRECISION
from .constants import USD
from .constants import VENUE
from .data import STREAM_JOIN_TIMEOUT_SECS
from .data import join_within
from .data import logged_failure
from .data import post
from .data import retrieved
from .data import weak_callback
from .data import weak_coroutine
from .providers import AlpacaInstrumentProvider
from .providers import counted
from .providers import credentials_from_env
from .providers import not_sent
from .providers import session_of
from .providers import with_http_timeout

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from uuid import UUID

    from alpaca.common.types import RawData
    from alpaca.trading.requests import OrderRequest
    from nautilus_trader import live
    from nautilus_trader.common import Clock
    from nautilus_trader.live import ClientCache
    from nautilus_trader.model import TraderId

PAPER_ONLY_RULE = "paper trading only: agents never place, modify or cancel live orders (AGENTS.md rule 8, ADR 0009)"

LIVE_HOSTS = frozenset(
    host
    for host in (
        urlsplit(BaseURL.TRADING_LIVE.value).hostname,
        urlsplit(BaseURL.TRADING_STREAM_LIVE.value).hostname,
    )
    if host
)
"""Alpaca's live trading hosts, from the pinned alpaca-py's `BaseURL` (`TRADING_LIVE`, `TRADING_STREAM_LIVE`)."""

ACTIVITIES_PATH = "/account/activities/FILL"
"""Alpaca's "Retrieve Account Activities of Specific Type" for fills, `GET /v2/account/activities/FILL`;
alpaca-py's `RESTClient.get` adds the `/v2` (https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md)."""

ACTIVITIES_PAGE_SIZE = 100
"""The reference's `page_size` maximum."""

FILL_PAGES_LIMIT = 10
"""Pages of fills read per report request before stopping with a warning: 1,000 fills, twice the 500 orders
`ORDERS_PAGE_LIMIT` lets the order reports hold."""

FILL_READ_TRIES = 3
"""Reads of Alpaca's fill activities before fills it does not list yet are given up on (ADR 0075): a fill
reports request with an open order the node does not hold then fails by name (`_open_order_fills`), and a
fill the stream shows missing is left with an error. A choice: how long the activities can lag is not on Alpaca's pages
(UNVERIFIED). The wait is alpaca-py's own between attempts of a read (`DEFAULT_RETRY_WAIT_SECONDS` in
`alpaca/common/constants.py` at 0.45.0, which retries a 429 or 504 `DEFAULT_RETRY_ATTEMPTS`, 3, times
after the first attempt); 3 reads in all keeps a start-up that fails within 6 s of waiting."""

FILL_READ_RETRY_SECS = 3.0
"""The wait between those reads, alpaca-py's `DEFAULT_RETRY_WAIT_SECONDS`."""

HELD_REPORT_TIMEOUT_SECS = 0.5
"""Wall-time bound for an entire report hold, including its REST reads and queued engine events.
A targeted report must not become a successful empty answer
(`targeted_report_response`, nautilus-trader@d53849b216e2 reconciliation.rs:549-556). The half second is
an adapter choice, not an upstream guarantee; targeted failure preserves incomplete coverage."""

HELD_REPORT_RETRY_SECS = 5.0
"""Minimum spacing after a timed-out hold before retrying an unchanged closed order, and between
named omissions of an order whose cache stays open. An adapter choice; cache progress can unblock
a request sooner. Disconnect discards the retry state, which is this client's own."""

HELD_REPORT_REREADS = 3
"""How often the client reads a finished order again by itself, `HELD_REPORT_RETRY_SECS` apart, after
a stream update's hold on it failed: no further update comes for an ended order, and the engine's
open-order check reads open orders only by default (`open_check_open_only`), so nothing else would. An
adapter choice, the count of `FILL_READ_TRIES`. Past the last the order is left, with an error, to the
next start-up; whether start-up reconciliation then brings its fills is UNVERIFIED."""

ORDERS_PAGE_LIMIT = 500
"""The most orders one `get_orders` call returns: `GetOrdersRequest.limit` "Defaults to 50 and max is 500"."""

TASK_GRACE_SECS = 1.0
"""How long `_disconnect` lets an update being applied finish before it cancels the task applying them;
then it waits `TASK_ABORT_SECS` for the task to end. The graceful and forced bounds of upstream's Coinbase
adapter's task drain at disconnect (`crates/adapters/coinbase/src/execution.rs`,
`finish_shutdown(Duration::from_secs(1), Duration::from_secs(2))`, at the pinned commit)."""

TASK_ABORT_SECS = 2.0
"""See `TASK_GRACE_SECS`."""

REASON_MAX_CHARS = 200
"""Venue text in a rejection reason is cut to this length; NautilusTrader's policies ask adapters to "bound
untrusted venue text before emission"."""

INSTRUMENT_REFUSAL = ("instrument update failed", "command queue is full")
"""What a line of the node's log holds when NautilusTrader refused an instrument's notification to this
client because its command queue was full: `Client ALPACA instrument update failed: RuntimeError: Client
ALPACA command queue is full` (`crates/live/src/python/client/mod.rs:1960-1972` and
`crates/live/src/python/runtime.rs:187-192` at the pinned `d53849b216e2`). `tests/test_instrument_load.py`
reads it off a real node's log, so a rewording upstream fails there; a window's record counts by it
(`canon_trading.session_window`)."""

DEFINITIVE_SUBMIT_REFUSALS = frozenset({403, 422})
"""Statuses "Create an Order" documents for a refused order: 403 "Buying power or shares is not
sufficient", 422 "Input parameters are not recognized". Any other failure leaves the outcome unknown."""

NOT_CANCELABLE = 422
""""Delete Order by ID": "If the order is no longer cancelable, the request will be rejected with status
422; otherwise accepted with return status 204"."""

REPLACE_REFUSAL = 422
"""The status a paper observation saw for a refused replace ("client_order_id must be unique", "order already
replaced"); "Replace Order by ID" documents no error status, so any other leaves the outcome unknown."""

MAX_REPLACE_CHAIN = 16
"""The most Alpaca reads one walk of a replace chain makes, back through `replaces` or on through
`replaced_by`. Links the orders already read, the client's index or the node's cache give cost none, so a
chain of any length is followed through them; a walk that would need more reads fails by name."""

ORDER_SIDES = {OrderSide.BUY: AlpacaOrderSide.BUY, OrderSide.SELL: AlpacaOrderSide.SELL}
ORDER_SIDES_FROM_ALPACA = {v: k for k, v in ORDER_SIDES.items()}

TIMES_IN_FORCE = {TimeInForce.DAY: AlpacaTimeInForce.DAY, TimeInForce.GTC: AlpacaTimeInForce.GTC}
"""The times in force Alpaca offers whole-share market, limit, stop and stop-limit orders without a sales
contact."""
TIMES_IN_FORCE_FROM_ALPACA = {v: k for k, v in TIMES_IN_FORCE.items()}

ORDER_TYPES_FROM_ALPACA = {
    AlpacaOrderType.MARKET: OrderType.MARKET,
    AlpacaOrderType.LIMIT: OrderType.LIMIT,
    AlpacaOrderType.STOP: OrderType.STOP_MARKET,
    AlpacaOrderType.STOP_LIMIT: OrderType.STOP_LIMIT,
    AlpacaOrderType.TRAILING_STOP: OrderType.TRAILING_STOP_MARKET,
}

ORDER_TYPES = frozenset(
    {
        OrderType.MARKET,
        OrderType.LIMIT,
        OrderType.STOP_MARKET,
        OrderType.STOP_LIMIT,
        OrderType.TRAILING_STOP_MARKET,
    }
)
"""Submission types; trailing stops require the separate submit_trailing_stops opt-in."""
STOP_TYPES = frozenset({OrderType.STOP_MARKET, OrderType.STOP_LIMIT})
SELL_TO_CLOSE_PROVEN = frozenset({OrderType.LIMIT, OrderType.STOP_MARKET, OrderType.STOP_LIMIT})
"""The order types on which `position_intent=sell_to_close` is shown, on paper, to be refused by Alpaca
when the sell would open a short (ADR 0111). The default of `sell_to_close_order_types`."""
REPORT_TRIGGER_TYPES = STOP_TYPES | {OrderType.TRAILING_STOP_MARKET}
LIMIT_PRICED = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT})

STOP_TRIGGER_TYPES = frozenset({TriggerType.DEFAULT, TriggerType.LAST_PRICE})
"""Alpaca elects a stop "on the consolidated print" ("Orders at Alpaca", "Order Handling Standards"): the
last trade. `DEFAULT` is "the venue's default trigger type"; `LAST_PRICE` "Uses the last traded price"."""

OPEN_STATUSES = {
    AlpacaOrderStatus.NEW: OrderStatus.ACCEPTED,
    AlpacaOrderStatus.ACCEPTED: OrderStatus.ACCEPTED,
    AlpacaOrderStatus.PENDING_NEW: OrderStatus.ACCEPTED,
    AlpacaOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    AlpacaOrderStatus.PENDING_CANCEL: OrderStatus.PENDING_CANCEL,
    # "The order is waiting to be replaced by another order" ("Order Lifecycle"): still working, at its
    # own terms, until `replaced`.
    AlpacaOrderStatus.PENDING_REPLACE: OrderStatus.ACCEPTED,
}
"""Alpaca's open order statuses this slice maps. `new`, `accepted` and `pending_new` all mean Alpaca has
received the order ("Order Lifecycle" on the stream page and "Get Order by ID"); NautilusTrader's
`ACCEPTED` is the venue's acknowledgement that it is "received and valid"."""

CLOSED_STATUSES = {
    AlpacaOrderStatus.FILLED: OrderStatus.FILLED,
    AlpacaOrderStatus.CANCELED: OrderStatus.CANCELED,
    AlpacaOrderStatus.EXPIRED: OrderStatus.EXPIRED,
    AlpacaOrderStatus.REJECTED: OrderStatus.REJECTED,
}

ACCEPTING_EVENTS = frozenset({"new", "accepted", "pending_new"})
FILL_EVENTS = frozenset({"fill", "partial_fill"})


class PaperOnlyError(ValueError):
    """A setting that would point the execution client anywhere but Alpaca's paper Trading API."""


class HeldReportError(RuntimeError):
    """A report hold ended with an error already logged against its order, never a missing answer."""


@dataclasses.dataclass
class ReportHoldFailure:
    """Bounded retry and log spacing for the targeted-report incomplete-coverage contract above."""

    state: tuple[bool, Decimal]
    retry_at: float
    log_after: float
    message: str


class AlpacaExecutionClientConfig(ExecutionClientConfig):
    """Paper execution configuration, with opt-in stream reports for orders the node does not hold.

    The adapter guide asks for adapter fields as keyword arguments of a Python `__init__`, with `**kwargs`
    for the common fields ("Define adapter configuration"). `report_external_orders` defaults to false;
    true reports outside orders and their complete trade-ID fills from the stream.
    `sell_to_close_on_long_exits` also defaults to false; true adds SELL_TO_CLOSE to an ordinary SELL
    when this account's cached net position in the instrument is long. `reduce_only_sells`, also false
    by default, enables SELL_TO_CLOSE for reduce-only SELLs; otherwise all reduce-only orders are denied.
    None of these settings sets reduce_only. `submit_trailing_stops`, false by default, enables the
    venue trailing-stop SELL contract in the module header. `report_external_trailing_stops`, false by default,
    reports an open trailing stop placed outside the node as an external order; with it false such an
    order fails start-up by name, since an outside order is never taken on unasked.
    `sell_to_close_order_types` scopes `sell_to_close_on_long_exits` and `reduce_only_sells`: the order
    types that may carry the field, by default `SELL_TO_CLOSE_PROVEN`; a type the adapter does not submit
    is refused.
    The client
    always builds alpaca-py's clients with `paper=True` and no `url_override`. Its `__init__` refuses what
    would mean otherwise, naming the rule:

    * `paper` other than `True`;
    * `url_override` in any form, and say so when it names a live host;
    * any other keyword, since NautilusTrader's native `ExecutionClientConfig` drops an unknown keyword
      without a word (observed with v2.0.0rc8), and a value naming a live host gets the paper-only error.
    """

    COMMON_FIELDS: ClassVar[frozenset[str]] = frozenset({"instrument_provider", "routing"})

    def __init__(
        self,
        *,
        paper: object = True,
        url_override: object = None,
        report_external_orders: bool = False,
        sell_to_close_on_long_exits: bool = False,
        reduce_only_sells: bool = False,
        sell_to_close_order_types: object = None,
        report_external_trailing_stops: bool = False,
        submit_trailing_stops: bool = False,
        **kwargs: object,
    ) -> None:
        if paper is not True:
            raise PaperOnlyError(
                f"AlpacaExecutionClientConfig refuses paper={paper!r}: {PAPER_ONLY_RULE}"
            )
        if url_override is not None:
            where = "Alpaca's live trading host" if names_live_host(url_override) else "a URL"
            raise PaperOnlyError(
                f"AlpacaExecutionClientConfig refuses url_override={url_override!r} ({where}): the client "
                f"connects to Alpaca's paper Trading API only; {PAPER_ONLY_RULE}",
            )
        if not isinstance(report_external_orders, bool):
            raise TypeError("report_external_orders must be a bool")
        self.report_external_orders = report_external_orders
        if not isinstance(sell_to_close_on_long_exits, bool):
            raise TypeError("sell_to_close_on_long_exits must be a bool")
        self.sell_to_close_on_long_exits = sell_to_close_on_long_exits
        if not isinstance(reduce_only_sells, bool):
            raise TypeError("reduce_only_sells must be a bool")
        self.reduce_only_sells = reduce_only_sells
        if sell_to_close_order_types is None:
            scope = SELL_TO_CLOSE_PROVEN
        else:
            if isinstance(sell_to_close_order_types, str) or not isinstance(
                sell_to_close_order_types, collections.abc.Iterable
            ):
                raise TypeError("sell_to_close_order_types must be a collection of OrderType")
            scope = frozenset(sell_to_close_order_types)
            if not all(isinstance(kind, OrderType) for kind in scope):
                raise TypeError("sell_to_close_order_types must be a collection of OrderType")
            unsupported = sorted(kind.name for kind in scope - ORDER_TYPES)
            if unsupported:
                raise ValueError(
                    f"sell_to_close_order_types names {', '.join(unsupported)}, which this adapter "
                    "does not submit; market, limit, stop, stop-limit and trailing-stop-market only"
                )
        # A tuple in the order of the types' names: instance fields are serialised with the config, and
        # the pinned serialiser takes a tuple of order types and no frozenset.
        self.sell_to_close_order_types = tuple(
            sorted(scope, key=lambda order_type: order_type.name)
        )
        if not isinstance(report_external_trailing_stops, bool):
            raise TypeError("report_external_trailing_stops must be a bool")
        self.report_external_trailing_stops = report_external_trailing_stops
        if not isinstance(submit_trailing_stops, bool):
            raise TypeError("submit_trailing_stops must be a bool")
        self.submit_trailing_stops = submit_trailing_stops
        for key, value in kwargs.items():
            if key in self.COMMON_FIELDS:
                continue
            if names_live_host(value):
                raise PaperOnlyError(
                    f"AlpacaExecutionClientConfig refuses {key}={value!r}, Alpaca's live trading host: "
                    f"{PAPER_ONLY_RULE}",
                )
            raise TypeError(
                f"AlpacaExecutionClientConfig has no setting {key!r}: it takes only "
                "report_external_orders, sell_to_close_on_long_exits, reduce_only_sells, "
                "sell_to_close_order_types, report_external_trailing_stops, submit_trailing_stops "
                f"and {sorted(self.COMMON_FIELDS)}, "
                "and NautilusTrader would "
                "drop an unknown one silently",
            )


def names_live_host(value: object) -> bool:
    """Whether `value` names one of Alpaca's live trading hosts, as a URL or a bare host name.

    Host names are compared whole, so `paper-api.alpaca.markets` is not taken for `api.alpaca.markets`, and
    without the trailing dot of a fully qualified name (`api.alpaca.markets.` names the same host).
    """
    return any(
        token.rstrip(".") in LIVE_HOSTS for token in re.findall(r"[a-z0-9.-]+", str(value).lower())
    )


class OrdersClient(Protocol):
    """The part of alpaca-py's `TradingClient` this client uses (signatures as in alpaca-py 0.45.0)."""

    def submit_order(self, order_data: OrderRequest) -> AlpacaOrder | RawData: ...

    def cancel_order_by_id(self, order_id: UUID | str) -> None: ...

    def get_order_by_id(self, order_id: UUID | str) -> AlpacaOrder | RawData: ...

    def get_order_by_client_id(self, client_id: str) -> AlpacaOrder | RawData: ...

    def get_orders(self, filter: GetOrdersRequest | None = None) -> list[AlpacaOrder] | RawData: ...

    def get_all_positions(self) -> list[AlpacaPosition] | RawData: ...

    def get_account(self) -> TradeAccount | RawData: ...

    def get_clock(self) -> AlpacaClock | RawData: ...

    def replace_order_by_id(
        self, order_id: UUID | str, order_data: ReplaceOrderRequest | None = None
    ) -> AlpacaOrder | RawData: ...

    def get(self, path: str, data: dict | str | None = None) -> Any: ...


type TradeUpdateHandler = Callable[[TradeUpdate | RawData], Awaitable[None]]


class TradeUpdateStream(Protocol):
    """The part of alpaca-py's `TradingStream` this client uses."""

    def subscribe_trade_updates(self, handler: TradeUpdateHandler) -> None: ...

    def run(self) -> None: ...

    def stop(self) -> None: ...


type StreamExit = tuple[TradeUpdateStream, BaseException | None]


def run_trade_stream(
    stream: TradeUpdateStream,
    loop: asyncio.AbstractEventLoop,
    on_exit: Callable[[StreamExit], None],
) -> None:
    """Run alpaca-py's trade stream on the calling thread and report, naming the stream, when it returns."""
    try:
        stream.run()
    except BaseException as e:
        post(loop, on_exit, (stream, e))
        raise
    post(loop, on_exit, (stream, None))


async def drain(consumer: asyncio.Task, idle: asyncio.Event) -> None:
    """Let the update `consumer` is applying finish, for at most `TASK_GRACE_SECS`, then cancel it and wait
    at most `TASK_ABORT_SECS` for it to end: the graceful and forced bounds of upstream's
    `TaskGroup::finish_shutdown`."""
    if not consumer.done():
        try:
            await asyncio.wait_for(idle.wait(), TASK_GRACE_SECS)
        except TimeoutError:
            pass
    consumer.cancel()
    await asyncio.wait({consumer}, timeout=TASK_ABORT_SECS)


def release_after[R](
    loop: asyncio.AbstractEventLoop, lock: asyncio.Lock, fn: Callable[..., R], *args: object
) -> R:
    """In a worker thread: return `fn(*args)`, then release `lock` on the node's loop, however `fn` ends."""
    try:
        return fn(*args)
    finally:
        try:
            loop.call_soon_threadsafe(lock.release)
        except RuntimeError:
            pass  # the loop is closed, and the lock with it


SENDING_ONCE = threading.local()
"""`active` on a worker thread while it runs an order command (`without_resend`)."""


def send_order_commands_once[C](client: C) -> C:
    """Add `end_at_first_error` to the response hooks of an alpaca-py REST client's `requests.Session`
    ("Event Hooks": a `Session`'s hooks "will then be called on every request made to the session"), once,
    and return the client. alpaca-py 0.45.0 keeps the session at `_session`, the attribute ADR 0039 reaches
    through `session_of`; a client without one is refused, as `with_http_timeout` refuses it."""
    session = session_of(client)
    if session is None:
        raise TypeError(
            f"{type(client).__name__} has no requests.Session at _session; refusing to run with "
            "alpaca-py's resend of order commands (ADR 0039)"
        )
    hooks = session.hooks.setdefault("response", [])
    if end_at_first_error not in hooks:
        hooks.append(end_at_first_error)
    return client


def without_resend[R](fn: Callable[..., R]) -> Callable[..., R]:
    """`fn`, an order command of a client `send_order_commands_once` has prepared, run as an order command:
    on the worker thread that runs it, `end_at_first_error` ends it at Alpaca's first error answer, so
    alpaca-py does not send it again."""

    def call(*args: object) -> R:
        SENDING_ONCE.active = True
        try:
            return fn(*args)
        finally:
            SENDING_ONCE.active = False

    call.__name__ = getattr(fn, "__name__", "call")
    return call


def end_at_first_error(
    response: requests.Response, *args: object, **kwargs: object
) -> requests.Response:
    """A requests response hook ("Event Hooks", Advanced Usage): during an order command, an error answer
    raises alpaca-py's `APIError` at once, the exception `RESTClient._one_request` raises for an answer it
    does not retry, built as it builds it (`APIError(response.text, http_error)`). alpaca-py 0.45.0 sends a
    request answered 429 or 504 up to three more times, an order command included, and its `TradingClient`
    takes no retry setting; NautilusTrader's policies send a submit, modify or cancel "Once, unless the
    safety test holds", and Alpaca's "Working with Orders" says not to resend an order that timed out. A
    429 ends it too: "rate limits are definitive only when venue-specific semantics prove that the command
    was not accepted" (policies), and Alpaca's pages read document none for the Trading API. Any other
    answer, and every answer outside an order command, goes to alpaca-py as it came."""
    if getattr(SENDING_ONCE, "active", False):
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise APIError(response.text, e) from e
    return response


def not_sent_reason(error: BaseException) -> str:
    """The reason for a command a connect timeout proves was never sent (ADR 0039)."""
    return f"not sent: Alpaca's API did not accept the connection ({bounded(repr(error))})"


def bounded(text: str) -> str:
    """Venue text cut to `REASON_MAX_CHARS`."""
    return text if len(text) <= REASON_MAX_CHARS else text[: REASON_MAX_CHARS - 1] + "…"


class ChainTooLong(ValueError):
    """A replace chain that needs more reads than `MAX_REPLACE_CHAIN`. Its words are the adapter's own
    and name no venue order ID, so `failure_cause` keeps them."""


def failure_cause(error: BaseException) -> str:
    """What failed, for a log line that must not name a venue order ID: the exception's class and, for
    Alpaca's own error, its HTTP status and body code, so a rate limit, a missing order and a server
    error read differently. The exception's text is left out: it can carry an order ID. Two exceptions:
    the adapter's own `ChainTooLong` keeps its words, and a body code that is not a number is left out,
    since only a number is sure to carry no ID."""
    if isinstance(error, ChainTooLong):
        return str(error)
    if not isinstance(error, APIError):
        return type(error).__name__
    try:
        status = error.status_code
    except AttributeError, ValueError, KeyError, TypeError:
        status = None
    try:
        code = error.code
    except ValueError, KeyError, TypeError:
        code = None
    if not (
        isinstance(code, int) or (isinstance(code, str) and code.isdecimal() and len(code) <= 12)
    ):
        code = None
    parts = [str(part) for part in (status, code) if part is not None]
    return f"APIError {': '.join(parts)}" if parts else "APIError"


def api_error_text(error: APIError) -> str:
    """Alpaca's optional body code and message, without its JSON wrapper, bounded.

    The pinned alpaca-py's `APIError.code` reads the JSON body's `code` and raises when it is absent;
    the HTTP status is separate. A code-free body keeps the existing message or raw-text fallback.
    """
    try:
        message = str(error.message)
    except ValueError, KeyError, TypeError:
        message = str(error)
    try:
        code = error.code
    except ValueError, KeyError, TypeError:
        code = None
    if code is not None:
        message = f"{code}: {message}"
    return bounded(message)


def decimal_value(value: object, field: str) -> Decimal:
    """An Alpaca decimal string or number as a finite `Decimal`, raising `ValueError` naming `field` for one
    that is not a number: `Decimal` raises `InvalidOperation`, an `ArithmeticError`, which the callers'
    `ValueError` handling would not take."""
    try:
        amount = Decimal(str(value))
    except ArithmeticError as e:
        raise ValueError(f"{field} {value!r} is not a number") from e
    if not amount.is_finite():
        raise ValueError(f"{field} {value!r} is not a number")
    return amount


def venue_trail(offset: object, kind: TrailingOffsetType) -> float:
    """Alpaca's one trail value: PRICE dollars or BASIS_POINTS / 100 percent.

    alpaca-py requests.py:642-648 takes floats; Nautilus orders/index.md:235-236 defines units.
    Reject an unrepresentable amount rather than silently underflowing it to zero or overflowing it.
    """
    if kind not in (TrailingOffsetType.PRICE, TrailingOffsetType.BASIS_POINTS):
        raise ValueError(f"trailing offset type {getattr(kind, 'name', kind)} is not supported")
    amount = decimal_value(offset, "trailing offset")
    if amount <= 0:
        raise ValueError(f"trailing offset {offset} must be positive")
    try:
        if kind == TrailingOffsetType.BASIS_POINTS:
            amount /= 100
        value = float(amount)
    except ArithmeticError as e:
        raise ValueError(
            f"trailing offset {offset} cannot be represented by alpaca-py's float"
        ) from e
    if not 0 < value < float("inf") or Decimal(str(value)) != amount:
        raise ValueError(f"trailing offset {offset} cannot be represented by alpaca-py's float")
    return value


def whole_quantity(value: object, field: str) -> Quantity:
    """A whole-share quantity from an Alpaca decimal string or number, refusing a fractional one."""
    amount = decimal_value(value, field)
    if amount != amount.to_integral_value():
        raise ValueError(f"{field} {value} is fractional; Equity is whole-share only")
    return Quantity(int(amount), 0)


def exact_report_price(value: object) -> Price:
    """A price for a report, at the adapter's precision, refusing a missing one and one that would need
    rounding."""
    if value is None:
        raise ValueError("no price")
    amount = decimal_value(value, "price")
    if amount != amount.quantize(Decimal(1).scaleb(-PRICE_PRECISION)):
        raise ValueError(f"price {value} is finer than {PRICE_PRECISION} decimals")
    return Price(float(amount), PRICE_PRECISION)


def wire_price(price: Price) -> float:
    """A NautilusTrader price as alpaca-py's request classes take it: their price fields are floats."""
    return float(Decimal(str(price)))


def fill_price(value: float) -> Price:
    """The fill price exactly as Alpaca sent it: the shortest decimal form of alpaca-py's float."""
    return Price.from_str(repr(float(value)))


def update_fill_values(update: TradeUpdate) -> tuple[str, Quantity, Price]:
    """The trade ID and exact per-event quantity and price (Alpaca's Websocket Streaming contract)."""
    if update.execution_id is None:
        raise ValueError("it has no execution_id to use as the trade ID")
    if update.qty is None or update.price is None:
        raise ValueError("it has no qty or price")
    return (
        str(update.execution_id),
        whole_quantity(update.qty, "fill quantity"),
        fill_price(update.price),
    )


def venue_limit_price(price: Price, side: OrderSide) -> Price:
    """The limit price as sent to Alpaca: on its increment, 2 decimals at or above $1.00 and 4 below
    ("Sub-penny increments for limit orders"), a buy rounded up and a sell down, so never less marketable;
    `price` itself when it is on the increment already."""
    amount = Decimal(str(price))
    step = Decimal(1).scaleb(-(2 if amount >= 1 else 4))
    rounded = amount.quantize(
        step, rounding=ROUND_CEILING if side == OrderSide.BUY else ROUND_FLOOR
    )
    return price if rounded == amount else Price(float(rounded), PRICE_PRECISION)


def sub_penny_denial(price: Price, what: str = "limit price") -> str | None:
    """Why Alpaca would reject this limit or stop price, or None: 2 decimals at or above $1.00, 4 below."""
    amount = Decimal(str(price))
    places = 2 if amount >= 1 else 4
    if amount != amount.quantize(Decimal(1).scaleb(-places)):
        return (
            f"{what} {amount.normalize()} has more than {places} decimals, which Alpaca rejects "
            f"for a price {'at or above' if amount >= 1 else 'below'} $1.00"
        )
    return None


EXTENDED_HOURS = "extended_hours"
"""The submit parameter that makes an order eligible outside the regular session. Venue-specific order
parameters travel in the command's `params` (adapter guide: commands "preserve native identifiers and
params"), as upstream's Bybit, Kraken and OKX adapters read theirs and `ExecTesterConfig.order_params`
sends them; the name is Alpaca's own field ("Orders at Alpaca", "Extended Hours Trading")."""


def extended_hours_param(params: object) -> tuple[bool, str | None]:
    """The submit's `extended_hours` flag, and why the params are refused, if they are: a parameter this
    client does not take is refused rather than dropped."""
    if not params:
        return False, None
    if not isinstance(params, dict):
        return False, f"order params {params!r} are not a mapping"
    unknown = sorted(str(k) for k in params if k != EXTENDED_HOURS)
    if unknown:
        return False, f"order params {unknown} are not taken; only {EXTENDED_HOURS!r}"
    value = params.get(EXTENDED_HOURS, False)
    if not isinstance(value, bool):
        return False, f"{EXTENDED_HOURS} must be true or false, got {value!r}"
    return value, None


def extended_hours_denial(order: Any) -> str | None:
    """Why Alpaca would reject this order as extended-hours eligible, or None: "Only limit orders with
    `time_in_force` set to `day` or `gtc` orders are accepted as extended hours eligible. All other order
    types and TIF values will be rejected with an error" ("Orders at Alpaca", "Extended Hours Trading")."""
    if order.order_type != OrderType.LIMIT:
        return f"{EXTENDED_HOURS} is for limit orders only at Alpaca, not {order.order_type.name}"
    return None  # `_denial` has already refused a time in force other than `DAY` and `GTC`


def holdable_id(value: str) -> ClientOrderId | None:
    """A client order ID as NautilusTrader holds it, or None for one it refuses (empty or not ASCII)."""
    try:
        return ClientOrderId(value)
    except ValueError:
        return None


def order_description(order: AlpacaOrder) -> str:
    """Name an order's terms and client lineage without its venue order ID."""
    side = getattr(order.side, "value", order.side)
    kind = getattr(order.type, "value", order.type)
    return (
        f"Alpaca order for {order.symbol} {side} {kind} (client lineage {order.client_order_id!r})"
    )


CLIENT_ORDER_ID_MAX = 128
"""Alpaca's limit on a client order ID, the replacement's included: "A unique identifier for the new
order. Automatically generated if not sent. (<= 128 characters)" ("Replace Order by ID", request body,
https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md, read 2026-11-12)."""
REPLACEMENT_STAMP_DIGITS = 19
"""The width of the clock stamp in a replacement's client order ID: nanoseconds since the epoch, which
is nineteen digits until 2286 and is padded to that before."""


def replacement_client_order_id(client_order_id: str, stamp_ns: int) -> str | None:
    """The client order ID a replacement of the node's order `client_order_id` is sent with: the same
    ID with `stamp_ns`, at a fixed width, after its last field; None when that field is not a number,
    when nothing comes before it, when the stamp is not a count of that width, or when the result
    would pass Alpaca's limit, and Alpaca then makes the ID as it did before.

    A replacement must carry a new ID: Alpaca refuses one the order already carries, 422
    "client_order_id must be unique" (paper, 2026-11-10, ADR 0042). Deriving it from the order's own
    keeps the form: `O-20261112-143000-001-001-7` gives `O-20261112-143000-001-001-7` and nineteen
    digits, still an ID of the node's form to whoever reads an account by it (a ruling of
    2026-11-12, ADR 0129), while an order whose own ID has another form keeps that other form. It is
    never an ID the node writes, whose counts are short, and the fixed width keeps two orders'
    replacements apart: the count is what is left when the stamp is taken off.
    """
    if not 0 <= stamp_ns < 10**REPLACEMENT_STAMP_DIGITS:
        return None
    base, dash, count = client_order_id.rpartition("-")
    if not (base and dash and count.isascii() and count.isdigit()):
        return None
    found = f"{base}-{count}{stamp_ns:0{REPLACEMENT_STAMP_DIGITS}d}"
    return found if len(found) <= CLIENT_ORDER_ID_MAX else None


def names_replacement(client_order_id: str, carried: object) -> bool:
    """Whether `carried` is an ID `replacement_client_order_id` gives a replacement of the node's order
    `client_order_id`: that ID and exactly the stamp's digits. It tells what this client names from an
    ID Alpaca or a hand made; it is never read for which order a replacement belongs to."""
    return (
        isinstance(carried, str)
        and len(carried) == len(client_order_id) + REPLACEMENT_STAMP_DIGITS
        and carried.startswith(client_order_id)
        and carried[len(client_order_id) :].isascii()
        and carried[len(client_order_id) :].isdigit()
    )


def replaces_as_asked(replacement: AlpacaOrder, request: ReplaceOrderRequest, order: Any) -> bool:
    """Whether Alpaca's replacement has the terms the replace request asked for, a quantity the request
    leaves out being the node order's own: a request that omits a field asks to keep it."""
    pairs = (
        (request.qty if request.qty is not None else order.quantity, replacement.qty),
        (request.limit_price, replacement.limit_price),
        (request.stop_price, replacement.stop_price),
    )
    if request.trail is not None:
        price_offset = order.trailing_offset_type == TrailingOffsetType.PRICE
        held_trail = replacement.trail_price if price_offset else replacement.trail_percent
        other_trail = replacement.trail_percent if price_offset else replacement.trail_price
        if (
            replacement.type != AlpacaOrderType.TRAILING_STOP
            or held_trail is None
            or other_trail is not None
            or decimal_value(held_trail, "trail") != Decimal(str(request.trail))
        ):
            return False
    return all(
        asked is None or (held is not None and Decimal(str(held)) == Decimal(str(asked)))
        for asked, held in pairs
    )


class FilledReading(Enum):
    """What Alpaca's `qty` and `filled_qty` on a replacement, and the `cum_qty` and `leaves_qty` of its
    fill activities, count once the order it replaced had fills. Alpaca's pages do not say; paper showed
    `CHAIN` overnight (ADR 0042), and the regular session is UNVERIFIED. Both readings are written here
    and tested, and `FILLED_READING` names the one in force."""

    CHAIN = "chain"
    """The replacement carries the order on: its `qty` is the order's and its `filled_qty` counts every fill
    of the replace chain."""

    OWN = "own"
    """The replacement is an order for what is left: its `qty` and `filled_qty` are its own, and the chain's
    add the fills of the orders before it."""


FILLED_READING = FilledReading.CHAIN
"""The reading in force, observed on paper (ADR 0042, 2026-11-10 New York, overnight, 2 of 2 runs): a
partly filled order replaced and then filled showed the replacement's `qty` as the order's, its `filled_qty`
and its activity's `cum_qty` counting the fills before it, and `leaves_qty` as what is left of the order.
Whether the regular session answers the same is UNVERIFIED; `OWN` stays written down and tested for it."""


def chain_filled(own: Decimal, earlier: Decimal) -> Decimal:
    """The shares a replace chain has filled, from its live order's `filled_qty` (`own`) and the
    `filled_qty` of the orders before it (`earlier`), under `FILLED_READING`. With `chain_quantity`, the one
    place the client's accounting of a chain turns on the reading."""
    return own if FILLED_READING is FilledReading.CHAIN else own + earlier


def chain_quantity(own: Decimal, earlier: Decimal) -> Decimal:
    """The node order's quantity, from its live Alpaca order's `qty` (`own`) and the `filled_qty` of the
    orders before it (`earlier`), under `FILLED_READING`."""
    return own if FILLED_READING is FilledReading.CHAIN else own + earlier


@dataclasses.dataclass
class Leg:
    """One Alpaca order a node order has been, as last seen: its links, status and `filled_qty`."""

    order_id: str
    replaces: str | None = None
    replaced_by: str | None = None
    status: AlpacaOrderStatus | None = None
    filled: Decimal | None = None


@dataclasses.dataclass(frozen=True)
class Ending:
    """An end Alpaca showed for a node order, `canceled` or `expired`, with more filled than the node
    held: sent once the node holds `shown` (`AlpacaExecutionClient._end_after_fills`)."""

    event: str
    venue_order_id: VenueOrderId
    ts_event: int
    shown: Decimal


@dataclasses.dataclass
class OrderRecord:
    """Everything the client holds of one node order's replace chain, after upstream's Binance spot
    `OrderBinding` (`current`, `superseded`) and `PendingReplacement` (`crates/adapters/binance/src/common/
    dispatch.rs` at the pin): the Alpaca orders it has been (`legs`), the one it is on now (`current`), the
    ones it has left (`superseded`), the replaces sent and awaiting an outcome, counted by the Alpaca order
    each was sent to (`pending`), the replace answers not yet confirmed (`answers`), and the client's own count of its fills,
    its quantity, the trade IDs applied and an end waiting for its fills (`end`). It is built from the node's cache when first needed, as Binance
    spot's `restore_order` rebuilds a binding from `order.venue_order_ids()`, so nothing of it rests on what
    an earlier connect kept, and dropped when the order ends (`cleanup_terminal`)."""

    client_order_id: ClientOrderId
    current: str | None = None
    legs: dict[str, Leg] = dataclasses.field(default_factory=dict)
    superseded: set[str] = dataclasses.field(default_factory=set)
    pending: collections.Counter[str] = dataclasses.field(default_factory=collections.Counter)
    answers: dict[str, AlpacaOrder] = dataclasses.field(default_factory=dict)
    filled: Decimal = Decimal(0)
    quantity: Decimal = Decimal(0)
    trades: set[str] = dataclasses.field(default_factory=set)
    end: Ending | None = None

    def see(self, order: AlpacaOrder) -> Leg:
        """Record Alpaca's view of one of the order's legs. A leg seen replaced stays replaced, with the
        order that replaced it, whatever an older copy delivered later shows: an order is replaced once."""
        order_id = str(order.id)
        leg = self.legs.setdefault(order_id, Leg(order_id))
        if order.replaces is not None:
            leg.replaces = str(order.replaces)
        if leg.status == AlpacaOrderStatus.REPLACED and order.status != AlpacaOrderStatus.REPLACED:
            return leg
        leg.status = order.status
        if order.replaced_by is not None:
            leg.replaced_by = str(order.replaced_by)
        leg.filled = share_count(order.filled_qty)
        return leg

    def took_over(self, order_id: str) -> bool | None:
        """Whether a replacement took the order over, as the record shows the order it replaces: True if
        that order is replaced by it, False if replaced by another, None while the record does not know."""
        leg = self.legs.get(order_id)
        prior = self.legs.get(leg.replaces) if leg is not None and leg.replaces else None
        if prior is None or prior.status != AlpacaOrderStatus.REPLACED or prior.replaced_by is None:
            return None
        return prior.replaced_by == order_id

    def earlier_filled(self, order_id: str) -> Decimal:
        """The `filled_qty` last seen on the orders before `order_id` in the chain, for `chain_filled`."""
        total = Decimal(0)
        leg = self.legs.get(order_id)
        seen: set[str] = set()
        while leg is not None and leg.replaces is not None and leg.replaces not in seen:
            seen.add(leg.replaces)
            leg = self.legs.get(leg.replaces)
            if leg is not None and leg.filled is not None:
                total += leg.filled
        return total


def alpaca_side(order: AlpacaOrder) -> OrderSide:
    side = ORDER_SIDES_FROM_ALPACA.get(order.side) if order.side is not None else None
    if side is None:
        raise ValueError(f"side {getattr(order.side, 'value', order.side)}")
    return side


def is_open_order(order: AlpacaOrder) -> bool:
    """Whether Alpaca's order still works: not closed, and not replaced (its replacement works)."""
    return order.status not in CLOSED_STATUSES and order.status != AlpacaOrderStatus.REPLACED


def order_status_report(
    order: AlpacaOrder,
    ts_init: int,
    trigger_type: TriggerType | None = None,
    client_order_id: ClientOrderId | None = None,
    skipped: Callable[[str], None] | None = None,
    status: OrderStatus | None = None,
    filled: Decimal | None = None,
    quantity: Decimal | None = None,
    advisory_trigger: Callable[[str], None] | None = None,
    *,
    trailing: bool = False,
) -> OrderStatusReport | None:
    """An `OrderStatusReport` for one Alpaca order, None for a closed one this slice does not map, whose
    reason `skipped` is given. A trailing stop is mapped only when `trailing` is true: the caller has
    established own ownership/lineage or enabled `report_external_trailing_stops`.

    An open order this slice does not map raises, so reconciliation fails rather than leaving a working
    order unknown to the node. `replaced` is skipped: the order replacing it is reported in its own right,
    under `client_order_id` when the client has traced it back to the node's order. A stop's trigger type
    is `trigger_type` when the node holds the order, else `DEFAULT`: Alpaca's order has no trigger type
    field. A trailing stop also carries its PRICE or BASIS_POINTS offset. `status`, `filled` and
    `quantity` stand in for what Alpaca's order shows when the client judges
    a replace chain as a whole (`AlpacaExecutionClient._order_report`, `chain_filled`).
    """
    try:
        return mapped_order_report(
            order,
            ts_init,
            trigger_type,
            client_order_id,
            status,
            filled,
            quantity,
            advisory_trigger,
            trailing=trailing,
        )
    except ValueError as e:
        if is_open_order(order):
            raise ValueError(f"{order_description(order)} is open with {e}") from e
        if skipped is not None:
            skipped(str(e))
        return None


def mapped_order_report(
    order: AlpacaOrder,
    ts_init: int,
    trigger_type: TriggerType | None = None,
    client_order_id: ClientOrderId | None = None,
    status: OrderStatus | None = None,
    filled: Decimal | None = None,
    quantity: Decimal | None = None,
    advisory_trigger: Callable[[str], None] | None = None,
    *,
    trailing: bool = False,
) -> OrderStatusReport:
    """`order_status_report`'s report, raising `ValueError` with what this slice does not map, open or
    closed."""
    if order.status == AlpacaOrderStatus.REPLACED:
        raise ValueError("status replaced: its replacement is reported instead")
    nautilus_status = OPEN_STATUSES.get(order.status) or CLOSED_STATUSES.get(order.status)
    if nautilus_status is None:
        raise ValueError(f"status {order.status.value}")
    if order.asset_class not in (None, AssetClass.US_EQUITY):
        raise ValueError(f"asset class {getattr(order.asset_class, 'value', order.asset_class)}")
    if order.order_class not in (AlpacaOrderClass.SIMPLE, AlpacaOrderClass.EMPTY):
        raise ValueError(f"order class {order.order_class.value}")
    order_type = ORDER_TYPES_FROM_ALPACA.get(order.type) if order.type is not None else None
    if order_type is None or (order_type == OrderType.TRAILING_STOP_MARKET and not trailing):
        raise ValueError(f"order type {getattr(order.type, 'value', order.type)}")
    time_in_force = TIMES_IN_FORCE_FROM_ALPACA.get(order.time_in_force)
    if time_in_force is None:
        raise ValueError(f"time in force {order.time_in_force.value}")
    if order.symbol is None or order.qty is None:
        raise ValueError("no symbol or no share quantity (a notional order)")
    total = whole_quantity(quantity if quantity is not None else order.qty, "quantity")
    filled_qty = whole_quantity(
        filled if filled is not None else order.filled_qty or 0, "filled quantity"
    )
    any_filled = filled_qty.as_double() > 0
    trailing_offset: Decimal | None = None
    trailing_offset_type: TrailingOffsetType | None = None
    if order_type == OrderType.TRAILING_STOP_MARKET:
        if (order.trail_price is None) == (order.trail_percent is None):
            raise ValueError("trailing_stop needs exactly one of trail_price or trail_percent")
        field = "trail_price" if order.trail_price is not None else "trail_percent"
        trailing_offset = decimal_value(getattr(order, field), field)
        if trailing_offset <= 0:
            raise ValueError(f"{field} {trailing_offset} must be positive")
        if order.trail_price is not None:
            trailing_offset_type = TrailingOffsetType.PRICE
        else:
            # Alpaca trail_percent=1.0 is 1%; Nautilus BASIS_POINTS=100 is 1% (sources in header).
            trailing_offset *= 100
            trailing_offset_type = TrailingOffsetType.BASIS_POINTS
    trigger_price = None
    if order_type in REPORT_TRIGGER_TYPES and (
        order.stop_price is not None or order_type in STOP_TYPES
    ):
        try:
            trigger_price = exact_report_price(order.stop_price)
        except (ValueError, ArithmeticError, OverflowError) as e:
            if order_type != OrderType.TRAILING_STOP_MARKET:
                raise
            # Alpaca's moving trigger is advisory; the pinned trailing model permits None. Offset
            # validation above is required and remains a named failure (sources in the header).
            if advisory_trigger is not None:
                advisory_trigger(str(e))
    if status is None:
        # Open with fills is partly filled, as `pending_replace` with fills is at its own terms and as a
        # replacement of a partly filled order shows its chain's fills.
        status = (
            OrderStatus.PARTIALLY_FILLED
            if nautilus_status == OrderStatus.ACCEPTED and any_filled
            else nautilus_status
        )
    return OrderStatusReport(
        account_id=ACCOUNT_ID,
        instrument_id=InstrumentId(Symbol(order.symbol), VENUE),
        venue_order_id=VenueOrderId(str(order.id)),
        order_side=alpaca_side(order),
        order_type=order_type,
        time_in_force=time_in_force,
        order_status=status,
        quantity=total,
        filled_qty=filled_qty,
        ts_accepted=dt_to_unix_nanos(order.submitted_at),
        ts_last=dt_to_unix_nanos(order.updated_at),
        ts_init=ts_init,
        client_order_id=client_order_id or own_client_id(order),
        price=exact_report_price(order.limit_price) if order_type in LIMIT_PRICED else None,
        trigger_price=trigger_price,
        trigger_type=(trigger_type or TriggerType.DEFAULT)
        if order_type in REPORT_TRIGGER_TYPES
        else None,
        trailing_offset=trailing_offset,
        trailing_offset_type=trailing_offset_type,
        avg_px=decimal_value(order.filled_avg_price, "average fill price")
        if any_filled and order.filled_avg_price
        else None,
    )


def own_client_id(order: AlpacaOrder) -> ClientOrderId:
    """Alpaca's client order ID as NautilusTrader holds it, raising `ValueError` naming it when it cannot."""
    try:
        return ClientOrderId(order.client_order_id)
    except ValueError as e:
        raise ValueError(f"a client order ID NautilusTrader cannot hold ({e})") from e


def share_count(value: object) -> Decimal | None:
    """`value`, a quantity Alpaca sends as a string, as a finite, non-negative number of shares, or None
    if absent or unreadable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        quantity = Decimal(str(value))
    except ArithmeticError:
        return None
    return quantity if quantity.is_finite() and quantity >= 0 else None


def fill_time(activity: dict) -> int:
    """An activity's `transaction_time`, "The time at which the execution occurred", in Unix nanoseconds."""
    return dt_to_unix_nanos(datetime.fromisoformat(str(activity["transaction_time"])))


def fill_report(activity: dict, ts_init: int) -> FillReport:
    """A `FillReport` for one Alpaca `FILL` activity ("Retrieve Account Activities of Specific Type").

    The trade ID is the UUID after `::` in the activity's `id`, which on paper is the trade update's
    `execution_id` for the same fill (ADR 0024), so a fill seen on the stream and again here has one ID.
    The price is kept exactly as Alpaca writes it. Alpaca's fill has no commission and no liquidity side:
    the commission is zero dollars, which upstream's execution spec expects a stream that omits fees to
    show ("TC-E85"), and the side `NO_LIQUIDITY_SIDE`, as on the stream. An activity has no client order
    ID; the engine matches the report by `order_id`. Raises `ValueError` for one this slice cannot map.
    """
    activity_id = str(activity["id"])
    _, sep, execution_id = activity_id.partition("::")
    if not sep or not execution_id:
        raise ValueError(f"id {activity_id} is not in `::` form")
    side = {"buy": OrderSide.BUY, "sell": OrderSide.SELL, "sell_short": OrderSide.SELL}.get(
        activity["side"]
    )
    if side is None:
        raise ValueError(f"side {activity['side']}")
    symbol = str(activity["symbol"])
    if "/" in symbol:
        raise ValueError(f"symbol {symbol} is not a US equity")
    return FillReport(
        account_id=ACCOUNT_ID,
        instrument_id=InstrumentId(Symbol(symbol), VENUE),
        venue_order_id=VenueOrderId(str(activity["order_id"])),
        trade_id=TradeId(execution_id),
        order_side=side,
        last_qty=whole_quantity(activity["qty"], "fill quantity"),
        last_px=Price.from_str(str(activity["price"])),
        commission=Money(0, USD),
        liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
        ts_event=fill_time(activity),
        ts_init=ts_init,
    )


def report_with(
    report: FillReport,
    *,
    venue_order_id: VenueOrderId | None = None,
    client_order_id: ClientOrderId | None = None,
) -> FillReport:
    """`report` on another Alpaca order (a fill listed under an order since replaced, reported on the
    order that replaced it, `AlpacaExecutionClient._replaced_fills`), or carrying a node order's client
    order ID, which a fill activity does not (`AlpacaExecutionClient._unanswered_named`); every other
    field kept."""
    return FillReport(
        account_id=report.account_id,
        instrument_id=report.instrument_id,
        venue_order_id=venue_order_id or report.venue_order_id,
        trade_id=report.trade_id,
        order_side=report.order_side,
        last_qty=report.last_qty,
        last_px=report.last_px,
        commission=report.commission,
        liquidity_side=report.liquidity_side,
        ts_event=report.ts_event,
        ts_init=report.ts_init,
        client_order_id=client_order_id or report.client_order_id,
        venue_position_id=report.venue_position_id,
        avg_px=report.avg_px,
    )


def fills_count(reports: list[FillReport]) -> Decimal:
    """The shares a list of fill reports adds up to."""
    return sum((r.last_qty.as_decimal() for r in reports), Decimal(0))


def fill_terms(report: FillReport) -> tuple:
    """What a fill report says of its fill, without its own report ID: two activities with one trade ID
    and the same terms are one fill listed twice."""
    return (
        report.instrument_id,
        report.order_side,
        report.last_qty,
        report.last_px,
        report.ts_event,
    )


def activity_order_id(activity: object) -> str | None:
    """The Alpaca order ID a fill activity names, if it names one."""
    order_id = activity.get("order_id") if isinstance(activity, dict) else None
    return str(order_id) if order_id is not None else None


def position_status_report(position: AlpacaPosition, ts_init: int) -> PositionStatusReport:
    """A `PositionStatusReport` for one Alpaca US equity position.

    The direction is taken from `side`; Alpaca documents `qty` as "The number of shares" without saying
    whether a short is negative, so its magnitude is used. Alpaca gives positions no timestamp, so
    `ts_last` is the time of the request.
    """
    side = {
        AlpacaPositionSide.LONG: PositionSide.LONG,
        AlpacaPositionSide.SHORT: PositionSide.SHORT,
    }[position.side]
    return PositionStatusReport(
        account_id=ACCOUNT_ID,
        instrument_id=InstrumentId(Symbol(position.symbol), VENUE),
        position_side=side,
        quantity=whole_quantity(
            abs(decimal_value(position.qty, "position quantity")),
            f"{position.symbol} position quantity",
        ),
        ts_last=ts_init,
        ts_init=ts_init,
        avg_px_open=decimal_value(position.avg_entry_price, "average entry price"),
    )


class AlpacaExecutionClient(ExecutionClient):
    """Send paper equity orders, including opt-in venue trailing-stop SELLs, and report their events."""

    def __init__(
        self,
        *,
        name: str,
        config: ExecutionClientConfig,
        cache: ClientCache,
        clock: Clock,
        trader_id: TraderId,
        instrument_provider: AlpacaInstrumentProvider,
        trading_client: OrdersClient | None = None,
        stream: TradeUpdateStream | None = None,
    ) -> None:
        """Retain configuration and local state. The alpaca-py clients are built in `_connect`.

        Only an `AlpacaExecutionClientConfig` is accepted, so the paper-only check always runs.
        """
        if not isinstance(config, AlpacaExecutionClientConfig):
            raise TypeError(
                f"AlpacaExecutionClient takes an AlpacaExecutionClientConfig, got {type(config).__name__}: "
                f"its checks hold the rule {PAPER_ONLY_RULE}",
            )
        super().__init__(
            name=name,
            config=config,
            cache=cache,
            clock=clock,
            trader_id=trader_id,
            venue=VENUE,
            account_id=ACCOUNT_ID,
            account_type=AccountType.CASH,
            oms_type=OmsType.NETTING,
            base_currency=USD,
            instrument_provider=instrument_provider,
        )
        self._provider = instrument_provider
        self._report_external_orders = config.report_external_orders
        self._sell_to_close_on_long_exits = config.sell_to_close_on_long_exits
        self._reduce_only_sells = config.reduce_only_sells
        self._sell_to_close_order_types = frozenset(config.sell_to_close_order_types)
        self._report_external_trailing_stops = config.report_external_trailing_stops
        self._submit_trailing_stops = config.submit_trailing_stops
        self._session_clock: tuple[AlpacaClock, tuple[float, float]] | None = None
        """Alpaca's last clock answer and this loop's time just before it was asked for."""
        # Companion fills while the core has not yet materialized an outside order. They are not
        # marked applied: only the engine's cache can confirm a report's fills (ADR 0075).
        self._external_fills: dict[str, dict[str, FillReport]] = {}
        self._external_reconciliation: set[str] = set()
        self._external_unloaded_symbols: set[str] = set()
        self._advisory_trigger_warned: set[str] = set()
        # Clients given here are the caller's: used on every connect, never replaced by ones built from
        # the environment, and not dropped at disconnect (the adapter guide closes what the adapter
        # creates in `_connect`). The live fields below are set in `_connect` and cleared on disconnect.
        self._given_trading = trading_client
        self._given_stream = stream
        # A given stream serves one connect: alpaca-py's stream keeps its handlers and its websocket
        # between runs, and a run abandoned at a timed-out disconnect may still be using it.
        self._given_stream_used = False
        self._trading: OrdersClient | None = None
        self._stream: TradeUpdateStream | None = None
        self._stream_thread: threading.Thread | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        # The clock reading the last replacement's client order ID carries (`_replacement_stamp`).
        self._last_replacement_stamp = 0
        # One alpaca-py call at a time on its one requests.Session (see data.py); rebuilt in `_connect`.
        self._rest_lock = asyncio.Lock()
        # Orders this client has seen Alpaca acknowledge, and orders it has stopped tracking. The
        # latter's snapshots wait for cache closure before additional fills can be reconciled.
        self._accepted: set[ClientOrderId] = set()
        self._finished: set[ClientOrderId] = set()
        # Closed orders retry after a spacing, even with unchanged fills. An open cache must first
        # close. Replace this dictionary on disconnect so old holds cannot write into a new session.
        self._report_hold_failures: dict[ClientOrderId, ReportHoldFailure] = {}
        self._report_rereads: dict[ClientOrderId, asyncio.Task[None]] = {}
        # One record per node order with a replace chain or fills to count (`OrderRecord`), built from
        # the node's cache when first needed: the cache takes a generated event later, not when it is
        # generated (observed), so an order's terms and end are judged on the record.
        self._records: dict[ClientOrderId, OrderRecord] = {}
        # Each Alpaca order ID traced, mapped to the client order ID its chain starts from, held by the
        # node or not (None for none NautilusTrader can hold): the index the node's cache and a walk of
        # `replaces` fill on a miss.
        self._index: dict[str, ClientOrderId | None] = {}
        # Alpaca order IDs read for `_unanswered_named` and found not to be a node order waiting on its
        # submit's answer; an order's client order ID does not change, so each is read once.
        self._not_waiting: set[str] = set()
        # Failed identity attempts, counted per Alpaca order until a read establishes its owner.
        self._unanswered_reads: collections.Counter[str] = collections.Counter()
        # Alpaca's acknowledgement of an order that was pending cancel or update when it came, kept for
        # the refusal that puts the order back to `SUBMITTED` (`_accept`).
        self._acknowledged: dict[ClientOrderId, tuple[VenueOrderId, int]] = {}
        # The later reads of fill activities for a node order whose fills Alpaca shows and does not list
        # yet (`_catch_up_later`), one per order, ended at disconnect.
        self._catching_up: dict[ClientOrderId, asyncio.Task[None]] = {}
        self._catch_up_targets: dict[ClientOrderId, Decimal] = {}
        self._retained_catch_up: set[ClientOrderId] = set()
        # Ends waiting for their fills when the client last disconnected, resumed at the next connect.
        self._waiting_ends: dict[ClientOrderId, Ending] = {}
        # Trade updates, applied in arrival order by one task on the node's loop.
        self._updates: asyncio.Queue[TradeUpdate] = asyncio.Queue()
        self._consumer: asyncio.Task[object] | None = None
        # Set while that task is not applying an update; a new one with each connect, so a task left
        # over from an earlier connect sets only its own.
        self._idle = asyncio.Event()
        self._idle.set()
        self._log = Logger(type(self).__name__)

    # -- lifecycle ---------------------------------------------------------------------------------

    @override
    async def _connect(self) -> None:
        await logged_failure(self._log, "execution client failed to connect", self._connect_venue())

    async def _connect_venue(self) -> None:
        if self._owner_loop is not self.loop:
            # An `asyncio.Lock` serves one loop. On the same loop it is kept: a call a cancelled hook left
            # running holds it until the call returns, so a given trading client, used again by this
            # connect, never has two calls on its one `requests.Session` at once.
            self._rest_lock = asyncio.Lock()
        self._owner_loop = self.loop
        self._stopping = False
        if self._given_stream is not None and self._given_stream_used:
            raise RuntimeError(
                "a trade updates stream given to the constructor serves one connect; build a new client "
                "to connect again"
            )
        self._trading, self._stream = self._given_trading, self._given_stream
        if self._trading is None or self._stream is None:
            api_key, secret_key = credentials_from_env()
            if self._trading is None:
                self._trading = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
            if self._stream is None:
                self._stream = TradingStream(api_key=api_key, secret_key=secret_key, paper=True)
        with_http_timeout(
            self._trading
        )  # a client passed in too; refused without a session (ADR 0039)
        send_order_commands_once(self._trading)  # refused the same way
        await self._provider.initialize()
        # Only what the cache does not hold. The node connects its data clients and puts their
        # instruments into the cache before it connects an execution client ("so that instruments are
        # in the cache before execution clients read them", `crates/live/src/node/mod.rs:35-48` at the
        # pin), and every instrument handed to the node is one more notification on this client's
        # command queue of 1,024, refused past that (the README, "Instrument load and the command queue").
        held = set(self.cache.instrument_ids(VENUE))
        for instrument in self._provider.list_all():
            if instrument.id not in held:
                self._handle_instrument(instrument)
        await self._report_account()
        self._updates = asyncio.Queue()
        self._idle = asyncio.Event()
        self._idle.set()
        self._consumer = self.create_task(
            self._consume_updates(self._updates, self._idle), "alpaca-trade-updates"
        )
        self._consumer.add_done_callback(retrieved)
        self._stream.subscribe_trade_updates(weak_coroutine(self._on_trade_update))
        self._stream_thread = threading.Thread(
            target=run_trade_stream,
            args=(self._stream, self._owner_loop, weak_callback(self._on_stream_exit)),
            name=f"{self.client_id}-trade-updates",
            daemon=True,
        )
        # Used from here on: a connect that failed before this point never ran it.
        self._given_stream_used = self._given_stream is not None
        self._stream_thread.start()
        self._resume_waiting_ends()

    def _resume_waiting_ends(self) -> None:
        """Give each end the last disconnect left waiting for its fills the rest of its reads: Alpaca no
        longer lists the order open, so the paper node's checks would never ask for it again."""
        waiting, self._waiting_ends = self._waiting_ends, {}
        for client_order_id, ending in waiting.items():
            order = self.cache.order(client_order_id)
            if order is None or order.is_closed:
                continue
            self._record(order).end = ending
            self._catch_up_later(client_order_id, ending.shown, ending.venue_order_id)

    @override
    async def _disconnect(self) -> None:
        await logged_failure(
            self._log, "execution client failed to disconnect", self._disconnect_venue()
        )

    async def _disconnect_venue(self) -> None:
        self._stopping = True
        thread = self._stream_thread
        if thread is not None and self._stream is not None:
            self._stream.stop()
        # The thread's join and the task's drain run together, so the client's own worst case is the longer
        # of the join and the drain (`TASK_GRACE_SECS` plus `TASK_ABORT_SECS`). Updates that still arrive
        # are dropped while stopping (`_enqueue_update`).
        consumer, self._consumer = self._consumer, None
        queue, idle = self._updates, self._idle
        waits: list[Awaitable[object]] = []
        if thread is not None:
            waits.append(join_within(thread, STREAM_JOIN_TIMEOUT_SECS))
        if consumer is not None:
            waits.append(drain(consumer, idle))
        await asyncio.gather(*waits)
        consumer_ended = consumer is None or consumer.done()
        while not queue.empty():  # so a task that outlived its cancel finds nothing left to apply
            queue.get_nowait()
        # Cleared whether or not the thread stopped, so a later connect builds fresh clients.
        self._stream_thread = None
        self._stream = None
        self._trading = None
        self._accepted.clear()
        self._finished.clear()
        self._report_hold_failures = {}
        self._waiting_ends = {
            cid: record.end for cid, record in self._records.items() if record.end is not None
        }
        self._records.clear()
        self._external_fills.clear()
        self._external_reconciliation.clear()
        self._external_unloaded_symbols.clear()
        self._advisory_trigger_warned.clear()
        self._index.clear()
        self._unanswered_reads.clear()
        self._acknowledged.clear()
        catching_up, self._catching_up = list(self._catching_up.values()), {}
        self._catch_up_targets.clear()
        self._retained_catch_up.clear()
        rereads, self._report_rereads = list(self._report_rereads.values()), {}
        for task in (*catching_up, *rereads):
            task.cancel()
        await asyncio.gather(*catching_up, *rereads, return_exceptions=True)
        if thread is not None and thread.is_alive():
            raise RuntimeError(
                f"Alpaca trade updates thread did not stop within {STREAM_JOIN_TIMEOUT_SECS} s",
            )
        if not consumer_ended:
            raise RuntimeError(
                "Alpaca trade updates task did not end within "
                f"{TASK_GRACE_SECS + TASK_ABORT_SECS} s of the disconnect",
            )

    @override
    async def _query_account(self, command: live.QueryAccount) -> None:
        """Read Alpaca's account and report its state, as at connect (Python adapter guide, "Query":
        `_query_account`). A query for an account this client does not hold is refused with a log line."""
        if command.account_id is not None and command.account_id != ACCOUNT_ID:
            self._log.error(
                f"Account query for {command.account_id}: this client holds {ACCOUNT_ID} only; nothing read"
            )
            return
        try:
            await self._report_account()
        except (APIError, OSError, ValueError, TypeError, RuntimeError, ArithmeticError) as e:
            # A failed read, a raw, incomplete or unparsable answer (each named), or a client not
            # connected: the state is left as it was.
            self._log.error(
                f"Alpaca account query failed: {e!r}; the account state is left as it was"
            )

    async def _report_account(self) -> None:
        account = await self._call(self._venue().get_account)
        if not isinstance(account, TradeAccount):
            raise TypeError(
                "alpaca-py returned raw account data; the client must use raw_data=False"
            )
        # Both are optional in alpaca-py's `TradeAccount`: a missing one is refused by name, not read as
        # USD or $0.
        if account.currency is None:
            raise ValueError("Alpaca's account answer has no currency")
        if account.currency != "USD":
            raise ValueError(f"Alpaca account currency is {account.currency}, not USD")
        if account.cash is None:
            raise ValueError("Alpaca's account answer has no cash")
        amount = decimal_value(account.cash, "cash")
        cents = amount.quantize(Decimal("0.01"))
        if cents != amount:
            # `Money` holds USD at 2 decimals and `Money.from_str` would round without a word.
            self._log.warning(f"Alpaca cash {amount} is reported to NautilusTrader as {cents} USD")
        cash = Money.from_str(f"{cents} USD")
        self.generate_account_state(
            balances=[AccountBalance(cash, Money(0, USD), cash)],
            margins=[],
            reported=True,
            ts_event=self.clock.timestamp_ns(),
            info={
                "account_number": account.account_number,
                "status": account.status.value,
                "equity": account.equity,
                "buying_power": account.buying_power,
                "non_marginable_buying_power": account.non_marginable_buying_power,
                "trading_blocked": account.trading_blocked,
            },
        )

    # -- order commands ----------------------------------------------------------------------------

    @override
    async def _submit_order(self, command: live.SubmitOrder) -> None:
        order = command.order
        extended_hours, denial = extended_hours_param(command.params)
        denial = denial or self._denial(order)
        if denial is None and extended_hours:
            denial = extended_hours_denial(order)
        if denial is None and order.order_type == OrderType.TRAILING_STOP_MARKET:
            # The one await before `OrderSubmitted`. The node hands this client its commands one at
            # a time, so a cancel sent meanwhile runs after this submit and cancels the order at
            # Alpaca (`test_a_cancel_sent_during_the_clock_read_...`).
            denial = await self._trailing_session_denial()
        if denial is not None:
            self.generate_order_denied(order, denial)
            return
        long_exit = (
            self._sell_to_close_on_long_exits
            and order.side == OrderSide.SELL
            and order.order_type in self._sell_to_close_order_types
            and sum(
                (
                    position.signed_qty
                    for position in self.cache.positions_open(
                        instrument_id=order.instrument_id, account_id=ACCOUNT_ID
                    )
                ),
                Decimal(0),
            )
            > 0
        )
        request = self._order_request(order, extended_hours, sell_to_close=long_exit)
        venue = self._venue()  # raises before `OrderSubmitted` when not connected: nothing was sent
        self.generate_order_submitted(order)
        try:
            placed = await self._call(venue.submit_order, request, once=True)
        except APIError as e:
            if e.status_code in DEFINITIVE_SUBMIT_REFUSALS:
                await self._refused(order, e)
                return
            self._log_unknown_outcome(
                order.client_order_id, f"HTTP {e.status_code}: {api_error_text(e)}"
            )
            return
        except OSError as e:
            if not_sent(e):
                # A connect timeout on the call's only attempt: nothing reached Alpaca (ADR 0039).
                self._reject(
                    order,
                    not_sent_reason(e),
                    self.clock.timestamp_ns(),
                )
                return
            self._log_unknown_outcome(order.client_order_id, repr(e))
            return
        if not isinstance(placed, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        if placed.status == AlpacaOrderStatus.REJECTED:
            self._reject(
                order,
                "Alpaca rejected the order on submission",
                dt_to_unix_nanos(placed.updated_at),
            )
            return
        self._accept(order, VenueOrderId(str(placed.id)), dt_to_unix_nanos(placed.submitted_at))

    async def _trailing_session_denial(self) -> str | None:
        """Why a trailing stop is not sent now, or None: Alpaca's clock must say the regular session is
        open, never an extended-hours flag or a local weekday estimate.

        One answer is reused until the boundary it names (`next_close` while open, `next_open` while
        closed): one request a session, not one a stop. Its age is measured from before the read and
        is the longer of two: this process's monotonic clock, which stops while the machine sleeps
        (`time.get_clock_info("monotonic")` on macOS), and the wall clock, which does not but can be
        set back. So an answer kept across a sleep is not trusted past the close it names. A read
        that fails denies by name and is not kept, so the next order asks again; nothing is assumed
        open.
        """
        loop = asyncio.get_running_loop()

        def age(asked: tuple[float, float]) -> float:
            return max(loop.time() - asked[0], time.time() - asked[1])

        kept = self._session_clock
        if kept is not None:
            clock, asked = kept
            boundary = clock.next_close if clock.is_open else clock.next_open
            if age(asked) >= (boundary - clock.timestamp).total_seconds():
                kept = self._session_clock = None
        if kept is None:
            asked = (loop.time(), time.time())
            try:
                clock = await self._call(self._venue().get_clock)
                if not isinstance(clock, AlpacaClock):
                    raise TypeError("alpaca-py clock must use raw_data=False")
            except (APIError, OSError, ValueError, TypeError, AttributeError) as e:
                return f"trailing stop regular session could not be verified ({failure_cause(e)})"
            self._session_clock = (clock, asked)
        remaining = (clock.next_close - clock.timestamp).total_seconds()
        if not clock.is_open or age(asked) >= remaining:
            return "trailing stop refused: regular session is not open (Alpaca clock)"
        return None

    async def _refused(self, order: Any, error: APIError) -> None:
        """Reject an order Alpaca refused, unless Alpaca holds it.

        Alpaca refuses a submit whose `client_order_id` it already holds, 422 "client_order_id must be
        unique" (paper, 2026-11-10), so a refusal does not prove the order absent. The submit itself is
        sent once (`without_resend`), so the order is looked up first: if Alpaca holds it, it is accepted, not
        rejected; if the lookup fails, the outcome is unknown.
        """
        client_order_id = order.client_order_id
        if client_order_id in self._accepted or self._ended(client_order_id):
            return  # a trade update has already shown that Alpaca holds it
        try:
            held = await self._call(self._venue().get_order_by_client_id, client_order_id.value)
        except APIError as e:
            if e.status_code != 404:
                self._log_unknown_outcome(
                    client_order_id,
                    f"refused ({error.status_code}), then the lookup failed (HTTP {e.status_code})",
                )
                return
            held = None
        except (OSError, ValueError, TypeError) as e:
            # A transport error, or a body alpaca-py cannot build: pydantic's `ValidationError` is a
            # `ValueError`, an empty or non-object body a `TypeError` (`Order(**answer)`), as in
            # `_orders_read`.
            self._log_unknown_outcome(
                client_order_id, f"refused ({error.status_code}), then the lookup failed ({e!r})"
            )
            return
        if client_order_id in self._accepted or self._ended(client_order_id):
            return  # a trade update applied during the lookup has settled it
        if isinstance(held, AlpacaOrder):
            self._log.warning(
                f"Alpaca refused a submit of {client_order_id} ({error.status_code}) but holds the "
                f"order: an earlier attempt reached it (status {held.status.value})",
            )
            if held.status == AlpacaOrderStatus.REJECTED:
                self._reject(
                    order,
                    f"Alpaca rejected the order (status rejected, after a refused submit "
                    f"{error.status_code}: {api_error_text(error)})",
                    dt_to_unix_nanos(held.updated_at),
                )
                return
            self._accept(order, VenueOrderId(str(held.id)), dt_to_unix_nanos(held.submitted_at))
            if not is_open_order(held):
                # Closed already, so its update may never come: Alpaca's read goes to the engine as a
                # report, as a `QueryOrder` answer does (`_query_order`).
                try:
                    await self._send_report(held)
                except HeldReportError:
                    pass  # the hold already logged the omitted report
                except APIError as e:
                    self._log.warning(
                        f"Alpaca report for refused submit {client_order_id} omitted "
                        f"({failure_cause(e)})"
                    )
            return
        self._reject(
            order,
            f"Alpaca refused the order ({error.status_code}): {api_error_text(error)}",
            self.clock.timestamp_ns(),
        )

    @override
    async def _cancel_order(self, command: live.CancelOrder) -> None:
        await self._cancel_logged(command.client_order_id, command.venue_order_id)

    def _pending_cancel(self, client_order_id: ClientOrderId) -> bool:
        order = self.cache.order(client_order_id)
        return order is not None and order.status == OrderStatus.PENDING_CANCEL

    async def _cancel_logged(
        self, client_order_id: ClientOrderId, venue_order_id: VenueOrderId | None
    ) -> None:
        try:
            await self._cancel(client_order_id, venue_order_id)
        except (ValueError, TypeError, RuntimeError) as e:
            # `_cancel` handles Alpaca's answers itself; these are a raw or unparsable answer (pydantic's
            # ValidationError is a ValueError) and a client not connected. Logged with no event, so a
            # cancel-all or the default batch cancel goes on to the next order.
            self._log.error(
                f"Alpaca cancel of {client_order_id} failed ({e!r}); the order is left as it is"
            )

    @override
    async def _cancel_all_orders(self, command: live.CancelAllOrders) -> None:
        """Cancel, one at a time by ID, every open or in-flight order the node holds for the command's
        instrument, and side if it names one.

        Alpaca's "Delete All Orders" cancels every open order of the account, whatever its symbol, so it
        is not used: as upstream's Kraken spot adapter does for its account-wide cancel-all, the matching
        orders are taken from the cache and canceled by ID. In-flight orders are included because Alpaca
        may hold an order the node still has as `SUBMITTED`. An order already `PENDING_CANCEL` is canceled
        again, as Kraken spot, Binance spot, Bybit and Interactive Brokers select every open order: its first
        cancel may have had no answer and the order may still work at Alpaca. A strategy sends this command only with `strategy_only=False`, and
        then means every strategy's orders, so none is filtered by strategy. A failure on one order is
        logged and the rest are still canceled.
        """
        selected: dict[ClientOrderId, None] = {}
        for found in (self.cache.orders_open, self.cache.orders_inflight):
            for order in found(instrument_id=command.instrument_id, side=command.order_side):
                selected.setdefault(order.client_order_id)
        if not selected:
            self._log.info(f"No open or in-flight {command.instrument_id} orders to cancel")
            return
        pending = [c for c in selected if self._pending_cancel(c)]
        for client_order_id in [c for c in selected if c not in pending] + pending:
            # Read again at its turn: the cancels before it took an HTTP round trip each, in which this
            # order may have filled, closed or been sent a cancel of its own.
            order = self.cache.order(client_order_id)
            if order is None or order.is_closed:
                continue
            await self._cancel_logged(client_order_id, order.venue_order_id)

    async def _cancel(
        self, client_order_id: ClientOrderId, venue_order_id: VenueOrderId | None
    ) -> None:
        """Cancel one order by Alpaca's order ID, looking it up by client order ID if the node has none."""
        order = self.cache.order(client_order_id)
        if order is None:
            self._log.error(f"Cannot cancel {client_order_id}: the node holds no such order")
            return
        venue_order_id = self._known_venue_id(client_order_id, venue_order_id, order.venue_order_id)
        try:
            if venue_order_id is None:
                found = await self._call(
                    self._venue().get_order_by_client_id, client_order_id.value
                )
                if not isinstance(found, AlpacaOrder):
                    raise TypeError("alpaca-py returned raw order data")
                venue_order_id = VenueOrderId(str(found.id))
            await self._call(self._venue().cancel_order_by_id, venue_order_id.value, once=True)
        except APIError as e:
            if e.status_code == NOT_CANCELABLE and venue_order_id is not None:
                await self._cancel_refused(order, venue_order_id, e)
                return
            self._log.error(
                f"Alpaca cancel for {client_order_id} got no definitive answer "
                f"(HTTP {e.status_code}: {api_error_text(e)}); the order is left as it is",
            )
            return
        except OSError as e:
            if not_sent(e):
                # A connect timeout on the cancel's only attempt: it never reached Alpaca (ADR 0039).
                if self._ended(client_order_id):
                    return  # an update applied during the attempt ended it
                self.generate_order_cancel_rejected(
                    order,
                    venue_order_id,
                    not_sent_reason(e),
                    self.clock.timestamp_ns(),
                )
                self._refusal_settled(order)
                return
            self._log.error(
                f"Alpaca cancel for {client_order_id} got no answer ({e!r}); the order is left as it is",
            )
        # Answered 204: the cancel is confirmed by Alpaca's `canceled` trade update.

    async def _cancel_refused(
        self, order: Any, venue_order_id: VenueOrderId, error: APIError
    ) -> None:
        """Reject a cancel Alpaca refused, unless Alpaca shows it pending cancel or canceled already.

        A 422 can answer a cancel of an order a cancel before it, or Alpaca, already took. If Alpaca shows
        the order pending cancel, the `canceled` trade update confirms it and nothing is generated here;
        if the lookup fails, nothing is either. If it shows the order canceled, the `canceled` update may
        never come (a dropped stream sends none), so the order is canceled from what Alpaca shows
        (`_canceled_unseen`). If it shows an unacknowledged order filled, `_send_report` brings its
        actual fills: a retained submit has no further automatic in-flight queries.
        """
        try:
            held = await self._call(self._venue().get_order_by_id, venue_order_id.value)
        except (APIError, OSError, ValueError, TypeError) as e:  # the classes `_refused` takes
            self._log.error(
                f"Alpaca refused the cancel of {order.client_order_id} ({error.status_code}) and the "
                f"lookup failed ({failure_cause(e)}); the order is left as it is",
            )
            return
        if self._ended(order.client_order_id):
            return  # an update applied during the lookup ended it: nothing is left to refuse
        if isinstance(held, AlpacaOrder) and held.status == AlpacaOrderStatus.FILLED:
            # The cancel met a fill: the stream's `fill` ends the order, or, if it was lost, the engine's
            # in-flight check reads the order through `_query_order`. A retained unanswered submit has
            # no more automatic queries at the pin, so use this read to bring its real trade-ID fills.
            if order.venue_order_id is None:
                self._accept(order, VenueOrderId(str(held.id)), dt_to_unix_nanos(held.submitted_at))
                await self._send_report(held)
                if not self._ended(order.client_order_id):
                    shown = share_count(held.filled_qty)
                    if shown is not None and self._held(order, self._record(order)) < shown:
                        self._catch_up_later(
                            order.client_order_id,
                            shown,
                            VenueOrderId(str(held.id)),
                            keep_reading=True,
                        )
            self._log.info(
                f"Alpaca refused the cancel of {order.client_order_id}: the order is filled; its fill "
                "settles it"
            )
            return
        if isinstance(held, AlpacaOrder) and held.status == AlpacaOrderStatus.CANCELED:
            await self._canceled_unseen(order.client_order_id, held)
            return
        if isinstance(held, AlpacaOrder) and held.status == AlpacaOrderStatus.PENDING_CANCEL:
            return
        if (
            isinstance(held, AlpacaOrder)
            and held.status == AlpacaOrderStatus.REPLACED
            and held.replaced_by is not None
        ):
            # The cancel named the order a replace has just replaced; cancel the replacement instead.
            await self._cancel(order.client_order_id, VenueOrderId(str(held.replaced_by)))
            return
        self.generate_order_cancel_rejected(
            order,
            venue_order_id,
            f"Alpaca refused the cancel ({error.status_code}): {api_error_text(error)}",
            self.clock.timestamp_ns(),
        )
        self._refusal_settled(order)

    async def _canceled_unseen(self, client_order_id: ClientOrderId, held: AlpacaOrder) -> None:
        """`OrderCanceled` for the node's order when Alpaca shows the Alpaca order that is now the node's
        canceled, as upstream's Bybit adapter emits `OrderCanceled` from an order it reads canceled
        (`cancel_all_orders`, `crates/adapters/bybit/src/execution.rs` at the pinned commit), stamped
        with Alpaca's `canceled_at`, else `updated_at`.

        The Alpaca order is the node's when the record names it current, or when its `replaces` chain
        leads to the node's order and the record has not left it (a replacement the node has not adopted
        yet, which a cancel redirected from its replaced order reaches). Bybit emits the cancel whatever the
        order filled; here the chain's filled shares as Alpaca shows them (`chain_filled`) must equal the
        node's count, since `_finish` drops every later update for it, so a fill the stream also missed
        would be lost: otherwise the order is left, with a warning, for reconciliation, whose report on it
        waits for that fill (`_settled_report`). The order is then finished, so a `canceled` update arriving late adds nothing. A node order
        already closed generates nothing."""
        order = self.cache.order(client_order_id)
        if order is None or order.is_closed or client_order_id in self._finished:
            return
        record = self._record(order)
        held_id = str(held.id)
        if held_id != record.current:
            if held_id in record.superseded:
                self._log.info(
                    f"Alpaca shows {held_id} canceled, which {client_order_id} no longer names "
                    "(replaced); ignored"
                )
                return
            try:
                root = await self._root_id(held)
            except (APIError, OSError, ValueError, TypeError) as e:
                self._log.warning(
                    f"Alpaca shows {held_id} canceled and tracing it failed ({failure_cause(e)}); "
                    f"{client_order_id} is left for reconciliation"
                )
                return
            if root != client_order_id:
                self._log.warning(
                    f"Alpaca shows {held_id} canceled, which is not {client_order_id}'s order; "
                    "the order is left for reconciliation"
                )
                return
            if client_order_id in self._finished:
                return  # an update applied during the trace ended it
        self._see(record, held)
        shown = share_count(held.filled_qty)
        filled = (
            chain_filled(shown, record.earlier_filled(held_id) if held.replaces else Decimal(0))
            if shown is not None
            else None
        )
        if filled is None or filled != max(record.filled, order.filled_qty.as_decimal()):
            self._log.warning(
                f"Alpaca shows {held_id} canceled with {held.filled_qty} filled, where "
                f"{client_order_id} has {order.filled_qty}; the order is left for reconciliation"
            )
            return
        self._finish(client_order_id)
        self.generate_order_canceled(
            order, VenueOrderId(held_id), dt_to_unix_nanos(held.canceled_at or held.updated_at)
        )

    @override
    async def _modify_order(self, command: live.ModifyOrder) -> None:
        """Replace the order on Alpaca ("Replace Order by ID", `TradingClient.replace_order_by_id`).

        Alpaca answers with "The new Order object with the new order ID" and says a success "does NOT
        guarantee the existing open order has been replaced". So `OrderUpdated` is not generated from the
        answer: it comes with Alpaca's `replaced` trade update for the node's order, which carries the old
        order with `replaced_by` (observed on paper, ADR 0042). The answer is kept for that update's values,
        and the replacement is mapped to the node's order by its Alpaca order ID. Alpaca refuses a
        replacement that keeps the original client order ID (observed), so a new one of the same form
        is named (`replacement_client_order_id`). Whether the replacement carries it is checked
        where a replacement is adopted (`_adopt`), whichever way it got there.
        """
        order = self.cache.order(command.client_order_id)
        if order is None:
            self._log.error(
                f"Cannot modify {command.client_order_id}: the node holds no such order"
            )
            return
        venue_order_id = self._known_venue_id(
            order.client_order_id, command.venue_order_id, order.venue_order_id
        )
        denial = self._modify_denial(order, command, venue_order_id)
        if (
            denial is None
            and order.order_type == OrderType.TRAILING_STOP_MARKET
            and "trailing_offset" in (command.params or {})
        ):
            # A trail change only: a quantity replace of a resting stop needs no open session.
            denial = await self._trailing_session_denial()
        if denial is not None or venue_order_id is None:
            self.generate_order_modify_rejected(
                order, venue_order_id, denial or "no venue order ID", self.clock.timestamp_ns()
            )
            self._refusal_settled(order)
            return
        request = ReplaceOrderRequest(
            qty=int(command.quantity.as_double()) if command.quantity is not None else None,
            # "Required if original order's `type` field was `limit` or `stop_limit`" (Replace Order by
            # ID), so the current price is sent when the command does not change it.
            # On Alpaca's increment, as a submit is (`venue_limit_price`); the `replaced` update's
            # `OrderUpdated` then carries the replacement's price, the one sent.
            limit_price=wire_price(venue_limit_price(command.price or order.price, order.side))
            if order.order_type in LIMIT_PRICED
            else None,
            stop_price=wire_price(command.trigger_price or order.trigger_price)
            if order.order_type in STOP_TYPES
            else None,
            trail=venue_trail(command.params["trailing_offset"], order.trailing_offset_type)
            if order.order_type == OrderType.TRAILING_STOP_MARKET
            and command.params
            and "trailing_offset" in command.params
            else None,
            # A new ID of the order's own form, so the replacement reads as the order did to whoever
            # reads the account by client order ID; never the order's own, which Alpaca refuses.
            client_order_id=replacement_client_order_id(
                order.client_order_id.value, self._replacement_stamp()
            ),
        )
        record = self._record(order)
        record.pending[venue_order_id.value] += 1
        try:
            answer = await self._call(
                self._venue().replace_order_by_id, venue_order_id.value, request, once=True
            )
        except APIError as e:
            if e.status_code == REPLACE_REFUSAL:
                await self._replace_refused(order, venue_order_id, request, e)
                return
            self._log_unknown_replace(order, f"HTTP {e.status_code}: {api_error_text(e)}")
            return
        except OSError as e:
            if not_sent(e):
                # A connect timeout on the replace's only attempt: it never reached Alpaca (ADR 0039).
                self._modify_rejected(
                    order,
                    venue_order_id.value,
                    venue_order_id,
                    not_sent_reason(e),
                    self.clock.timestamp_ns(),
                )
                return
            self._log_unknown_replace(order, repr(e))
            return
        if not isinstance(answer, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        answer_id = str(answer.id)
        self._index[answer_id] = order.client_order_id
        if self._records.get(order.client_order_id) is not record:
            return  # the order ended while the replace was answered; its updates are dropped
        if answer_id not in record.legs:  # the answer is older than any update of the replacement
            self._see(record, answer)
        if answer_id != record.current and answer_id not in record.superseded:
            record.answers[answer_id] = answer  # `replaced` takes it; adopted already, not kept

    def _replacement_stamp(self) -> int:
        """The node's clock in nanoseconds, and never the same reading twice in one run: two replaces in
        one reading of the clock still get two IDs. Across runs it rests on that clock not having
        stepped back past an earlier replace of the same order; if it has, Alpaca refuses the ID as not
        unique and the modify is rejected by name."""
        self._last_replacement_stamp = max(
            self.clock.timestamp_ns(), self._last_replacement_stamp + 1
        )
        return self._last_replacement_stamp

    async def _replace_refused(
        self,
        order: Any,
        venue_order_id: VenueOrderId,
        request: ReplaceOrderRequest,
        error: APIError,
    ) -> None:
        """Reject a modify Alpaca refused, unless Alpaca shows the order replaced as asked.

        A second replace of an order already replaced is answered 422 "order already replaced" (observed),
        and the replace itself is sent once (`without_resend`). So the order is looked up: replaced by an
        order with the terms this request asked for, an earlier replace took it and the `replaced`
        trade update brings `OrderUpdated`; replaced with other terms, it was another modify, and this one
        is rejected; if a lookup fails, the outcome is unknown.
        """
        try:
            held = await self._call(self._venue().get_order_by_id, venue_order_id.value)
            replacement = None
            for _ in range(MAX_REPLACE_CHAIN):  # to the live order, as `_replaced` follows it
                if (
                    not isinstance(held, AlpacaOrder)
                    or held.status != AlpacaOrderStatus.REPLACED
                    or held.replaced_by is None
                ):
                    break
                held = replacement = await self._call(
                    self._venue().get_order_by_id, str(held.replaced_by)
                )
        except (APIError, OSError, ValueError, TypeError) as e:  # the classes `_refused` takes
            self._log_unknown_replace(
                order, f"refused ({error.status_code}), then the lookup failed ({e!r})"
            )
            return
        if isinstance(replacement, AlpacaOrder) and replaces_as_asked(replacement, request, order):
            self._log.warning(
                f"Alpaca refused a replace of {order.client_order_id} ({error.status_code}) but shows "
                "the order replaced as asked: an earlier attempt reached it",
            )
            return
        self._modify_rejected(
            order,
            venue_order_id.value,
            venue_order_id,
            f"Alpaca refused the replace ({error.status_code}): {api_error_text(error)}",
            self.clock.timestamp_ns(),
        )

    def _modify_rejected(
        self,
        order: Any,
        target: str,
        venue_order_id: VenueOrderId | None,
        reason: str,
        ts_event: int,
    ) -> None:
        """`OrderModifyRejected` for the replace this client sent to Alpaca order `target`, once: Alpaca can
        refuse one on the HTTP answer and on the stream, and the second refusal generates nothing; nor
        does one for an order that has ended, whose record is dropped (NautilusTrader drops a modify
        rejection for a filled order, observed)."""
        record = self._records.get(order.client_order_id)
        if record is None or record.pending[target] <= 0:
            self._log.info(
                f"Alpaca refused a replace of {order.client_order_id} ({reason}) with no modify awaiting an "
                "outcome (answered already, or sent before a reconnect); nothing generated",
            )
            return
        record.pending[target] -= 1
        self.generate_order_modify_rejected(order, venue_order_id, reason, ts_event)
        self._refusal_settled(order)

    def _log_unknown_replace(self, order: Any, detail: str) -> None:
        self._log.error(
            f"Alpaca replace of {order.client_order_id} got no definitive answer ({detail}); a trade "
            "update or reconciliation resolves it",
        )

    def _replace_in_flight(self, order: Any) -> bool:
        """Whether a replace of this order was sent and Alpaca's `replaced` for it is not yet applied."""
        record = self._records.get(order.client_order_id)
        return record is not None and any(count > 0 for count in record.pending.values())

    def _modify_denial(
        self, order: Any, command: live.ModifyOrder, venue_order_id: VenueOrderId | None
    ) -> str | None:
        """Why the client will not send this modify, or None: only what it can prove. What Alpaca alone
        knows, such as an order in `accepted` that "cannot be replaced", is left to Alpaca's answer."""
        if venue_order_id is None:
            return "Alpaca has not acknowledged the order yet (no venue order ID to replace)"
        if command.quantity is not None and not command.quantity.as_double().is_integer():
            return f"quantity {command.quantity} is fractional; whole shares only"
        # alpaca-py's `ReplaceOrderRequest` raises for a quantity or stop price that is not positive ("qty
        # must be greater than 0", "stop_price must be greater than 0", observed with 0.45.0), which left
        # the modify with no outcome; a limit price of 0 it passes, and no order rests at $0.
        for field, value in (
            ("quantity", command.quantity),
            ("limit price", command.price),
            ("stop price", command.trigger_price),
        ):
            if value is not None and not value.as_double() > 0:
                return f"{field} {value} is not positive"
        if command.price is not None and order.order_type not in LIMIT_PRICED:
            return f"a {order.order_type.name} order has no limit price to change"
        if (
            order.is_reduce_only
            and order.side == OrderSide.SELL
            and command.quantity is not None
            and command.quantity > order.quantity
        ):
            # Alpaca's replace carries no position intent, and what it checks when a `sell_to_close`
            # order grows is not shown on paper, so the reduce-only contract (send an enforcing
            # instruction or reject before transport) is kept by rejecting; a smaller quantity cannot
            # open a short and is sent.
            return (
                "raising the quantity of a reduce-only SELL is not supported: Alpaca's replace carries "
                "no position intent"
            )
        if (
            command.price is not None
            and not self._replace_in_flight(order)  # one in flight may change what the order holds
            and (sent := venue_limit_price(command.price, order.side)) == order.price
            and command.quantity in (None, order.quantity)
            and command.trigger_price in (None, getattr(order, "trigger_price", None))
        ):
            # A replace that changes nothing: `ExecTester`'s one-tick amend of a whole-cent buy (TC-E30)
            # rounds back to the order's own price.
            return (
                f"limit price {command.price} on Alpaca's increment is {sent}, the order's own; "
                "nothing to change"
            )
        if command.trigger_price is not None:
            if order.order_type not in STOP_TYPES:
                return f"a {order.order_type.name} order has no stop price to change"
            return sub_penny_denial(command.trigger_price, "stop price")
        if order.order_type == OrderType.TRAILING_STOP_MARKET:
            params = getattr(command, "params", None) or {}
            if not isinstance(params, dict):
                return "trailing modify params must be a mapping"
            unknown = sorted(str(key) for key in params if key != "trailing_offset")
            if unknown:
                return f"trailing modify params {unknown} are not taken; only 'trailing_offset'"
            if "trailing_offset" in params:
                # The setting and the side gate the trail change only: a quantity replace of a
                # trailing stop the node holds, an outside one included, is the existing path.
                if not self._submit_trailing_stops:
                    return (
                        "trailing stop trail change is not supported: submit_trailing_stops is off"
                    )
                if order.side != OrderSide.SELL:
                    return "trailing stop BUY trail change is not supported; SELL only"
                try:
                    venue_trail(params["trailing_offset"], order.trailing_offset_type)
                except ValueError as e:
                    return str(e)
            elif command.quantity is None:
                return "trailing modify needs params['trailing_offset'] or a quantity change"
        return None

    def _denial(self, order: Any) -> str | None:
        """Why this slice will not send the order, or None. Checked before anything reaches Alpaca.

        rc8's ExecutionClient retains the owning node's ClientCache (live/clients.py:389-416).
        ClientCache.instrument is its read-only instrument lookup (crates/live/src/python/client/
        mod.rs:131-156); native Lighter submission checks use the shared cache too (crates/adapters/
        lighter/src/execution.rs:2049-2059). A data-only load need not be in this client's provider.
        """
        instrument = (
            self._provider.find(order.instrument_id) if order.instrument_id.venue == VENUE else None
        )
        if instrument is None and order.instrument_id.venue == VENUE:
            instrument = self.cache.instrument(order.instrument_id)
        if not isinstance(instrument, Equity):
            return f"{order.instrument_id} is not a loaded {VENUE} equity"
        if not instrument.info.get("tradable", False):
            return f"{order.instrument_id} is not tradable on Alpaca (asset tradable=false)"
        if order.order_type not in ORDER_TYPES:
            return (
                f"order type {order.order_type.name} is not supported; market, limit, stop, "
                "stop-limit and opt-in trailing-stop-market only"
            )
        if order.order_type == OrderType.TRAILING_STOP_MARKET:
            if not self._submit_trailing_stops:
                return (
                    "order type TRAILING_STOP_MARKET is not supported: submit_trailing_stops is off"
                )
            if order.side != OrderSide.SELL:
                return "trailing stop BUY is not supported; SELL only"
            if order.activation_price is not None:
                return "trailing stop activation price is not supported"
            if order.trigger_price is not None:
                return "trailing stop initial trigger price is not supported; Alpaca derives it"
            if order.trigger_type != TriggerType.DEFAULT:
                return f"trailing stop trigger type {order.trigger_type.name} is not supported; DEFAULT only"
            try:
                venue_trail(order.trailing_offset, order.trailing_offset_type)
            except ValueError as e:
                return str(e)
        if order.time_in_force not in TIMES_IN_FORCE:
            return f"time in force {order.time_in_force.name} is not supported; DAY or GTC only"
        if order.is_quote_quantity:
            return "quote quantity is not supported; whole shares only"
        if order.is_reduce_only:
            if not self._reduce_only_sells:
                return "reduce-only is not supported"
            if order.side == OrderSide.BUY:
                return (
                    "reduce-only BUY is not supported: Alpaca has no instruction that enforces it"
                )
            if order.order_type not in self._sell_to_close_order_types:
                return (
                    f"reduce-only {order.order_type.name} SELL is not supported: sell_to_close is "
                    "not shown to be enforced for that order type"
                )
        if (
            order.order_list_id is not None
            or order.linked_order_ids
            or order.parent_order_id is not None
        ):
            return "contingent orders and order lists are not supported"
        if not order.quantity.as_double().is_integer():
            return f"quantity {order.quantity} is fractional; whole shares only"
        if len(order.client_order_id.value) > CLIENT_ORDER_ID_MAX:
            return f"client order ID is longer than Alpaca's {CLIENT_ORDER_ID_MAX} characters"
        if getattr(order, "display_qty", None) is not None:  # a market order has no such field
            # Alpaca's order has no display quantity field ("Create an Order"); a stop-market order can
            # carry one in NautilusTrader too, and it would be sent at its full quantity.
            return "display quantity is not supported"
        if order.order_type in STOP_TYPES:
            if order.trigger_type not in STOP_TRIGGER_TYPES:
                return (
                    f"trigger type {getattr(order.trigger_type, 'name', order.trigger_type)} is not "
                    "offered: Alpaca elects a stop on the last trade (DEFAULT or LAST_PRICE)"
                )
            denial = sub_penny_denial(order.trigger_price, "stop price")
            if denial is not None:
                return denial
        if order.order_type in LIMIT_PRICED and order.is_post_only:
            return "post-only is not supported"
        return None

    @staticmethod
    def _order_request(
        order: Any, extended_hours: bool = False, *, sell_to_close: bool = False
    ) -> OrderRequest:
        fields: dict[str, Any] = {
            "symbol": order.instrument_id.symbol.value,
            "qty": float(int(order.quantity.as_double())),
            "side": ORDER_SIDES[order.side],
            "time_in_force": TIMES_IN_FORCE[order.time_in_force],
            "client_order_id": order.client_order_id.value,
        }
        if extended_hours:
            fields["extended_hours"] = True  # only ever with a limit order, `DAY` or `GTC`
        if order.side == OrderSide.SELL and (order.is_reduce_only or sell_to_close):
            fields["position_intent"] = PositionIntent.SELL_TO_CLOSE
        if order.order_type == OrderType.LIMIT:
            return LimitOrderRequest(
                **fields, limit_price=wire_price(venue_limit_price(order.price, order.side))
            )
        if order.order_type == OrderType.STOP_MARKET:
            return StopOrderRequest(**fields, stop_price=wire_price(order.trigger_price))
        if order.order_type == OrderType.STOP_LIMIT:
            return StopLimitOrderRequest(
                **fields,
                stop_price=wire_price(order.trigger_price),
                limit_price=wire_price(venue_limit_price(order.price, order.side)),
            )
        if order.order_type == OrderType.TRAILING_STOP_MARKET:
            field = (
                "trail_price"
                if order.trailing_offset_type == TrailingOffsetType.PRICE
                else "trail_percent"
            )
            return TrailingStopOrderRequest(
                **fields, **{field: venue_trail(order.trailing_offset, order.trailing_offset_type)}
            )
        return MarketOrderRequest(**fields)

    def _log_unknown_outcome(self, client_order_id: ClientOrderId, detail: str) -> None:
        self._log.error(
            f"Alpaca submit for {client_order_id} got no definitive answer ({detail}); the order stays "
            "SUBMITTED until a trade update or reconciliation resolves it",
        )

    # -- order events, on the node's loop ----------------------------------------------------------

    def _accept(self, order: Any, venue_order_id: VenueOrderId, ts_event: int) -> None:
        """Generate `OrderAccepted` once per order, and only for an order still awaiting it.

        An order Alpaca acknowledges while a cancel or modify sent before the acknowledgement is pending
        (no venue order ID yet) is left pending, so the cancel's outcome is still awaited; the
        acknowledgement is kept, and generated if the cancel or modify is refused, which puts the order
        back to `SUBMITTED` (`_refusal_settled`). Without it the order would stay `SUBMITTED` for the
        in-flight check to resolve.
        """
        client_order_id = order.client_order_id
        if client_order_id in self._accepted or client_order_id in self._finished:
            return
        # The cached order, not the command's snapshot: a cancel may have moved it on since the submit.
        current = self.cache.order(client_order_id) or order
        if current.status in (OrderStatus.INITIALIZED, OrderStatus.SUBMITTED, OrderStatus.RELEASED):
            self._accepted.add(client_order_id)
            self._acknowledged.pop(client_order_id, None)
            self._sent_price_updated(current, venue_order_id, ts_event)
            self.generate_order_accepted(current, venue_order_id, ts_event)
        elif (
            current.status in (OrderStatus.PENDING_CANCEL, OrderStatus.PENDING_UPDATE)
            and current.venue_order_id is None
        ):
            self._acknowledged[client_order_id] = (venue_order_id, ts_event)
        else:
            self._accepted.add(client_order_id)  # acknowledged already, as the engine holds it

    def _refusal_settled(self, order: Any) -> None:
        """After a cancel or modify refusal for an order Alpaca acknowledged while it was pending:
        `OrderAccepted`, which the refusal's return to `SUBMITTED` now awaits (the engine applies the two
        in the order generated)."""
        acknowledged = self._acknowledged.pop(order.client_order_id, None)
        if acknowledged is None or order.client_order_id in self._accepted:
            return
        record = self._records.get(order.client_order_id)
        if self._ended(order.client_order_id) or (record is not None and record.filled > 0):
            return  # moved on since: a fill, or an end, implies the acknowledgement
        current = self.cache.order(order.client_order_id) or order
        if current.venue_order_id is not None or current.filled_qty.as_double() > 0:
            return  # acknowledged already, as the engine holds it (a query report, a fill)
        self._accepted.add(order.client_order_id)
        self._sent_price_updated(current, *acknowledged)
        self.generate_order_accepted(order, *acknowledged)

    def _sent_price_updated(self, order: Any, venue_order_id: VenueOrderId, ts_event: int) -> None:
        """`OrderUpdated` with the limit price sent, just before `OrderAccepted`, when it was moved onto
        Alpaca's increment (`venue_limit_price`): the node's order then holds what Alpaca holds, and a
        report of the order reconciles with no difference. Before, not after: a strategy that modifies on
        `OrderAccepted` (upstream's `ExecTester` does) would otherwise take this update for its modify's
        answer. NautilusTrader applies `OrderUpdated` to a
        submitted or released order in place (`crates/model/src/orders/mod.rs`)."""
        if order.order_type not in LIMIT_PRICED:
            return
        sent = venue_limit_price(order.price, order.side)
        if sent == order.price:
            return
        self._log.info(
            f"{order.client_order_id} was sent at {sent}, its limit {order.price} on Alpaca's increment",
        )
        self.generate_order_updated(
            order,
            venue_order_id,
            order.quantity,
            sent,
            order.trigger_price if order.order_type in STOP_TYPES else None,
            None,
            ts_event,
        )

    def _known_venue_id(
        self,
        client_order_id: ClientOrderId,
        given: VenueOrderId | None,
        held: VenueOrderId | None,
    ) -> VenueOrderId | None:
        """Alpaca's order ID for a command: the command's, the node's, or one an acknowledgement kept while
        a cancel or modify was pending (`_accept`)."""
        kept = self._acknowledged.get(client_order_id)
        return given or held or (kept[0] if kept is not None else None)

    def _ended(self, client_order_id: ClientOrderId) -> bool:
        """Whether the node's order has ended, as this client has seen it or the engine holds it: read
        again after each `await`, since an update applied meanwhile may have ended it."""
        if client_order_id in self._finished:
            return True
        order = self.cache.order(client_order_id)
        return order is not None and order.is_closed

    def _reject(self, order: Any, reason: str, ts_event: int) -> None:
        """`OrderRejected`, once: nothing is generated for an order that has ended already (an update
        applied while a submit's answer or a lookup was awaited)."""
        if self._ended(order.client_order_id):
            self._log.info(
                f"Alpaca rejection of {order.client_order_id} ({reason}) came after the order ended; "
                "nothing generated"
            )
            return
        self._finish(order.client_order_id)
        self.generate_order_rejected(order, reason, ts_event, False)

    async def _consume_updates(
        self, updates: asyncio.Queue[TradeUpdate], idle: asyncio.Event
    ) -> None:
        """Apply trade updates one at a time, in the order the stream delivered them: an update for a
        replacement may need a lookup on Alpaca before it can be applied. The queue and the event are
        this connect's own, so a task left after a disconnect reads no new queue and marks no new event."""
        while True:
            update = await updates.get()
            idle.clear()
            try:
                await self._apply_trade_update(update)
            except Exception as e:  # noqa: BLE001 - one update's failure must not stop the rest
                self._log.error(f"Alpaca trade update {update.event} could not be applied: {e!r}")
            finally:
                idle.set()

    async def _apply_trade_update(self, update: TradeUpdate) -> None:
        if self._stopping:
            return
        try:
            await self._apply(update)
        except HeldReportError:
            return  # the hold logged its order and why it ended
        except (ValueError, TypeError, RuntimeError, APIError, OSError) as e:
            # Nothing else reports it: name the update and keep the stream.
            self._log.error(
                f"Alpaca trade update {update.event} for {update.order.client_order_id} failed: {e!r}",
            )

    # -- the record of each node order's replace chain (`OrderRecord`) -------------------------------

    def _record(self, order: Any) -> OrderRecord:
        """The node order's record, built from the node's cache when first needed, as Binance spot's
        `restore_order` rebuilds its binding: the order's current venue order ID, the ones it has held
        before as superseded, its filled quantity, its quantity and its trade IDs."""
        client_order_id = order.client_order_id
        record = self._records.get(client_order_id)
        if record is not None:
            return record
        current = order.venue_order_id.value if order.venue_order_id is not None else None
        record = OrderRecord(
            client_order_id,
            current=current,
            filled=order.filled_qty.as_decimal(),
            quantity=order.quantity.as_decimal(),
            trades={str(trade_id) for trade_id in order.trade_ids},
        )
        for venue_order_id in order.venue_order_ids:
            leg_id = venue_order_id.value
            record.legs.setdefault(leg_id, Leg(leg_id))
            self._index[leg_id] = client_order_id
            if leg_id != current:
                record.superseded.add(leg_id)
        self._records[client_order_id] = record
        return record

    def _see(self, record: OrderRecord, alpaca_order: AlpacaOrder) -> Leg:
        """Record Alpaca's view of one of the order's legs and index it to the order."""
        self._index[str(alpaca_order.id)] = record.client_order_id
        return record.see(alpaca_order)

    def _held_by_venue_id(self, alpaca_order_id: str) -> ClientOrderId | None:
        """The node's order the cache indexes this Alpaca order ID to, if it holds one."""
        client_order_id = self.cache.client_order_id(VenueOrderId(alpaca_order_id))
        if client_order_id is None or self.cache.order(client_order_id) is None:
            return None
        return client_order_id

    async def _node_order_id(
        self, alpaca_order: AlpacaOrder, known: dict[str, AlpacaOrder] | None = None
    ) -> ClientOrderId | None:
        """The node's client order ID for an Alpaca order, or None if the node holds none: its own client
        order ID, then the index, then the node's cache, and a walk of `replaces` only on a miss."""
        own = holdable_id(alpaca_order.client_order_id)
        if own is not None and self.cache.order(own) is not None:
            return own
        root = await self._root_id(alpaca_order, known)
        return root if root is not None and self.cache.order(root) is not None else None

    async def _root_id(
        self, alpaca_order: AlpacaOrder, known: dict[str, AlpacaOrder] | None = None
    ) -> ClientOrderId | None:
        """The client order ID of the order an Alpaca order's `replaces` chain starts from, or None if it
        has none NautilusTrader can hold.

        A replacement never carries the node's own client order ID: it carries the one this client sent
        with the replace (`replacement_client_order_id`) or one Alpaca made (observed). Neither is read
        for what it replaces; it is traced back through its `replaces` links. Each link is resolved from
        what costs no read first: the client's index, the node's cache (`ClientCache.client_order_id`, which holds every venue order ID a node order has
        had), and the orders already fetched; only then is the order read, at most `MAX_REPLACE_CHAIN`
        times, and a walk that would need more fails by name rather than stopping at an order that is not
        the chain's first. An order Alpaca names in `replaces` and does not find (404) is a chain that
        cannot be read, and fails the same way. Every result is indexed by Alpaca's order ID, unique to
        each order, never by a client order ID, which an order made outside the node can leave empty or
        share; a chain that ends without reaching the node's orders is indexed too, so an order traced
        before the node holds its original (the start-up window) resolves once it does, and no chain is
        walked twice. A client order ID NautilusTrader cannot hold (empty or not ASCII, as an order made
        outside the node can carry) ends the chain: the trace stops at the order before it.
        """
        failures = self._report_hold_failures
        order_id = str(alpaca_order.id)
        self._check_report_session(holdable_id(alpaca_order.client_order_id), failures)
        if order_id in self._index:
            return self._index[order_id]
        held = self._held_by_venue_id(order_id)
        if held is not None:
            self._index[order_id] = held
            return held
        root = holdable_id(alpaca_order.client_order_id)
        replaces = alpaca_order.replaces
        walked = {order_id}
        reads = 0
        while replaces is not None and str(replaces) not in walked:
            prior_id = str(replaces)
            walked.add(prior_id)
            if prior_id in self._index:
                root = self._index[prior_id] or root
                break
            held = self._held_by_venue_id(prior_id)
            if held is not None:
                root = held
                break
            prior = (known or {}).get(prior_id)
            if prior is None:
                if reads == MAX_REPLACE_CHAIN:
                    raise ChainTooLong(
                        f"replace chain needs more than {MAX_REPLACE_CHAIN} reads to trace"
                    )
                reads += 1
                prior = await self._call(self._venue().get_order_by_id, prior_id)
            if not isinstance(prior, AlpacaOrder):
                raise TypeError(
                    "alpaca-py returned raw order data; the client must use raw_data=False"
                )
            prior_cid = holdable_id(prior.client_order_id)
            if prior_cid is None:
                break  # the order before cannot be held: the trace stops here
            root = prior_cid
            if self.cache.order(root) is not None:
                break
            replaces = prior.replaces
        self._check_report_session(root, failures)
        self._index[order_id] = root
        return root

    def _external_keys(self, order: AlpacaOrder) -> set[str]:
        """The known leg and client lineage of an outside order left to reconciliation."""
        keys = {f"order:{order.id}"}
        if order.replaces is not None:
            keys.add(f"order:{order.replaces}")
        root = self._index.get(str(order.id))
        if root is not None:
            keys.add(f"client:{root.value}")
        return keys

    def _external_legs(self, order: AlpacaOrder) -> set[str]:
        """Pending fill buckets belonging to this order's traced chain, including this leg."""
        order_id = str(order.id)
        root = self._index.get(order_id)
        return {order_id} | {
            leg_id
            for leg_id in self._external_fills
            if root is not None and self._index.get(leg_id) == root
        }

    def _leave_external_to_reconciliation(self, order: AlpacaOrder, reason: str) -> None:
        """Drop an incomplete stream fill set and name the order once; reconciliation owns it now."""
        keys = self._external_keys(order)
        if not self._external_reconciliation.intersection(keys):
            self._log.warning(
                f"{order_description(order)} left to reconciliation: {reason}; "
                "stored stream fills dropped, no fill inferred and no extra read"
            )
        self._external_reconciliation.update(keys)
        for leg_id in self._external_legs(order):
            self._external_reconciliation.add(f"order:{leg_id}")
            self._external_fills.pop(leg_id, None)

    async def _report_external_update(self, update: TradeUpdate) -> None:
        """Bootstrap an outside order through `_handle_report(report, fills)` (Python adapter guide).

        Every companion fill keeps the stream's trade ID, exact price, quantity and time. Pending
        reports can overtake Python cache reads, so retain complete open snapshots' fills until the
        engine holds the order. Closed snapshots release them immediately, and superseded buckets move
        to the replacement. A missing fill leaves the chain to reconciliation once, never a partial
        bundle. Nothing here fetches a missing fill or marks a report applied (ADR 0075).
        """
        order_id = str(update.order.id)
        event = getattr(update.event, "value", update.event)
        if event == "replaced":
            # The replacement is the reportable order. Move its predecessor's pending fills without
            # retaining a bucket for every old leg while the native engine creates the order.
            pending = self._external_fills.pop(order_id, {})
            if pending and update.order.replaced_by is not None:
                target = str(update.order.replaced_by)
                self._index[target] = self._index.get(order_id)
                pending.update(self._external_fills.get(target, {}))
                self._external_fills[target] = pending
            self._log.warning(
                f"{order_description(update.order)} is not reported: status replaced: "
                "its replacement is reported instead"
            )
            return
        # Do not mutate the stored set until mapping succeeds and the complete report is sent.
        pending = dict(self._external_fills.get(order_id, {}))
        if event in FILL_EVENTS:
            try:
                trade_id, last_qty, last_px = update_fill_values(update)
                symbol = update.order.symbol
                if symbol is None:
                    raise ValueError("no symbol")
                report = FillReport(
                    account_id=ACCOUNT_ID,
                    instrument_id=InstrumentId(Symbol(symbol), VENUE),
                    venue_order_id=VenueOrderId(order_id),
                    trade_id=TradeId(trade_id),
                    order_side=alpaca_side(update.order),
                    last_qty=last_qty,
                    last_px=last_px,
                    commission=Money(0, USD),
                    liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
                    ts_event=dt_to_unix_nanos(update.timestamp),
                    ts_init=self.clock.timestamp_ns(),
                )
            except ValueError as e:
                self._leave_external_to_reconciliation(update.order, str(e))
                return
            previous = pending.get(trade_id)
            if previous is not None and fill_terms(previous) != fill_terms(report):
                self._leave_external_to_reconciliation(
                    update.order, "a repeated trade ID has different quantity, price or time"
                )
                return
            pending[trade_id] = report
        # `_node_order_id` has already traced this leg through `_root_id`. Fills from the same
        # outside replace chain must travel together, just as held-order catch-up groups its legs.
        legs = self._external_legs(update.order)
        observed: dict[str, FillReport] = dict(pending)
        for leg_id in legs - {order_id}:
            for trade_id, fill in self._external_fills.get(leg_id, {}).items():
                previous = observed.get(trade_id)
                if previous is not None and fill_terms(previous) != fill_terms(fill):
                    self._leave_external_to_reconciliation(
                        update.order,
                        "a repeated trade ID has different quantity, price or time in its replace chain",
                    )
                    return
                observed[trade_id] = fill
        sent = await self._send_report(update.order, list(observed.values()))
        for leg_id in legs:
            self._external_fills.pop(leg_id, None)
        if sent is not None and is_open_order(update.order) and observed:
            self._external_fills[order_id] = observed

    async def _apply(self, update: TradeUpdate) -> None:
        """Apply one trade update through the node order's record.

        Each Alpaca order of the chain plays one part in the record: the order the node's order is on now
        (`current`), one it has left (`superseded`), or a replacement it has not moved to. A late update of
        a superseded order is ignored, but for its fills, as Binance spot's `record_order_update` returns
        nothing for a superseded venue order ID while a fill on one is the node order's. A replacement is
        adopted once, by `replaced` or by its own update when the record shows it took over (the order it
        replaces is replaced by it) or an execution proves it live, as Binance spot's
        `record_order_update_from_fill` updates from a fill; its `new` alone does not adopt it, since it
        may still be rejected. A replacement's `rejected` took over only if the order it replaces is
        replaced by it, as the record shows or, failing that, one read of that order: then nothing works
        at Alpaca and the node's order ends (`_rejected_takeover`); else the modify failed, or the
        replacement was one a later replace left behind.
        """
        alpaca_order = update.order
        if self._report_external_orders:
            symbol = alpaca_order.symbol
            if (
                symbol is not None
                and self._provider.find(InstrumentId(Symbol(symbol), VENUE)) is None
            ):
                if symbol not in self._external_unloaded_symbols:
                    self._external_unloaded_symbols.add(symbol)
                    self._log.warning(
                        f"{order_description(alpaca_order)} is in an instrument this client has not "
                        "loaded; not reported"
                    )
                for leg_id in self._external_legs(alpaca_order):
                    self._external_fills.pop(leg_id, None)
                return
            keys = self._external_keys(alpaca_order)
            if self._external_reconciliation.intersection(keys):
                # A later reconciliation may have materialized it; then use the normal event path.
                candidates = (
                    holdable_id(alpaca_order.client_order_id),
                    self._index.get(str(alpaca_order.id)),
                    self._held_by_venue_id(str(alpaca_order.id)),
                )
                if not any(
                    cid is not None and self.cache.order(cid) is not None for cid in candidates
                ):
                    self._external_reconciliation.update(keys)
                    return
        try:
            client_order_id = await self._node_order_id(alpaca_order)
        except (APIError, OSError, ValueError, TypeError, RuntimeError) as e:
            if not self._report_external_orders:
                raise
            # The fill has not been stored yet. A lost trace must not poison every later snapshot,
            # and a transport exception's text may contain the venue ID or request URL.
            self._leave_external_to_reconciliation(
                alpaca_order, f"tracing failed ({failure_cause(e)})"
            )
            return
        order = self.cache.order(client_order_id) if client_order_id is not None else None
        if client_order_id is None or order is None:
            if self._report_external_orders:
                await self._report_external_update(update)
                return
            self._log.info(
                f"Alpaca trade update {update.event} for {alpaca_order.symbol}, "
                "which this node does not hold; reconciliation reports it",
            )
            return
        for leg_id in self._external_legs(alpaca_order):
            self._external_fills.pop(leg_id, None)
        self._external_reconciliation.difference_update(self._external_keys(alpaca_order))
        event = getattr(update.event, "value", update.event)
        leg_id = str(alpaca_order.id)
        venue_order_id = VenueOrderId(leg_id)
        ts_event = dt_to_unix_nanos(update.timestamp)
        if client_order_id in self._finished:
            if event == "rejected" and alpaca_order.replaces is not None:
                # Alpaca: if the old order "is filled before the replacing (new) order reaches the
                # execution venue, the replacing (new) order is rejected". NautilusTrader drops a modify
                # rejection for a filled order (observed), so the fill stands as the outcome.
                self._log.info(
                    f"Alpaca rejected the replacement {leg_id} of {client_order_id}, which had filled "
                    "first; the fill is the outcome",
                )
                return
            # A snapshot can outlive the client's tracking. Wait for the queued end to reach the
            # cache, then reconcile any additional real fills instead of discarding the snapshot.
            try:
                await self._send_report(alpaca_order)
            except HeldReportError, APIError, OSError:
                # A hold that ended unsettled, or a read of the snapshot's own mapping that failed
                # before it. Nothing else reads an ended order again: Alpaca sends it no further
                # update, and the engine's open-order check reads open orders only by default.
                self._reread_report_later(client_order_id, venue_order_id)
                raise
            return
        if order.is_closed:
            # Ended by the engine: from a report, which reaches it only once the node's order holds every
            # fill it shows (`_settled_report`), so a fill update arriving after it is one the order
            # holds; or by the engine's own resolution of an order its checks could not settle (ADR 0075,
            # a known limit). The client's state for the order is dropped.
            self._finish(client_order_id)
            self._log.info(
                f"Alpaca trade update {event} for {client_order_id}, which has ended; ignored"
            )
            return
        record = self._record(order)
        leg = self._see(record, alpaca_order)
        if event == "replaced":
            await self._replaced(order, record, alpaca_order, ts_event)
            return
        if event == "order_replace_rejected":
            record.answers = {k: a for k, a in record.answers.items() if str(a.replaces) != leg_id}
            self._modify_rejected(
                order,
                leg_id,
                venue_order_id,
                "Alpaca rejected the replace (trade update)",
                ts_event,
            )
            return
        if event == "pending_replace":
            return  # NautilusTrader generates `OrderPendingUpdate` itself when a modify is sent
        if leg_id != record.current and (
            leg_id in record.superseded or (leg.replaces is None and record.current is not None)
        ):
            if event in FILL_EVENTS:
                # An execution on the order before it was replaced, which can arrive after the
                # replacement's own fills.
                await self._fill(order, record, update, venue_order_id, ts_event)
            else:
                self._log.info(
                    f"Alpaca trade update {event} for {venue_order_id}, which {client_order_id} no longer "
                    "names (replaced); ignored",
                )
            return
        if leg_id != record.current and leg.replaces is not None:
            took_over = record.took_over(leg_id)
            if event == "rejected":
                if took_over is None:
                    took_over = await self._took_over(alpaca_order, None)
                    if self._ended(client_order_id):
                        return  # an update applied during the read ended it
                if took_over:
                    self._adopt(order, record, alpaca_order, ts_event)
                    self._rejected_takeover(order, record, leg_id, ts_event)
                    return
                self._rejected_replacement(order, record, alpaca_order, ts_event)
                return
            if took_over is False:
                self._log.info(
                    f"Alpaca trade update {event} for {leg_id}, a replacement of {client_order_id}'s "
                    "order that another replace left behind; ignored",
                )
                return
            if not took_over and event in FILL_EVENTS:
                behind = await self._behind_current(record, alpaca_order)
                if self._ended(client_order_id):
                    return  # an update applied during the read ended it
                if behind is None:
                    # Its place in the chain is not known: neither adopted nor counted now; the later
                    # reads place it and bring its fill by its trade ID (ADR 0075).
                    self._catch_up_later(
                        client_order_id, self._shown(record, alpaca_order), venue_order_id
                    )
                    return
                if behind:
                    # A fill on an order of the chain the node's order has already moved past: applied
                    # under the order it is on now (`_count_fill`), never adopted.
                    record.superseded.add(leg_id)
                    await self._fill(order, record, update, venue_order_id, ts_event)
                    return
            if took_over or event in FILL_EVENTS:
                self._adopt(order, record, alpaca_order, ts_event)
            elif event not in ("canceled", "expired", "order_cancel_rejected"):
                return  # its `new`: the replacement may still be rejected; `replaced` adopts it
        elif record.current is None:
            record.current = leg_id  # the order's own first update, before the submit's answer
        if event == "rejected":
            if alpaca_order.replaces is not None:
                self._rejected_takeover(order, record, leg_id, ts_event)
                return
            self._reject(
                order,
                f"Alpaca rejected the order (trade update, status {alpaca_order.status.value})",
                ts_event,
            )
            return
        if event == "order_cancel_rejected":
            self.generate_order_cancel_rejected(
                order, venue_order_id, "Alpaca rejected the cancel (trade update)", ts_event
            )
            self._refusal_settled(order)
            return
        if event not in ACCEPTING_EVENTS | FILL_EVENTS | {"canceled", "expired"}:
            self._log.warning(
                f"Alpaca trade update {event} for {client_order_id} is not mapped in this slice; ignored",
            )
            return
        self._accept(order, venue_order_id, dt_to_unix_nanos(alpaca_order.submitted_at))
        if event in FILL_EVENTS:
            await self._fill(order, record, update, venue_order_id, ts_event)
            return  # `_fill` ends the order once its fills reach its quantity
        if event in ("canceled", "expired"):
            ending = Ending(event, venue_order_id, ts_event, self._shown(record, alpaca_order))
            await self._end_after_fills(order, record, ending, walk=False)

    def _rejected_takeover(
        self, order: Any, record: OrderRecord, leg_id: str, ts_event: int
    ) -> None:
        """The node's order, moved to a replacement Alpaca then rejected: nothing works at Alpaca, so it
        ends. Rejected if it has no fill; partly filled, it ends canceled, as Binance spot's
        `reject_replace` emits the old order's withheld `OrderCanceled` when its replacement is rejected,
        since NautilusTrader drops `OrderRejected` for a partly filled order (`crates/model/src/orders/
        mod.rs` at the pin: no `PartiallyFilled` to `Rejected` transition)."""
        if max(record.filled, order.filled_qty.as_decimal()) > 0:
            self._finish(order.client_order_id)
            self._log.info(
                f"Alpaca rejected the replacement {leg_id} of {order.client_order_id} after it took over; "
                "the order ends canceled with its fills"
            )
            self.generate_order_canceled(order, VenueOrderId(leg_id), ts_event)
            return
        self._reject(order, f"Alpaca rejected the replacement order {leg_id}", ts_event)

    def _rejected_replacement(
        self, order: Any, record: OrderRecord, replacement: AlpacaOrder, ts_event: int
    ) -> None:
        """A replacement rejected without taking over: the modify that sent it failed, if one awaits an
        outcome on the order it replaces; a replacement a later replace left behind changes nothing."""
        target = str(replacement.replaces)
        record.answers.pop(str(replacement.id), None)
        prior = record.legs.get(target)
        if prior is not None and prior.replaced_by not in (None, str(replacement.id)):
            self._log.info(
                f"Alpaca rejected {replacement.id}, a replacement of {order.client_order_id}'s order "
                f"{target}, which {prior.replaced_by} replaced; ignored"
            )
            return
        self._modify_rejected(
            order,
            target,
            VenueOrderId(str(replacement.id)),
            f"Alpaca rejected the replacement order {replacement.id} (trade update)",
            ts_event,
        )

    def _finish(self, client_order_id: ClientOrderId) -> None:
        """Mark the node's order ended and drop its record, as Binance spot's `cleanup_terminal` does; the
        index entries of its Alpaca orders go too: a late update of one is still known as the ended
        order's, through the node's cache, which holds every venue order ID the order has had. Retire
        its later-read target; an already outstanding read exits on return and stays cancellable at
        disconnect. Only the retry that itself ended the order may log that it caught up."""
        self._finished.add(client_order_id)
        record = self._records.pop(client_order_id, None)
        for leg_id in record.legs if record is not None else ():
            if self._index.get(leg_id) == client_order_id:
                del self._index[leg_id]
        self._acknowledged.pop(client_order_id, None)

        self._catch_up_targets.pop(client_order_id, None)
        if self._catching_up.get(client_order_id) is asyncio.current_task():
            # This read brought the ending fill and may log that fact. Another task's outstanding
            # read stays tracked for disconnect; after it returns (or its retry wait ends), it exits
            # without another read or crediting itself for a fill the stream brought.
            self._catching_up.pop(client_order_id, None)

    async def _replaced(
        self, order: Any, record: OrderRecord, old: AlpacaOrder, ts_event: int
    ) -> None:
        """Alpaca's `replaced` for an order of the node's chain: `OrderUpdated` with the replacement's
        values.

        The update carries the old order, with `replaced_by` (observed). The replacement is the PATCH
        answer if it is in, otherwise read from Alpaca, and followed on if Alpaca has replaced it again
        since; if none yields it, nothing is generated and reconciliation settles the order, so no value
        is invented. A replacement the record has already moved to or past, by an earlier `replaced` or
        its own update, is not adopted again. One Alpaca has closed already took over and ended with no
        working order left: the order ends with it, unless Alpaca shows it more filled than the node has
        seen (`chain_filled`), when its own updates, still to come, end it.
        """
        if old.replaced_by is None:
            self._log.error(
                f"Alpaca replaced {old.id} without saying by which order; nothing applied"
            )
            return
        record.superseded.add(str(old.id))
        target = str(old.replaced_by)
        if target == record.current or target in record.superseded:
            return  # adopted already, by the replacement's own update: nothing to read or update
        replacement: AlpacaOrder | RawData | None = record.answers.pop(target, None)
        try:
            if replacement is None:
                replacement = await self._call(self._venue().get_order_by_id, target)
            for _ in range(MAX_REPLACE_CHAIN):
                if not isinstance(replacement, AlpacaOrder):
                    raise TypeError(
                        "alpaca-py returned raw order data; the client must use raw_data=False"
                    )
                if (
                    replacement.status != AlpacaOrderStatus.REPLACED
                    or replacement.replaced_by is None
                ):
                    break
                # Replaced again since: the live order is further on.
                self._see(record, replacement)
                record.superseded.add(str(replacement.id))
                next_id = str(replacement.replaced_by)
                replacement = record.answers.pop(next_id, None) or await self._call(
                    self._venue().get_order_by_id, next_id
                )
        except (APIError, OSError, ValueError, TypeError) as e:  # the classes `_refused` takes
            self._log.error(
                f"Alpaca replaced {order.client_order_id} by {old.replaced_by}, which could not be read "
                f"({e!r}); no OrderUpdated, reconciliation settles it",
            )
            return
        if not isinstance(replacement, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        if self._ended(order.client_order_id):
            return  # an update or a command's answer ended it during the reads
        replacement_id = str(replacement.id)
        self._adopt(order, record, replacement, ts_event)
        shown = share_count(replacement.filled_qty)
        seen = max(record.filled, order.filled_qty.as_decimal())
        if not is_open_order(replacement) and (
            shown is None or chain_filled(shown, record.earlier_filled(replacement_id)) > seen
        ):
            # Closed with fills the node lacks: their updates may never come (ADR
            # 0075), so the fills are brought by their trade IDs and the end follows them.
            closed = replacement.status in (AlpacaOrderStatus.CANCELED, AlpacaOrderStatus.EXPIRED)
            if shown is not None and closed:
                closed_at = dt_to_unix_nanos(
                    replacement.canceled_at or replacement.expired_at or replacement.updated_at
                )
                event = (
                    "canceled" if replacement.status == AlpacaOrderStatus.CANCELED else "expired"
                )
                ending = Ending(
                    event, VenueOrderId(replacement_id), closed_at, self._shown(record, replacement)
                )
                await self._end_after_fills(order, record, ending, walk=False)
                return
            if shown is not None and replacement.status == AlpacaOrderStatus.FILLED:
                target = self._shown(record, replacement)
                if not await self._catch_up(
                    order, record, target, VenueOrderId(replacement_id), walk=False
                ):
                    self._catch_up_later(
                        order.client_order_id, target, VenueOrderId(replacement_id)
                    )
                return
            self._log.info(
                f"Alpaca shows the replacement {replacement_id} of {order.client_order_id} "
                f"{replacement.status.value} with {replacement.filled_qty} filled, more than the node "
                "has seen; its own updates end the order"
            )
            return
        if replacement.status == AlpacaOrderStatus.REJECTED:
            self._rejected_takeover(order, record, replacement_id, ts_event)
        elif replacement.status in (AlpacaOrderStatus.CANCELED, AlpacaOrderStatus.EXPIRED):
            self._finish(order.client_order_id)
            closed_at = dt_to_unix_nanos(
                replacement.canceled_at or replacement.expired_at or replacement.updated_at
            )
            venue_id = VenueOrderId(replacement_id)
            if replacement.status == AlpacaOrderStatus.CANCELED:
                self.generate_order_canceled(order, venue_id, closed_at)
            else:
                self.generate_order_expired(order, venue_id, closed_at)

    def _adopt(
        self, order: Any, record: OrderRecord, replacement: AlpacaOrder, ts_event: int
    ) -> None:
        """`OrderUpdated` moving the node's order to Alpaca's replacement, once per replacement, as Binance
        spot's `record_order_update` moves its binding's `current` and pushes the old one to `superseded`.
        Every replace sent to an order the record has now left has its outcome."""
        replacement_id = str(replacement.id)
        if record.current == replacement_id:
            return  # adopted already: one transition, one `OrderUpdated`
        self._see(record, replacement)
        if replacement.replaces is not None and not names_replacement(
            order.client_order_id.value, replacement.client_order_id
        ):
            # Alpaca made the ID (none could be named, or the one named was not kept), or a hand
            # replaced the order: said here, once for the replacement, by whichever path adopts it.
            self._log.warning(
                f"The replacement of {order.client_order_id} carries a client order ID this client "
                "did not name: it is the node's by its `replaces` link only, and an account read by "
                "client order ID does not show it as the node's"
            )
        # The fills before this replacement: the record's view of the orders it replaced, or the node's
        # own count, all of it on those orders until this one is adopted.
        earlier = max(
            record.earlier_filled(replacement_id), record.filled, order.filled_qty.as_decimal()
        )
        qty = share_count(replacement.qty)
        report = order_status_report(
            replacement,
            self.clock.timestamp_ns(),
            order.trigger_type if order.order_type in STOP_TYPES else None,
            order.client_order_id,
            quantity=chain_quantity(qty, earlier) if qty is not None else None,
            trailing=self._report_external_trailing_stops
            or order.order_type == OrderType.TRAILING_STOP_MARKET,
        )
        if report is None:
            raise ValueError(f"replacement {replacement_id} is not an order this slice maps")
        if record.current is not None:
            record.superseded.add(record.current)
        if replacement.replaces is not None:
            record.superseded.add(str(replacement.replaces))
        record.superseded.discard(replacement_id)
        record.current = replacement_id
        record.quantity = report.quantity.as_decimal()
        record.answers.pop(replacement_id, None)
        for target in record.superseded:
            record.pending.pop(target, None)
        self.generate_order_updated(
            order,
            VenueOrderId(replacement_id),
            report.quantity,
            report.price,
            report.trigger_price,
            None,
            ts_event,
        )

    async def _fill(
        self,
        order: Any,
        record: OrderRecord,
        update: TradeUpdate,
        venue_order_id: VenueOrderId,
        ts_event: int,
    ) -> None:
        """`OrderFilled` for one fill update, once per trade ID (`_count_fill`), and then the fills Alpaca's
        order shows that the node does not hold yet, by their own trade IDs (`_catch_up`).

        A fill the update cannot carry is logged and ends nothing; its order's next read brings it. The
        order Alpaca shows filled counts its whole quantity, whatever `filled_qty` the update carries."""
        try:
            trade_id, last_qty, last_px = update_fill_values(update)
        except ValueError as e:
            self._log.error(
                f"Alpaca {update.event} for {order.client_order_id} not applied: {e}; the order's next "
                "read brings it from Alpaca's fill activities",
            )
        else:
            self._count_fill(
                order,
                record,
                venue_order_id,
                trade_id,
                last_qty,
                last_px,
                ts_event,
                f"{update.event}",
            )
        client_order_id = order.client_order_id
        if self._ended(client_order_id):
            return
        alpaca_order = update.order
        leg_id = str(alpaca_order.id)
        shown = self._shown(record, alpaca_order)
        if shown > self._held(order, record) and not await self._catch_up(
            order, record, shown, VenueOrderId(leg_id), walk=False
        ):
            self._catch_up_later(client_order_id, shown, VenueOrderId(leg_id))

    @staticmethod
    def _shown(record: OrderRecord, alpaca_order: AlpacaOrder) -> Decimal:
        """The shares an update's copy of one of the chain's Alpaca orders shows the node's order filled,
        at most its quantity: the order Alpaca shows filled counts its whole quantity."""
        leg_id = str(alpaca_order.id)
        earlier = record.earlier_filled(leg_id) if alpaca_order.replaces is not None else Decimal(0)
        own = share_count(alpaca_order.filled_qty)
        shown = chain_filled(own, earlier) if own is not None else Decimal(0)
        if alpaca_order.status == AlpacaOrderStatus.FILLED:
            whole = share_count(alpaca_order.qty)
            if whole is not None:
                shown = max(shown, chain_quantity(whole, earlier))
        return min(shown, record.quantity)

    async def _behind_current(self, record: OrderRecord, alpaca_order: AlpacaOrder) -> bool | None:
        """Whether a replacement the record has not adopted lies behind the order the record is on now: its
        `replaces` links, from the record or read at Alpaca, never reach that order. Such an order is not
        adopted (ADR 0075: a fill on the middle order of a chain moved the node's
        order back to it); one whose links reach it is ahead of the record and adopted, so a modify's
        replacement takes its quantity before its fills count. None,
        with a warning, when a link cannot be read or the chain runs past `MAX_REPLACE_CHAIN`: the order
        cannot be placed, and neither guess is safe (ADR 0075: taken as ahead, it
        moved the node's order back; taken as behind, a raised quantity would end it early)."""
        current = record.current
        link = str(alpaca_order.replaces) if alpaca_order.replaces is not None else None
        try:
            for _ in range(MAX_REPLACE_CHAIN):
                if current is None or link is None:
                    return current is not None
                if link == current:
                    return False
                leg = record.legs.get(link)
                if leg is not None and leg.replaces is not None:
                    link = leg.replaces
                    continue
                read = await self._call(self._venue().get_order_by_id, link)
                if not isinstance(read, AlpacaOrder):
                    raise TypeError("alpaca-py returned raw order data")
                link = str(read.replaces) if read.replaces is not None else None
        except (APIError, OSError, ValueError, TypeError) as e:
            self._log.warning(
                f"Alpaca order {link} of {record.client_order_id}'s chain could not be read ({e!r}); the "
                f"replacement {alpaca_order.id} is not placed, and its fills wait for the next read"
            )
            return None
        self._log.warning(
            f"The chain of {alpaca_order.id} is longer than {MAX_REPLACE_CHAIN} orders; the replacement "
            "is not placed, and its fills wait for the next read"
        )
        return None

    async def _end_after_fills(
        self, order: Any, record: OrderRecord, ending: Ending, walk: bool
    ) -> None:
        """End the node's order as Alpaca did, `canceled` or `expired`, after the fills Alpaca shows on it
        that the node does not hold, brought by their trade IDs (`_catch_up`).

        Ended at once, `_finish` drops the order's record and every later update for it, so a fill the
        stream lost before the end would never be applied (ADR 0075);
        a status report showing the end and more filled is held back from the engine, which would infer
        the fill (`_settled_report`), and the paper node's checks, reading open orders only, never ask
        again. So the end waits on the record while the activities lag (`_catch_up_later`), and is sent
        once the node holds what it showed: after the fills, on the same queue, so the engine applies them
        first. When the reads run out the order ends with what the node holds, with an error."""
        record.end = ending
        if ending.shown > self._held(order, record) and not await self._catch_up(
            order, record, ending.shown, ending.venue_order_id, walk=walk
        ):
            self._catch_up_later(order.client_order_id, ending.shown, ending.venue_order_id)
            return
        self._send_end(order.client_order_id)

    def _send_end(self, client_order_id: ClientOrderId, ran_out: bool = False) -> None:
        """Send the end waiting on the node's order once it holds the fills the end showed, or, with
        `ran_out`, with what it holds."""
        record = self._records.get(client_order_id)
        order = self.cache.order(client_order_id)
        if record is None or record.end is None or order is None or self._ended(client_order_id):
            return
        ending = record.end
        held = self._held(order, record)
        if held < ending.shown:
            if not ran_out:
                return
            self._ran_out(
                client_order_id,
                ending.shown,
                held,
                f"the order ends {ending.event} with what the node holds",
            )
        self._finish(client_order_id)
        if ending.event == "canceled":
            self.generate_order_canceled(order, ending.venue_order_id, ending.ts_event)
        else:
            self.generate_order_expired(order, ending.venue_order_id, ending.ts_event)

    def _count_fill(
        self,
        order: Any,
        record: OrderRecord,
        venue_order_id: VenueOrderId,
        trade_id: str,
        last_qty: Quantity,
        last_px: Price,
        ts_event: int,
        source: str,
    ) -> bool:
        """`OrderFilled` for one Alpaca fill unless the node holds its trade ID, and the node's order ended
        once its fills reach its quantity; whether it was counted.

        A fill is known by Alpaca's trade ID alone, from every source (ADR 0075): the stream's
        `execution_id` and a fill activity's trade ID are the same UUID (ADR 0024), and NautilusTrader's
        order "enforces one quantity-bearing fill per `trade_id`" ("Stable `trade_id` values let the engine
        deduplicate the same fill across both channels", `docs/concepts/execution/index.md` at the pin).
        An ID the record or the node's order holds is not counted again. The order ends on the record's own
        count, not on Alpaca's event name, so a fill on the order a replace superseded that arrives after
        the replacement's `fill` is still applied (as upstream's Binance spot keeps a superseded venue
        order bound to the node's order, `record_order_update_from_fill`). A fill on an Alpaca order the
        node's order never held is applied under the order it is on now, as NautilusTrader's
        `reconcile_fill_report` applies a fill report under the order's own venue order ID
        (`crates/execution/src/reconciliation/orders.rs` at the pin): applied under its own, it would move
        the node's order back to an order Alpaca has replaced."""
        client_order_id = order.client_order_id
        if trade_id in record.trades or trade_id in {str(t) for t in order.trade_ids}:
            self._log.info(
                f"Alpaca {source} {trade_id} for {client_order_id} was counted already; ignored"
            )
            return False
        record.trades.add(trade_id)
        self._acknowledged.pop(client_order_id, None)  # a fill implies it
        current = record.current
        if (
            current is not None
            and venue_order_id.value != current
            and venue_order_id.value not in {v.value for v in order.venue_order_ids}
        ):
            venue_order_id = VenueOrderId(current)
        record.filled = self._held(order, record) + last_qty.as_decimal()
        self._filled(order, venue_order_id, trade_id, last_qty, last_px, ts_event)
        if record.filled >= record.quantity:
            self._finish(client_order_id)
        else:
            self._send_end(client_order_id)  # an end waiting for this fill
        return True

    @staticmethod
    def _held(order: Any, record: OrderRecord) -> Decimal:
        """The shares the node holds filled: the record's own count of what it has applied, or the node's
        order's, whichever is more (the cache takes a generated fill later, and a fill report the engine
        applied is only in the cache)."""
        return max(record.filled, order.filled_qty.as_decimal())

    async def _catch_up(
        self,
        order: Any,
        record: OrderRecord,
        shown: Decimal,
        venue_order_id: VenueOrderId,
        walk: bool = True,
        logged: bool = True,
    ) -> bool:
        """Bring the fills Alpaca shows on a node order and the node does not hold, from Alpaca's fill
        activities for every order of its replace chain, each under its own trade ID, price and time
        (`_count_fill`); whether the node then holds `shown` shares, or the order has ended.

        So the engine never infers a fill for an order the node holds (ADR 0075): it infers one from a
        status report showing more filled than the order, under an ID of its own, which no later source
        can be matched to by trade ID. The chain is the record's orders and `venue_order_id`'s, walked at
        Alpaca too unless `walk` is false (a trade update, whose orders the record holds, so the updates
        queued behind it wait on no walk). Activities are counted oldest first. A trade ID listed with two
        quantities or prices cannot be told apart, and nothing of the order is brought."""
        client_order_id = order.client_order_id
        if not await self._adopt_if_ahead(order, record, venue_order_id):
            return False  # its place in the chain is not known yet: held for the later reads
        if self._ended(client_order_id):
            return True  # an update applied during the read ended it
        order = self.cache.order(client_order_id) or order
        chain = set(record.legs) | {venue_order_id.value}
        try:
            if walk:
                chain |= await self._chain_ids(venue_order_id)
            listed = self._listed_fills(await self._fill_activities(None, chain), warn=True)
        except (APIError, OSError, TypeError, KeyError, ValueError) as e:
            self._log.warning(
                f"Alpaca fill activities of {client_order_id} could not be read "
                f"({failure_cause(e)}); its fills Alpaca shows are not reported until they are read"
            )
            return False
        if listed is None:
            self._log.warning(
                f"Alpaca lists a fill of {client_order_id} twice with different terms; its fills are not "
                "reported until they agree"
            )
            return False
        if self._ended(client_order_id):
            if logged:
                self._caught_up(client_order_id, 1, shown, shown)
            return True  # an update applied during the read ended it
        order = self.cache.order(client_order_id) or order
        record = self._records.get(client_order_id, record)
        known = record.trades | {str(t) for t in order.trade_ids}
        # Each order whose fills are to be counted is placed in the chain first, whichever order the read
        # was asked for: a replacement ahead is adopted, so its quantity applies before its fills; one not
        # placed keeps its fills for the later reads.
        unplaced: set[str] = set()
        for leg in sorted(
            {r.venue_order_id.value for r in listed.values() if r.trade_id.value not in known}
        ):
            if not await self._adopt_if_ahead(order, record, VenueOrderId(leg)):
                unplaced.add(leg)
            if self._ended(client_order_id):
                return True  # an update applied during the reads ended it
        order = self.cache.order(client_order_id) or order
        record = self._records.get(client_order_id, record)
        known = record.trades | {str(t) for t in order.trade_ids}
        for report in sorted(listed.values(), key=lambda r: r.ts_event):
            if report.trade_id.value in known or report.venue_order_id.value in unplaced:
                continue
            self._count_fill(
                order,
                record,
                report.venue_order_id,
                report.trade_id.value,
                report.last_qty,
                report.last_px,
                report.ts_event,
                "fill activity",
            )
            if self._ended(client_order_id):
                if logged:
                    self._caught_up(client_order_id, 1, shown, shown)
                return True
        held = self._held(order, record)
        if logged and held >= shown and not unplaced:
            self._caught_up(client_order_id, 1, shown, held)
        return held >= shown and not unplaced

    async def _adopt_if_ahead(
        self, order: Any, record: OrderRecord, venue_order_id: VenueOrderId
    ) -> bool:
        """Place the Alpaca order a read showed in the replace chain before its fills are brought, and adopt
        it when it is a replacement ahead of the order the record is on, so its quantity applies first;
        whether it was placed. Upstream emits "an `OrderUpdated` if the venue snapshot shows a confirmed
        quantity/price amendment" before the fills, since a fill "would otherwise close the order under the
        stale local quantity" (`generate_reconciliation_order_events`, nautilus-trader@d53849b216e2
        `crates/execution/src/reconciliation/orders.rs:45-53`; its pre-fill events applied before the fills
        in `reconcile_order_with_fills`, `crates/live/src/execution/manager.rs:4522`). A modify raising the
        quantity whose `replaced` was lost otherwise ended the node's order FILLED on the old quantity
        (ADR 0075). One read, made only for an order the record is not on; an
        order that cannot be read or placed is not placed, and its fills wait for the later reads."""
        target = venue_order_id.value
        if record.current is None or target == record.current or target in record.superseded:
            return True
        try:
            read = await self._call(self._venue().get_order_by_id, target)
            if not isinstance(read, AlpacaOrder):
                raise TypeError("alpaca-py returned raw order data")
        except (APIError, OSError, ValueError, TypeError) as e:
            self._log.warning(
                f"Alpaca order {target} of {order.client_order_id}'s chain could not be read ({e!r}); its "
                "fills wait for the next read"
            )
            return False
        if read.replaces is None:
            record.superseded.add(
                target
            )  # the chain's first order, behind the one the record is on
            return True
        behind = await self._behind_current(record, read)
        if behind is None:
            return False
        if behind:
            record.superseded.add(target)  # so it is not read again
            return True
        if self._ended(order.client_order_id):
            return True
        if read.status == AlpacaOrderStatus.REJECTED or record.took_over(target) is False:
            return True  # rejected, or left behind by another replace: not the node's order now
        if read.status == AlpacaOrderStatus.REPLACED:
            live = await self._live_end(read, {})  # replaced since: the order now working
            if live is None:
                return False
            read = live
            target = str(read.id)
        # The reads above awaited: an update may have moved the record meanwhile.
        if self._ended(order.client_order_id):
            return True
        if target == record.current or target in record.superseded:
            return True
        try:
            self._adopt(order, record, read, self.clock.timestamp_ns())
        except ValueError as e:
            self._log.warning(f"Alpaca order {target} is not adopted: {e}")
            return False
        return True

    def _caught_up(
        self, client_order_id: ClientOrderId, read: int, shown: Decimal, held: Decimal
    ) -> None:
        """The line a paper run counts: which read of Alpaca's fill activities brought the fills an Alpaca
        read showed (ADR 0075's Confirmation weighs `FILL_READ_TRIES` and `FILL_READ_RETRY_SECS` by it)."""
        self._log.info(
            f"Alpaca fill catch-up for {client_order_id}: caught up on read {read} of {FILL_READ_TRIES} "
            f"({shown} shown, {held} held)"
        )

    def _ran_out(
        self, client_order_id: ClientOrderId, shown: Decimal, held: Decimal, follows: str
    ) -> None:
        """The error a paper window treats as not ok: the reads ran out with fills Alpaca shows unlisted."""
        self._log.error(
            f"Alpaca fill catch-up for {client_order_id}: ran out after {FILL_READ_TRIES} reads ({shown} "
            f"shown, {held} held); {follows}"
        )

    def _listed_fills(self, activities: list, warn: bool = False) -> dict[str, FillReport] | None:
        """Alpaca's fill activities as fill reports by trade ID, each fill once, or None when one trade
        ID is listed with two quantities, prices or times, which cannot be told apart. An activity that
        cannot be read is skipped, with a warning if `warn`."""
        ts_init = self.clock.timestamp_ns()
        listed: dict[str, FillReport] = {}
        for activity in activities:
            try:
                report = fill_report(activity, ts_init)
            except (KeyError, TypeError, ValueError, ArithmeticError) as e:
                if warn:
                    name = activity.get("id") if isinstance(activity, dict) else repr(activity)
                    self._log.warning(f"Alpaca fill activity {name} is not reported: {e}")
                continue
            seen = listed.setdefault(report.trade_id.value, report)
            if fill_terms(seen) != fill_terms(report):
                return None
        return listed

    def _catch_up_later(
        self,
        client_order_id: ClientOrderId,
        shown: Decimal,
        venue_order_id: VenueOrderId,
        *,
        keep_reading: bool = False,
    ) -> None:
        """Read the fill activities again, `FILL_READ_TRIES` times in all, `FILL_READ_RETRY_SECS` apart,
        for fills a trade update shows and Alpaca does not list yet: nothing else would read them while
        Alpaca shows the order closed, since the engine's open-order check reads open orders only by
        default (`open_check_open_only`). These reads walk the replace chain at Alpaca, which the update's
        own read does not. An update showing more while the reads wait, or during one, widens them and
        gives them their reads again. Past the last read the order is left with an error, as Alpaca shows
        it, to the next start-up. A retained submit whose cancel found it filled has no automatic
        recovery queries left: `keep_reading` keeps these reads going until it fills or disconnects,
        naming and counting each unsuccessful attempt."""
        if self._ended(client_order_id):
            return
        waiting = self._catch_up_targets.get(client_order_id)
        self._catch_up_targets[client_order_id] = max(shown, waiting or Decimal(0))
        if keep_reading:
            self._retained_catch_up.add(client_order_id)
        if client_order_id in self._catching_up:
            return

        async def later() -> None:
            target, tries = shown, 1  # the update's own read was the first
            try:
                while client_order_id in self._retained_catch_up or tries < FILL_READ_TRIES:
                    if client_order_id in self._retained_catch_up:
                        self._log.warning(
                            f"Alpaca retained submit fill read {tries} failed for {client_order_id}; "
                            "activities are incomplete; reading again until filled or the node stops"
                        )
                    await asyncio.sleep(FILL_READ_RETRY_SECS)
                    if self._stopping:
                        return
                    target, tries = self._widened(client_order_id, target, tries)
                    order = self.cache.order(client_order_id)
                    if order is None or self._ended(client_order_id):
                        return
                    record = self._record(order)
                    caught = self._held(order, record) >= target or await self._catch_up(
                        order, record, target, venue_order_id, logged=False
                    )
                    if (
                        self._ended(client_order_id)
                        and self._catching_up.get(client_order_id) is asyncio.current_task()
                    ):
                        return  # the stream or engine ended it while this read was out
                    widened, tries = self._widened(client_order_id, target, tries + 1)
                    if caught and widened == target:
                        if client_order_id in self._retained_catch_up:
                            self._log.info(
                                f"Alpaca retained submit fill catch-up for {client_order_id}: "
                                f"caught up on read {tries} ({target} shown)"
                            )
                        else:
                            self._caught_up(
                                client_order_id,
                                tries,
                                target,
                                max(self._held(order, record), target),
                            )
                        self._send_end(client_order_id)
                        return
                    target = widened
                record = self._records.get(client_order_id)
                if record is not None and record.end is not None:
                    self._send_end(client_order_id, ran_out=True)
                    return
                order = self.cache.order(client_order_id)
                held = (
                    self._held(order, record) if order is not None and record is not None else None
                )
                self._ran_out(
                    client_order_id,
                    target,
                    held if held is not None else Decimal(0),
                    "the node's order holds what the activities list",
                )
            finally:
                if self._catching_up.get(client_order_id) is asyncio.current_task():
                    self._catching_up.pop(client_order_id, None)
                    self._catch_up_targets.pop(client_order_id, None)
                    self._retained_catch_up.discard(client_order_id)

        self._catching_up[client_order_id] = asyncio.ensure_future(later())

    def _widened(
        self, client_order_id: ClientOrderId, target: Decimal, tries: int
    ) -> tuple[Decimal, int]:
        """The count the later reads wait for and the reads it has had: a larger count a later update
        showed starts its reads again, after the update's own."""
        latest = self._catch_up_targets.get(client_order_id, target)
        return (latest, 1) if latest > target else (target, tries)

    def _filled(
        self,
        order: Any,
        venue_order_id: VenueOrderId,
        trade_id: str,
        last_qty: Quantity,
        last_px: Price,
        ts_event: int,
    ) -> None:
        self.generate_order_filled(
            order,
            venue_order_id,
            None,
            TradeId(trade_id),
            last_qty,
            last_px,
            USD,
            None,
            LiquiditySide.NO_LIQUIDITY_SIDE,
            ts_event,
        )

    def _on_stream_exit(self, exit: StreamExit) -> None:
        stream, error = exit
        if stream is self._stream and not self._stopping:
            self._log.error(f"Alpaca trade updates stream ended while connected: {error!r}")

    async def _on_trade_update(self, update: TradeUpdate | RawData) -> None:
        """On the stream's thread: hand the update to the node's loop, where it is queued in order."""
        if self._owner_loop is None:
            return
        if isinstance(update, TradeUpdate):
            post(self._owner_loop, self._enqueue_update, update)
        else:  # alpaca-py hands raw data only to a stream built with `raw_data=True`
            post(
                self._owner_loop,
                self._log.error,
                f"Alpaca trade update not parsed: {update!r:.200}",
            )

    def _enqueue_update(self, update: TradeUpdate) -> None:
        if not self._stopping:
            self._updates.put_nowait(update)

    # -- reconciliation reports --------------------------------------------------------------------

    @override
    async def _generate_order_status_report(
        self, command: live.GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        return await self._report_result(
            "order status report failed", self._order_status_report(command)
        )

    @override
    async def _generate_order_status_reports(
        self, command: live.GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        return await self._report_result(
            "order status reports failed", self._order_status_reports(command)
        )

    async def _report_result[R](self, what: str, step: Awaitable[R]) -> R:
        """The report-hook error contract of `logged_failure`, without logging a hold's end twice."""
        try:
            return await step
        except HeldReportError:
            raise
        except Exception as e:
            self._log.error(f"Alpaca {what}: {e}")
            raise

    @override
    async def _generate_fill_reports(self, command: live.GenerateFillReports) -> list[FillReport]:
        return await logged_failure(self._log, "fill reports failed", self._fill_reports(command))

    @override
    async def _generate_position_status_reports(
        self, command: live.GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        return await logged_failure(
            self._log, "position status reports failed", self._position_status_reports(command)
        )

    @override
    async def _query_order(self, command: live.QueryOrder) -> None:
        """Read one order from Alpaca and hand its status report to the engine.

        NautilusTrader's in-flight check sends `QueryOrder` for an order left `SUBMITTED`, `PENDING_CANCEL`
        or `PENDING_UPDATE` past its threshold, and its open-order check for an order the venue's open
        orders leave out; past the last retry an order never answered is resolved locally, a `SUBMITTED`
        one as rejected (`check_inflight_orders`, `crates/live/src/execution/manager.rs` at the pinned
        commit). Upstream's Binance spot, Bybit, Coinbase, Kraken spot and OKX adapters answer it the same
        way (`query_order` in each `execution.rs` at the pinned commit): the order's status report, sent
        to the engine; an order not found or a read that fails is logged and nothing is sent, so the
        engine's next check asks again. Here the report is `_order_status_report`'s, the one start-up
        reconciliation reads, and it goes to the engine through the base class's `_handle_report`.
        """
        try:
            report = await self._order_status_report_by(
                command.venue_order_id, command.client_order_id
            )
        except (APIError, OSError, ValueError, TypeError, RuntimeError) as e:
            self._log.warning(
                f"Alpaca order query for {command.client_order_id} failed ({e!r}); no report sent"
            )
            return
        if report is None:
            self._log.warning(
                f"Alpaca holds no order {command.venue_order_id or command.client_order_id} for the "
                f"query of {command.client_order_id}; no report sent"
            )
            return
        try:
            if report.client_order_id in self._finished:
                assert report.client_order_id is not None
                await self._hold_report(
                    report.client_order_id,
                    lambda report=report: self._finished_report(report, deliver=True),
                )
                return
            report = await self._settled_report(report)
        except HeldReportError:
            return  # the hold logged its end; the command worker must not log it a second time
        if report is not None:
            self._handle_report(report)

    async def _send_report(
        self, held: AlpacaOrder, fills: list[FillReport] | None = None
    ) -> OrderStatusReport | None:
        """Send a mapped report, with complete companion fills kept in the same engine delivery."""
        # Identify an already known finished order without REST. Mapping may itself walk the root
        # or sum earlier fills, so it belongs inside the same deadline as settlement on the stream.
        candidates = (
            holdable_id(held.client_order_id),
            self._index.get(str(held.id)),
            self._held_by_venue_id(str(held.id)),
        )
        finished_id = next((cid for cid in candidates if cid in self._finished), None)
        if finished_id is not None:

            async def finished_snapshot() -> OrderStatusReport | None:
                report = await self._order_report(held)
                if report is None:
                    return None
                return await self._finished_report(report, fills, deliver=True)

            return await self._hold_report(finished_id, finished_snapshot)
        try:
            report = await self._order_report(held)
        except (APIError, OSError, ValueError, TypeError, RuntimeError) as e:
            cause = e.__cause__ or e
            # Mapping ValueErrors name the unsupported field. Transport and SDK exceptions can
            # contain order IDs in their body, URL or model input: name their class only.
            reason = str(cause) if type(cause) is ValueError else failure_cause(cause)
            reason = reason.replace(str(held.id), "[venue order]")
            if held.replaces is not None:
                reason = reason.replace(str(held.replaces), "[prior venue order]")
            if fills is not None:
                self._leave_external_to_reconciliation(held, reason)
            else:
                self._log.warning(f"{order_description(held)} could not be reported ({reason})")
            return None
        # Mapping can yield while the order ends. Settlement owns the one delivery, including
        # checking whether these companion fills have overtaken the mapped snapshot.
        if report is not None and report.client_order_id in self._finished:
            assert report.client_order_id is not None
            return await self._hold_report(
                report.client_order_id,
                lambda report=report: self._finished_report(report, fills, deliver=True),
            )
        if report is not None and fills is not None:
            fills = [report_with(fill, venue_order_id=report.venue_order_id) for fill in fills]
            if fills_count(fills) != report.filled_qty.as_decimal():
                self._leave_external_to_reconciliation(
                    held,
                    f"shows {report.filled_qty} filled, stream trade IDs account for {fills_count(fills)}",
                )
                return None
            self._handle_report(report, fills or None)
            return report
        report = await self._settled_report(report) if report is not None else None
        if report is not None:
            self._handle_report(report)
        return report

    async def _hold_report[R](
        self, client_order_id: ClientOrderId, request: Callable[[], Awaitable[R]]
    ) -> R:
        """One deadline for mapping, REST and cache waits, with spaced retries after a timeout.

        Targeted errors mark coverage incomplete (execution/reconciliation.rs:530-556); a successful
        None would let the manager resolve a real order as missing. The worker call can outlive its
        canceled await, retaining the REST lock until it returns (_call), but not this stream hold.
        """

        def cache_state() -> tuple[bool, Decimal]:
            order = self.cache.order(client_order_id)
            return (
                (order.is_closed, order.filled_qty.as_decimal())
                if order is not None
                else (False, Decimal(0))
            )

        failures = self._report_hold_failures
        initial_state = cache_state()
        failed = failures.get(client_order_id)
        now = monotonic()
        if failed is not None:
            progressed = initial_state[0] and initial_state != failed.state
            if not progressed and (not initial_state[0] or now < failed.retry_at):
                if now >= failed.log_after:
                    self._log.warning(
                        f"Alpaca held report for {client_order_id} omitted: a previous hold timed "
                        "out; waiting for cache closure or the retry spacing "
                        f"HELD_REPORT_RETRY_SECS={HELD_REPORT_RETRY_SECS} s"
                    )
                    failed.log_after = now + HELD_REPORT_RETRY_SECS
                raise HeldReportError(failed.message)
            # Reserve the retry before yielding, so another request cannot retry alongside it.
            failed.state = initial_state
            failed.retry_at = now + HELD_REPORT_RETRY_SECS
        try:
            async with asyncio.timeout(HELD_REPORT_TIMEOUT_SECS):
                result = await request()
        except TimeoutError:
            if self._stopping or failures is not self._report_hold_failures:
                message = (
                    f"Alpaca held report for {client_order_id} ended after disconnect; omitted"
                )
                self._log.warning(message)
                raise HeldReportError(message) from None
            message = (
                f"Alpaca held report for {client_order_id} timed out after "
                f"{HELD_REPORT_TIMEOUT_SECS} s: mapping, REST and cache settlement exceeded the "
                "whole hold bound; report omitted; retries after cache closure are spaced by "
                f"HELD_REPORT_RETRY_SECS={HELD_REPORT_RETRY_SECS} s"
            )
            next_attempt = monotonic() + HELD_REPORT_RETRY_SECS
            failures[client_order_id] = ReportHoldFailure(
                initial_state, next_attempt, next_attempt, message
            )
            self._log.error(message)
            raise HeldReportError(message) from None
        except asyncio.CancelledError:
            self._log.warning(
                f"Alpaca held report for {client_order_id} ended: request canceled during settlement"
            )
            raise
        self._check_report_session(client_order_id, failures)
        if failures.get(client_order_id) is failed:
            failures.pop(client_order_id, None)
        return result

    def _reread_report_later(
        self, client_order_id: ClientOrderId, venue_order_id: VenueOrderId
    ) -> None:
        """Read a finished order again, `HELD_REPORT_REREADS` times at most and
        `HELD_REPORT_RETRY_SECS` apart, after a stream update's hold on it failed: by a timeout, whose
        retry would otherwise wait for a request that never comes, because Alpaca's activities did
        not list the fills the update showed yet, or because a read for the snapshot's mapping
        failed. Each read is the order's own, fresh, and goes through the same hold and the same
        delivery as the update's; fills reach the engine only by their trade IDs, never inferred. A
        re-read and a later update can each queue the same fills before the cache shows them: the
        engine's bundle path drops a fill whose trade ID the order holds (`reconcile_fill_report`,
        nautilus-trader@d53849b216e2 `crates/execution/src/reconciliation/orders.rs:785-791`, called
        per fill at `engine/mod.rs:1769`). One task per order; an update that fails while it waits
        adds none. Canceled at disconnect. Past the last read the order is left, by name, to the
        next start-up."""
        if (
            self._stopping
            or client_order_id not in self._finished
            or client_order_id in self._report_rereads
        ):
            return
        failures = self._report_hold_failures

        async def later() -> None:
            try:
                for _ in range(HELD_REPORT_REREADS):
                    failed = failures.get(client_order_id)
                    spaced = 0.0 if failed is None else failed.retry_at - monotonic()
                    await asyncio.sleep(max(HELD_REPORT_RETRY_SECS, spaced))
                    if self._stopping or failures is not self._report_hold_failures:
                        return
                    try:
                        report = await self._order_status_report_by(venue_order_id, client_order_id)
                        if report is None or report.client_order_id != client_order_id:
                            self._log.warning(
                                f"Alpaca held report for {client_order_id}: a timed re-read did "
                                "not find the order as the node's"
                            )
                            continue
                        await self._hold_report(
                            client_order_id,
                            lambda report=report: self._finished_report(report, deliver=True),
                        )
                    except HeldReportError:
                        continue  # the hold logged its order and why it ended
                    except (APIError, OSError, ValueError, TypeError, RuntimeError) as e:
                        # Transport and SDK errors can carry order IDs: name the class only.
                        reason = str(e) if type(e) is ValueError else failure_cause(e)
                        self._log.warning(
                            f"Alpaca held report for {client_order_id}: a timed re-read failed "
                            f"({reason})"
                        )
                        continue
                    return
                self._log.error(
                    f"Alpaca held report for {client_order_id}: {HELD_REPORT_REREADS} timed re-reads, "
                    f"{HELD_REPORT_RETRY_SECS} s apart, did not settle it; any fill Alpaca shows "
                    "beyond the node's is left to the next start-up"
                )
            finally:
                if self._report_rereads.get(client_order_id) is asyncio.current_task():
                    self._report_rereads.pop(client_order_id, None)

        self._report_rereads[client_order_id] = asyncio.ensure_future(later())

    def _check_report_session(
        self,
        client_order_id: ClientOrderId | None,
        failures: dict[ClientOrderId, ReportHoldFailure],
    ) -> None:
        if self._stopping or failures is not self._report_hold_failures:
            message = f"Alpaca held report for {client_order_id} ended after disconnect; omitted"
            self._log.warning(message)
            raise HeldReportError(message)

    async def _wait_report_cache(
        self, client_order_id: ClientOrderId, ready: Callable[[Any], bool]
    ) -> Any:
        """Wait for actual engine state inside _hold_report's single deadline.

        The pinned node polls report futures before queued events (node/mod.rs:1653-1877), so neither
        `_finished` nor one event-loop turn proves that the engine applied the generated end.
        """
        order = self.cache.order(client_order_id)
        waited = order is None or not ready(order)
        if waited:
            self._log.info(
                f"Alpaca report for {client_order_id} held: waiting for the cache to close the ended "
                "order and apply its reported fills"
            )
        while order is None or not ready(order):
            await asyncio.sleep(0.01)
            order = self.cache.order(client_order_id)
        if waited:
            self._log.info(
                f"Alpaca report for {client_order_id} released: the cache closed the ended order "
                "and applied its reported fills"
            )
        return order

    async def _finished_report(
        self,
        report: OrderStatusReport,
        companion_fills: list[FillReport] | None = None,
        *,
        deliver: bool = False,
    ) -> OrderStatusReport:
        """Settle an ended order with real fills before delivering or returning its status once.

        The continuous path is execution/src/engine/mod.rs:1679-1800: real trade IDs, then an
        authoritative snapshot which can void fills (execution/src/reconciliation/orders.rs:117-157).
        Re-read an older snapshot before pairing it with newer fills; never infer a missing trade.
        Send callers deliver one bundle here. Return hooks emit only FillReports here and wait for
        their application before returning the status, so no OrderStatusReport is delivered twice.
        Late partial fills preserve CANCELED/EXPIRED (model/src/orders/mod.rs:1355-1376).
        """
        client_order_id = report.client_order_id
        assert client_order_id is not None
        failures = self._report_hold_failures
        self._check_report_session(client_order_id, failures)
        order = await self._wait_report_cache(client_order_id, lambda held: held.is_closed)
        self._check_report_session(client_order_id, failures)
        shown = report.filled_qty.as_decimal()
        if deliver and shown <= order.filled_qty.as_decimal() and not companion_fills:
            # A late ACCEPTED/TRIGGERED snapshot cannot add a fill; forwarding it can only attempt
            # an invalid transition on the closed order (reconciliation/orders.rs:327-420).
            return report
        try:
            if not deliver and shown < order.filled_qty.as_decimal():
                # Startup mass status uses is_snapshot=true (manager.rs:980-988), unlike a plain
                # continuous report: returning an old total here could void a genuine cached fill.
                newer = await self._order_status_report_by(report.venue_order_id, client_order_id)
                if newer is None or newer.client_order_id != client_order_id:
                    raise ValueError("newer order snapshot unavailable for its cached fills")
                report = newer
                shown = report.filled_qty.as_decimal()
                order = self.cache.order(client_order_id) or order
                if shown < order.filled_qty.as_decimal():
                    raise ValueError("newer order snapshot still shows fewer fills than the cache")
            if shown == order.filled_qty.as_decimal() and not companion_fills:
                return report
            if companion_fills is None:
                chain = {v.value for v in order.venue_order_ids}
                chain |= await self._chain_ids(report.venue_order_id)
                listed = self._listed_fills(await self._fill_activities(None, chain), warn=True)
                if listed is None:
                    raise ValueError("fill activities disagree on a trade ID's terms")
                companion_fills = list(listed.values())

            def unseen() -> list[FillReport]:
                # Re-read after every await: a concurrent request may have delivered these fills.
                current = self.cache.order(client_order_id) or order
                known = {str(t) for t in current.trade_ids}
                return [
                    report_with(fill, venue_order_id=report.venue_order_id)
                    for fill in sorted(companion_fills, key=lambda fill: fill.ts_event)
                    if fill.trade_id.value not in known
                ]

            fills = unseen()
            order = self.cache.order(client_order_id) or order
            total = order.filled_qty.as_decimal() + fills_count(fills)
            if fills and total > shown:
                newer = await self._order_status_report_by(report.venue_order_id, client_order_id)
                if newer is None or newer.client_order_id != client_order_id:
                    raise ValueError("newer order snapshot unavailable for its listed fills")
                report = newer
                shown = report.filled_qty.as_decimal()
                fills = unseen()
                order = self.cache.order(client_order_id) or order
                total = order.filled_qty.as_decimal() + fills_count(fills)
            if total < shown or (fills and total != shown):
                raise ValueError(
                    f"fill activities account for {total} shares, snapshot shows {shown}"
                )
        except (APIError, OSError, ValueError, TypeError, KeyError, RuntimeError) as e:
            message = (
                f"Alpaca held report for {client_order_id} ended: additional fills unread ({e!r})"
            )
            self._log.error(message)
            raise HeldReportError(message) from e
        self._check_report_session(client_order_id, failures)
        if deliver:
            if fills:
                self._handle_report(report, fills)
            # Delivery is complete. A cache wait after this point could falsely time out and
            # poison retry state for a report already queued in the engine.
            return report
        for fill in fills:
            # A refreshed replacement ID may not be indexed yet. Individual fills must use
            # the cached leg until the returned status reconciles that replacement.
            self._handle_report(
                report_with(fill, venue_order_id=order.venue_order_id or report.venue_order_id)
            )
        order = await self._wait_report_cache(
            client_order_id,
            lambda held: held.is_closed and held.filled_qty.as_decimal() >= shown,
        )
        self._check_report_session(client_order_id, failures)
        if shown < order.filled_qty.as_decimal():
            message = (
                f"Alpaca held report for {client_order_id} omitted: cache fills overtook snapshot"
            )
            self._log.error(message)
            raise HeldReportError(message)
        return report

    async def _settled_report(self, report: OrderStatusReport) -> OrderStatusReport | None:
        """`report`, once the node's order has applied every fill it shows, or None to hold it back.

        The engine infers a fill from a status report showing more filled than the node's order, under
        an ID of its own (`report.filled_qty - order.filled_qty()`, `reconcile_order_report` at the pin),
        which no later source of that fill can be matched to by trade ID (ADR 0075). So a report on an
        order the node holds open reaches the engine only when the order, as the node's cache holds it,
        shows at least as many filled. Short of that, the fills the node does not hold are brought by
        their own trade IDs from Alpaca's fill activities (`_catch_up`) and the report is held back: a
        report returned to the engine can reach it before the fills just generated do (observed), and the
        engine's next check asks again. While the activities do not list them, it is held back with a
        warning. An order the client has finished waits for the cache to close, bounded and logged;
        additional fills are reconciled by trade ID before the report is released. A timed-out hold
        raises, never returns a successful empty answer. An order the node does not hold is reported
        as read: reconciliation creates it with its fill reports (`_open_order_fills`)."""
        client_order_id = report.client_order_id
        order = self.cache.order(client_order_id) if client_order_id is not None else None
        if client_order_id in self._finished:
            assert client_order_id is not None
            return await self._hold_report(client_order_id, lambda: self._finished_report(report))
        if order is None or client_order_id is None or order.is_closed:
            return report
        shown = report.filled_qty.as_decimal()
        # The model's invariant: an order's filled total never goes down. A terminal report showing fewer
        # fills than the node holds, or than an end already waiting for its fills, is an older snapshot,
        # held until the reads catch up; the client ends the order itself (ADR
        # 0075). The engine keeps the same rule for its own cached total, a report below it being a "fill
        # decrease" it ignores without void evidence, though it still applies a canceled or expired one
        # (`has_material_fill_decrease`, `reconcile_fill_decrease_terminal`, nautilus-trader@d53849b216e2
        # `crates/execution/src/reconciliation/orders.rs:117-125, 206-240`); holding the end for an end
        # already waiting is this adapter's own.
        waiting = self._records.get(client_order_id)
        known = max(
            order.filled_qty.as_decimal(),
            waiting.filled if waiting is not None else Decimal(0),
            waiting.end.shown if waiting is not None and waiting.end is not None else Decimal(0),
            self._catch_up_targets.get(client_order_id, Decimal(0)),
        )
        if report.order_status in (OrderStatus.CANCELED, OrderStatus.EXPIRED) and known > shown:
            self._log.info(
                f"Alpaca's report shows {client_order_id} {report.order_status.name.lower()} with "
                f"{shown} filled, fewer than the {known} the node holds or waits for; held back as older"
            )
            # The end it shows still stands: the client sends it once the node holds what it waits for.
            record = self._record(order)
            if record.end is None:
                event = "canceled" if report.order_status == OrderStatus.CANCELED else "expired"
                record.end = Ending(event, report.venue_order_id, report.ts_last, known)
            if self._held(order, record) >= record.end.shown:
                self._send_end(client_order_id)
            else:
                self._catch_up_later(client_order_id, record.end.shown, report.venue_order_id)
            return None
        if shown <= order.filled_qty.as_decimal():
            return report
        record = self._record(order)
        if report.order_status in (OrderStatus.CANCELED, OrderStatus.EXPIRED):
            # Ended at Alpaca: the client ends it after its fills, since nothing may ask again once the
            # venue no longer lists it open (ADR 0075).
            event = "canceled" if report.order_status == OrderStatus.CANCELED else "expired"
            ending = Ending(event, report.venue_order_id, report.ts_last, shown)
            await self._end_after_fills(order, record, ending, walk=True)
            return None
        if self._held(order, record) < shown and not await self._catch_up(
            order, record, shown, report.venue_order_id
        ):
            self._log.warning(
                f"Alpaca shows {shown} of {client_order_id} filled and the node holds "
                f"{self._held(order, record)}; its status report is held back, and the fills not "
                "reported until Alpaca lists them"
            )
            return None
        self._log.info(
            f"Alpaca shows {shown} of {client_order_id} filled; its status report waits for the node's "
            "order to apply the fills brought by their trade IDs"
        )
        return None

    async def _order_status_report(
        self, command: live.GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        report = await self._order_status_report_by(command.venue_order_id, command.client_order_id)
        return await self._settled_report(report) if report is not None else None

    async def _order_status_report_by(
        self, venue_order_id: VenueOrderId | None, client_order_id: ClientOrderId | None
    ) -> OrderStatusReport | None:
        if venue_order_id is None and client_order_id is not None:
            # The node's own record or cache names the Alpaca order its order is on now: one read, where
            # a lookup by client order ID gives the chain's first order and a walk to the order working.
            record = self._records.get(client_order_id)
            held = self.cache.order(client_order_id)
            current = record.current if record is not None else None
            if current is None and held is not None and held.venue_order_id is not None:
                current = held.venue_order_id.value
            venue_order_id = VenueOrderId(current) if current is not None else None
        try:
            if venue_order_id is not None:
                found = await self._call(self._venue().get_order_by_id, venue_order_id.value)
            elif client_order_id is not None:
                found = await self._call(
                    self._venue().get_order_by_client_id, client_order_id.value
                )
            else:
                raise ValueError("an order status report needs a venue or client order ID")
        except APIError as e:
            if e.status_code == 404:
                return None
            raise
        walked: dict[str, AlpacaOrder] = {}
        for _ in range(MAX_REPLACE_CHAIN):
            if not isinstance(found, AlpacaOrder):
                raise TypeError(
                    "alpaca-py returned raw order data; the client must use raw_data=False"
                )
            if found.status != AlpacaOrderStatus.REPLACED or found.replaced_by is None:
                break
            # The node's IDs can name an order since replaced; the live order is its replacement.
            walked[str(found.id)] = found
            try:
                found = await self._call(self._venue().get_order_by_id, str(found.replaced_by))
            except APIError as e:
                if e.status_code != 404:
                    raise
                # As a 404 on the first lookup: Alpaca holds no such order, so there is no report.
                self._log.warning(
                    f"Alpaca order {found.id} was replaced by {found.replaced_by}, which Alpaca does "
                    "not find; no report"
                )
                return None
        if not isinstance(found, AlpacaOrder):
            raise TypeError("alpaca-py returned raw order data; the client must use raw_data=False")
        return await self._order_report(found, walked)

    async def _order_status_reports(
        self, command: live.GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        """Every open order, whenever it was submitted, up to `ORDERS_PAGE_LIMIT` (Alpaca's most per
        request; a full page is logged as possibly truncated), and, unless the command asks for open
        orders only, the orders submitted in its window.

        The open orders are read on their own, with no window, as upstream's Kraken spot adapter reads
        its open orders before its windowed order history (`request_order_status_reports_checked`,
        `crates/adapters/kraken/src/http/spot/client.rs` at the pinned commit): Alpaca's `after` and
        `until` filter on submission time, so a window would miss a working order placed before it, and
        a read of every status returns the newest `ORDERS_PAGE_LIMIT`, which newer closed orders can
        fill without it.
        """
        symbols = (
            [command.instrument_id.symbol.value] if command.instrument_id is not None else None
        )
        # Open orders first, as Kraken spot reads them: an order in both reads keeps the later copy, so
        # one that closed between the two is reported closed.
        requests = [
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=ORDERS_PAGE_LIMIT, symbols=symbols)
        ]
        if not command.open_only:
            requests.append(
                GetOrdersRequest(
                    status=QueryOrderStatus.ALL,
                    limit=ORDERS_PAGE_LIMIT,
                    after=unix_nanos_to_dt(command.start) if command.start is not None else None,
                    until=unix_nanos_to_dt(command.end) if command.end is not None else None,
                    symbols=symbols,
                )
            )
        found: dict[str, AlpacaOrder] = {}
        for request in requests:
            orders = await self._call(self._venue().get_orders, request)
            if not isinstance(orders, list):
                raise TypeError(
                    "alpaca-py returned raw order data; the client must use raw_data=False"
                )
            if len(orders) >= ORDERS_PAGE_LIMIT:
                self._log.warning(
                    f"Alpaca returned {len(orders)} orders, its most per request; older ones may be "
                    "missing",
                )
            found.update((str(order.id), order) for order in orders)
        # An order replaced between the two reads can be in the first as working and its replacement in
        # the second: the replacement is reported, under the node's ID, and the stale copy is not. A
        # rejected replacement does not count: the order it replaced may have filled first.
        # While Alpaca shows the old order `pending_replace`, the replace has not taken effect: the old
        # order is reported at its own terms and the replacement is not.
        superseded: set[str] = set()
        not_yet: set[str] = set()
        for order in found.values():
            if order.replaces is None or order.status == AlpacaOrderStatus.REJECTED:
                continue
            prior = found.get(str(order.replaces))
            if prior is not None and prior.status == AlpacaOrderStatus.PENDING_REPLACE:
                not_yet.add(str(order.id))
            else:
                superseded.add(str(order.replaces))
        reports = []
        for order in found.values():
            if str(order.id) in superseded or str(order.id) in not_yet:
                continue
            report = await self._order_report(order, found)
            try:
                report = await self._settled_report(report) if report is not None else None
            except HeldReportError:
                # Even an empty bulk answer permits targeted lookup; its error marks coverage
                # incomplete for just this order (manager.rs:2457-2472, 2590-2597 at the pin).
                continue
            if report is not None:
                reports.append(report)
        return reports

    async def _fill_reports(self, command: live.GenerateFillReports) -> list[FillReport]:
        """One report per Alpaca `FILL` activity in the command's window, newest first from Alpaca, and
        one per fill, at any age, of each open order the node does not hold (`_open_order_fills`).

        The window and the instrument are applied here, on each activity's `transaction_time` and
        `symbol`: what Alpaca's `after` and `until` compare for a fill is not documented (ADR 0024), and
        the endpoint has no symbol filter. Alpaca returns activities newest first by default, so paging
        stops at the first activity before the window's start, or after `FILL_PAGES_LIMIT` pages. If the
        history cannot be read, the failure is logged and no fill is reported but the open orders', so
        the engine infers fills from the order reports as it did before fill reports existed; a body that
        is not JSON, or not a list, counts as unread. An order whose listed fills cannot be shown to be
        all its fills, as when the window cuts older fills off, gets no reports (`_complete`), so the
        engine infers its fills rather than taking a part of them. An open order the node does not hold
        is the exception (ADR 0075): its fills at any age are read and reported, since the engine would
        infer those outside the window under IDs its later fills could not be matched to, and they are
        not judged again by `_complete`, so no later read can leave them to inference; one whose fills
        Alpaca does not list as its status report counts them fails the request (`_open_order_fills`), as
        does a read of the open orders that fails on every try.
        """
        # For one order, every order of its replace chain: a fill listed under an order since replaced
        # is reported on the order that replaced it, as the bulk request reports it.
        chain = (
            await self._chain_ids(command.venue_order_id)
            if command.venue_order_id is not None
            else None
        )
        try:
            activities = await self._fill_activities(command.start, chain)
        except (APIError, OSError, TypeError, KeyError, ValueError) as e:
            self._log.error(
                f"Alpaca fill history could not be read ({e!r}); fills are inferred from the order "
                "reports, but for the open orders the node does not hold, read on their own"
            )
            activities = []
        whole_orders, whole = await self._open_order_fills(command, chain)
        # Those orders' fills are `whole`'s alone, at any age, and never judged again below.
        activities = [a for a in activities if activity_order_id(a) not in whole_orders]
        ts_init = self.clock.timestamp_ns()
        reports: list[FillReport] = []
        for activity in activities:
            try:
                report = fill_report(activity, ts_init)
            except (KeyError, TypeError, ValueError, ArithmeticError) as e:
                name = activity.get("id") if isinstance(activity, dict) else repr(activity)
                self._log.warning(f"Alpaca fill activity {name} is not reported: {e}")
                continue
            if command.start is not None and report.ts_event < command.start:
                continue
            if command.instrument_id is not None and report.instrument_id != command.instrument_id:
                continue
            reports.append(report)
        # Each activity was asked for by a chain member's ID; the chain is applied once its fills are
        # moved to the order now working.
        # The end is applied after the check: a fill after it is still one of the order's (ADR
        # 0075).
        deferred: set[str] = set()
        completed = (
            await self._unanswered_named(
                [
                    r
                    for r in await self._complete(reports, activities)
                    if command.end is None or r.ts_event <= command.end
                ],
                deferred=deferred,
            )
            + whole
        )
        completed = [r for r in completed if chain is None or r.venue_order_id.value in chain]
        if deferred:
            # A successful empty/partial response permits synthetic position reconciliation at the
            # pin (node/reconciliation.rs:458-480). Deliver the identified fills independently, then
            # fail this request's coverage so no inference hides the discrepancy that asks again.
            for report in completed:
                self._handle_report(report)
            raise RuntimeError(
                "Alpaca fill identity is unread for a pending submit; "
                "reconciliation is deferred until the next check"
            )
        return completed

    def _waiting(self) -> dict[ClientOrderId, Any]:
        """The node's orders in flight with no Alpaca order ID: submits it never heard answered."""
        return {
            order.client_order_id: order
            for order in self.cache.orders_inflight(venue=VENUE)
            if order.venue_order_id is None
        }

    async def _unanswered_named(
        self, reports: list[FillReport], *, deferred: set[str] | None = None
    ) -> list[FillReport]:
        """`reports`, each fill of an Alpaca order that is a submit the node never heard answered carrying
        that node order's client order ID.

        A fill activity names its Alpaca order and no client order ID. For an order whose submit got no
        answer the node holds no Alpaca order ID, so NautilusTrader's `reconcile_fill_report` finds no
        order by the report's and makes the fill an external order
        (`materialize_external_order_from_fill`, `crates/execution/src/engine/mod.rs` at the pin), while
        the paper nodes' engine keeps the order itself submitted (`RETAIN_UNRESOLVED`, ADR 0044). The
        engine matches a report by its client order ID first. Upstream's OKX adapter sets the one the
        venue echoes on its fill reports (`parse_fill_report`, `crates/adapters/okx/src/common/parse.rs`
        at the pin); Coinbase's leaves it unset, since its fill records carry none. Alpaca's fill
        activities carry none either, but its order does (alpaca-py's `Order.client_order_id`).

        Only while the node holds such an order: each Alpaca order a report names that the node does not
        know, in the instrument and on the side of one, is read (`_orders_read`), once (`_not_waiting`),
        and its owner found as the order reports find it (`_node_order_id`). A report is named only when
        that owner is, after the reads, still such an order of the report's instrument and side, so an
        order closed or answered meanwhile is not named. An order or ancestor that cannot be read has
        only its own reports withheld and is read again by the next request, counting and naming every
        failed attempt. An unnamed report would materialize an external order at the pin and hide the
        discrepancy that triggers the next check. A failed trace proves no ownership and is never
        recorded in `_not_waiting`.
        """
        waiting = self._waiting()
        if not waiting:
            return reports
        kinds = {(order.instrument_id, order.side) for order in waiting.values()}
        unknown = sorted(
            {
                r.venue_order_id.value
                for r in reports
                if r.client_order_id is None
                and (r.instrument_id, r.order_side) in kinds
                and r.venue_order_id.value not in self._not_waiting
                and self._held_by_venue_id(r.venue_order_id.value) is None
            }
        )
        if not unknown:
            return reports
        owners: dict[str, ClientOrderId] = {}
        failures: dict[str, str] = {}
        for order_id, alpaca_order in (await self._orders_read(unknown)).items():
            if alpaca_order is None:
                failures[order_id] = "order could not be read"
                continue
            try:
                owner = await self._node_order_id(alpaca_order)
            except (APIError, OSError, ValueError, TypeError) as e:
                failures[order_id] = f"ancestor could not be traced ({failure_cause(e)})"
                continue
            self._unanswered_reads.pop(order_id, None)
            if owner is not None and owner in waiting:
                owners[order_id] = owner
            else:
                self._not_waiting.add(order_id)
        for order_id, reason in failures.items():
            self._unanswered_reads[order_id] += 1
            report = next(r for r in reports if r.venue_order_id.value == order_id)
            self._log.warning(
                f"Alpaca fill identity read {self._unanswered_reads[order_id]} failed for "
                f"{report.instrument_id} {report.order_side.name} ({reason}); "
                "its reports are held for the next request"
            )
        if deferred is not None:
            deferred.update(failures)
        # After the reads: an order closed or answered meanwhile is left out.
        waiting = self._waiting()
        named: list[FillReport] = []
        for report in reports:
            if report.venue_order_id.value in failures:
                continue
            owner = (
                owners.get(report.venue_order_id.value) if report.client_order_id is None else None
            )
            order = waiting.get(owner) if owner is not None else None
            if (
                order is not None
                and order.instrument_id == report.instrument_id
                and order.side == report.order_side
            ):
                report = report_with(report, client_order_id=order.client_order_id)
            named.append(report)
        return named

    async def _open_order_fills(
        self, command: live.GenerateFillReports, chain: set[str] | None
    ) -> tuple[set[str], list[FillReport]]:
        """The fills, at any age and each once by trade ID, of the replace chains of the open Alpaca orders
        the node does not hold that show fills, in the command's instrument or `chain` if it names either,
        each reported on the open order, and the IDs of those chains; failing, naming each order and why,
        when their activities do not list the fills their status report shows after `FILL_READ_TRIES`
        tries `FILL_READ_RETRY_SECS` apart. Every exit is one of the two: the request has no path that
        leaves such an order's fills to inference (ADR 0075).

        An order's fills are what its status report counts: every fill of its chain when they add up to
        the report's filled shares, or, under `FilledReading.CHAIN`, its own when only they do (a
        replacement that does not count the earlier orders' fills, as `_replaced_fills` reads it). Listed
        fills short of the report, or more than it by any other count, are not shown to be its fills,
        whatever the activities' `cum_qty` says.

        Reconciliation would infer the fills it is not given from such an order's status report, under
        IDs of its own, which the stream's updates for those fills and the client's later reads could not
        be matched to by trade ID (ADR 0075); so they are reported at any age, outside the window too,
        and a fill not listed at all fails the request, and start-up with it. An order is judged by its
        status report as the order reports make it (`_order_report`), so the same orders are chosen and
        its filled shares counted under the same reading. Each try reads the open orders and walks each
        chain again, so a read that fails or a walk cut short counts as a try; the cost, per request, is
        one read of the open orders and, for each such order, its chain's reads and activities."""
        symbols = (
            [command.instrument_id.symbol.value] if command.instrument_id is not None else None
        )
        why: dict[str, str] = {}
        for attempt in range(1, FILL_READ_TRIES + 1):
            why = {}
            ids: set[str] = set()
            reports: list[FillReport] = []
            try:
                orders = await self._call(
                    self._venue().get_orders,
                    GetOrdersRequest(
                        status=QueryOrderStatus.OPEN, limit=ORDERS_PAGE_LIMIT, symbols=symbols
                    ),
                )
                if not isinstance(orders, list):
                    raise TypeError(f"Alpaca's orders endpoint answered {type(orders).__name__}")
            except (APIError, OSError, TypeError, ValueError) as e:
                why[""] = f"a read failed: {e!r}"
                orders = []
            # A replacement whose replaced order is open too (`pending_replace`) after it, so the chain's
            # fills go to the order the order reports name.
            open_ids = {str(o.id) for o in orders if isinstance(o, AlpacaOrder)}
            orders = sorted(
                orders,
                key=lambda o: isinstance(o, AlpacaOrder) and str(o.replaces) in open_ids,
            )
            for order in orders:
                order_id = str(order.id) if isinstance(order, AlpacaOrder) else None
                if order_id is None or order_id in ids or str(order.replaces) in ids:
                    continue  # its chain's already, or the replacement of an order reported with it
                if chain is not None and order_id not in chain:
                    continue
                if order.replaces is None and not share_count(order.filled_qty):
                    continue  # no fills, and no chain before it to hold any
                try:
                    if await self._node_order_id(order) is not None:
                        continue  # the node's own: its reports wait for its fills (`_settled_report`)
                    report = await self._order_report(order)
                    if report is None or report.filled_qty.as_decimal() <= 0:
                        continue
                    # Its own links, then a walk at Alpaca for the rest of the chain.
                    links = {str(x) for x in (order.replaces, order.replaced_by) if x is not None}
                    order_chain = {order_id} | links | await self._chain_ids(VenueOrderId(order_id))
                    listed_activities = await self._fill_activities(None, order_chain)
                except (APIError, OSError, TypeError, KeyError, ValueError) as e:
                    why[order_id] = f"a read failed: {e!r}"
                    continue
                listed = self._listed_fills(listed_activities)
                if listed is None:
                    why[order_id] = "a trade ID listed with two quantities, prices or times"
                    continue
                shown = report.filled_qty.as_decimal()
                every = list(listed.values())
                own = [r for r in every if r.venue_order_id.value == order_id]
                if fills_count(every) == shown:
                    kept = every
                elif FILLED_READING is FilledReading.CHAIN and fills_count(own) == shown:
                    kept = own
                else:
                    why[order_id] = (
                        f"{fills_count(every)} of {shown} listed"
                        if fills_count(every) < shown
                        else f"{fills_count(every)} listed where it shows {shown} filled"
                    )
                    continue
                ids |= order_chain
                reports.extend(
                    r
                    if r.venue_order_id.value == order_id
                    else report_with(r, venue_order_id=VenueOrderId(order_id))
                    for r in kept
                )
            if not why:
                return ids, reports
            if attempt < FILL_READ_TRIES:
                await asyncio.sleep(FILL_READ_RETRY_SECS)
        named = "; ".join(
            f"open order {name} ({cause})" if name else f"the read of the open orders ({cause})"
            for name, cause in sorted(why.items())
        )
        raise ValueError(
            f"Alpaca's fills of the open orders the node does not hold could not be shown all listed "
            f"after {FILL_READ_TRIES} reads: {named}; reconciliation would infer them under IDs no "
            "later fill can be matched to"
        )

    async def _chain_ids(self, venue_order_id: VenueOrderId) -> set[str]:
        """The Alpaca order IDs of `venue_order_id`'s replace chain: the order, the ones it replaced
        (`replaces`) and the ones replacing it (`replaced_by`), each at most `MAX_REPLACE_CHAIN` links;
        a link that cannot be read ends the walk that way, with a warning."""
        ids = {venue_order_id.value}
        try:
            start = await self._call(self._venue().get_order_by_id, venue_order_id.value)
        except (APIError, OSError, ValueError, TypeError) as e:
            self._log.warning(
                f"Alpaca order {venue_order_id} could not be read for its replace chain ({e!r}); its "
                "own fills are read"
            )
            return ids
        if not isinstance(start, AlpacaOrder):
            return ids
        for link in ("replaces", "replaced_by"):
            order = start
            for _ in range(MAX_REPLACE_CHAIN):
                next_id = getattr(order, link)
                if next_id is None or str(next_id) in ids:
                    break
                ids.add(str(next_id))
                try:
                    order = await self._call(self._venue().get_order_by_id, str(next_id))
                except (APIError, OSError, ValueError, TypeError) as e:
                    self._log.warning(
                        f"Alpaca order {next_id}, in the replace chain of {venue_order_id}, could not "
                        f"be read ({e!r}); the chain's fills are read as far as it"
                    )
                    break
                if not isinstance(order, AlpacaOrder):
                    break
        return ids

    async def _fill_activities(self, start: int | None, order_ids: set[str] | None = None) -> list:
        """Alpaca's fill activities from `start` on (all of them for None), for `order_ids` or every
        order."""
        if order_ids is None:
            return await self._fill_pages(start, None)
        activities: list = []
        for order_id in sorted(order_ids):
            activities.extend(await self._fill_pages(start, order_id))
        return activities

    async def _fill_pages(self, start: int | None, order_id: str | None) -> list:
        params: dict[str, str | int] = {"direction": "desc", "page_size": ACTIVITIES_PAGE_SIZE}
        if order_id is not None:
            params["order_id"] = order_id
        activities: list = []
        for _ in range(FILL_PAGES_LIMIT):
            page = await self._call(self._venue().get, ACTIVITIES_PATH, params)
            if not isinstance(page, list):
                raise TypeError(f"Alpaca's activities endpoint answered {type(page).__name__}")
            activities.extend(page)
            try:
                oldest = fill_time(page[-1]) if page else None
            except KeyError, TypeError, ValueError:
                oldest = (
                    None  # an unreadable last row; paging goes on, and the row is skipped later
                )
            if len(page) < ACTIVITIES_PAGE_SIZE or (
                start is not None and oldest is not None and oldest < start
            ):
                return activities
            token = page[-1].get("id") if isinstance(page[-1], dict) else None
            if not isinstance(token, str) or not token:
                # The reference's `page_token` is "the ID of the end of your current page of results";
                # without it the read cannot go on. What was read is kept, and `_complete` drops an
                # order whose older fills this leaves out.
                self._log.warning(
                    "Alpaca fill history stopped at an activity with no readable `id`; older fills, if "
                    "any, are not reported",
                )
                return activities
            params["page_token"] = token
        self._log.warning(
            f"Alpaca fill history read to its limit of {FILL_PAGES_LIMIT} pages of {ACTIVITIES_PAGE_SIZE}; "
            "older fills, if any, are not reported",
        )
        return activities

    async def _complete(self, reports: list[FillReport], activities: list) -> list[FillReport]:
        """Drop the reports of an order whose listed fills cannot be shown to be all of its fills, so the
        engine infers that order's fills rather than taking a part of them (package README).

        An order's listed fills are all of them when its newest activity leaves nothing to fill
        (`leaves_qty` 0, "The quantity of shares remaining to be filled") and they add up to that
        activity's `cum_qty` ("The cumulative quantity of shares involved in the execution"). Short of
        `cum_qty`, as when the window or the page limit cut older fills off, they are not. Otherwise
        (shares left to fill, either field absent or unreadable) Alpaca's order is read and they are all
        of them only when its `filled_qty` is no more than they add up to, whatever the fills' age: the
        activities can lag the stream by an amount no page states. An order that cannot be read, or that
        alpaca-py cannot build, keeps no reports. The cost is in `_orders_read` and `_replaced_fills`.
        """
        newest: dict[str, dict] = {}
        for activity in activities:  # newest first
            if isinstance(activity, dict) and activity.get("order_id") is not None:
                newest.setdefault(str(activity["order_id"]), activity)
        # NautilusTrader's order "enforces one quantity-bearing fill per `trade_id`" ("Duplicate fill
        # detection", `docs/concepts/execution/index.md` at the pinned commit), so an activity listed
        # twice is one fill, counted and reported once. Whether Alpaca ever repeats a row is not on its
        # pages (UNVERIFIED); a trade ID listed with two quantities or prices cannot be told apart, and
        # its order's fills are left to the engine.
        partial: set[str] = set()
        first: dict[tuple[str, str], FillReport] = {}
        unique: list[FillReport] = []
        for report in reports:
            key = (report.venue_order_id.value, report.trade_id.value)
            seen = first.get(key)
            if seen is None:
                first[key] = report
                unique.append(report)
            elif fill_terms(seen) != fill_terms(report):
                partial.add(key[0])
        conflicted = set(partial)  # never shown complete, chain or not
        reports = unique
        listed: dict[str, Decimal] = {}
        for report in reports:
            key = report.venue_order_id.value
            listed[key] = listed.get(key, Decimal(0)) + report.last_qty.as_decimal()
        to_check: list[str] = []
        for order_id, qty in listed.items():
            last = newest.get(order_id, {})
            cum = share_count(last.get("cum_qty"))
            leaves = share_count(last.get("leaves_qty"))
            # Listed fills short of `cum_qty` are read too: in a replace chain they can be all of an
            # order's own while its `cum_qty` counts the chain's (`FilledReading.CHAIN`); on its own, the
            # order's `filled_qty` shows the fills missing.
            if cum is None or leaves != 0 or qty < cum:
                to_check.append(order_id)
        held = await self._orders_read(to_check)
        moved, chained = await self._replaced_fills(held, listed, newest, partial, conflicted)
        for order_id in to_check:
            if order_id in chained:
                continue
            order = held.get(order_id)
            shown = share_count(order.filled_qty) if order is not None else None
            if shown is None or shown > listed[order_id]:
                partial.add(order_id)
        for order_id in partial - chained:
            self._log.warning(
                f"Alpaca lists only some fills of order {order_id}, or they cannot be shown to be all of "
                "them; its fills are left to the engine to infer",
            )
        return [
            report_with(r, venue_order_id=moved[r.venue_order_id.value])
            if r.venue_order_id.value in moved
            else r
            for r in reports
            if r.venue_order_id.value not in partial
        ]

    async def _replaced_fills(
        self,
        held: dict[str, AlpacaOrder | None],
        listed: dict[str, Decimal],
        newest: dict[str, dict],
        partial: set[str],
        conflicted: set[str],
    ) -> tuple[dict[str, VenueOrderId], set[str]]:
        """For each order read that Alpaca shows replaced, the live order its fills move to, by the
        replaced order's ID, and every order this settles (moved, kept or added to `partial` here, each
        with its own warning).

        Alpaca lists a fill before a replace under the order replaced, which is not reported: its
        replacement is, under the node's client order ID. The fill is the node's order's, as on the
        stream, where a fill on a superseded order is applied to the node's order, as upstream's Binance
        spot keeps a cancel-replace's superseded venue order IDs bound to the node's order
        (`OrderBinding`'s `superseded`, `record_order_update_from_fill`,
        `crates/adapters/binance/src/common/dispatch.rs` at the pinned commit). So it is reported on the
        live order, when that is consistent with the live order's report. Under
        `FILLED_READING`, `CHAIN`, observed on paper overnight (ADR 0042):

        * if the live order lists fills of its own, its newest activity's `cum_qty` must equal its
          `filled_qty` (nothing on it unlisted, read either way); then its own fills adding up to
          `filled_qty` show it counts only its own: they are kept and the earlier ones left to the
          engine; the chain's adding up to it shows it counts them: they move to it;
        * if it lists none, a `filled_qty` equal to the earlier fills is read as counting them (a fill
          of its own that is not listed yet, adding up to the same, cannot be told apart: UNVERIFIED),
          and a `filled_qty` of 0 as not counting them;
        * anything else, or a trade ID repeated with other terms, leaves the chain to the engine.

        A replaced order's newest fill always leaves shares to fill (a filled order cannot be replaced),
        so it is among the orders read; each link not read already costs one `get_order_by_id`.
        """
        ends: dict[str, AlpacaOrder | None] = {}
        for order_id, order in list(held.items()):
            if order is not None and order.status == AlpacaOrderStatus.REPLACED:
                ends[order_id] = await self._live_end(order, held)
        # The orders read on the way to a live order are of its chain too.
        for order_id, order in list(held.items()):
            if (
                order_id not in ends
                and order is not None
                and order.status == AlpacaOrderStatus.REPLACED
            ):
                ends[order_id] = await self._live_end(order, held)
        moved: dict[str, VenueOrderId] = {}
        settled: set[str] = set()
        for order_id, end in ends.items():
            if end is None:
                partial.add(order_id)
                settled.add(order_id)
        for live_id in {str(end.id) for end in ends.values() if end is not None}:
            members = sorted(
                k for k, end in ends.items() if end is not None and str(end.id) == live_id
            )
            live = next(end for end in ends.values() if end is not None and str(end.id) == live_id)
            earlier = sum((listed.get(k, Decimal(0)) for k in members), Decimal(0))
            own = listed.get(live_id, Decimal(0))
            filled = share_count(live.filled_qty)
            settled.update([*members, live_id])
            cum = share_count(newest.get(live_id, {}).get("cum_qty"))
            if conflicted & {*members, live_id} or (own > 0 and (cum is None or cum != filled)):
                outcome = "partial"
            elif FILLED_READING is FilledReading.OWN:
                # Each order counts its own fills: the chain's are all listed when each order's are.
                each = all(
                    listed.get(k, Decimal(0))
                    == share_count(getattr(held.get(k), "filled_qty", None))
                    for k in members
                )
                outcome = "moved" if each and own == filled else "partial"
            elif own > 0:
                if own == filled:
                    outcome = "own only"
                else:
                    outcome = "moved" if own + earlier == filled else "partial"
            elif filled == earlier:
                outcome = "moved"
            else:
                outcome = "own only" if filled == 0 else "partial"
            if outcome == "moved":
                moved.update((k, VenueOrderId(live_id)) for k in members)
                partial.discard(live_id)  # shown complete with the chain
                continue
            partial.update(members)
            if outcome == "own only":
                partial.discard(live_id)
            else:
                partial.add(live_id)
            self._log.warning(
                f"Alpaca lists fills of order {', '.join(members)}, replaced by {live_id}, adding up to "
                f"{earlier}, where {live_id} lists {own} of its own and shows {live.filled_qty} filled; "
                + (
                    "it does not count the earlier fills, which are left to the engine to infer"
                    if outcome == "own only"
                    else "the chain's fills are left to the engine to infer"
                ),
            )
        return moved, settled

    async def _live_end(
        self, order: AlpacaOrder, known: dict[str, AlpacaOrder | None]
    ) -> AlpacaOrder | None:
        """The order at the end of a replaced order's `replaced_by` links, from the orders already read
        first, or None, with a warning, when it cannot be read or lies past `MAX_REPLACE_CHAIN`."""
        end = order
        try:
            for _ in range(MAX_REPLACE_CHAIN):
                if end.status != AlpacaOrderStatus.REPLACED or end.replaced_by is None:
                    return end
                found = known.get(str(end.replaced_by)) or await self._call(
                    self._venue().get_order_by_id, str(end.replaced_by)
                )
                if not isinstance(found, AlpacaOrder):
                    raise TypeError("alpaca-py returned raw order data")
                known[str(found.id)] = found  # read once, and judged with the chain's other orders
                end = found
        except (APIError, OSError, ValueError, TypeError) as e:
            self._log.warning(
                f"Alpaca order {order.id} was replaced and the order replacing it could not be read "
                f"({e!r}); its fills are left to the engine to infer"
            )
            return None
        if end.status != AlpacaOrderStatus.REPLACED or end.replaced_by is None:
            return end
        self._log.warning(
            f"Alpaca order {order.id} has more than {MAX_REPLACE_CHAIN} replaces; its fills are left to "
            "the engine to infer"
        )
        return None

    async def _orders_read(self, order_ids: list[str]) -> dict[str, AlpacaOrder | None]:
        """Each order as Alpaca shows it, None where it cannot be read.

        One order costs one `get_order_by_id`. More cost one `get_orders` ("Get All Orders",
        `GetOrdersRequest`) for the account's newest `ORDERS_PAGE_LIMIT` orders of any status, with no
        symbol or time bound (an activity's symbol is not documented to be its order's), and one `get_order_by_id` for each order not among them, one at a time; if
        none is among them, that is one request more than reading each.
        """
        found: dict[str, AlpacaOrder | None] = {}
        wanted = set(order_ids)
        if len(wanted) > 1:
            try:
                orders = await self._call(
                    self._venue().get_orders,
                    GetOrdersRequest(status=QueryOrderStatus.ALL, limit=ORDERS_PAGE_LIMIT),
                )
                if not isinstance(orders, list):
                    raise TypeError(f"Alpaca's orders endpoint answered {type(orders).__name__}")
                for order in orders:
                    if isinstance(order, AlpacaOrder) and str(order.id) in wanted:
                        found[str(order.id)] = order
            except (APIError, OSError, TypeError, ValueError) as e:
                self._log.warning(
                    f"Alpaca orders not read to check fills ({failure_cause(e)}); reading each"
                )
        for order_id in order_ids:
            if order_id in found:
                continue
            try:
                order = await self._call(self._venue().get_order_by_id, order_id)
            except (APIError, OSError, TypeError, ValueError) as e:
                # alpaca-py builds `Order(**answer)`: an empty or non-object body is a TypeError, a
                # value its model refuses a pydantic `ValidationError`, which is a `ValueError`.
                self._log.warning(f"Alpaca order not read to check its fills ({failure_cause(e)})")
                found[order_id] = None
                continue
            found[order_id] = order if isinstance(order, AlpacaOrder) else None
        return found

    async def _position_status_reports(
        self, command: live.GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        positions = await self._call(self._venue().get_all_positions)
        if not isinstance(positions, list):
            raise TypeError(
                "alpaca-py returned raw position data; the client must use raw_data=False"
            )
        ts_init = self.clock.timestamp_ns()
        reports = []
        for position in positions:
            instrument_id = InstrumentId(Symbol(position.symbol), VENUE)
            if command.instrument_id is not None and instrument_id != command.instrument_id:
                continue  # before it is read, so another symbol's position cannot fail this report
            if position.asset_class != AssetClass.US_EQUITY:
                self._log.warning(
                    f"Alpaca position in {position.symbol} ({position.asset_class.value}) is not a US "
                    "equity; not reported",
                )
                continue
            if self._provider.find(instrument_id) is None:
                # NautilusTrader fails start-up on a position report for an instrument it has not
                # loaded ("instrument missing from cache", observed); upstream's Coinbase adapter keeps
                # its reports to the instruments it loaded.
                self._log.warning(
                    f"Alpaca position in {position.symbol} is in an instrument this client has not "
                    "loaded; not reported",
                )
                continue
            reports.append(position_status_report(position, ts_init))
        # Alpaca's "All Open Positions" lists the account's open positions, and "Once a position is closed,
        # it will no longer be queryable" (https://docs.alpaca.markets/us/reference/getallopenpositions.md).
        # NautilusTrader takes only "An explicit flat report" as evidence of flat ("Missing reports do not
        # mean flat", reconciliation.md at the pinned commit), so each loaded instrument Alpaca lists no
        # position in is reported flat; otherwise fills inferred from part of the order history can leave
        # an external position Alpaca does not hold.
        held = {report.instrument_id for report in reports}
        for instrument in self._provider.list_all():
            wanted = command.instrument_id is None or instrument.id == command.instrument_id
            if wanted and instrument.id not in held:
                reports.append(
                    PositionStatusReport(
                        account_id=ACCOUNT_ID,
                        instrument_id=instrument.id,
                        position_side=PositionSide.FLAT,
                        quantity=Quantity(0, 0),
                        ts_last=ts_init,
                        ts_init=ts_init,
                    )
                )
        return reports

    async def _order_report(
        self, order: AlpacaOrder, known: dict[str, AlpacaOrder] | None = None
    ) -> OrderStatusReport | None:
        """One Alpaca order's report, None for a closed order skipped with a warning naming why.

        A replacement is reported under the node's client order ID, traced through `replaces`; a
        replaced order is skipped (its replacement is reported), so it needs no lookup. A trace that fails,
        on a transport error or on an order alpaca-py cannot build (pydantic's `ValidationError`, a
        `ValueError`, or the `TypeError` of an empty body, the classes `_orders_read` takes),
        skips a closed order, and fails the report for an open one: the node cannot tell whose order a
        working replacement is, and an open order left out of the reports reads as one Alpaca does not
        hold, as any open order this slice cannot represent fails start-up (ADR 0030).
        """
        try:
            root = (
                await self._root_id(order, known)
                if order.status != AlpacaOrderStatus.REPLACED
                else None
            )
        except (APIError, OSError, ValueError, TypeError) as e:
            if is_open_order(order):
                raise ValueError(
                    f"{order_description(order)} is open and tracing it failed ({failure_cause(e)})"
                ) from e
            self._log.warning(
                f"{order_description(order)} is not reported: tracing it failed ({failure_cause(e)})"
            )
            return None

        # Reported under the chain's first client order ID, the node's, held or not: after a restart
        # the node's original order is created under its own ID.
        node_id = root
        held = self.cache.order(node_id) if node_id is not None else None
        status: OrderStatus | None = None
        filled: Decimal | None = None
        quantity: Decimal | None = None
        if order.replaces is not None and order.status != AlpacaOrderStatus.REPLACED:
            if order.status == AlpacaOrderStatus.REJECTED and not await self._took_over(
                order, known
            ):
                # A rejected replacement (Alpaca rejects it when the old order fills first, or a later
                # replace left it behind) did not take over: the old order carries the outcome. Any other
                # replacement is the node's order now, filled or open, whether or not the node has seen
                # `replaced`; so is a rejected one whose old order Alpaca shows replaced by it.
                return None
            own = share_count(order.filled_qty) or Decimal(0)
            try:
                earlier = (
                    await self._earlier_filled(order, known)
                    if FILLED_READING is FilledReading.OWN
                    else Decimal(0)
                )
            except (APIError, OSError, ValueError, TypeError) as e:
                if is_open_order(order):
                    raise ValueError(
                        f"{order_description(order)} is open and its chain's fills could "
                        f"not be read ({failure_cause(e)})"
                    ) from e
                self._log.warning(
                    f"{order_description(order)} is not reported: its chain's fills ({failure_cause(e)})"
                )
                return None
            chained = chain_filled(own, earlier)
            if order.status == AlpacaOrderStatus.REJECTED:
                # A rejected order may show nothing filled whatever the reading: the order it replaced,
                # and the node's own order, show the fills it took over.
                prior = (known or {}).get(str(order.replaces))
                record = self._records.get(node_id) if node_id is not None else None
                chained = max(
                    chained,
                    share_count(prior.filled_qty) or Decimal(0)
                    if prior is not None
                    else Decimal(0),
                    held.filled_qty.as_decimal() if held is not None else Decimal(0),
                    record.filled if record is not None else Decimal(0),
                )
            filled = chained if chained != own else None
            qty = share_count(order.qty)
            if qty is not None and chain_quantity(qty, earlier) != qty:
                quantity = chain_quantity(qty, earlier)
            if order.status == AlpacaOrderStatus.REJECTED and chained > 0:
                # A takeover Alpaca rejected after fills ends canceled, as on the stream
                # (`_rejected_takeover`): a rejected report would lose the fills.
                status = OrderStatus.CANCELED
        # As upstream's Coinbase adapter patches a report from what the node sent: the trigger type
        # the node's order holds, which Alpaca's order does not carry.
        trigger_type = (
            held.trigger_type
            if held is not None and held.order_type in REPORT_TRIGGER_TYPES
            else None
        )
        renamed = (
            node_id if node_id is not None and node_id.value != order.client_order_id else None
        )
        return order_status_report(
            order,
            self.clock.timestamp_ns(),
            trigger_type,
            renamed,
            lambda reason: self._log.warning(
                f"{order_description(order)} ({order.status.value}) is not reported: {reason}",
            ),
            status,
            filled,
            quantity,
            lambda reason: self._warn_advisory_trigger(order, reason),
            trailing=self._report_external_trailing_stops
            or (held is not None and held.order_type == OrderType.TRAILING_STOP_MARKET),
        )

    def _warn_advisory_trigger(self, order: AlpacaOrder, reason: str) -> None:
        """Name an unrepresentable advisory trailing trigger once per venue order, without its ID."""
        order_id = str(order.id)
        if order_id in self._advisory_trigger_warned:
            return
        self._advisory_trigger_warned.add(order_id)
        self._log.warning(
            f"{order_description(order)} advisory trailing trigger omitted ({reason}); reported as None"
        )

    async def _earlier_filled(
        self, order: AlpacaOrder, known: dict[str, AlpacaOrder] | None
    ) -> Decimal:
        """The `filled_qty` of every order a replacement's chain replaced, from the orders already read
        first, then at most `MAX_REPLACE_CHAIN` reads; needed only under `FilledReading.OWN`."""
        total = Decimal(0)
        replaces, walked, reads = order.replaces, {str(order.id)}, 0
        while replaces is not None and str(replaces) not in walked:
            prior_id = str(replaces)
            walked.add(prior_id)
            prior = (known or {}).get(prior_id)
            if prior is None:
                if reads == MAX_REPLACE_CHAIN:
                    raise ChainTooLong(
                        f"more than {MAX_REPLACE_CHAIN} reads to sum the chain's fills"
                    )
                reads += 1
                prior = await self._call(self._venue().get_order_by_id, prior_id)
            if not isinstance(prior, AlpacaOrder):
                raise TypeError("alpaca-py returned raw order data")
            total += share_count(prior.filled_qty) or Decimal(0)
            replaces = prior.replaces
        return total

    async def _took_over(
        self, replacement: AlpacaOrder, known: dict[str, AlpacaOrder] | None
    ) -> bool:
        """Whether Alpaca shows the order a replacement replaces as replaced by it, from the orders
        already fetched first (one replaced by a later replacement after this one was rejected does not
        count); an order that cannot be read counts as not replaced, so the old order's report
        stands, as before. The node's own record comes first: a replacement the node has moved its
        order to took over, whatever a read of the order before it gives, and one the record shows the
        order before it replaced by, or by another, needs no read. The stream (`_apply`) and the reports
        both judge a rejected replacement by this, and a read is recorded."""
        replacement_id = str(replacement.id)
        node_id = self._index.get(replacement_id)
        if node_id is not None:
            record = self._records.get(node_id)
            held = self.cache.order(node_id)
            if (record is not None and record.current == replacement_id) or (
                held is not None
                and held.venue_order_id is not None
                and held.venue_order_id.value == replacement_id
            ):
                return True
        record = self._records.get(node_id) if node_id is not None else None
        known_by_record = record.took_over(replacement_id) if record is not None else None
        if known_by_record is not None:
            return known_by_record
        prior = (known or {}).get(str(replacement.replaces))
        if prior is None:
            try:
                prior = await self._call(self._venue().get_order_by_id, str(replacement.replaces))
            except (APIError, OSError, ValueError, TypeError) as e:
                self._log.warning(
                    f"{order_description(replacement)}: the order it replaces could not be read "
                    f"({failure_cause(e)}); the replacement is taken not to have taken over"
                )
                return False
        if isinstance(prior, AlpacaOrder) and record is not None:
            self._see(record, prior)
        return (
            isinstance(prior, AlpacaOrder)
            and prior.status == AlpacaOrderStatus.REPLACED
            and str(prior.replaced_by) == replacement_id
        )

    # -- alpaca-py calls ---------------------------------------------------------------------------

    def _venue(self) -> OrdersClient:
        if self._trading is None:
            raise RuntimeError("Alpaca execution client is not connected")
        return self._trading

    async def _call[R](self, fn: Callable[..., R], *args: object, once: bool = False) -> R:
        """Call a `TradingClient` method in a worker thread, one call at a time; with `once`, an order
        command, its request is sent once (`without_resend`; the client was prepared at connect).

        As `data.AlpacaDataClient._one_historical_call`: the lock is held on the node's loop and released
        by the worker when the call returns, and the call is shielded, so a cancelled hook leaves it
        running and the lock held until it ends.
        """
        loop = asyncio.get_running_loop()
        worker = counted(fn)
        if once:
            worker = without_resend(worker)
        lock = self._rest_lock
        await lock.acquire()
        try:
            call = asyncio.ensure_future(
                asyncio.to_thread(release_after, loop, lock, worker, *args)
            )
        except BaseException:
            lock.release()
            raise
        call.add_done_callback(retrieved)  # read if the hook awaiting it was cancelled meanwhile
        return cast("R", await asyncio.shield(call))
