---
status: proposed
date: 2026-11-14
decision-makers: lead orchestrator (Claude) for ADR 0110's change (f); the live lane (Claude) for the reader
review-by: 2027-02-12
---

# The live request budget is read from a local settings file outside the repository, the paper figure when it is absent (ADR 0110, change (f)); open: no node reads it yet

## Context and Problem Statement

ADR 0110's change (f) asks for "the live request budget, read from an untracked local settings file", and
ADR 0103's point 6 keeps everything about the owner's account out of tracked files. Paper work records its
requests against one planning figure in `request_budget.py` (ADR 0083), and a paper node throttles at a
ceiling kept under it (`paper_node.REQUEST_CEILING`). Where does a live node get its figure, and what does
it do when the file is missing or wrong?

## Decision Drivers

* No figure for the live account is in a tracked file, a commit or a pull request, nor in a message or
  an error the reader makes.
* Absent means the paper figure, said so: never a guess at the live one.
* A file that is there and wrong is an error, never read as absent.

## Considered Options

1. An environment variable set by each `live:*` task.
2. A file under the checkout, ignored by git.
3. A file under the config folder, outside every checkout.

## Decision Outcome

Chosen: **option 3**, `proposed` until the gap below closes. Option 1 puts the figure in a task's text or
in a shell's history. Option 2 keeps it one `git add -f` from a commit, and a new worktree would lack it.

1. **The file** is `canon/live/settings.toml` under the config folder (`CLAUDE_CONFIG_DIR`, else
   `~/.claude`), where `paper:session` keeps its window records. It is TOML, read with Python's `tomllib`.
   The owner or the lead writes it by hand; nothing in the repository writes it.
2. **One key:** `trading_requests_per_minute`, a positive whole number: the Trading API's request limit a
   minute for the live account. What Alpaca's pages state about that limit is under Evidence; the figure
   of any one account is `UNVERIFIED` here, and is the owner's to take from Alpaca.
3. **`canon_trading.live_budget.read_request_budget()`** returns the figure and where it came from:
   * no file, or a file that sets nothing at all: the paper figure
     (`request_budget.TRADING_REQUEST_LIMIT_PER_MINUTE`), with a source that says which case it was;
   * a file that cannot be read or parsed, is or is under a link whose target is missing, is not TOML,
     sets any name but the key at its top level (a table, a misspelt key, the key in other case), or sets
     the key to anything but a positive whole number: an error by name, never read as absent. Its message
     repeats nothing of the file, a name it set included; it carries no cause and no context, since the
     parser's and the decoder's own errors hold the file's text; and no frame of the reader in its
     traceback holds what the file held. A wrong value or name may be a slip of the real figure;
   * a path that resolves to a place in the checkout the module runs from, or under a folder holding a
     `.git` entry that is there: refused, whether or not the file is. Three clauses: the checkout by name,
     the checkout by the identity of the folders above the path, and a `.git` entry in one of them. The
     last two are `arm_comparison._inside_checkout`'s; where this checkout cannot be compared, that one
     raises and this one goes on to the `.git` test.
4. **The figure is kept out of** `repr`, `str` and every message of the module; `dataclasses.asdict` and
   `astuple` give it, as the field does. A caller that records it does so outside the repository (ADR
   0103, point 6); the node change says where, if anywhere, a run prints it, since `session_window`
   prints its paper limit in its report today.

**The gap, and what would close it.** No node reads the budget. Closed when a live node's configuration
takes its request ceiling from it, keeping a reserve under the figure as `paper_node.REQUEST_CEILING` does
under the paper one, with a test that an absent file gives the paper node's ceiling there. ADR 0110's
change (b) is the adapter's client configuration; the ceiling is the node configuration's. From then the
reader bears on the order path and belongs on `scripts/land`'s `TRADING_PATHS`.

### Consequences

* Good: a missing file is visible: the answer's source names which case it was.
* Bad: the file is one per config folder, not one per machine, and the repository does not back it up. A
  session under another config folder, or a second machine, reads the paper figure until someone writes
  the file there; no message names the folder that was read.
* Bad: a config folder kept under git is refused by name. A work tree with no `.git` entry of its own,
  one named by `GIT_DIR` and `GIT_WORK_TREE` or by `core.worktree`, is not recognised: the figure is read
  from it, though git could commit it from there.
* Bad: whether the paper figure is at, under or over the live account's enforced limit is `UNVERIFIED`
  (Evidence). The reader returns a limit, not a ceiling: the node keeps its own reserve under it.
* Bad: the figure has no upper bound: no page read gives one, so a larger slip is taken as given.
* Bad: importing the reader imports `request_budget`, whose observation clock starts at its import.

### Confirmation

`tests/test_live_budget.py`, 39 cases with made-up figures in a temporary folder: each case above, the
config folder's path with and without `CLAUDE_CONFIG_DIR`, and that the figure is in no `repr` or message.
One of them, another spelling of the checkout's path, is skipped on a volume that tells upper case from
lower. With the module absent the file failed at collection; eight test functions of the review's round
failed before its fix (of the 37 cases then, 21 failed and 16 passed, 2026-11-14), and each of the
checkout rule's three clauses has a case that fails with the clause removed. No test reads this machine's
config folder. No paper run applies: the module reaches no account.

## Evidence

Read 2026-11-14.

* https://docs.python.org/3/library/tomllib.html — "Added in version 3.11"; `tomllib.load`: "The first
  argument should be a readable and binary file object"; "A `TOMLDecodeError` will be raised on an invalid
  TOML document". Fetched.
* https://alpaca.markets/support/usage-limit-api-calls — dated December 2022: "Please note that the API is
  throttled, currently 200 requests per minute, per account." and "Currently we do not support users
  increasing the limit by users themselves." It does not say whether paper and live differ. Fetched.
* https://alpaca.markets/support/increase-api-rate-limit — dated December 2022: "Alpaca is willing to
  increase your rate limit to 1,000/calls a minute", for an account made non-retail. It names neither the
  Trading API nor a key or an account as the limit's scope. Fetched.
* https://raw.githubusercontent.com/alpacahq/alpaca-docs/master/content/api-references/trading-api/_index.md
  — archived, undated: "The rate limit is 200 requests every minute per API key." Fetched. It disagrees
  with the support page on the scope: per key here, per account there.
* https://docs.alpaca.markets/llms.txt — the part read lists a Rate Limits page under the Broker API and
  none under the Trading API. Fetched, truncated: the first 39,954 of 172,984 characters word for word.
* https://docs.alpaca.markets/us/docs/broker-api-rate-limits.md — "Alpaca does not publish fixed Broker
  API rate-limit numbers."; the Broker API only. Fetched.
* `UNVERIFIED`: any live account's own limit; whether paper and live accounts are limited apart; whether
  the limit today is per key or per account. No current Alpaca page read states a Trading API limit.
* `src/canon_trading/request_budget.py` on `main` — `TRADING_REQUEST_LIMIT_PER_MINUTE`, the paper figure,
  "a documented planning budget" (ADR 0083); its header marks live enforcement `UNVERIFIED`. Read.
* `src/canon_trading/paper_node.py` on `main` — `REQUEST_CEILING`, "a working reserve under Alpaca's 200 a
  minute". Read.
* `src/canon_trading/arm_comparison.py` on `main` — `_inside_checkout`: "in this checkout or in any other
  git checkout, however it is spelled", by `os.path.samefile` or a `.git` entry. Read.
* `src/canon_trading/session_window.py` on `main` — the window's report carries
  `request_record(limit_per_minute=...)` and is printed as JSON at the end of a run. Read.
* `mise.toml` on `main`, `paper:session` — the window records under
  `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/canon/watch/windows`. Read.
* ADR 0103, point 6 — nothing about the owner's account enters a tracked file, a commit or a pull
  request. ADR 0110, change (f) — the words quoted in Context. Read.
* 2026-11-14: second readers (the `evidence-verifier`, twice, and `/code-review` at high and at max
  effort) corrected this record and the reader before landing: an unknown name and a link without its
  target were read as absent and are now refused; the parser's error stayed on the error's context and no
  longer does; another spelling of the checkout's path, and another checkout, were let through; the limit
  was written as the ceiling; the Alpaca pages above were not cited; what the checkout rule does not
  recognise was first worded wrongly.
