# Alpaca execution client — replace handling fix

## What was wrong

In `packages/canon-alpaca/src/canon_alpaca/execution.py`, the modify path (Alpaca's
replace) assumed Alpaca's updates arrive once, in order, and that the PATCH answer
arrives before its `replaced` update. Confirmed by running the 11 named tests
before the change: 13 cases failed.

1. **Missed `replaced`, replacement fills.** Reconciliation suppressed every closed
   replacement whose venue ID the node's order did not yet name
   (`_order_report`: `order.status in CLOSED_STATUSES`), so a filled replacement
   was reported by neither the replaced order (skipped) nor itself, and the fill
   was lost.
2. **Double rejection.** A replace refused on the HTTP answer and again with
   `order_replace_rejected` generated two `OrderModifyRejected` events; neither
   path knew the other had reported it.
3. **Duplicate `replaced`.** The second delivery generated a second
   `OrderUpdated`; nothing recorded that the replace had been applied.
4. **Fill before `replaced`.** A fill on the replacement was applied under the
   old order's terms, so a fill larger than the old quantity was dropped as an
   overfill, and the order took the replacement's terms only afterwards.
5. **Late end of the replaced order.** A `canceled`/`expired` for the old order
   after it was replaced closed the node's order while its replacement still
   worked.
6. **`pending_replace` unmapped.** `OPEN_STATUSES` had no `pending_replace`, so
   an open mid-replace order raised in `order_status_report` and failed start-up
   reconciliation; its fills were not considered either.
7. **Replacement moved on.** When the order read at `replaced` was itself
   `pending_replace` or already `replaced`, `order_status_report` raised
   (`replaced` is deliberately unmapped) and no `OrderUpdated` was generated,
   instead of following the chain to the live order.
8. **Second modify taken for a retry.** `replaces_as_asked` treated a quantity
   the request left unset as "any quantity", so a second modify whose effective
   quantity (the node's current quantity, which an unset quantity keeps) differed
   from the first replacement's was mistaken for alpaca-py's retry of the first
   and got no rejection.
9. **Late PATCH answer kept.** An answer arriving after its `replaced` had been
   applied was stored in `_answers` and never removed, so answers accumulated
   for the whole session.
10. **Rejected replacement after the old order filled.** `_apply` returned on
    `client_order_id in self._finished` before the branch for a replacement's
    `rejected`, so when the old order filled first, that branch was dead:
    nothing settled the modify and nothing said what became of it.

## What I changed, and why that way

Only `packages/canon-alpaca/src/canon_alpaca/execution.py` (plus the package
README, below). No dependency added; no test file touched.

- **Pending-modify tracking.** New `self._modifying: dict[str, ClientOrderId]`
  (venue order ID being replaced → node's order), registered in `_modify_order`
  before the PATCH call, cleared on disconnect, and settled by the first outcome
  through a new `_settle_modify` helper. Every `OrderModifyRejected` generation
  for a sent modify now goes through that settlement: the HTTP-refusal path
  (`_replace_refused`), the stream's `order_replace_rejected`, and a
  replacement's `rejected` each generate only if they settle a pending modify,
  so Alpaca reporting one refusal twice is one rejection (defect 2). The
  alpaca-py-retry exception is preserved and checked first: if Alpaca shows the
  order replaced as asked, no rejection is generated and the modify stays
  pending for its `replaced`.
- **Effective terms for the retry check.** `replaces_as_asked` takes the node's
  current quantity and uses it when the request leaves `qty` unset, because an
  unset field keeps the order's current term. A second modify asking (in effect)
  for the node's quantity is therefore no longer matched to a first replacement
  holding a different quantity, and is rejected (defect 8).
- **Idempotent, chain-following `replaced`.** `_replaced` now returns without
  generating when the node's order no longer names the old order (already
  applied or moved on — defect 3), and follows `replaced_by` links (from
  `_answers` first, then `get_order_by_id`, bounded by the existing
  `MAX_REPLACE_CHAIN`) while the replacement's status is `replaced`, to the
  live order (defect 7). The unreadable-replacement behaviour is unchanged: log
  "no OrderUpdated, reconciliation settles it" and generate nothing. The actual
  generation is factored into `_apply_replacement`, shared with the fill path,
  which pops any kept answer for that replacement, maps its client order ID,
  generates `OrderUpdated`, and settles the modify.
- **Fill before `replaced`.** In `_apply`, a `fill`/`partial_fill` traced to a
  replacement whose ID the node's order does not yet name first calls
  `_apply_replacement` with that order, generating `OrderUpdated` under the
  replacement's quantity/prices, and only then generates the fill (defect 4).
  If the replacement cannot be mapped, the exception is logged by the existing
  update handler and the fill is not applied under the old terms.
- **Late end for the replaced order.** A `canceled`/`expired` traced to the
  node's own (old) order whose venue ID the node's order no longer names is
  logged and ignored (defect 5). Ends for the replacement itself (a different
  Alpaca client order ID) are unaffected.
- **Rejection after a fill, before the finished check.** A replacement's
  `rejected` is now handled before the `_finished` early return. If the node's
  order has filled, the modify is settled (`_modifying` no longer holds it),
  a warning is logged — "which had filled first; the fill is the outcome, and
  the modify is settled" — and no event is generated (NautilusTrader would drop
  a modify rejection for a filled order); otherwise it is the modify's
  `OrderModifyRejected` (defect 10). A fill deliberately leaves the pending
  modify in place so this later rejection can settle it; other terminal events
  (`canceled`, `expired`, `rejected`) settle it directly.
- **`pending_replace` status.** `OPEN_STATUSES` maps Alpaca's
  `pending_replace` to NautilusTrader's `ACCEPTED`, and `order_status_report`
  overrides it to `PARTIALLY_FILLED` when `filled_qty > 0` (defect 6). The
  `pending_replace` trade *update* is still ignored, as before, because
  NautilusTrader generates `OrderPendingUpdate` itself.
- **Filled replacement reported.** `_order_report` suppresses a traced
  replacement only when its status is `rejected` (closed without taking over,
  when the old order filled first), instead of any closed status, so a filled
  replacement is reported `FILLED` under the node's client order ID even when
  `replaced` was missed (defect 1).
- **Late answers not kept.** After a successful PATCH, the answer is stored in
  `_answers` only while the node's order still names the order the answer
  replaces (`answer.replaces`); if its `replaced` (or a fill on it) was applied
  while the answer was in flight, the order already holds its values and the
  answer is discarded instead of being kept for the session (defect 9).
- **Docs.** The module header's Modify paragraph and `pending_replace` limit,
  and `packages/canon-alpaca/README.md`'s Modify bullet and modify test
  coverage line, were updated to state the above (stream confirmation includes
  a fill on the replacement first; confirmations are applied once; a late end
  does not close the replacement; late answers are not kept; refusals are once
  per modify; `pending_replace` reporting). No ADR was changed: ADR 0036's
  decision (option (b), `OrderUpdated` on Alpaca's confirmation, values from
  the answer or a read) still holds, and its `UNVERIFIED` items remain
  unverified on paper.

No new test file was added: the regression tests for every defect are the ones
already in this checkout (`packages/canon-alpaca/tests/test_execution_client.py`,
unmodified, as instructed), which failed before the change and pass after it.

## Upstream API / contract the change follows

- Alpaca **"Replace Order by ID"** (`TradingClient.replace_order_by_id`,
  alpaca-py 0.45.0): the answer is "The new Order object with the new order ID"
  and a success "does NOT guarantee the existing open order has been replaced" —
  so `OrderUpdated` still waits for stream confirmation, never the answer
  alone; the answer is only a source of the replacement's values. Unset request
  fields keeping the current term is the same endpoint's model (only what is
  sent is replaced), which is what the effective-quantity check encodes.
- Alpaca's **trade updates stream** (`replaced` carrying the old order with
  `replaced_by`, paper-observed, ADR 0036) and its fill semantics: a fill's
  `qty` is that fill's shares, applied against the order's current quantity —
  hence applying the replacement's terms before its fill.
- alpaca-py's **`OrderStatus`** (verified this session against the pinned
  package: the enum includes `PENDING_REPLACE = 'pending_replace'`): an order
  in that status is open and mid-replace.
- NautilusTrader v2.0.0rc8's **execution policies / `ExecutionClient`
  contract** (as the file header already cites): `OrderUpdated`,
  `OrderModifyRejected` (once per modify command),
  `generate_order_updated`/`generate_order_modify_rejected`, and order status
  reports with `ACCEPTED` / `PARTIALLY_FILLED` for open orders.

## What I ran, with its result

- The 11 named tests before the change: **13 cases failed** (two tests are
  parametrized ×2), confirming the defects.
- The 11 named tests after the change: **13 passed**.
- `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -q`: **114 passed**.
- `mise exec -- uv run --locked pytest -q` (whole workspace, offline):
  **370 passed, 21 skipped** — the skips are the paper tests, which need a key
  and were not run, per instructions.
- `mise exec -- uv run --locked ruff check .`: **All checks passed**.
- `mise exec -- uv run --locked ruff format --check .`: **98 files already formatted**.
- `mise exec -- uv run --locked ty check`: **All checks passed** (one
  invalid-assignment diagnostic in the new `_replaced` chain code appeared
  during development and was fixed by widening the local's annotation to
  `AlpacaOrder | RawData | None`).

`scripts/ci` was not run: the task states it expects files this copy leaves
out, so its failure here may not be mine; the tests and the three code checks
above are the graded gates. Nothing was landed (`scripts/land` not run), no
paper test was run, no credential was read, and no network was used beyond
`uv` installing the pinned `uv.lock` packages. The change is left uncommitted
in the working tree: `packages/canon-alpaca/src/canon_alpaca/execution.py` and
`packages/canon-alpaca/README.md`.

## Anything I am unsure of

- **Late ends in other shapes (UNVERIFIED against Alpaca's pages).** The
  late-end guard covers `canceled`/`expired` for the old order after a
  replace. A late `fill` for the old order after its replacement took over, or
  a `canceled` for a replacement that has not yet taken over, keep their prior
  behaviour; no test or document in this checkout settles what Alpaca sends in
  those races.
- **Replacement `rejected` with no pending modify.** Such an update is now
  dropped silently when no modify is in flight for the order (e.g. after a
  reconnect cleared `_modifying`). That is what makes duplicate reports one
  rejection, but a rejection arriving only after a restart would be missed
  until reconciliation; no paper observation covers it.
- **Two modifies genuinely in flight for one order** share one `_modifying`
  entry (keyed by the venue ID they replace), so the first outcome settles
  both as far as duplicate suppression is concerned. The node normally has one
  pending update per order; the second-modify test exercises exactly this
  overlap and passes, but finer per-command attribution is not attempted.
- Paper behaviour (`order_replace_rejected`, `pending_replace`, replacement
  `new`/`rejected` timing) remains as ADR 0036 records it: `UNVERIFIED` beyond
  the 2026-11-10 observation; I did not run any paper test.
