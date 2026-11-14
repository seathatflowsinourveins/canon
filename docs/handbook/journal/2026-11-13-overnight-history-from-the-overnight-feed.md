### 2026-11-13-overnight-history-from-the-overnight-feed: Overnight history comes from the overnight feed, at least 15 minutes old

* area: data
* session: 2026-11-13
* result: The history loader could not load the overnight session, so nothing recorded covered 20:00 to 04:00 New York. Loaded GET only from the overnight feed, `boats`, one overnight session's quotes, trades and minute bars came back with no 429 and no refusal; for one name's busiest overnight hour the overnight feed held thousands of trades and quotes and the consolidated feed's history held none. One session, six loads (two each of quotes, trades and bars)
* evidence: pull request 449 (its Test Plan and Evidence), `src/canon_trading/history_load.py:150-156` and `src/canon_trading/mover.py:549-555` at 32389697; the loads' output, kept outside the repository
* lesson: The consolidated feed's history is no record of the overnight session; overnight history is asked of the overnight feed, with an end at least 15 minutes old, and a feed it refuses stops the load by name rather than falling back
* change: the loader takes `--feed boats` for bars, trades and quotes into roots of their own, bounded to 8 symbols and 8 hours ending at least 15 minutes before now, and refuses `--feed overnight` by name (pull request 449)
* next: none
* status: adopted
* supersedes: none
* source: lead
* owner: none
* closes: none
