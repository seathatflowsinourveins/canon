---
status: accepted
date: 2026-11-12
decision-makers: repo owner (requirement, 2026-11-12); lead orchestrator (Claude)
review-by: 2027-02-10
---

# A third paper pair, account C (`protect`), is stored, checked, removed and read on ADR 0069's pattern; nothing trades on it yet

## Context and Problem Statement

The owner opened a third paper account for protection practice (2026-11-12). Its key pair has to be in the
login keychain before any task can use it, and entering it is the owner's step alone (ADR 0029, ADR 0033).
ADR 0069 keeps two accounts apart by which pair each task reads; the same pattern has to hold for three
before the pair is entered.

## Decision Drivers

* No new mechanism: ADR 0029's keychain items, ADR 0033's hidden prompt, ADR 0069's one service per account.
* No two accounts hold one pair.
* The stored pair can be confirmed without anyone reading it: the items' presence, then a GET-only read.
* Nothing that places an order on C is part of this change.

## Considered Options

1. A third keychain service on ADR 0069's pattern, taken by the store, check, remove and account tasks only.
2. Land the pair's tasks together with the standing protector that will use it.
3. Reuse account A's or B's pair for protection practice.

## Decision Outcome

Chosen: **option 1**. Option 2 holds the owner's one manual step behind a build still in review. Option 3
puts a standing node on an account whose tests and windows assume they are alone on it (ADR 0069).

| Part | What it is |
|---|---|
| Service | `canon-alpaca-paper-protect`, the same two item names, label `Alpaca-paper-protect-<item>`. |
| `paper:store protect` | ADR 0033's prompt and ADR 0029's check against Alpaca's paper API. Before either prompt it reads the key ID stored for A and for B (never a secret) and refuses an entered key ID equal to either, before the paper API is asked. `paper:store` and `paper:store windows` refuse C's stored key ID the same way. A key ID that cannot be read to compare stops the task before anything is typed. |
| `curl -q` | The store's one request, which carries the pair on stdin, passes `-q` first, so curl reads no curlrc. That is all it does: see the limit below. |
| `paper:check protect`, `paper:remove protect` | One service each, as for A and B; neither reads a value. |
| `paper:account protect` | GET only (ADR 0070's data-only mode), no lock: balances, positions, open orders with their lineage, `no_shorting`; no account number or order ID. This is how a stored pair is shown to work. |
| Everything else | `paper:test`, `paper:session`, `paper:data`, `paper:history`, `paper:long-only` and `paper:close` do not accept `protect`. The tasks that trade on C come with the standing protector and its account record, neither yet on `main`; ADR 0142 (2026-11-13) admits one before them, `paper:protect protect`. |

The refusal compares key IDs, as ADR 0069's does, so it catches the same pair entered twice and not two
pairs of one account: neither the keychain nor the paper API check says which account a pair reaches
(ADR 0069, UNVERIFIED there and here).

### Consequences

* Good: the owner can enter C's pair now, and a session can confirm it without reading it.
* Good: the store's request no longer depends on the user's curl configuration, for all three accounts.
* Bad: `scripts/bootstrap --check` has rows for A's and B's pairs and none yet for C's; `paper:check
  protect` is the check until it does.
* Limit, for all three accounts: `-q` skips only curlrc. A proxy named in the environment (`HTTPS_PROXY`,
  `ALL_PROXY`) still routes the request, and `curl` is found on PATH, unlike `/usr/bin/security`. The
  store runs in the owner's own terminal with the owner's environment; changing either is a decision for
  the paper and live stores together, not made here.
* Bad: Alpaca staff state three paper accounts per owner, so C is the login's last; a fourth use needs
  another owner.
* UNVERIFIED: that a third market-data connection is allowed for this login beside A's and B's; no task
  here opens one. That C's pair is accepted by the paper API, until the owner has stored it.

### Confirmation

`scripts/test-lanes`, against a stand-in `security` and `curl` on a pseudo-terminal: `paper:store protect`
refuses a key ID stored for A and one stored for B before the paper API is asked, with each other key ID
read once and no secret read; stores under C's service and label when both differ; stops before any
prompt when one cannot be read; `paper:store` and `paper:store windows` refuse C's stored key ID; curl's
first argument is `-q`; `paper:check protect` and `paper:remove protect` touch C's two items only; under
the real mise, `paper:check protect` is admitted by the task's `choices`. Twelve of these failed before
the change. `tests/test_custody.py`: `paper:account protect` reads C's service
only, in data-only mode, and prints neither value (two cases failed before). After the owner stores the
pair: `mise run paper:check protect` and `mise run paper:account protect`, recorded here with the date.

2026-11-12, 10:09:58 New York, from the main checkout at `1c096ad` (the lead's read, after the owner
stored the pair): `mise run paper:check protect` found both items in the login keychain, and
`mise run paper:account protect` was answered by the account.

**Amendment, 2026-11-13 — the standing protector's task is admitted on C.** The "Everything else" row
said the tasks that trade on C come with the standing protector and its account record. `mise run
paper:protector protect`, the standing protector's task (the lead's 24/5 plan of 2026-11-13, Decision D
and unit 10; ADR 0146 for protection around the clock), not yet on `main`, is admitted beside
`paper:account protect` and `paper:protect protect`, on the owner's requirement of 2026-11-13, as the
lead restates it, that C's holding has its exit in every session, and on custody's line that the owner's
fill is recognised at once and protected (`docs/handbook/philosophy.md`, What never changes); its
account record is the evidence its unit lands with. It sells only to protect a long it finds,
reduce-only and for the uncovered quantity only; it never opens or adds to a position, never cancels,
resizes or adopts an order it did not send, and freezes a symbol with a foreign quantity. It takes
`--row OWN1|OWN3` and refuses F2 and IEX by name. On C it runs only after its proof on account A and
only with the lead's go by name (`--lead-go "protect <row>"`), and it and `paper:protect protect` never
send on C at once (one writer per account). These terms are `UNVERIFIED` until the task lands with its
tests. A standing runner (the plan's unit U-g, not yet on `main`) may start it back to back across the
session parts: each start reads the level book, its hold file and its inbox in that step; on C it starts
nothing without the lead's standing go, by name, in a file it reads at every start; it stops at the
first start that finds a hold; the owner's word outranks both. No other task gains C's pair, and no task
reads two pairs.

## Evidence

Read 2026-11-12.

* https://docs.alpaca.markets/us/docs/paper-trading.md — "To create a new paper account, click the paper
  account number in the upper left corner of the dashboard" and select "Open New Paper Account"; "Don't
  forget to generate new API keys for any newly created account". The page does not say how many paper
  accounts one may have — fetched (read through a summarising fetch; the quotes are as it returned them).
* https://forum.alpaca.markets/t/feature-request-more-paper-trading-accounts/18125 — Alpaca staff,
  2025-11-07: "Each owner is currently allowed 3 paper accounts and 1 live account"; "The rate limits for
  Alpaca data plans is enforced per owner and not per account" — fetched (summarising fetch); a forum
  statement, not documentation.
* `man curl` on this machine (`/usr/bin/curl` 8.7.1) — `-q, --disable`: "If used as the first parameter on
  the command line, the curlrc config file is not read or used" — bundled.
* ADR 0069 — one service per account, the key-ID refusal, and "a pair reaches one account" left UNVERIFIED
  — read. ADR 0029 and ADR 0033 — the keychain items, the check against the paper API, the hidden prompt
  — read. ADR 0070 — data-only mode refuses every method but GET — read.
