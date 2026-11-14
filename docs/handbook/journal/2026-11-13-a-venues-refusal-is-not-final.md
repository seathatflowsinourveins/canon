### 2026-11-13-a-venues-refusal-is-not-final: A venue's refusal is not final, and a freeze from an outside cause ends when the cause ends

* area: exits
* session: 2026-11-13
* result: On main the owner protector takes the refusal of a protective sell that had no sale under way as final: it freezes the holding for the rest of the run with one ALERT, and only the next start re-arms it. A refused sale already under way is sent again once per chase interval, about every 5 s, with no ALERT. Alpaca's wash-trade protection refuses an opposite-side order only while the order it would meet still rests, so where the owner keeps a resting buy in the symbol the first sell is refused and the holding stays unprotected for the rest of a run that can last most of a day. One paper run of four kinds of sale beside a resting opposite order, each refused with Alpaca's wash-trade reason, and engine runs of the resend; the lead withdrew the premise that a refusal is definitive the same night
* evidence: `src/canon_trading/owner_protector.py:1931-1966` and `tests/test_owner_protector_denied.py` at 32389697; Alpaca's user-protection page as ADR 0110 quotes it (its wash-trade protection "also applies to your paper trading account"); the paper run, the engine runs and the lead's log of 2026-11-13, kept outside the repository
* lesson: A refusal says what the venue would not take at that moment, not what it will never take. A freeze whose cause is outside the strategy (a resting order, a cover that cannot be read) ends when that cause ends; only a freeze of the strategy's own making waits for the next start. A resend is paced and told once, never silent
* change: none on main; the protector unit that resends a refused sell at a paced interval with one ALERT per episode, and ends each outside freeze when its cause reads gone, is built and not on main, so its effect is UNVERIFIED here
* next: That unit's engine run and a paper run beside a resting opposite order, then a new entry that closes this one
* status: open
* supersedes: none
* source: lead
* owner: the owner protector's build lane
* closes: the protector unit that ends an outside freeze with its cause, on main with its engine run named in its pull request
