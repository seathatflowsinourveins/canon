# Report: Alpaca replace arriving late, twice or out of order (`packages/canon-alpaca`)

## What was wrong

The execution client turned each Alpaca trade update about a replace into an event on its own. It kept no
record of which modify an update belonged to, or of which Alpaca orders a node order had already moved past.
Two things followed. A modify could get two outcomes (2, 3), or the wrong outcome (5, 8). And state left
behind by one path misled another (1, 4, 7, 9, 10). Per defect:

1. `_order_report` skipped every closed replacement the node's order did not yet name, meaning to drop a
   replacement that was rejected because the old order filled first. It also dropped a replacement that
   had taken over and then filled after its `replaced` was missed. The old order is `replaced`, which is
   skipped too, so the node's order was never reported.
2. The HTTP 422 path (`_replace_refused`) and the `order_replace_rejected` branch each generated
   `OrderModifyRejected`.
3. `_replaced` had no way to tell that it had already applied a `replaced`.
4. A fill on the replacement was applied before its `OrderUpdated`, so it was checked against the old
   quantity.
5. `canceled` and `expired` were applied to the node's order whichever Alpaca order they named.
6. `pending_replace` was not in `OPEN_STATUSES`, so `order_status_report` raised for it.
7. `_replaced` passed the replacement straight to `order_status_report`. That raises for `replaced` and
   (before this change) for `pending_replace`.
8. The retry check, `replaces_as_asked`, compared only the fields the request set. B's request set only
   the price, which A's replacement also held. A's replacement differs in quantity, and the client held
   A's PATCH answer, but neither was checked.
9. The answer was stored if the order had not finished, even when `replaced` had already been applied
   and popped nothing.
10. The old order's fill added the order to `_finished`. `_apply` returned on `_finished` before it
    reached the branch for a rejected replacement.

## What I changed, and why that way

All of it is in `packages/canon-alpaca/src/canon_alpaca/execution.py`. One design choice carries most of
the fixes: the client now keeps a record of its own replaces, instead of reading the cache back.
NautilusTrader's `generate_*` methods "Queue" their events (`nautilus_trader/live/clients.py` as
installed, v2.0.0rc8), so the cache can lag behind what the client has just generated.

- **`_modifying`**: each modify the client sends, keyed by (the Alpaca order ID the replace went to, a
  number for that modify), mapped to the node's order. It is recorded before the PATCH is sent. The HTTP
  answer, `replaced`, `order_replace_rejected`, a rejected replacement, a fill on the replacement, and the
  order's end each settle the modify once (`_settle`). A trade update finds its modify with
  `_open_modify`: the one whose PATCH answer is the replacement named, otherwise the first one sent.
  This fixes 2 and 10, and gives 8 and 9 something to check against.
- **`_answers`** is now keyed by modify and kept only while that modify is open (9). The order's end
  settles its open modifies (`_finish`), so neither map outlives the order.
- **`_superseded`**: the Alpaca orders a node order has moved past, filled in by `_update_to`, the one
  place that now generates `OrderUpdated`. Any update for one of these orders except a fill is stale and
  is logged and dropped (3, 5). A fill is still applied, because a fill on the old order before the
  replace is real. Entries are removed when the order ends.
- **Fill before `replaced` (4)**: `_take_over`. Only a working order fills, so a fill on a replacement
  shows that it took over. If the node's order still names the order the replacement replaces,
  `OrderUpdated` is generated from the replacement's terms as the update carries them, and the fill
  follows it. The later `replaced` is then stale.
- **A replacement that has moved on (7)**: `_replaced` follows `replaced_by` to the live order, up to
  `MAX_REPLACE_CHAIN` links, the way `_order_status_report` already did. `pending_replace` is handled by
  the next fix.
- **`pending_replace` (6)**: mapped to `ACCEPTED`, or `PARTIALLY_FILLED` when the order has fills. Until
  `replaced`, the order still works on its own terms, and the replace may still be rejected. I chose this
  over `PENDING_UPDATE` because the node did not necessarily send the modify, and test 6 requires
  `PARTIALLY_FILLED`.
- **Retry check (8)**: `replaces_as_asked` now compares every term the modify meant. A quantity the
  request leaves out is the order's own quantity when the modify was sent. A refusal counts as
  alpaca-py's retry only if no other modify of the client accounts for the replacement: none holds it as
  its PATCH answer, and while this modify is open, the old order has not already been moved past by
  another modify's `replaced`. The log line "an earlier attempt reached it" still fires when the retry's
  own `replaced` arrives before the 422 is handled.
- **Reconciliation (1)**: a closed replacement is skipped only when Alpaca does not show the order it
  replaces as `replaced` by it (`_took_over`). The old order is read from the orders already fetched,
  otherwise from Alpaca.
- **Rejected replacement after a fill (10)**: `_replacement_rejected` runs before the end check. It
  settles the modify. If the node's order has ended, it logs "… which had filled first; the fill is the
  outcome" (or names the other end). Otherwise it generates `OrderModifyRejected` as before. Such a
  rejection is ignored when the replacement is one the order has already moved past.

Docs corrected to match:
- The module header (the modify paragraph, and the known limit on reports).
- The package README: the Modify bullet; the Stream bullet, which already wrongly listed `replaced` as
  unmapped; and the test table.
- ADR 0036, one clause: "one per modify; after the old order has filled, the fill is the outcome and the
  rejection is logged". This clarifies the decision and does not change it.

Tests added in a new file, `packages/canon-alpaca/tests/test_replace_terms.py`:
- The full-terms retry comparison.
- `pending_replace` without fills is reported `ACCEPTED`.

No existing test file was edited, and no dependency was added.

## Upstream API or contract followed

- NautilusTrader `ExecutionClient` (v2.0.0rc8, as pinned): the `generate_order_updated`,
  `generate_order_modify_rejected` and `generate_order_filled` calls, unchanged.
- Alpaca "Replace Order by ID": as the code and ADR 0036 already quote it. The paper observation in
  ADR 0036 is that `replaced` carries the old order with `replaced_by`, and that the old order's status is
  `replaced`. `_took_over` and the forward walk rest on that observation.
- Alpaca's rule that a replacement is rejected when the old order "is filled before" it reaches the venue,
  as quoted in the code.
- alpaca-py 0.45.0: `AlpacaOrderStatus.PENDING_REPLACE` and `TradingClient.get_order_by_id`.

## What I ran, and the result

All runs are from the checkout root, under `mise exec -- uv run --locked`:
- Before the change: `pytest packages/canon-alpaca/tests/test_execution_client.py`: 13 failed, 101 passed.
  The 13 are the 11 named tests, two of them parametrized.
- After the change:
  - `pytest` (whole workspace): **373 passed, 21 skipped**. The skipped tests are the paper tests, which
    need keys; they were not run.
  - The 11 named tests alone: 13 passed.
  - The modify, replace and pending subset (34 tests): 5 runs in a row, all passed, to check for timing
    flakes.
  - `ruff check .`: all checks passed. `ruff format --check .`: 99 files already formatted.
    `ty check`: all checks passed.
  - `scripts/canon lint`: clean. I did not run `scripts/ci`, since the brief says this copy leaves out
    files it needs.

## What I am unsure of

- **No independent review ran.** The repo asks for `/code-review` before landing, but this brief forbids
  starting agents, and nothing here lands. The change touches the order event path and deserves a
  `/code-review` at high effort before it lands.
- **Alpaca behaviour (UNVERIFIED this session; no network):**
  - `pending_replace`: that it means "still working on its own terms". ADR 0036 already lists
    `pending_replace` as unverified on paper.
  - Whether Alpaca sends `canceled` for an old order it has replaced.
  - Whether a stream `order_replace_rejected` can follow a 422 for the same replace. Only the stand-in
    does that.
- **NautilusTrader behaviour (UNVERIFIED here):** that it drops a modify rejection for a filled order. I
  took this from the test's own comment and did not check it.
- **Case not covered: a `canceled` or `expired` for the old order that arrives *before* its `replaced`**,
  with a modify open. It is still applied as a cancel. Telling it apart would need a read of the old order
  at that moment. I left it out of scope; defect 5 and its test concern updates after the replace was
  applied.
- **Case not covered: fill before `replaced` across a chain.** `_take_over` acts only when the node's
  order names the order the replacement directly replaces. With two `replaced` updates both missed, a fill
  on the second replacement is still applied under the old terms.
- **No modify record after a restart.** Records are not kept across a restart, so a
  `order_replace_rejected` for a modify sent before it is logged and generates nothing. Reconciliation is
  left to settle that order.
- **The modify key assumes concurrent hooks.** If two modifies are sent to the same Alpaca order before
  either is answered, `_open_modify` picks the first one sent that has no answer. I assumed NautilusTrader
  may run the hooks concurrently; I did not check how it dispatches them.
