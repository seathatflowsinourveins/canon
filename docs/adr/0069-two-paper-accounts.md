---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2027-02-08
---

# Two Alpaca paper accounts: A for the paper tests, B for the session windows

## Context and Problem Statement

One paper account serves `paper:test` and `paper:session` under one lock (ADR 0029, ADR 0044). A test that
leaves an order or a share there in the window's symbol makes the next window refuse to start, and a test can
lose the lock to the next window. ADR 0044 named the remedy, a second paper account for session runs, as the
owner's to create, and the owner has created it. How is the second pair kept, and what keeps each account to
its use?

## Decision Drivers

* Each pair keeps every property ADR 0029 and ADR 0033 give the first: it is in no file, command line or
  output, and it is checked against the paper API before it is stored.
* A pair reaches one account: read from Alpaca's new keys "for any newly created account"; no page read
  states it (UNVERIFIED). So the tasks keep each account to its use by which pair each one reads, not by
  convention.
* Every existing use is unchanged.

## Considered Options

1. A copy of each of the three keychain tasks per account (`paper-windows:store` and so on).
2. The three tasks take the account as an argument, from a fixed list, with account A as the default.
3. As option 2, and `paper:store` also compares the account `GET /v2/account` names for the pair entered
   with the other account's (its `id` and `account_number`).

## Decision Outcome

Chosen: **option 2**. Each task has one body, so the two accounts cannot drift apart between copies.
Option 3 would also catch two pairs of one account, but it needs the other account's id kept somewhere or
its secret read to ask the API; it is left for when the reading side is built.

| Account | Name | Keychain service | For | Never for |
|---|---|---|---|---|
| A | `tests`, the default | `canon-alpaca-paper` | `paper:test` | a session window |
| B | `windows` | `canon-alpaca-paper-windows` | `paper:session` and its schedule (ADR 0050) | a paper test |

Each service holds ADR 0029's two items, accounts `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. The variable
names, the adapter and `canon_alpaca` stay as they are. B's items are labelled `Alpaca-paper-windows-<item>`.

1. **`mise run paper:store|paper:check|paper:remove [tests|windows]`.** Each task's mise `usage` spec takes
   one optional argument, default `tests`, with the choices `tests` and `windows`. mise refuses any other
   name, or a second word, before the script starts, and each script refuses a name outside the two again.
   `paper:store windows` keeps all of ADR 0033: it needs a terminal, turns echo off before its hidden
   prompts, refuses an `AK` key ID, stores only on a 200 from the paper API, passes values on stdin only,
   reads both back, and removes both on any failure.
2. **No pair in both accounts.** `paper:store` refuses a key ID equal to the one stored for the other
   account, before it asks the paper API. Two equal pairs have equal key IDs, so this catches the same pair
   entered for both accounts without reading the other account's secret. It also refuses a pair that
   shares only the key ID, which is not the same pair; whether Alpaca ever issues one is UNVERIFIED.
   `security find-generic-password -w` writes the stored key ID to a pipe into the task's shell, and bash's
   `[`, a builtin, compares the two, so neither is in any process's arguments, printed or written, as in
   ADR 0033's read-back. The read comes before the prompts, bounded at 30 s; any status but 44 (absent)
   stops the task. The refusal holds both ways, whichever pair was entered first.
3. **Locks, decided now, built when a task reads account B.** Each account gets its own lock:
   `canon-paper-test.lock` for A, as today, and `canon-paper-windows.lock` for B, so a test and a window
   no longer wait for each other. Each Alpaca login gets one lock for the market-data stream,
   `canon-alpaca-stream.lock`, held by any run that opens a stream. The trading API's request limit is per
   account, "200 requests per minute, per account" (a support page that does not name paper accounts, so
   for paper UNVERIFIED). The stream limit belongs to the user: "in many subscriptions (or without one)
   this limit is 1". So two paper accounts of one login share one stream (read from "from a user"; no
   page states it for a second paper account, so this is UNVERIFIED).
4. **What this change builds:** storing, checking and removing B's pair, and an optional
   `paper key pair (windows)` row in `scripts/bootstrap --check` that shows present or absent and never a
   value. **What it does not build yet:** no task puts account B's pair in an environment; the only read
   of it is `paper:store`'s comparison of B's key ID when A's pair is stored. `paper:test` and
   `paper:session` both still read A under one lock. A later change moves `paper:session` and its
   schedule to B and adds the locks in clause 3; until then account B sits unused.

This amends ADR 0029, whose tasks act on one service, and, once a window reads B, ADR 0044's "Sharing the
account" and ADR 0050's schedule. Each of the three points here.

### Consequences

* Good: with no argument, the three tasks act on account A as before. `paper:check` and `paper:remove`
  print one more line, naming the account and service, and `paper:store` makes one more read, B's key
  ID, which can refuse.
* Good: once `paper:session` reads B, a failed test cannot stop a window, and each account has its own
  request limit (UNVERIFIED for paper, clause 3).
* Bad: the refusal catches the same pair entered for both accounts. It does not catch two different pairs
  of one account: neither the keychain nor the paper API check here says which account a pair reaches.
* Bad: two runs that each open a stream still contend for the login's one stream, which the stream lock
  orders.

### Confirmation

`scripts/test-lanes`, each task's body from `mise.toml` with a `security` stand-in: `windows` uses B's
service only, no argument or `tests` A's only, any other name is refused before `security` is called.
`paper:store windows` refuses A's key ID before the paper API, reads A's key ID once and A's secret never,
stops before any prompt when A's key ID cannot be read, and otherwise stores B's two items under B's label
with no value in a `security` argument; A's store refuses B's key ID. The three tasks, copied by name, run
under the real mise, which picks the service from the argument and refuses `live` with no `security` call.
On a pseudo-terminal, with the real `security` and throwaway services, `paper:store windows` refuses A's
pair and stores another, which reads back equal under B's label, A's items unchanged. Against the tasks as
they were, 20 cases fail (run): every case for `windows`, a refused name and the same-pair refusal.

The trial, 2026-11-10, mise 2026.11.5, a throwaway folder: the `usage` spec above gave `tests` with no
argument and `windows` with one. `bogus` failed with "Invalid choice", a second word with "unexpected word",
an inherited `usage_account` was cleared, and no argument was appended to the script. `type [` in bash
3.2.57 (`/bin/bash`, the bash the tasks get): "[ is a shell builtin". `security` showed an item's label as
attribute `0x00000007` (macOS 27.2).

The owner's step: `mise run paper:store windows`, then `mise run paper:check windows`.

## Evidence

Read 2026-11-10.

* https://docs.alpaca.markets/us/docs/paper-trading.md — "create and delete paper accounts, rather than
  resetting them"; "Open New Paper Account"; "generate new API keys for any newly created account"
  (fetched).
* https://alpaca.markets/support/usage-limit-api-calls — "200 requests per minute, per account" (fetched).
* https://docs.alpaca.markets/us/docs/streaming-market-data.md — "The number of connections to a single
  endpoint from a user is limited based on the user's subscription, but in many subscriptions (or without
  one) this limit is 1"; "only 1 active stream connection"; `connection limit exceeded` (fetched).
* https://raw.githubusercontent.com/jdx/mise/v2026.11.5/docs/tasks/task-arguments.md — the `usage` field,
  `choices`, "A missing required argument or a value outside the choices fails before the script starts",
  `usage_<name>` variables, "mise clears `usage_*` variables inherited from the calling environment",
  "Without a spec, mise forwards extra arguments" (fetched at the tag `scripts/bootstrap` pins, the mise
  that ran the trial; the trial, run).
* `man security` and ADR 0033 — `find-generic-password` `-w` displays only the password; `-i` and `-X`
  as ADR 0033 uses them (read).
* https://docs.alpaca.markets/us/reference/getaccount-1.md — `GET /v2/account` returns the account's `id`
  and `account_number` (fetched by the second reader; option 3).
* ADR 0029, ADR 0033, ADR 0044 and ADR 0050, for the tasks, the lock and the schedule this builds on.
* Reviews, 2026-11-10. `/code-review` (high): nine findings, seven fixed (the real-mise cases' environment
  and copy; the other key ID read before the prompts, bounded; the help; `paper:check`'s exit 3 for a bad
  name; the key-ID claim), two left: the README's paper section, for the change that moves
  `paper:session` to B, and one map per body, each checked by a `windows` case. Second reader
  (`evidence-verifier`): it corrected "no task reads account B", "as before", the request limit, a pair
  reaching one account, the window's symbol and the mise source line, and found option 3. All fixed above.
