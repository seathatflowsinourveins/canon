# Second reader: rf5bf
Mark: fix
Blocker: none
## Findings
1. packages/canon-alpaca/README.md:290-292 and packages/canon-alpaca/src/canon_alpaca/execution.py:144-147. The
   added text says an open order whose client order ID NautilusTrader cannot hold fails start-up (the header:
   "fails the report when it is open"). When an earlier order in its `replaces` chain has an ID NautilusTrader
   can hold, the code reports the open order under that ID and start-up goes on. Shown by probe P10, chain D
   (an open order `订单-2` that replaced `ext-1`): start-up completes, and the node holds `ext-1` open. P5 gives
   the same report from `_order_report`. (severity: fix)
2. packages/canon-alpaca/README.md:295-296, execution.py:147-148 and the `_root_id` docstring at
   execution.py:1500-1502. The text says "A replacement whose own ID is holdable is reported under it even when
   the order it replaces is not", and the docstring says an order whose own ID cannot be held is "skipped". The
   code walks past an unholdable link to an earlier holdable ID when the chain has one. An open order whose chain
   holds no such ID fails the report; it is not skipped. Shown by probe P10, chain C (`ext-1` replaced by
   `订单-2`, replaced by the open `alpaca-made-2`): start-up holds `ext-1`, not `alpaca-made-2`. The statement's
   last sentence asks for "under its own ID", and the case's own fix gives `alpaca-made-2`. In P5 chain E (an
   open order `订单-2` whose original `订单-1` is also unholdable), the report raises "is open with invalid
   string" rather than skipping. The report discloses that it walks past such a link ("What I am unsure of"), so
   a review would have aligned code and text one way or the other; this is not blocker 5. The report's third
   tracing bullet ("an order whose own ID is unholdable has no root") is wrong only for a three-link chain: P5
   chain F gives `alpaca-made-1`. (severity: fix)
## Per-defect check
1. "Cancel-all skips an order that is already `PENDING_CANCEL`": removed.
   - `test_cancel_all_cancels_again_an_order_whose_cancel_had_no_answer` passes on the change and fails on the
     starting code.
   - P7: a repeat that Alpaca refuses with 422 while it shows the order pending cancel is still sent. That gives
     2 cancels on the change against 1 on the starting code, no `OrderCancelRejected`, and the order stays
     `PENDING_CANCEL`.
   - P8: the pending order is canceled in the same cancel-all as a working one.
2. "Tracing such an order raises outside the handling that skips an order the client cannot report, so one
   closed order like that aborts the mass status and with it start-up reconciliation": removed.
   - The four named tests (five cases) pass on the change and fail on the starting code.
   - P1: a closed order with an empty, all-whitespace or non-ASCII ID is skipped with a warning that names it.
   - P3: such a closed order beside a valid open order no longer stops that order's reconciliation.
   - P2: an open order with any of those three IDs fails start-up with "Alpaca order <id> (AAPL) is open with
     invalid string ...". On the starting code start-up also fails, but the log line names no order.
   - P5 and P10, chain T: a valid replacement of an unholdable original is reported under its own ID.
   - P4: a trade update for such an order is logged as one the node does not hold. On the starting code it
     failed with a `ValueError`.
   - A three-link chain takes an earlier holdable ID (finding 2).
## Probes
All under `$S/rf5bf/second/`. The starting tree is `base/`, made with `harness.py tree`. `fix/` is the starting
tree with the fix's `execution.py` diff applied (`probes/fix-src.patch`). Each tree ran its own `.venv` from the
lock.
- Named tests (`-k` on the four): change 5 passed; starting code 5 failed. Whole
  `test_execution_client.py` on the change: 140 passed.
- `probes/probe_ids.py`, run in the change's environment (nautilus-trader 2.0.0rc8). `ClientOrderId` raises
  `ValueError` for `""` ("was empty"), for `" "`, `"   "`, tab and newline ("was all whitespace"), and for
  CJK and accented input ("contained a non-ASCII char"). It accepts `" a"`, `"a\x00b"` and IDs of 128 and 1000
  characters. The helper returns None exactly for the refused ones; no other exception type occurs.
  `ClientOrderId.__doc__` is "Represents a valid client order ID (assigned by the Nautilus system).", as the
  report quotes.
- `probes/test_probe_rf5bf.py`, copied beside `test_execution_client.py` in each tree, run from that tree's
  root, then removed. JSON output is in `probes/out-copy.json`, `out-base.json` and `out-fix.json`. Totals:
  change 17 passed; starting code 10 failed and 7 passed (the 7 only record, and record a raise); fix 3 failed
  and 14 passed (P1 fails there only on the change's warning wording).
  - P1, closed order with an empty, whitespace-only or non-ASCII ID. Change: start-up completes and logs
    "Alpaca order 3e1f... (AAPL, canceled) is not reported: its client order ID, ...". Starting code: "Failed
    to get mass status".
  - P2, open order with the same three IDs. Change: "Failed to get mass status", with the reason naming the
    order. Starting code: fails with "Alpaca order status reports failed: invalid string for 'value', was ...",
    with no order named.
  - P3, a closed non-ASCII order beside an open `ext-open-1`. Change: `["ext-open-1"]`. Starting code: start-up
    aborted.
  - P4, a stream update for an open order with an ID of `""` or `订单-9`. Change: "which this node does not
    hold", and the node's own order fills. Starting code: "Alpaca trade update new for ... failed: ValueError".
  - P5, `_root_id` and `_order_report` on each chain, records only. On the change:

    | Chain | Root | Reported under |
    |---|---|---|
    | T | alpaca-made-1 | alpaca-made-1 |
    | C | ext-1 | ext-1 |
    | D | ext-1 | ext-1 |
    | F | alpaca-made-1 | alpaca-made-1 |
    | E (open) | None | raises "Alpaca order 2222... (AAPL) is open with invalid string" |
    | G (closed) | None | None |

    Starting code: every chain raises `ValueError`. Fix: the same as the change except chain C, which gives
    alpaca-made-2.
  - P6, three traces of chain E with no prior fetched. Change: 3 `get_order_by_id` calls. Fix: 1. Starting
    code: raises.
  - P7, the repeat is refused 422 while Alpaca shows the order pending cancel. Change and fix: 2 cancels, events
    `OrderSubmitted, OrderAccepted, OrderPendingCancel`, status `PENDING_CANCEL`. Starting code: 1 cancel.
  - P8, the cancel sequence in a cancel-all with one pending order and one working order. Change: `[pending,
    working]` when the pending order was placed first, `[working, pending]` otherwise. Fix: `[working, pending]`
    both times. Starting code: `[working]`.
  - P9, two open orders with `""` IDs: P1 replaced `ext-1`, P2 replaced nothing. Change: tracing P1 then P2
    gives `ext-1, ext-1`; P2 then P1 gives a raise by name, then `ext-1`. Fix: `ext-1, ext-1`, and in the other
    order both raise. Starting code: all raise.
  - P10, node start-up with chains C and D. Change: `["ext-1"]` and `["ext-1"]`. Fix: `["alpaca-made-2"]` and
    `["ext-1"]`. Starting code: "Failed to get mass status" for both.
- `scripts/canon lint` in the copy: "canon: lint clean", as the report says.
- Pinned upstream, read in `~/.cache/canon/upstream-src/nautilus-trader@d53849b216e2/crates/adapters/`:
  - Kraken spot (`kraken/src/execution/spot.rs:1608-1640`): `orders_open` plus `orders_inflight`.
  - Interactive Brokers (`cancel_all_targets`, `interactive_brokers/src/execution/core.rs:2289-2320`):
    `orders_open`.
  - Binance spot (`binance/src/spot/execution.rs:2122-2130`) and Bybit (`bybit/src/execution.rs:2054-2062`):
    `orders_open` for a sided command.
  - None of those four filters by status. Coinbase (`coinbase/src/execution.rs:1209-1262`) leaves
    `PendingCancel` out. This supports the ADR 0036 edit.
- Blocker checks:
  - 1: no test value is recognised; grade.json `flags` is empty; no test file changed; IDs and chains the tests
    do not use behave the same way (P1, P2, P5, P10).
  - 2: the helper calls NautilusTrader's own `ClientOrderId` and catches its `ValueError`, as the case's fix
    does; no dependency is added.
  - 3: every None from the helper ends in a warning, a named raise or a "does not hold" log line (P1, P2, P4).
  - 4: no behaviour that worked before is broken (P3, P4, P7, the 140 tests).
  - 5: the report's runs and claims were re-checked: named tests before and after, lint, the warning text, the
    `ClientOrderId` refusals and docstring, and the upstream selection.
- Not re-run here: the whole workspace suite, ruff and ty. grade.json records 497 passed, 24 skipped and all
  three checks true (UNVERIFIED by this reader).
## Declined to judge
- Where a repeat cancel goes relative to the working orders' cancels: the statement asks for no order, and the
  change's README and header say "in no specified order". P8 shows the change follows the cache's order, so the
  repeat can go first; the case's fix sends it last. A slower cancel-all when the repeat hangs is a latency
  preference, and no input here shows a wrong outcome.
- That a trace finding no holdable ID is not cached: P6 shows three lookups for three traces on the change and
  one on the case's fix. This is an efficiency difference, reached only by an order whose every `replaces` link
  is unholdable, and no wrong outcome is shown.
- Two orders that share an empty client order ID: in P9, traced in one order, an open order with no holdable ID
  takes the other's root `ext-1` instead of failing start-up. The trace cache is keyed by the raw string in the
  starting code and in the case's fix as well; in the other order, the fix also fails the order that has a
  holdable root. Whether Alpaca lists two orders with an empty client order ID is UNVERIFIED.
- What Alpaca answers to a repeat cancel of an order it already shows pending cancel (204 or 422): UNVERIFIED.
  Paper runs are out of bounds for this reader, and P7 exercises only the stand-in's 422.
- That Alpaca can list an order with an empty or non-ASCII client order ID: taken from the case statement, not
  checked against Alpaca this session (UNVERIFIED).
