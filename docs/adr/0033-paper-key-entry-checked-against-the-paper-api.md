---
status: accepted
date: 2026-11-10
decision-makers: repo owner (request, 2026-11-10); master orchestrator (Claude)
review-by: 2027-02-08
---

# Entering the Alpaca paper key pair: a hidden prompt, checked against the paper API before it is stored

## Context and Problem Statement

ADR 0029 keeps the Alpaca paper key pair in the login keychain, written once by `mise run paper:store`.
Before anything is stored, how does that task ask for the pair, and how does it tell a paper pair from a
live one?

## Decision Drivers

* A value is never in a file, a command line or a conversation (ADR 0009; AGENTS.md rule 8).
* Paper only at entry, using only what Alpaca documents. The adapter's own refusal stays the control.

## Considered Options

1. `security`'s own prompt (`-w` last), the first design.
2. The task's own hidden prompt, the pair checked against Alpaca's paper API before `security` stores it.

## Decision Outcome

Chosen: **option 2**, in `mise run paper:store` (the task table is in ADR 0029).

1. **Paper only at entry.** Alpaca gives a key form only as a hint: paper keys "often begin with a
   PK... prefix and Live keys with an AK... prefix, these prefixes are visual hints". It also says "you
   cannot use your live account's credentials with the paper API, or vice versa". So `paper:store` refuses
   a key ID starting with `AK`, and stores nothing unless `GET /v2/account` on `paper-api.alpaca.markets`
   answers 200, with the headers on curl's stdin (`-H @-`). A 401 or 403 is refused as not a paper pair;
   any other answer, or none, is "could not check". With no terminal it stops before asking.

A departure from the first design, from the trial:

* **The prompt is the task's, not `security`'s.** With `-w` last, `security` prompts and stores at once,
  before the pair can be checked. So `paper:store` reads both with `read -s`, checks them, then gives each
  to `security add-generic-password ... -w` on stdin, entry and retype. That stdin is read is observed,
  not documented, and a single unmatched line stored an empty value with exit 0. So each item is read
  back and compared, and on any difference both are deleted.

### Consequences

* Bad: `paper:store` needs the network.

### Confirmation

The four task bodies, `paper:store`'s among them, written out with a bash shebang, passed shellcheck 0.11.0
(run once, by hand; ADR 0029).

The trial, 2026-11-10, mise 2026.11.5 and macOS 27.2, with made-up values under a throwaway service:

* `security add-generic-password -U -a ALPACA_API_KEY -s <throwaway> -w` on a pty, typed by `expect`:
  "password data for new item:", "retype password for new item:", nothing echoed, exit 0. A `-w` read
  returned at once, no dialog: length 20, sha256 `096b0bf83314`, the made-up value's.
* The four tasks, only the service name changed. `paper:store`, a made-up pair: "Alpaca's paper API
  answered 401 ... Nothing stored", exit 1. With two items placed, `paper:store` refused, "already in the
  login keychain". After review: `paper:store` with stdin not a terminal said "needs a terminal" and
  exited 1; a made-up key ID starting `AK` was refused before any request ("starts with AK"), exit 1.
* UNVERIFIED: `paper:store` with a real paper pair (none was used); the code Alpaca's paper API gives a
  live pair.

## Evidence

Read 2026-11-10.

* Split from ADR 0029 on 2026-11-10, with no change of decision; `review-by` is ADR 0029's. The lines
  below are ADR 0029's, unchanged; the `man security` and review lines support both records and are kept
  in each; the review line covers the record before the split, so "All fixed above" means the two
  together.
* `man security` on this machine — `-w`: "Put at end of command to be prompted (recommended)"; `-U`;
  `-T`; "By default, the application which creates an item is trusted to access its data without
  warning. You can remove this default access by explicitly specifying an empty app pathname: -T """;
  `find-generic-password` `-g` and `-w` display the password; `delete-generic-password`. It does not say
  that `-w` reads stdin (read).
* https://docs.alpaca.markets/us/docs/authentication.md — paper accounts use `paper-api.alpaca.markets`;
  the live-credentials sentence; the two headers. https://docs.alpaca.markets/us/docs/paper-trading.md —
  "a different API key from your live account". https://docs.alpaca.markets/us/reference/getaccount-1.md
  — `GET /v2/account`, servers "Paper" and "Live" (fetched raw).
* Alpaca's docs, alpaca-py 0.45.0 (`common/rest.py`, `trading/client.py`) and `alpacahq/cli` at
  `4f83692cd24e`, read by a research subagent and summarised: no key-form rule there; `PK...` as an
  example in the CLI's README and https://docs.alpaca.markets/us/docs/alpacas-cli.md.
* https://alpaca.markets/learn/api-key-security-best-practices-for-alpaca-builders — the PK/AK "visual
  hints" sentence, and "The authoritative security boundary is the host environment itself" (found by the
  second reader; fetched, sentence read verbatim; the page gives no date).
* `man curl` (8.7.1) — "Using @- makes curl read the header file from stdin" (read). The trial (run).
* Reviews, 2026-11-10. `/code-review` (high): nine findings; five fixed (an empty item passing
  `paper:test`, no terminal, a network failure blamed on the pair, the service name unchecked, the README
  overstating who can read the item), the two permission rules recorded as open, two left as they are.
  Second reader (`evidence-verifier`): every quote held and it re-ran the trials; it corrected "only
  `paper:test` reads the items", found the PK/AK hint (now an entry refusal) and had "Always Allow"
  labelled. All fixed above.
* Second reader (`evidence-verifier`) of the split, 2026-11-10: nothing missing or changed in meaning, no
  citation wrong; it corrected the shellcheck line and the review line's scope.
