---
status: proposed
date: 2026-11-12
decision-makers: repo owner (requirement, 2026-11-12); lead orchestrator (Claude) for the bounds, the pick and the default
review-by: 2026-12-12
---

# A reduce-only sell goes to Alpaca as `position_intent=sell_to_close`, which Alpaca refuses when it would open a short; open: the field on a market order, and live

## Context and Problem Statement

Upstream's way to stop new entries while exits keep working is the `REDUCING` trading state, which admits
only reduce-only orders. Our adapter refuses every reduce-only order, so `REDUCING` admits nothing through
it, and Alpaca's order has no reduce-only field. What may the adapter send for `reduce_only=true` without
discarding the flag? What stops entries meanwhile (ADR 0110, gap (i)) is ADR 0115's.

## Decision Drivers

* Upstream's contract: an execution client "must never silently discard `reduce_only=true`. It must either
  send a documented venue instruction that enforces the same intent or reject the order before transport."
* The lead's bound, 2026-11-12: no form that can silently drop the flag, and the fast sell path pays for
  nothing it does not use.
* Only what Alpaca is shown to refuse counts as enforcing.

## Considered Options

1. Keep rejecting every reduce-only order.
2. Send a reduce-only SELL with `position_intent=sell_to_close`; reject every reduce-only BUY.
3. Send a reduce-only SELL only on an account whose `no_shorting` read true at connect.
4. The same, with `no_shorting` read before each send.

## Decision Outcome

Chosen: **option 2**. `proposed` until the gaps below close; until a node turns a setting on, `main`
refuses every reduce-only order.

* **The form.** With `reduce_only_sells`, a reduce-only SELL carries `position_intent=sell_to_close`;
  nothing else changes on the wire and no account setting is read. A reduce-only BUY is refused before
  transport: `buy_to_close` was not probed and no strategy here covers a short.
* **Scoped by order type** (`sell_to_close_order_types`; the lead, 2026-11-12): a limit, a stop and a
  stop-limit carry the field, the types a paper run shows Alpaca enforcing. A market order is outside
  the scope until the same is shown for it: a reduce-only market SELL is refused before transport.
* **What makes it enforcing is a paper run, not the reference,** which says only "Represents the
  desired position strategy." On paper account A with shorting allowed, `sell_to_close` while flat was
  refused, "422 42210000 position intent mismatch, inferred: sell_to_open, specified: sell_to_close";
  for the share held it was accepted; the same sells with no intent were accepted while flat. So the
  instruction is on the order and holds whatever the account's mode.
* **Why not the others.** Option 3, the first draft, was corrected by the verifier: a mode turned off
  while the node runs would let a later reduce-only sell open a short, a silent discard, and upstream's
  own adapters refuse where a venue cannot express the flag. Option 4 answers that but costs a request
  before every sale and needs the account long-only. Option 1 conforms and leaves `REDUCING` unusable.
* **Every closing sell** (the lead): `sell_to_close_on_long_exits`, off by default, puts the field on a
  SELL with no reduce-only flag when the node's cached net position in the instrument is long, within
  the same order-type scope. It sets no reduce-only flag, and a wrong cache errs safe: long in the cache
  and flat at Alpaca is refused there; flat in the cache is today's plain sell. The lead turns it on
  for every paper account in its own change. The adapter sends the order once and reports a refusal
  with Alpaca's code and words.
* **Live.** No account needs long-only for this, live included.

**Gaps, and what would close each.**

* **The market order is outside the scope.** An execution-only node cannot send one: NautilusTrader's
  risk engine denies a market order while the cache holds no price. So a market exit carries no intent,
  and after a holding changed outside the node it is refused only where the account is long-only. That
  is accepted on a long-only account and nowhere else (the lead, 2026-11-12): on an account that
  allows a short, and live, a node sends no plain market sell, and an exit that would need one is a
  marketable limit carrying the intent. Closed by the proof on a node with a data feed; the market
  order then joins the scope in a one-line change.
* **A replace of a `sell_to_close` order.** Alpaca's replace carries no position intent; what it checks
  when such an order's quantity grows is UNVERIFIED, so the adapter rejects that modify. Closed by a
  paper run of the replace, which the adapter's rule then follows.
* **A mismatch is not handled by any strategy.** What a strategy does after the refusal is not built.
  Closed by a later change with a failing test first.
* **A run in `REDUCING`.** Closed by a paper run in which an entry is denied and a released protective
  sell with the flag passes.
* **Live.** Everything here is a paper account's behaviour. That a live account refuses the same way is
  UNVERIFIED and is not relied on while the settings are off.

**What else the adapter change carries.** With every setting off the adapter behaves as on `main`, on
the wire and at start-up. Two more settings land off: `report_external_trailing_stops`, which reports an
open trailing stop placed outside the node as an external order instead of failing the start by name (an
outside order is never taken on unasked; the lead, 2026-11-12), and `report_external_orders`, whose open
defects the adapter's README lists.

### Consequences

* Good: the flag is never dropped, a sale pays no extra request, no account's configuration is touched,
  and stopping entries does not depend on it (ADR 0115).
* Bad: the enforcement rests on behaviour Alpaca shows and does not document; a change there would show
  as accepted sells in the paper proofs, which is why they stay as standing tests.

### Confirmation

Each landing adds its line here. The probe's prints, the account reads and their times are from run
logs kept outside the repository, so a later reader cannot re-check them from it: UNVERIFIED beyond
this record. "Far over the ask" in the probe is the probe's own choice of limit, not a measured figure.

* 2026-11-12, 03:28 New York, account A, overnight part: a one-off probe, approved by the lead for one
  run and not kept in the repository, since a standing test must not change an account's configuration.
  `no_shorting` was turned off inside one `mise run paper:test` under the account's lock and restored in
  a `finally`; `mise run paper:long-only tests check` then printed `no_shorting: true`, the starting
  value. Limit sells far over the ask, Alpaca's answer at the submit: flat, `sell_to_close`: "422
  42210000 position intent mismatch, inferred: sell_to_open, specified: sell_to_close"; flat, no intent:
  accepted; flat, `sell_to_open`: accepted; long 1, 2 shares, `sell_to_close`: "403 40310000 insufficient
  qty available for order (requested: 2, available: 1)"; the same with no intent: the same 403; long 1,
  1 share, `sell_to_close`: accepted; long 1, 1 share, `sell_to_open`: "422 42210000 position intent
  mismatch, inferred: sell_to_close, specified: sell_to_open".
* 2026-11-12, 03:23, account A long-only,
  `tests/test_paper_protect_seam.py::test_paper_a_sell_with_no_shares_or_beyond_the_long_is_refused_on_a_long_only_account`,
  1 passed: 1 share while flat, "403 40310000 account is not allowed to short"; 2 shares while long 1,
  the insufficient-qty 403; 1 share while long 1, accepted.
* 2026-11-12, 04:41, account A long-only, pre-market, through the adapter build before its landing,
  on an execution-only node (the test file lands with the adapter change),
  `tests/test_paper_sell_to_close.py::test_paper_a_sell_to_close_reaches_alpaca_through_the_adapter`,
  1 passed: a reduce-only SELL while flat is rejected, "Alpaca refused the order (422): 42210000:
  position intent mismatch, inferred: sell_to_open, specified: sell_to_close", where a sell with no
  intent got the long-only 403 in the 03:23 run above, so the field reaches Alpaca and its intent check
  comes first. On an
  adopted long a plain sell with the setting off, one with it on and a reduce-only sell each worked.
  The intent read back from Alpaca is its own inference, `sell_to_close` for all three, and shows
  nothing about what was sent.
* 2026-11-12, 04:45, the same account and part,
  `::test_paper_a_plain_sell_after_the_holding_changed_is_refused_by_its_intent`, 1 passed: the node
  adopts a long, the share is sold at Alpaca outside the node, and the node's cache still says long.
  With `sell_to_close_on_long_exits` the strategy's plain sell is rejected by the same 422; without it
  the sell carries no intent and only the account's long-only setting refuses it ("403 40310000 account
  is not allowed to short").
* The adapter change, `packages/canon-alpaca/tests/test_external_orders.py`,
  `test_execution_reasons.py` and `test_execution_client.py`, 451 cases: with `reduce_only_sells` off
  every reduce-only order is denied before transport with today's reason; on, a reduce-only SELL of
  each of the three order types in scope carries `sell_to_close`, a reduce-only market SELL is denied
  by name, and a BUY is denied; with `sell_to_close_on_long_exits` a market SELL of a cached long
  carries no field and the three carry it; a scope that names the market order turns it on for it; an
  order with neither the flag nor `sell_to_close_on_long_exits` carries no field; a reduce-only market
  SELL carries the field once the scope names the market order, and an empty scope denies every
  reduce-only SELL; a modify that raises the quantity of a resting reduce-only SELL is rejected
  before transport, since the replace carries no intent; the config serialises with its scope; the
  422 reaches the strategy with Alpaca's code and words; an order whose chain cannot be traced or
  reported is named with Alpaca's status and numeric code, or the adapter's own named limit, never the
  response text.
* 2026-11-12, 05:44 to 05:49, account A long-only, pre-market, the two tests of
  `tests/test_paper_sell_to_close.py` through that form with `reduce_only_sells` on, 2 passed: the
  same refusals as at 04:41 and 04:45.
* 2026-11-12, 09:38 to 09:40, account A long-only, regular session, execution-only node,
  `::test_paper_a_regular_session_order_types_carry_sell_to_close`, 1 passed: while flat, a reduce-only
  SELL as a limit, a stop and a stop-limit was each rejected with the same 422; on an adopted long each
  of the three was accepted and seen working, then canceled. The node's cache ended with the long
  still in it, since the share was then sold outside the node; at Alpaca the account was flat:
  `mise run paper:account tests` printed positions 0 and open orders 0 at 09:40:30, and again at
  09:48:51 (the lead's read), `no_shorting: true`. The
  market SELL was denied by the node, `MARKET_PRICE_UNAVAILABLE: order_type=MARKET`, and never reached
  Alpaca. The run at 09:31 had stopped in its own set-up: its opening buy, a limit 5 cents through the
  IEX ask, did not fill in 60 s; in the regular session the set-up now buys by market order.
* 2026-11-12, 10:10 to 10:20, account A long-only, regular session, the adapter as it lands with every
  setting off but those a test turns on: the three tests of `tests/test_paper_sell_to_close.py` and the
  four of `tests/test_paper_protect_seam.py`, with the seam's ten cleaning cases, 17 passed; the same
  refusals and acceptances as above. `mise run paper:account tests` at 10:20:22: positions 0, open
  orders 0, `no_shorting: true` (read between 10:20:22, the run's exit, and 10:20:23).
* 2026-11-12, 11:28 to 11:34, the same account and session, after the fix round, on `main` at `22c892d`:
  the three tests of `tests/test_paper_sell_to_close.py`, 3 passed, the refused plain sell now asserted
  by its reason, "position intent mismatch". `mise run paper:account tests` at 11:34:31: positions 0,
  open orders 0, `no_shorting: true`.
* 2026-11-13, 14:29 and 14:33 New York, account A, regular session, two runs of a one-off probe that
  is not kept in the repository, `no_shorting` turned off inside each `mise run paper:test` and restored
  in a `finally`: the same answers for a TRAILING STOP sell (`DAY`, a 20% trail) as for the limit sells
  of 2026-11-12, Alpaca's answer at the submit. Long 1, 2 shares, `sell_to_close`: "403 40310000
  insufficient qty available for order (requested: 2, available: 1)"; the same with no intent: the same
  403. Flat, 1 share, `sell_to_close`: "422 42210000 position intent mismatch, inferred: sell_to_open,
  specified: sell_to_close". Not sent: a trailing stop with no intent while flat. Before and after each
  run `mise run paper:long-only tests check` printed `no_shorting: true` and `mise run paper:account
  tests` positions 0, open orders 0 (the last reads at 14:29:42 and 14:33:10). One observation each, one
  symbol; the `GTC` form is not shown.

## Evidence

Read 2026-11-12.

* `nautilus-trader@d53849b216e2` `docs/concepts/adapters.md:276-292` — the reduce-only contract, quoted
  above; "An equivalent enforcing instruction can replace the literal venue flag" — resolved.
* Same pin, `docs/concepts/execution/index.md:278-303` — what `REDUCING` admits; "a protective stop must
  set `reduce_only=true`" — resolved. `crates/risk/src/engine/mod.rs:723-775` — a reducing submission
  needs the command's `position_id` and an opposing open position — resolved.
* Same pin, `crates/adapters/binance/src/spot/execution.rs:2268-2270`,
  `coinbase/src/http/client.rs:1594-1597`, `derive/src/execution.rs:1357-1363`,
  `interactive_brokers/src/execution/core.rs:2192` — each refuses a reduce-only order before transport;
  `binance/src/futures/execution.rs:1897-1901`, `:1805-1823` — a position mode read at connect, and the
  venue's rejection of a mismatch — resolved.
* https://docs.alpaca.markets/us/reference/postorder.md — no reduce-only field; `position_intent`, enum
  `buy_to_open`, `buy_to_close`, `sell_to_open`, `sell_to_close`, "Represents the desired position
  strategy." — fetched raw.
* https://github.com/alpacahq/Alpaca-API/issues/250 — 2024-04-25, paper: "position intent mismatch.
  inferred sell to close" — fetched through `gh issue view`.
* https://docs.alpaca.markets/us/reference/getaccountconfig-1.md — `no_shorting`: "If true, account becomes
  long-only mode."; it does not say what is refused — fetched raw.
* https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca — dated Mar 4, 2026; section
  27, code 40310000, first cause "The account has no_shorting set to true or shorting_enabled set to false
  in the account configuration"; section 26, a sell larger than the free shares is refused — fetched as
  HTML, read as text.
* `alpaca-py@c42add7c0a70` `alpaca/trading/requests.py:352-373` — every order request carries
  `position_intent` — resolved. `packages/canon-alpaca/src/canon_alpaca/execution.py:1930` on `main`
  before this change, `_denial`: "reduce-only is not supported" — read.
* ADR 0110 on `main` — gap (i), which names this record — read.
* `nautilus-trader@d53849b216e2` `crates/risk/src/engine/mod.rs:1715-1728`, `deny_no_market_price`, the
  denial `MarketPriceUnavailable` with the order type and instrument; `:2137-2145`, `check_order` calls
  it when no price is found — resolved.
* `sell_to_close` on a market order, and on a live account — UNVERIFIED.
* `/code-review`, 2026-11-12, third read: the modify path sent a larger quantity with no intent; the
  first paper test could pass without the field being sent; the scope kept the config from
  serialising. Fixed above, each with a test that failed first. The evidence-verifier's second
  round: the two strategies' text on the market exit, the pointer to ADR 0115, the 03:23 attribution,
  and what of the Confirmation a later reader cannot re-check. Applied.
* evidence-verifier, 2026-11-12: the connect-time read as a possible silent discard, upstream's refusing
  adapters, the Learn page's date and full cause, the live consequence, a path outside the repository in
  this list, and the pointer to the mismatch message. Applied above; the form moved from option 3 to
  option 2 on the probe, and the lead picked it.
