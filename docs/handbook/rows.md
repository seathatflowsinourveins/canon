# The rows table

One line per threshold or setting: its value, whether it is UNCALIBRATED or calibrated, the source or
study behind it, and the date it was set. "Calibrated" means a study or measurement made here set the
value and the row names it; every other row is UNCALIBRATED, however carefully it was chosen. A row
changes between sessions only, and only on ADR 0083's adoption test: a result above the incumbent and
above holding flat at doubled costs, with a deflated Sharpe ratio of 0.95 or above (ADR 0010); the
requirement's walk-forward wording is not what governs ([philosophy.md](philosophy.md)). This table is not
the registry: "registry" here means `canon/upstreams.json`, the pinned upstreams.

The strategy is to read its settings from these values, and a value missing from `rows/` is unavailable,
never defaulted; that reading is not yet built (roadmap step 7). A row whose source is "the owner's
requirement" rests on the requirement of 2026-11-11, an instruction and not evidence; where ADR 0083
also states it, the row says so, and where it does not, no record holds it yet. `tests/test_handbook.py`
checks each row's form: a unique name, a value, one of the two states, a source and an ISO date. ADR 0087,
0088, 0089, 0091, 0101 and 0106 are `proposed` as this page is written: a row citing one is what is built and
on `main`, and open until the record is accepted.

Each row is one line in a file of its own under `rows/`: `settings/` for the first table and
`window-settings/` for the second. A file is named by its place and its row's name, three digits and a
slug, as `010-dsr-threshold.md`; the places are spaced by ten, so a new row takes any free number
between its neighbours and nothing is renumbered. Adding a row is adding one new file in its table's
folder, and changing a row edits that row's one file; this page holds no row. `tests/test_handbook.py`
checks that each file holds one row line, that no row's name repeats across any two files, and that this
page lists none of them.

| Row | Value | Status | Source or study | Date |
|---|---|---|---|---|

Every row of this table is one file under [settings/](rows/settings/).

The owner protector's `gain_lock` setting is **off** unless `paper:protector --gain-lock` is
explicitly requested (the lead, 2026-11-13): **not a default until its replay proof**. With it off,
the ordinary trail remains in force; with it on, `ROUND_TRIP_COST` and `NOISE_FLOOR` in
`src/canon_trading/gain_lock.py` govern arming and the cost floor (constants there, not yet rows of
this table).

The selectable trail and re-entry rows are a window's settings, not tuned by the bars study.
The bars replay accepts only the fixed trail and the existing clock cooldown; another mode is refused
by its field and value. These window rows are excluded from its trial keys and decode as the fixed
rows. The defaults remain the fixed trail and the 300-second clock cooldown (the lead, 2026-11-12), in
`MoverConfig` and `WindowConfig`; a Mover window's command line that names no trail runs arm OWN1
(ADR 0106, 2026-11-12).

| Row | Value | Status | Source or study | Date |
|---|---|---|---|---|

Every row of this table is one file under [window-settings/](rows/window-settings/).
