# /code-review high on r56d4's first/copy

Run from the scoring session as a forked `/code-review` at high effort, target the copy's uncommitted diff. Finished 2026-11-13 by 15:38:05 New York, read from date after it ended (187 s, 14 tool uses). These are the review's candidate findings, in its order, unverified by the scoring session. They are not marks.

```json
[
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1520,
    "summary": "The 'superseded order' early return runs before `_adopt_if_live`, so a cancel or expiry of a newer replacement is dropped whenever the node has already adopted an older one.",
    "failure_scenario": "A→B is adopted, so `_adopted[coid]=B`. The node then modifies B→C. Alpaca's `replaced` for B is late or missed, and C's `expired` (a DAY order at the close) or `canceled` arrives. The check `adopted != str(alpaca_order.id)` is true (B != C), so the update is logged as 'superseded' and returns. `_adopt_if_live`, whose `_replaces_chain_contains` would have found that C follows B, is never reached. The node's order stays open at B's terms although Alpaca has closed it, until reconciliation."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 2057,
    "summary": "`_replacement_took_over` returns True for any closed replacement whose prior is REPLACED. It never checks that `prior.replaced_by` is this order, so a rejected replacement gets reported as the node's order.",
    "failure_scenario": "Modify 1 creates A→B and B is rejected (for example on buying power) while A stays live. Modify 2 then replaces A with C, so A becomes `replaced` with `replaced_by=C`. At reconciliation B is closed (REJECTED), its prior A shows REPLACED, and the function returns True. B is then reported under the node's client order ID with status REJECTED, beside C's open report. That can close or reject the node's working order. Before this change B was skipped."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 320,
    "summary": "Mapping `pending_replace` as an open status means a mid-replace snapshot reports two open orders under one node client order ID with different venue order IDs.",
    "failure_scenario": "Start-up or reconciliation runs while Alpaca holds A (`pending_replace`) and its replacement B (`new`/`pending_new`). `_order_report(B)` traces `replaces` to the node's ID and reports B open with venue B. A is now reported open too, under the same ID with venue A, because `pending_replace` is in OPEN_STATUSES. The engine gets conflicting venue order IDs for one order. Reports come newest first, so A's report can revert the order to venue A after B's."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1504,
    "summary": "A replacement's `rejected` calls `_modify_rejected` keyed on the replacement's ID (B), while the HTTP 422 and `order_replace_rejected` paths key on the replaced order's ID (A). The 'one rejection per modify' dedup therefore does not hold across these answers.",
    "failure_scenario": "One modify A→B gets `order_replace_rejected` for A (key A is settled, rejection 1). Alpaca then also sends `rejected` for replacement B. `venue_order_id` is B here, B is not in `_settled_modifies`, and a second OrderModifyRejected is generated for the same modify. The fix is to key on `alpaca_order.replaces`, as the finished branch above already does."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1186,
    "summary": "Dedup state is keyed per venue order ID rather than per modify attempt, and `_begin_modify` clears it. A late stream refusal of an earlier modify is therefore reported as a rejection of the current one.",
    "failure_scenario": "Modify 1 on A is refused by HTTP 422 (A settled, one OrderModifyRejected). The strategy sends modify 2 on A, and `_begin_modify` discards A from `_settled_modifies`. The stream's late `order_replace_rejected` for modify 1 then arrives and `_modify_rejected(A)` emits a second OrderModifyRejected. Modify 2 may still succeed, and its `replaced` would then follow a rejection."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1514,
    "summary": "A `canceled`/`expired` update for an order with `replaced_by` set is always routed to `_replaced`, without checking that the replacement took over. If the replacement was rejected, a real cancel of the node's order is swallowed and a dead order is adopted.",
    "failure_scenario": "Replacement B of A is rejected (A not filled, still live). A keeps `replaced_by=B`, and the user then cancels A, so Alpaca sends `canceled` for A. The branch calls `_replaced`, and `_live_order(B)` returns the rejected B because its status is not REPLACED. `_adopt` emits OrderUpdated with B's terms. OrderCanceled is never generated, so the node's order stays open."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 577,
    "summary": "`replaces_as_asked` now compares an omitted `qty` against the replaced order's `qty`. For a partially filled order, the replacement's `qty` semantics are UNVERIFIED in ADR 0036, so a successful retry can be misjudged as a different modify.",
    "failure_scenario": "A has 10 shares with 4 filled. A price-only modify (qty omitted) reaches Alpaca, alpaca-py's 504 retry gets 422 'order already replaced', and B has qty 6 (or anything other than A.qty). `held_before=10` does not equal 6, so the modify is rejected. The `replaced` update then brings OrderUpdated after the OrderModifyRejected, which contradicts it."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1632,
    "summary": "`_replaces_chain_contains` and `_live_order` make up to 16 sequential REST GETs inside the single serial trade-update consumer. They ignore the local `_replacements`/`_answers` maps, and they block every other order's updates while they run.",
    "failure_scenario": "A fill for a replacement arrives while `_adopted` names another order. Each `replaces` hop is a `get_order_by_id` round trip, rate-limited and subject to alpaca-py's 429/504 retries, run on the queue that applies all trade updates in order. Fills and cancels for other orders wait behind it. A lookup error silently returns False, which skips the adopt, and the fill is then applied under the old quantity, the overfill case this change was meant to fix."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 788,
    "summary": "`_handled_replacements` and `_settled_modifies` gain one entry per modify and are only cleared on disconnect. `_modifying` is written and deleted but never read by production code.",
    "failure_scenario": "A long session with frequent modifies grows both sets without bound, the same session-long leak the new comment in `_modify_order` says it avoids for `_answers`. `_modifying` is dead state (only a test inspects it) but is still maintained in `_begin_modify`, `_settle_modify` and `_modify_rejected`. `_replaced` also adds to `_handled_replacements` twice and sets `_replacements` twice (lines 1587, 1590 and again in `_adopt`)."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1511,
    "summary": "Moving the `_finished` early return below the `replaced`/`pending_replace` branches means a late `replaced` for a closed order now does REST lookups. It can also emit OrderUpdated on a closed order.",
    "failure_scenario": "The node's order has closed (filled or canceled) and a duplicate or late `replaced` arrives whose chain `_already_adopted` does not match, for example after a restart cleared `_adopted` and the cached venue ID differs. `_replaced` runs `_live_order` GETs and `_adopt`, which calls `generate_order_updated` on a FILLED or CANCELED order. Before this change, finished orders returned before any replace handling."
  }
]
```
