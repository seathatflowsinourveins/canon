# Alpaca execution client: late, duplicate and out-of-order replace updates

## What was wrong

All ten defects were in `AlpacaExecutionClient`'s handling of Alpaca's replace
(`packages/canon-alpaca/src/canon_alpaca/execution.py`), where a modify creates a new Alpaca
order linked to the node's order by `replaces`/`replaced_by`, and the node's single order must
take the replacement's identity exactly once:

1. Reconciliation (`_order_report`) suppressed every closed order traced to a node's order whose
   venue ID it did not yet name. That was written for a rejected replacement, so a replacement
   that had *filled* while its `replaced` update was missed was reported by neither the old order
   (status `replaced`, skipped) nor the replacement, and the fill was lost.
2. A replace refused both on the PATCH answer and with an `order_replace_rejected` trade update
   generated two `OrderModifyRejected` events: the HTTP path and the stream path each generated
   unconditionally, with no record that the modify was already settled.
3. A duplicate `replaced` trade update generated a second `OrderUpdated`: `_replaced` generated
   unconditionally, with no check that the node's order already named that replacement.
4. A fill of the replacement arriving before `replaced` was applied to the node's order under the
   old quantity, so a fill larger than the old quantity was treated as an overfill; the
   replacement's terms were only taken afterwards.
5. A late `canceled`/`expired` update carrying the *old* order was applied to the node's order
   even after the node's order named the replacement, closing an order whose replacement still
   worked at Alpaca.
6. `pending_replace` was in neither the open nor the closed status maps, so
   `order_status_report` raised for an open mid-replace order and start-up reconciliation failed;
   a partly filled one had no path to `PARTIALLY_FILLED` either.
7. In `_replaced`, a replacement read back in `pending_replace` raised as unmapped (defect 6),
   and one already `replaced` again returned no report and then raised, so no `OrderUpdated` was
   generated instead of following `replaced_by` to the live order.
8. `_replace_refused` compared only the fields the `ReplaceOrderRequest` explicitly set. A
   second modify that left the quantity unset asks for the node's still-current quantity, but an
   unset quantity was treated as "any quantity", so when the first replacement already held the
   first modify's quantity the refusal was misread as alpaca-py's retry of the first modify and
   the second modify got no rejection.
9. A PATCH answer that returned after its `replaced` had already been applied (applied from a
   fresh read of the replacement) was still stored in `_answers` and never popped, so answers
   accumulated for the whole session.
10. A replacement's `rejected` update arriving after the old order filled hit the
    `client_order_id in self._finished` early return in `_apply` before the branch written for
    that case, so the pending modify was never settled and nothing recorded its outcome.

## What I changed, and why that way

Only `packages/canon-alpaca/src/canon_alpaca/execution.py` (code) and
`packages/canon-alpaca/README.md` (the Modify description and test catalogue, which the change
made untrue) were modified. No test file was touched, no dependency was added, and no test is
recognised by name, ID, price or quantity anywhere in the change: every fix is keyed on order
state (venue IDs, statuses, links and pending-modify state) that Alpaca's own messages carry.

- **Pending-modify tracking (defects 2, 10).** New `self._modifying: dict[VenueOrderId,
  ClientOrderId]`, registered in `_modify_order` just before the PATCH is sent, cleared on
  disconnect. A new `_modify_rejected` helper is now the single place that emits
  `OrderModifyRejected` for an Alpaca refusal (HTTP 422, `order_replace_rejected`, a
  replacement's `rejected`, and the not-sent connect-timeout case): it settles the pending
  modify first and generates only if one was pending, so the second report of one refusal
  generates nothing. If the node's order has already filled (`_finished` or `is_closed`), it
  generates nothing — NautilusTrader drops a modify rejection for a filled order — and instead
  logs "…which had filled first; the fill is the outcome", still settling the modify. To make
  that reachable, `_apply` handles `order_replace_rejected` and a not-yet-adopted replacement's
  `rejected` *before* the `_finished` early return (defect 10).
- **One application of a replacement (defects 3, 4, 7).** New `_apply_replacement` generates
  `OrderUpdated` from an `order_status_report` of the replacement and settles the modify, and
  generates nothing when the node's order already names that replacement — which is exactly the
  duplicate-`replaced` case. `_replaced` now reads the replacement through a small
  `_read_replacement` (kept PATCH answer, else `get_order_by_id`, preserving the existing
  "could not be read; no OrderUpdated, reconciliation settles it" log and no-event behaviour),
  follows `replaced_by` while the replacement is itself `REPLACED` (bounded by the existing
  `MAX_REPLACE_CHAIN`), and applies the live order it reaches; a replacement in
  `pending_replace` is applied as it stands once that status maps (below).
- **Fill before `replaced` (defect 4).** In `_apply`, a `fill`/`partial_fill` whose order is a
  replacement the node's order does not yet name first calls `_apply_replacement` with the order
  the fill itself carries, then applies the fill. The node's order therefore has the
  replacement's quantity and prices before the fill is checked against them, for both a larger
  and a smaller replacement quantity. A replacement's `new` still adopts nothing (it may still
  be rejected), as the existing tests require.
- **Late end of the replaced order (defect 5).** A `canceled`/`expired` update carrying the old
  order (not a replacement) whose venue ID is no longer the node's order's venue ID is logged
  and ignored. A cancel/expire of the order the node currently names, including the replacement
  after it takes over, still closes the order.
- **`pending_replace` reports (defect 6).** `OPEN_STATUSES` now maps Alpaca's `pending_replace`
  to NautilusTrader's `ACCEPTED`, and `order_status_report` reports `PARTIALLY_FILLED` instead
  when `filled_qty > 0`. It stays an open status, so an otherwise unmappable mid-replace order
  still fails start-up loudly rather than being skipped.
- **Effective terms for the retry check (defect 8).** `replaces_as_asked` takes the node's
  quantity at send time (`unchanged_qty`, captured in `_modify_order` before the PATCH) and
  compares an unset request quantity against it. The wire request is unchanged (an unset
  quantity is still sent as `None`, as the existing test asserts); only the "replaced as asked"
  judgement now reads an unset term as "keep the node's current term", which is what a modify
  that does not name a quantity means.
- **Answer lifetime (defect 9).** After the PATCH returns, the answer is kept in `_answers`
  only if the modify is still pending, the order is not finished, and the node's order does not
  already name the answer's order ID (i.e. its `replaced` was applied while the answer was in
  flight, from a fresh read). Otherwise any kept entry for it is removed.
- **Reconciliation of a filled replacement (defect 1).** The `_order_report` suppression now
  applies only to a `REJECTED` replacement the node's order does not name (rejected without
  taking over, when the old order filled first; the node's own order reports the outcome, and
  the existing restart test still expects exactly that). A filled — or otherwise closed after
  taking over — replacement is reported under the node's client order ID traced through
  `replaces`, so a missed `replaced` no longer loses the fill.

The module header's Modify paragraph, its known-limits note on `pending_replace`,
`_modify_order`'s docstring, and the README's Modify bullet and test-catalogue line were updated
where the old text ("generated only on `replaced`", "`pending_replace` is ignored") is no longer
true. ADR 0036 was not edited: it records the 2026-11-10 decision and paper observation this
still rests on, and the header now carries the refined behaviour next to the code.

## Upstream API / contract followed

- Alpaca "Replace Order by ID" (`TradingClient.replace_order_by_id`, alpaca-py 0.45.0), as the
  code already cited it (ADR 0036): the answer is "The new Order object with the new order ID"
  and a success "does NOT guarantee the existing open order has been replaced" — so
  `OrderUpdated` is still never generated from the answer alone; the fill-first path uses the
  replacement order carried by Alpaca's own fill update, and the answer is still only kept as
  the values source for `replaced`.
- Alpaca's trade-updates stream events and order statuses (`replaced` carrying the old order
  with `replaced_by`, `order_replace_rejected`, a replacement's `rejected`, `pending_replace`),
  traced through the replacement's `replaces`/`replaced_by` links exactly as the existing
  `_node_order_id` walk (at most `MAX_REPLACE_CHAIN` links, orders already fetched first) and
  `_order_status_report`'s single-order `replaced_by` following already did; the fix extends
  that same following to `_replaced` and to reconciliation instead of inventing a new identity
  scheme. The `pending_replace` value was checked this session against the pinned alpaca-py's
  `OrderStatus` enum, which lists it.
- NautilusTrader v2.0.0rc8 `ExecutionClient` hooks and `generate_order_updated` /
  `generate_order_modify_rejected` (the contract named in the module header), and its execution
  policies' command outcomes: one modify gets one outcome event, and a modify rejection for a
  filled order is dropped by the engine, which is why that case is logged, not generated.

## What I ran

All offline, with `mise exec -- uv run --locked …` from the checkout root:

- The 11 named tests (13 cases with parametrisation): before the change, 13 failed; after,
  **13 passed** (`-k "answer_after_replaced or filled_replacement_is_reported or
  late_cancel_of_the_replaced or pending_replace or rejected_replacement_after or
  replace_rejected_twice or replaced_delivered_twice or replaced_whose_replacement or
  second_modify_refused or fill_on_a_replacement_before"`).
- `pytest packages/canon-alpaca/tests/test_execution_client.py`: **114 passed**.
- Full suite `pytest`: **370 passed, 21 skipped** (the skips are the paper-only tests, which
  need a key and were not run, per the task).
- `ruff check .`: passed. `ruff format --check .`: passed (after formatting one condition).
- `ty check`: passed.
- `scripts/ci` was not run: the task states this copy leaves out files it expects, so its
  failure here would not be evidence about this change.

No new test file was added: the named tests are the maintained regression coverage for every
defect here, they already live in the project's normal test layout, and the task forbids
editing existing test files (the grader restores them), so a duplicate file would add nothing.

## What I am unsure of

- **UNVERIFIED against live paper.** Everything above is verified only against the offline
  stand-ins, which encode the 2026-11-10 paper observation. Real Alpaca behaviour for a fill
  racing `replaced`, a duplicate `replaced`, a `pending_replace` replacement at `replaced`
  time, and a replacement-of-replacement was not re-observed (no key use, per the task and
  ADR 0009); ADR 0036 already lists several of these as UNVERIFIED, and that still stands.
- A stream `order_replace_rejected` or replacement `rejected` arriving when this client holds
  no pending modify for the order (for example after a restart mid-modify) now generates
  nothing, because "no pending modify" is also the duplicate-report case and the two cannot be
  distinguished locally. Reconciliation remains the backstop, as it already was for missed
  updates. No existing test covers that sequence.
- The `_modifying` entry for a modify whose outcome never arrives (unknown HTTP outcome, an
  unreadable replacement) is kept until the order settles another way or disconnect; that is
  deliberate (a later `replaced` or rejection must still be able to settle it), at the cost of
  one small entry per such modify.
