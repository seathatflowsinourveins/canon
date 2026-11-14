# /code-review high on rf5bf's first/copy

Run from the scoring session as a forked `/code-review` at high effort, target the copy's uncommitted diff. Finished 2026-11-13 by 15:29:16 New York, read from date after it ended (120 s, 12 tool uses). These are the review's candidate findings, in its order, unverified by the scoring session. They are not marks.

```json
[
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1540,
    "summary": "`_root_id` now writes a traced root into `_replacements` under the raw Alpaca client order ID even when that ID is one NautilusTrader cannot hold (e.g. \"\" or whitespace). Such IDs need not be unique, so the next unrelated order with the same raw ID picks up the cached root.",
    "failure_scenario": "Outside order X has client_order_id \"\" and replaces P, which has a holdable ID, so `_replacements[\"\"] = P`. A later unrelated outside order Z, also with \"\" and no `replaces`, returns P from the cache at line 1504. `_order_report` then reports Z under P's ID with Z's venue ID and quantities. If the node holds P, `_apply` maps Z's trade updates (fills, cancels) onto the node's order P."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1112,
    "summary": "Cancel-all no longer skips a PENDING_CANCEL order. An order Alpaca has already filled, but whose fill the node has not yet processed, now gets a second cancel, and that cancel produces a spurious OrderCancelRejected.",
    "failure_scenario": "A strategy cancels order O (node: PENDING_CANCEL), and O fills at Alpaca before the fill update arrives. The strategy, or `on_stop`, sends `cancel_all_orders`. The client re-sends the cancel and gets 422. `_cancel_refused` looks the order up and sees FILLED, which is neither CANCELED, PENDING_CANCEL nor REPLACED, so it calls `generate_order_cancel_rejected`. The order goes from PENDING_CANCEL back to its earlier status and the strategy gets an OrderCancelRejected it can act on (retrying, re-hedging) just before the fill lands. Before this change the order was skipped."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1109,
    "summary": "The comment above the per-order re-read still says the order may have \"been sent a cancel of its own\", but the condition below it no longer skips such an order.",
    "failure_scenario": "A maintainer reading the comment will think orders with a cancel in flight are filtered here. The comment contradicts the new behaviour and the docstring, which invites someone to reintroduce the PENDING_CANCEL skip or mis-diagnose duplicate cancels."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1112,
    "summary": "Cancel-all now also re-sends a cancel while the strategy's own cancel for the same order is still in flight. Each such order costs a duplicate DELETE plus a 422 lookup GET against Alpaca's rate limit.",
    "failure_scenario": "The common `on_stop` pattern of `cancel_order` for each order and then `cancel_all_orders(strategy_only=False)`, or the reverse, sends roughly three requests per open order instead of one. With many orders this pushes toward Alpaca's per-minute limit and alpaca-py's 429 retries, and slows shutdown, all to cancel orders already being canceled."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1539,
    "summary": "When an order's own ID is unholdable and nothing in its chain is holdable, `_root_id` returns None and caches nothing. Every later call re-walks the `replaces` chain with `get_order_by_id` HTTP calls.",
    "failure_scenario": "An outside order with a non-ASCII ID that replaces another such order. Each of its trade updates (new, partial_fill, fill, ...) goes through `_apply` and `_node_order_id`, and each report generation repeats up to `MAX_REPLACE_CHAIN` REST lookups with the same result. Caching a negative result (a sentinel) would remove the repeated I/O on the stream path."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1113,
    "summary": "Re-sending a cancel treats the symptom, not the cause. When the repeat gets 422 and Alpaca shows the order CANCELED, `_cancel_refused` returns without generating OrderCanceled, so a cancel whose answer or update was lost still leaves the node at PENDING_CANCEL.",
    "failure_scenario": "The stream drops the `canceled` update, so the node holds O as PENDING_CANCEL. Cancel-all re-sends, Alpaca answers 422, and the lookup shows CANCELED, so the code returns at the CANCELED/PENDING_CANCEL check with no event. O stays PENDING_CANCEL indefinitely, which is the 'unanswered cancel' this change was meant to fix. The fix belongs in the lookup: generate OrderCanceled when it shows CANCELED, or reconcile the order's status."
  },
  {
    "file": "packages/canon-alpaca/src/canon_alpaca/execution.py",
    "line": 1499,
    "summary": "The `_root_id` docstring says an order whose chain ends at an unholdable ID is 'skipped when that is the ID it cannot hold'. In fact an open one raises and fails start-up. The MAX_REPLACE_CHAIN warning also says the order 'is traced to the order N links back' even when root ends up None.",
    "failure_scenario": "An open outside order with an empty client_order_id and no `replaces` reaches `order_status_report`, which raises 'is open with invalid string' and fails mass status. That contradicts 'skipped' in the docstring. A chain longer than MAX_REPLACE_CHAIN made only of unholdable IDs logs that it was traced to an order when nothing was traced and the result is None."
  },
  {
    "file": "docs/adr/0036-alpaca-adapter-order-management.md",
    "line": 41,
    "summary": "The ADR's evidence is rewritten to claim Kraken spot, Binance spot, Bybit and Interactive Brokers 'include every open order' (meaning they re-cancel PENDING_CANCEL) with no file, line or pin cited, and the Coinbase precedent that supported the old rule is dropped without saying Coinbase still differs.",
    "failure_scenario": "AGENTS.md rule 4: \"Anything not checked this session against a source is `UNVERIFIED`. Never present a guess as a finding.\" The same paragraph says Bybit sends the venue's cancel-all for an unsided command and Interactive Brokers 'selects from the cache', so it is unclear whether either re-sends a cancel for PENDING_CANCEL. An evidence-verifier pass cannot check the new claim against a pinned source."
  },
  {
    "file": "packages/canon-alpaca/README.md",
    "line": 230,
    "summary": "The re-wrapped bullets leave broken lines: 'cancels every open order of the' at line 230 and 'does an open order in anything but' at line 293 are cut short mid-sentence.",
    "failure_scenario": "Ragged source lines in the adapter's README, which AGENTS.md says 'comes first'. The next edit has to re-wrap them again, and the diffs get noisier."
  }
]
```
