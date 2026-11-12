---
status: proposed
date: 2026-11-12
decision-makers: repo owner (requirement, 2026-11-12); lead orchestrator (Claude) for the mechanism
review-by: 2026-12-12
---

# The live lane's mechanism, nine changes; open: the proof of the strategies' own entry switch, the hand-over between a stop Alpaca holds and the emulated one, the live node's cache database and the per-name trail

## Context and Problem Statement

ADR 0103 lets one live lane trade the owner's live account, through `mise run live:*` tasks only, and
nothing of that exists yet. What must be built before anything trades live, and which parts have no source
or no proof yet?

## Decision Drivers

* ADR 0103's terms: the live pair reaches only the live tasks; every other session stays on paper.
* A protection that dies with the node is not enough on its own (ADR 0103, Consequences).
* Stopping new exposure must never stop an exit. Upstream's mechanism comes before one of our own (rule 3).

## Considered Options

1. Build the nine changes below, each on its own branch, proven on a third paper account first.
2. Build the live path as one change.

## Decision Outcome

Chosen: **option 1**, `proposed` until the four gaps below close. Option 2 puts the whole order path in one
review. Each change below is its own branch, with a failing test first and `/code-review` at high effort.
The order path is large enough for `/code-review ultra`, which only the owner starts. Nothing trades live
until this record is accepted and its live order tasks are on `main` (ADR 0103, point 3).

* **(a)** `live:store`, `live:check`, `live:remove`, run by the owner in their own terminal, in the manner of
  `paper:store` (ADR 0029, ADR 0033, ADR 0069), in a keychain service of their own.
* **(b)** A live client configuration in the adapter, a class separate from the paper one. Today
  `AlpacaExecutionClientConfig` raises `PaperOnlyError` on `paper` other than `True`, on any
  `url_override`, and on any other keyword naming a live host from alpaca-py's `BaseURL`
  (`execution.py:345-352`, `:458-502`). `_connect_venue` (`:1222`), which `_connect` (`:1219`) calls, builds
  `TradingClient` and `TradingStream` with `paper=True` whatever the config holds (`:1239`, `:1241`). The
  paper class keeps all of that. These describe the paper class and move with this change:
  `execution.py:343` (`PAPER_ONLY_RULE`, citing rule 8 as it stood), `test_execution_client.py:3161`,
  `packages/canon-alpaca/README.md:99`, `pyproject.toml:8`, `README.md:231` and `:260`, and the label at
  `docs/architecture.md:15`. Reduce-only stays refused before transport (gap (i)).
* **(c)** Live custody, as `paper:account` and `paper:close` are for paper; the protector as a standing
  node; and the order-on-request task, which places what the owner asks and protects it from its fill.
  All are proven first on a third paper account, with the hand-over of gap (ii).
  Any buy is sized on fresh cash: after a fill the account is asked for, one query at a time; a buy counts
  against free cash until a report taken after a query sent after its fill, a sale's proceeds count only
  once a report has them, and a working entry holds back its unfilled quantity times its price. A sale
  never waits on that query.
* **(d)** The dead-node guard: something outside the live node that closes the position when the node dies.
  Nothing in the repository has that name yet.
* **(e)** An arming switch, a tracked file on `main` in the manner of `canon/paper-schedule`, off by default.
  The absence of the stored pair is the first switch. Off makes every strategy submit no buy
  (gap (i)).
* **(f)** The live request budget, read from an untracked local settings file.
* **(g)** The live account's own account lock and stream lock.
* **(h)** The live lane's skills, written here, each wrapping a `live:*` task.
* **(i)** The ranked list synced to a watchlist on the live account, where the owner places orders: a
  `live:*` task that reads and writes watchlists only, never an order, and never a symbol it did not put
  there. Proven first on a paper account's watchlist.

**Gaps, and what would close each.**

* **(i) Stopping entries while exits keep working.** The switch is the strategies' own (ADR 0111, `proposed`, not on `main` yet): Mover
  and the protector read the arming file at each decision and submit no buy while it says off, re-entries
  included; sells are untouched. Closed by a paper proof that with the switch off a qualifying entry is not
  submitted and is recorded as `entries_off`, and a due protective sell goes out. Upstream's `REDUCING`
  state, which permits "Eligible individual reduce-only submissions, cancels, and queries" and requires
  that "a protective stop must set `reduce_only=true`", is not the switch here: the adapter refuses a
  reduce-only order before transport (`execution.py:1893-1894`), as upstream's adapters for venues that
  cannot short do. It may be added later as a second layer: on paper Alpaca enforced
  `position_intent=sell_to_close` on the order itself, refusing a sell that would open a short, with no
  account setting needed (ADR 0111); proven through the adapter on paper first, UNVERIFIED on live. `HALTED` does not work: it "denies released orders,
  including protective stops". Alpaca's `suspend_trade` does not either, read literally: "If true, new
  orders are blocked", and a released exit reaches Alpaca as a new order.
* **(ii) A stop Alpaca holds, beside the emulated one.** Alpaca says that with an open position "you would
  not be able to submit two conditional closing orders since Alpaca’s system would view one of the two
  orders as exceeding the available position quantity". So a stop held by Alpaca for the full quantity and
  the emulator's released sell cannot both work: in the regular session it is one or the other, with a
  hand-over at the session's edges. Closed by measuring the unprotected gap of that hand-over on the third
  paper account.
* **(iii) Whether the live node configures a cache database**, which decides whether emulated orders come
  back after a restart. Closed by the node's configuration and a paper restart.
* **(iv) The per-name trail distance.** No record on `main` registers one; ADR 0088, `proposed`, has one
  fixed `trail` of 0.02 for every name. Closed by a record that registers it before any result.

### Consequences

* Good: each change is small enough to review, and the paper account proves each before live.
* Bad: until (i) and (ii) close, live protection outside a running node rests on the dead-node guard alone.

### Confirmation

Each change adds a line here when it lands: its tests, its `/code-review`, and its paper run on the third
account. Today none is on `main`: no `live:*` task in `mise.toml` (read).

## Evidence

Read 2026-11-12.

* https://nautilustrader.io/docs/md/latest/concepts/execution/index.md — "Trading state": the `REDUCING`
  row and the `HALTED` sentence quoted above. Fetched raw. The same text is at the pin, `v2.0.0rc8`
  (`d53849b216e2`), `docs/concepts/execution/index.md:276-298`, in `scripts/canon src nautilus-trader`'s
  copy. Fetched at the pin.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md — the two conditional closing orders sentence, in
  its bracket orders section; no reduce-only parameter found on the page. Fetched raw.
* https://docs.alpaca.markets/us/reference/getaccountconfig-1.md — `suspend_trade`: "If true, new orders are
  blocked." Fetched raw. Whether it blocks a closing order is not stated there.
* https://nautilustrader.io/docs/md/latest/concepts/orders/emulated.md — an emulated order is held by the
  local `OrderEmulator`; it is reactivated at startup only from "the configured cache database". Fetched raw.
* `packages/canon-alpaca/src/canon_alpaca/execution.py` at `9a1eef5`, the lines cited in (b) and gap (i);
  the other files listed in (b). Read.
* ADR 0088 (`status: proposed`, `trail` 0.02 of the entry price), ADR 0029, 0033, 0050, 0069 and 0103. Read.
* `git ls-files` at `9a1eef5` — no file naming a dead-node guard. Run.
* evidence-verifier, 2026-11-12, second pass: gap (i) recast on upstream's `REDUCING`, gap (ii) on Alpaca's
  closing-order sentence, and one gate stated one way. Applied above.
* ADR 0111 (`proposed`, read on the protection lane's branch at `6856c67` with its working changes) and its
  evidence-verifier pass, 2026-11-12: the entry switch is the arming file; a reduce-only sell goes as
  `position_intent=sell_to_close`, refused on paper when it would open a short; main refuses every
  reduce-only order until that lands. Gap (i) rewritten on it.
