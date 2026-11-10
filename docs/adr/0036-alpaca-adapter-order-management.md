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
symbol, listed with "Get All Orders" and each canceled by ID.

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

### Consequences

* Good: the command cancels exactly what it names, through one tested cancel path.
* Bad: one HTTP call per order, one at a time; an order Alpaca holds that the node does not yet, or one
  placed after the cache was read, is not canceled.

### Confirmation

**Cancel-all.** Spec rows: TC-E41 ("Cancel all on stop") runs on paper through upstream's `ExecTester`,
whose stop sends `CancelAllOrders` with `strategy_only=false`, with one limit buy $50 below the bid; the
row's sell is left out, since from a flat account Alpaca refuses it, 403 "cannot open a short sell while a
long buy order is open" (observed). It passed on 2026-11-10, its order ending `OrderCanceled`. The card
expects `OrderPendingCancel` first; with `strategy_only=false` the pinned `Strategy` sends the command and
generates none, and the binding has no `generate_*` for it, as upstream's Binance spot emits `OrderCanceled`
alone. The paper tests follow upstream's practice: the spec's rows through its tester where one covers the
case; alpaca-py's own tests mock the paper endpoints with documented payloads, as the offline tests here
stand in for the venue. The test places nothing while an AAPL order is open on the account. TC-E40 is the
existing cancel test (passed on paper the same day); TC-E42 and TC-E43 reach no new hook; TC-E44 is not run.

`packages/canon-alpaca/tests/test_execution_client.py`, offline in `scripts/ci`: cancel-all of a
reconciled order and two placed ones, each by ID, through a real strategy's command (no side gives
`order_side=None`, observed); a side filter; a refused cancel rejected while the other is canceled; an
unexpected failure logged while the other is canceled; nothing sent when nothing matches.
`test_upstream_contract.py` pins `CancelAllOrders`, the cache's `side=` and the batch default.
`test_paper_cancel_all_on_stop_tc_e41` runs in `mise run paper:test`.

## Evidence

Read 2026-11-10. "raw" means the page's markdown or source fetched with `curl` and read, not summarised.

* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md: "Handle order commands"
  and the `_batch_cancel_orders` default; identical to the pinned commit but for two version lines
  (fetched, raw; diffed).
* `docs/concepts/execution/policies.md` at NautilusTrader `d53849b216e2` (the pin): the failure-event
  table and "without inventing an outcome". The `latest` page adds `OrderCancelDenied` and
  `OrderModifyDenied`, which the pinned binding does not have (fetched, raw; diffed).
* `crates/adapters/kraken/src/execution/spot.rs`, `binance/src/spot/execution.rs`, `bybit/src/execution.rs`,
  `interactive_brokers/src/execution/core.rs` and `coinbase/src/execution.rs` at `d53849b216e2`:
  `cancel_all_orders` and `batch_cancel_orders`; Kraken cancels in venue batches of up to 50, Bybit in one
  batch request (fetched, raw). `betfair/src/execution.rs`, `okx/src/execution.rs` and
  `okx/src/websocket/client.rs` at the same commit: the wider venue cancels (fetched, raw).
* `crates/trading/src/strategy/mod.rs` at `d53849b216e2`: `cancel_all_orders` sends the command only
  when `strategy_only` is false (fetched, raw).
* NautilusTrader `v2.0.0rc8` as installed: `ExecutionClient` hooks and `generate_*` signatures,
  `CancelAllOrders` fields, `ClientCache.orders_open` and `orders_inflight` (read with `inspect`).
* https://docs.alpaca.markets/us/reference/deleteallorders-1.md: account-wide, no parameters, 207 per
  order (fetched, raw).
* https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md: 422 when no longer cancelable,
  otherwise 204 (fetched, raw).
* https://nautilustrader.io/docs/md/latest/developer_guide/spec_exec_testing.md: groups 3 to 5, the
  TC-E41 card and `ExecTester`'s Python setup (fetched, raw). `crates/testkit/src/testers/exec/strategy.rs`
  at `d53849b216e2`: `on_stop` and its `cancel_all_orders(..., false, ...)` (fetched, raw).
* Paper runs, 2026-11-10, `mise run paper:test`, which takes the lock (ran; logs kept outside the repo):
  TC-E41 as the spec writes it, the buy canceled on stop and the sell refused 241 times with the 403
  above. Of the runs after it, three failed on the test's own code (fixed), one on connect timeouts to
  Alpaca's paper host (it answered again at once), and two refused to start on another session's open
  AAPL orders; the last, with the buy alone, passed all three paper tests.
* https://github.com/nautechsystems/nautilus_trader `RELEASES.md`: v2.0.0rc9, tagged 2026-11-10, adds
  `OrderCancelDenied` and `OrderModifyDenied` (read by the second reader). The pin stays rc8 until it moves
  through `adopt-upstream`.
* https://github.com/alpacahq/alpaca-py at `v0.45.0`: `tests/trading/trading_client/test_order_routes.py`
  mocks the paper base URL for orders and replaces (fetched, raw).
* https://docs.alpaca.markets/us/llms.txt, "Orders": the Trading API's order endpoints; none cancels a
  list of orders by ID (fetched, raw).
* Second reader, 2026-11-10 (`evidence-verifier`): the decision stands. Corrected above: "never wider
  than the command" (Betfair and OKX are), the retry argument against option 1 (the same table limits a
  cancel by ID), and sources for the pending-cancel skip and for no cancel-by-list endpoint; added
  option 3.
