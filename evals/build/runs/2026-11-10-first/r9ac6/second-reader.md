# Second reader: r9ac6
Mark: fix
Blocker: none
## Findings
1. packages/canon-alpaca/src/canon_alpaca/execution.py:1245 — `_reject_modify` returns with nothing logged when no pending command matches (`attempt is None`, or the attempt has already been popped). That is the intended dedupe for a refusal arriving a second time (defect 2). But an `order_replace_rejected`, or a replacement's `rejected`, that matches no tracked command at all now produces no event and no log line. Before the change these always became `OrderModifyRejected`. Sequence: a modify is sent, the client goes through `_disconnect` (line 843 clears `_modifying`) and reconnects, and then Alpaca's refusal arrives. `_pending_modify` returns None and the node's order stays pending update with nothing logged. The window is narrow, but rule 5 wants at least a warning that tells "already settled" apart from "no command known". (severity: fix)
2. packages/canon-alpaca/src/canon_alpaca/execution.py:1144, 1156, 1189 — on the unknown-outcome paths (`_log_unknown_replace`), the `_ModifyAttempt` stays in `_modifying` and is removed only if a later stream update matches it by replacement id or by `replaces_as_asked`. If none comes, the entry stays for the whole session. This is the same kind of build-up defect 9 removes for `_answers`, though it is one small object per unknown-outcome modify. A review would have asked for a bound or a note in the header's known limits. (severity: fix)

No blocker found:
- 1, test-gaming: `flags` is empty, no test file was changed, and the new test file only adds cases.
- 2, rule 3: only a stdlib `dataclass` was added, and no dependency. The dedupe sets are justified because node events are queued, which the comment at lines 772-776 explains.
- 3, hidden failure: the reconciliation suppression was narrowed from every closed status to `REJECTED`, which is what defect 1 asks for, not a weakened check. A failed read in the `replaced` chain propagates to `_apply_trade_update`, which logs it.
- 4, broken behaviour: I traced these by hand and each holds:
  - an alpaca-py retry where the stream `replaced` comes first (the attempt is matched in `_confirm_modify`, then `same_call` holds through `replacement_id`);
  - a fill on R1, then O's `replaced` read showing R1 already replaced by R2, then R1's `replaced` (one `OrderUpdated`, no roll-back);
  - two pending modifies where `replaced` matches only the one whose expected qty fits.
- 5, report: each report claim checked holds in the code: per-command attempts, answers kept only while pending, the confirmed and superseded ID sets, terms applied before the first replacement fill, the chain bounded by `MAX_REPLACE_CHAIN`, the `pending_replace` mapping, the narrowed reconciliation suppression, the late rejection logged after a fill, the documentation updates, 7 added cases, and existing tests unchanged.

## Per-defect check
1. filled-replacement-lost-when-replaced-missed — removed: `_order_report` (about line 1960) now suppresses a traced replacement only when it is `REJECTED` and not adopted, so a filled, canceled or expired replacement is reported under the node's ID.
2. refused-replace-rejected-twice — removed: one `_ModifyAttempt` per command, and `_reject_modify` pops it once, whether HTTP (`_replace_refused`) or the stream (`order_replace_rejected` through `_pending_modify`) settles it first.
3. replaced-applied-twice — removed: `_replaced` returns if `old.id in _replaced_orders`, and `_update_replacement` skips IDs already in `_updated_orders`.
4. replacement-fill-before-replaced — removed: a fill on an unadopted replacement calls `_update_replacement` (an `OrderUpdated` with the replacement's terms) before `_fill`, and it also marks the old ID confirmed, so a later `replaced` is a no-op.
5. late-cancel-of-replaced-order — removed: `canceled`, `expired` or `rejected` for an ID in `_replaced_orders`, or for a non-current ID in `order.venue_order_ids`, is ignored.
6. pending-replace-fails-startup — removed: `PENDING_REPLACE` is mapped to `ACCEPTED` in `OPEN_STATUSES`, or to `PARTIALLY_FILLED` in `order_status_report` when `filled_qty` > 0.
7. replacement-moved-on-before-replaced — removed: `_replaced` follows `replaced_by` while the status is `REPLACED`, up to `MAX_REPLACE_CHAIN`, and `pending_replace` now maps instead of raising.
8. second-modify-refusal-taken-for-retry — removed: `expected` includes the unchanged qty, and `same_call` refuses a replacement that is already confirmed, held in `_answers`, or claimed by another attempt.
9. answers-kept-for-the-session — removed: `_confirm_modify` pops the answer and settles the attempt, and a late PATCH answer is stored only `if attempt in self._modifying`.
10. rejected-replacement-after-fill-never-reached — removed: the `_finished` check now comes after the `order_replace_rejected` and rejected-replacement branches, and `_reject_modify` logs "which had filled first; the fill is the outcome".

## What I ran
- `mise exec -- uv run --locked pytest -q packages/canon-alpaca/tests/test_execution_client.py packages/canon-alpaca/tests/test_replace_lifecycle.py`: 121 passed in 55.97s.
- `scripts/canon lint`: lint clean.
- `mise exec -- uv run --locked ruff check packages/canon-alpaca`: all checks passed.
- `mise exec -- uv run --locked ty check`: all checks passed.
- I checked the NautilusTrader stub for `Order.venue_order_ids` (the venv's `model/__init__.pyi` line 2493): it exists.
- Not run: the full suite (the grader reports 377 passed), `scripts/ci`, paper tests and the network.
- UNVERIFIED: whether Alpaca lists an open replacement next to a `pending_replace` old order. If it does, start-up would get two reports for one client order ID. I found no source here, so I did not count it as a finding.
