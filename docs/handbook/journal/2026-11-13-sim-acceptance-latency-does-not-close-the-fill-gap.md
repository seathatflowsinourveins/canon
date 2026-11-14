### 2026-11-13-sim-acceptance-latency-does-not-close-the-fill-gap: A latency model set at the venue's acceptance time leaves the paper fill gap where it was

* area: execution
* session: 2026-11-13
* result: A mechanism run, ranking nothing (2026-11-13 is a forward date). One regular-session Mover window, arm OWN1, replayed twice on NautilusTrader's engine on recorded SIP data: once at zero latency, once with the engine's static latency model at the paper venue's measured times from submission to acceptance (median 27 ms over 239 orders) and to cancellation (median about 42 ms over 14), both medians from the paper Mover logs. Both runs ended flat with 0 handler errors (in 66,025 and 66,038 log lines) and matched 5 entries, with 2 only on paper and 2 only on the engine. With the latency model the engine's median entry fill lag rose from 0.90 to 1.03 s against paper's 1.92 s, and its median exit from trigger to fill stayed at 0.62 s against paper's 2.33 s; the same logs put paper's median from submission to first fill at 483 ms. A third run, a sensitivity observation and not a profile, set the submission latency at 1.203 s: the entry fill lag overshot to 8.86 s and the exit's trigger to fill stayed at 0.62 s
* evidence: the run farm's runs a1-v1, a1-v2a and a1-v2b (the third's medians read here from its own parity table), their parity set and its latency measurement, kept outside the repository; `StaticLatencyModel` in crates/execution/src/models/latency.rs:217 at the nautilus-trader pin d53849b2
* lesson: Paper's delay is in the fill, not in the order's acceptance: a latency set at acceptance moves entries a little and exits not at all, and neither submission latency tried, one of them past a second, changed the exit's time from release to fill (0.62 s), for a reason not yet read at the pin; so the profile's next switch models when paper fills, from submission to fill, and the exit is where the gap is widest
* change: none; the farm runs the second profile (v1 with the latency model) as its measured candidate
* next: A profile switch that delays fills by the measured time from submission to fill, run on the same window, with exit trigger to fill counted against paper's
* status: open
* supersedes: none
* source: lead
* owner: the run farm lane
* closes: the farm's run of this window under a profile that delays fills by paper's measured time from submission to fill, with the exit's trigger-to-fill set beside paper's
