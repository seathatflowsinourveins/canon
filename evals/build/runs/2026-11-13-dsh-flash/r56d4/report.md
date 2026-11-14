# canon-alpaca replace handling: late, duplicate and out-of-order updates

## What was wrong

`AlpacaExecutionClient` treated Alpaca's replace as a mostly one-way transition: the PATCH answer was stored,
`OrderUpdated` was generated only from the stream's `replaced`, and terminal order updates were applied in
arrival order without asking which venue order the node's order currently names. That produced ten related
faults:

1. A replacement that filled after `replaced` was missed was dropped from reconciliation: the old order was
   skipped as `replaced`, and the filled replacement was treated as one that "closed without taking over".
2. The same refusal reported by the HTTP answer and by `order_replace_rejected` produced two
   `OrderModifyRejected` events for one modify.
3. Two `replaced` updates for the same replace produced two `OrderUpdated` events.
4. A fill for a replacement that arrived before `replaced` was applied under the old order's quantity; a
   larger fill was dropped as an overfill and the replacement's terms were adopted only afterwards.
5. A `canceled` or `expired` update for an already-replaced old order closed the node's order while the
   replacement was still working.
6. `pending_replace` was not in the status map, so an open order mid-replace failed start-up reconciliation,
   and a partially filled one could not be reported partly filled.
7. A replacement read at `replaced` that was itself `pending_replace`, or already replaced again, was not
   followed; `order_status_report` rejected/none'd it and no `OrderUpdated` was generated.
8. A second modify, refused after the first had replaced the order, was mistaken for alpaca-py's retry when
   the first replacement happened to hold the prices it sent, because the comparison ignored the quantity
   the request left unchanged.
9. A PATCH answer that returned after its `replaced` had been applied was stored and never removed.
10. A replacement rejected after the old order filled returned at the `_finished` guard before the branch
    written for that case, so nothing was logged or settled.

## What changed, and why that way

All changes are in `packages/canon-alpaca/src/canon_alpaca/execution.py`.

* Added a per-node-order record of the live Alpaca order already adopted (`_adopted`), the set of
  replacement IDs whose answer/link was consumed (`_handled_replacements`), and per-modify attempt state
  (`_modifying`, `_settled_modifies`). These are cleared on disconnect.
* `_replaced` now resolves the replacement from the PATCH answer or Alpaca, then follows its own
  `replaced_by` links to the live order (`_live_order`). `pending_replace` is treated as the live order
  (Alpaca's "Order Lifecycle"); a chain that moved on is followed to the newest order.
* `_adopt` generates the single `OrderUpdated` for a replacement, records it, consumes the matching PATCH
  answer, and settles the modify. `_already_adopted` makes duplicate `replaced` updates and duplicate
  adoption attempts no-ops.
* A replacement's fill, cancel or expiry arriving before `replaced` now adopts the replacement's terms
  first (`_adopt_if_live`). It will move forward through a chain of replacements but not back to one already
  superseded (`_replaces_chain_contains`). This keeps a fill larger than the old quantity from being
  dropped.
* `_apply` handles the replacement-rejected-after-old-filled case before the `_finished` guard: it logs
  `"which had filled first; the fill is the outcome"`, drops the answer, and settles the modify instead of
  returning silently. `canceled`/`expired` updates for an order Alpaca has replaced (or that is no longer the
  node's adopted order) no longer close the node's order.
* `_modify_order` registers each attempt, drops a PATCH answer whose replacement was already handled, and
  `_modify_rejected` emits at most one `OrderModifyRejected` per attempt whether the refusal first arrives
  on HTTP or on the stream. A later modify starts a new attempt.
* `replaces_as_asked` now fills request fields that were omitted (`qty`, limit and stop price) from the
  replaced order's terms, because a PATCH replaces only the fields it names. A later modify of another
  quantity is therefore not mistaken for the earlier retry.
* `order_status_report` maps `pending_replace` as an open `ACCEPTED` order, and as `PARTIALLY_FILLED` when
  it has fills.
* `_order_report` now asks `_replacement_took_over` before discarding a closed replacement: the order named
  in `replaces` showing `replaced` means the replacement took the node's order over and must be reported
  (fixing the missed-`replaced` fill); a filled/canceled/expired prior order means it did not.

README and ADR 0036 were corrected where they stated that `OrderUpdated` comes only from `replaced`, that a
replacement rejected after the old order filled becomes `OrderModifyRejected`, that `pending_replace` is
unmapped, or that `replaced` is an unmapped stream event.

No dependency was added. No test file was edited; the regression tests already in the checkout were used.

## Upstream API / contract followed

* NautilusTrader `ExecutionClient` hooks and `generate_order_updated` / `generate_order_filled` /
  `generate_order_canceled` / `generate_order_expired` / `generate_order_modify_rejected`, per the pinned
  Python adapter guide and execution policies already cited in the module header.
* Alpaca "Replace Order by ID": the answer is the replacement with a new ID, and a success does not
  guarantee the old order was replaced.
* Alpaca trade-update stream / "Order Lifecycle": `replaced`, `replaced_by`, `replaces`,
  `order_replace_rejected`, and `pending_replace` as a live order status.
* The paper observation of 2026-11-10 already recorded in ADR 0036 (`test_paper_replace.py`) for the
  Alpaca-made client order ID, `replaces`/`replaced_by`, and the 422 "order already replaced" refusal.

## What was run, with results

From the checkout root, with the pinned environment:

* `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -q -k "<the 11 named tests>"`
  — **13 passed** (the two `test_fill_on_a_replacement...` parameter cases make 13), 101 deselected.
* `mise exec -- uv run --locked pytest packages/canon-alpaca/tests -q` — **242 passed, 19 skipped** (paper
  tests skipped without credentials).
* `mise exec -- uv run --locked pytest -q` — the whole workspace: **370 passed, 21 skipped**.
* `mise exec -- uv run --locked ruff check .` — all checks passed.
* `mise exec -- uv run --locked ruff format --check .` — 98 files already formatted.
* `mise exec -- uv run --locked ty check` — all checks passed.
* `scripts/canon lint` — `canon: lint clean`.

`scripts/ci` was not run: this copy leaves out files it expects. No paper test and nothing needing a key or
network was run; nothing landed.

## What I am unsure of

* The new handling is proven only against the offline stand-ins and the existing tests. The corresponding
  live-Alpaca behavior remains **UNVERIFIED** by this session (no paper run, by the task's rules); ADR 0036
  still lists the affected observations as not shown by a run.
* `pending_replace` is mapped from Alpaca's documented order lifecycle; how often Alpaca sends it, and
  whether its `filled_qty` is always current, is not observed here.
* The retry/refusal discriminator (`replaces_as_asked` inheriting the replaced order's omitted terms) is a
  best-effort distinction between alpaca-py's internal retry and a genuinely different second modify. Two
  byte-identical modifies are indistinguishable at the wire level; the implemented rule treats a refusal as
  a retry only when the replacement already holds every term the request asked for, including the inherited
  old quantity. The named test for this case passes, but the exact ambiguity a real Alpaca sequence creates
  is not paper-verified.
* A fill on the old order racing a replace while a replacement also exists is still the least certain path;
  the code follows the ADR's stated outcome (old fill applies, replacement rejection is settled rather than
  emitted as `OrderRejected`), but no offline test in this checkout exercises a partial fill on the old order
  racing a replacement.
* The ADR 0036 amendment is textual; its `status`, date and `review-by` were left as they were because this
  session only corrected claims the code change made untrue.
