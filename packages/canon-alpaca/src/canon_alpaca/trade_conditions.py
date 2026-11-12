"""Which Alpaca trade prints become ticks when the data client's `trade_condition_filter` is on.

The rule is the venue's bars rule: a print becomes a tick if and only if Alpaca's own minute bars take its
price. Alpaca's Market Data FAQ, "How are bars aggregated?" (https://docs.alpaca.markets/us/docs/market-data-faq.md,
updatedAt 2026-09-21, read 2026-11-12, sha256 b6d9225409d2211b60cfeed72e8716fc1e05f2d7593ffed6dc120a0b5ebaef13)
gives, per condition code, tape and bar type, whether a print updates the bar's open / close, high / low and
volume, and: "A trade can have more than one condition. In this case the strictest rule is applied." For a
minute bar every row's open / close and high / low agree, so a code here takes the price when both are
green on its minute-bar row; a print takes the price when every one of its codes does.

The codes are those Alpaca's `GET /v2/stocks/meta/conditions/trade?tape=A|B|C` answered on 2026-11-12
(26 for tapes A and B, 33 for tape C). Five of them have no FAQ row and are plan-sourced, by the lead's
ruling of 2026-11-12: tapes A and B `8` from the CTS Pillar Output Specification 2.11b, p.82 (sha256
44609348e4c67ca58d8dd469389e74bc5d399f096dd56cc194816f9108624a72), tape C `1`, `S`, `8` and `E` from the UTP
Data Feed Services Specification 4.1, p.48 (sha256 68659d2f3ab4057adfad52cc20fb1c51c5b69481865d365014cec237a5399401).
Should the FAQ gain a row for one of them, the FAQ row wins.

This differs from the consolidated tapes' last-sale rule on `T` (extended hours trade on A and B, Form T
on C): both plans say it does not update the last sale, while Alpaca's minute bars take its price. It is
kept in every part of the session (the lead, 2026-11-12), since outside the regular session nearly every
print carries it. A code not in this table, a tape other than A, B or C, or a print with no conditions is
unknown: it becomes no tick and is counted by name, and a code the venue's map shows that this table does
not know refuses the client's connect. Corrections and cancels (`T` "c" and "x" on the stream) are not
handled here.
"""

import threading
from collections import Counter
from collections.abc import Callable
from collections.abc import Iterable
from typing import NamedTuple

__all__ = [
    "CONDITIONS_PATH",
    "TABLE",
    "TAPES",
    "Rule",
    "condition_counts",
    "count_print",
    "excluded_by",
    "read_condition_map",
    "unknown_codes",
]

CONDITIONS_PATH = "/stocks/meta/conditions/trade"
"""Alpaca's per-tape map of trade-condition codes, under the data client's `v2`
(https://docs.alpaca.markets/us/reference/stockmetaconditions-1.md)."""


TAPES = ("A", "B", "C")
"""The SIP tapes: A and B under the CTA plan, C under the UTP plan."""


class Rule(NamedTuple):
    takes_price: bool
    source: str


TABLE: dict[str, dict[str, Rule]] = {
    "A": {
        " ": Rule(True, "FAQ line 280: Regular Sale, minute bar"),
        "4": Rule(False, "FAQ line 311: Derivatively Priced, minute bar"),
        "5": Rule(True, "FAQ line 313: Market Center Reopening Trade, minute bar"),
        "6": Rule(True, "FAQ line 314: Market Center Closing Trade, minute bar"),
        "7": Rule(False, "FAQ line 315: Qualified Contingent Trade, minute bar"),
        "8": Rule(False, "plan-sourced, CTS p.82 '8 Reserved': LAST NO, HIGH/LOW NO"),
        "9": Rule(False, "FAQ line 316: Corrected Consolidated Close, minute bar"),
        "B": Rule(False, "FAQ line 283: Average Price Trade, minute bar"),
        "C": Rule(False, "FAQ line 285: Cash Sale, minute bar"),
        "E": Rule(True, "FAQ line 287: Automatic Execution, minute bar"),
        "F": Rule(True, "FAQ line 288: Intermarket Sweep, minute bar"),
        "H": Rule(False, "FAQ line 291: Price Variation Trade, minute bar"),
        "I": Rule(False, "FAQ line 292: Odd Lot Trade, minute bar"),
        "K": Rule(True, "FAQ line 293: Rule 127 or Rule 155, minute bar"),
        "L": Rule(True, "FAQ line 294: Sold Last, minute bar"),
        "M": Rule(False, "FAQ line 295: Market Center Official Close, minute bar"),
        "N": Rule(False, "FAQ line 296: Next Day, minute bar"),
        "O": Rule(True, "FAQ line 297: Market Center Opening Trade, minute bar"),
        "P": Rule(False, "FAQ line 298: Prior Reference Price, minute bar"),
        "Q": Rule(False, "FAQ line 300: Market Center Official Open, minute bar"),
        "R": Rule(False, "FAQ line 301: Seller, minute bar"),
        "T": Rule(True, "FAQ line 302: Extended Hours Trade, minute bar"),
        "U": Rule(False, "FAQ line 304: Extended Trading Hours, minute bar"),
        "V": Rule(False, "FAQ line 305: Contingent Trade, minute bar"),
        "X": Rule(True, "FAQ line 307: Cross Trade, minute bar"),
        "Z": Rule(False, "FAQ line 309: Sold Out Of Sequence, minute bar"),
    },
    "B": {
        " ": Rule(True, "FAQ line 280: Regular Sale, minute bar"),
        "4": Rule(False, "FAQ line 311: Derivatively Priced, minute bar"),
        "5": Rule(True, "FAQ line 313: Market Center Reopening Trade, minute bar"),
        "6": Rule(True, "FAQ line 314: Market Center Closing Trade, minute bar"),
        "7": Rule(False, "FAQ line 315: Qualified Contingent Trade, minute bar"),
        "8": Rule(False, "plan-sourced, CTS p.82 '8 Reserved': LAST NO, HIGH/LOW NO"),
        "9": Rule(False, "FAQ line 316: Corrected Consolidated Close, minute bar"),
        "B": Rule(False, "FAQ line 283: Average Price Trade, minute bar"),
        "C": Rule(False, "FAQ line 285: Cash Sale, minute bar"),
        "E": Rule(True, "FAQ line 287: Automatic Execution, minute bar"),
        "F": Rule(True, "FAQ line 288: Intermarket Sweep, minute bar"),
        "H": Rule(False, "FAQ line 291: Price Variation Trade, minute bar"),
        "I": Rule(False, "FAQ line 292: Odd Lot Trade, minute bar"),
        "K": Rule(True, "FAQ line 293: Rule 127 or Rule 155, minute bar"),
        "L": Rule(True, "FAQ line 294: Sold Last, minute bar"),
        "M": Rule(False, "FAQ line 295: Market Center Official Close, minute bar"),
        "N": Rule(False, "FAQ line 296: Next Day, minute bar"),
        "O": Rule(True, "FAQ line 297: Market Center Opening Trade, minute bar"),
        "P": Rule(False, "FAQ line 298: Prior Reference Price, minute bar"),
        "Q": Rule(False, "FAQ line 300: Market Center Official Open, minute bar"),
        "R": Rule(False, "FAQ line 301: Seller, minute bar"),
        "T": Rule(True, "FAQ line 302: Extended Hours Trade, minute bar"),
        "U": Rule(False, "FAQ line 304: Extended Trading Hours, minute bar"),
        "V": Rule(False, "FAQ line 305: Contingent Trade, minute bar"),
        "X": Rule(True, "FAQ line 307: Cross Trade, minute bar"),
        "Z": Rule(False, "FAQ line 309: Sold Out Of Sequence, minute bar"),
    },
    "C": {
        "1": Rule(
            True, "plan-sourced, UTP p.48 '1 Stopped Stock (Regular Trade)': High/Low Yes, Last Yes"
        ),
        "4": Rule(False, "FAQ line 311: Derivatively Priced, minute bar"),
        "5": Rule(True, "FAQ line 313: Market Center Reopening Trade, minute bar"),
        "6": Rule(True, "FAQ line 314: Market Center Closing Trade, minute bar"),
        "7": Rule(False, "FAQ line 315: Qualified Contingent Trade, minute bar"),
        "8": Rule(False, "plan-sourced, UTP p.48 '8 Placeholder For 611 Exempt': TBD"),
        "9": Rule(False, "FAQ line 316: Corrected Consolidated Close, minute bar"),
        "@": Rule(True, "FAQ line 281: Regular Sale, minute bar"),
        "A": Rule(True, "FAQ line 282: Acquisition, minute bar"),
        "B": Rule(True, "FAQ line 284: Bunched Trade, minute bar"),
        "C": Rule(False, "FAQ line 285: Cash Sale, minute bar"),
        "D": Rule(True, "FAQ line 286: Distribution, minute bar"),
        "E": Rule(False, "plan-sourced, UTP p.48 'E Placeholder': TBD"),
        "F": Rule(True, "FAQ line 288: Intermarket Sweep, minute bar"),
        "G": Rule(False, "FAQ line 289: Bunched Sold Trade, minute bar"),
        "H": Rule(False, "FAQ line 291: Price Variation Trade, minute bar"),
        "I": Rule(False, "FAQ line 292: Odd Lot Trade, minute bar"),
        "K": Rule(True, "FAQ line 293: Rule 127 or Rule 155, minute bar"),
        "L": Rule(True, "FAQ line 294: Sold Last, minute bar"),
        "M": Rule(False, "FAQ line 295: Market Center Official Close, minute bar"),
        "N": Rule(False, "FAQ line 296: Next Day, minute bar"),
        "O": Rule(True, "FAQ line 297: Market Center Opening Trade, minute bar"),
        "P": Rule(False, "FAQ line 298: Prior Reference Price, minute bar"),
        "Q": Rule(False, "FAQ line 300: Market Center Official Open, minute bar"),
        "R": Rule(False, "FAQ line 301: Seller, minute bar"),
        "S": Rule(True, "plan-sourced, UTP p.48 'S Split Trade': High/Low Yes, Last Yes"),
        "T": Rule(True, "FAQ line 302: Extended Hours Trade, minute bar"),
        "U": Rule(False, "FAQ line 304: Extended Trading Hours, minute bar"),
        "V": Rule(False, "FAQ line 305: Contingent Trade, minute bar"),
        "W": Rule(False, "FAQ line 306: Average Price Trade, minute bar"),
        "X": Rule(True, "FAQ line 307: Cross Trade, minute bar"),
        "Y": Rule(True, "FAQ line 308: Yellow Flag Regular Trade, minute bar"),
        "Z": Rule(False, "FAQ line 309: Sold Out Of Sequence, minute bar"),
    },
}
"""Per tape, each code Alpaca's map shows, whether a minute bar takes its price, and the line that says so."""


def excluded_by(tape: str | None, conditions: Iterable[str] | str | None) -> list[str]:
    """The codes that keep a print from becoming a tick: those whose minute-bar row takes no price, and
    any unknown one as `unknown:<code>` (or `unknown:tape <tape>`, `unknown:no conditions`). Empty when
    the print becomes a tick."""
    rules = TABLE.get(tape or "")
    if rules is None:
        return [f"unknown:tape {tape}"]
    codes = [conditions] if isinstance(conditions, str) else list(conditions or ())
    if not codes:
        return ["unknown:no conditions"]
    out: list[str] = []
    for code in codes:
        rule = rules.get(code)
        if rule is None:
            out.append(f"unknown:{code}")
        elif not rule.takes_price:
            out.append(code)
    return out


def unknown_codes(answers: dict[str, dict[str, str]]) -> list[str]:
    """Each `tape code (name)` the venue's map shows that `TABLE` does not know."""
    return [
        f"tape {tape} {code!r} ({name})"
        for tape, mapping in sorted(answers.items())
        for code, name in sorted(mapping.items())
        if code not in TABLE.get(tape, {})
    ]


def read_condition_map(
    get: Callable[[str, dict[str, str]], object], before_each: Callable[[], None] | None = None
) -> dict[str, dict[str, str]]:
    """One GET of Alpaca's trade-condition map per tape, each answer checked to be a non-empty map from
    one-character codes to non-empty names before the next is asked. `before_each` runs before every GET
    (a pacer). Raises `ValueError` naming the tape otherwise."""
    answers: dict[str, dict[str, str]] = {}
    for tape in TAPES:
        if before_each is not None:
            before_each()
        answer = get(CONDITIONS_PATH, {"tape": tape})
        if (
            not isinstance(answer, dict)
            or not answer
            or not all(
                isinstance(k, str) and len(k) == 1 and isinstance(v, str) and v
                for k, v in answer.items()
            )
        ):
            raise ValueError(
                f"tape {tape}: Alpaca's condition map is not a map of one-character codes to names "
                f"({type(answer).__name__}); nothing more was asked"
            )
        answers[tape] = dict(answer)
    return answers


SOURCES = ("stream", "request")
"""Where a judged print came from: the live stream, or a client's trade request (history)."""

_COUNTS: dict[str, Counter[str]] = {source: Counter() for source in SOURCES}
_COUNTS_LOCK = threading.Lock()


def count_print(source: str, excluded: list[str]) -> int:
    """Add one judged print to the process's tally for `source`: `kept` when nothing excluded it (count it
    only once the tick is made), else each excluding code. Returns the new count of `kept` or of the
    first code. Safe from any thread."""
    with _COUNTS_LOCK:
        counts = _COUNTS[source]
        if not excluded:
            counts["kept"] += 1
            return counts["kept"]
        counts.update(excluded)
        return counts[excluded[0]]


def condition_counts() -> dict[str, dict[str, int]]:
    """The prints every client's filter kept (`kept`) and left out, by code, in this process, per source
    with any; a print with several excluding codes counts under each. Empty while no filter has judged a
    print. As `canon_alpaca.providers.endpoint_counts` is for requests, a window's record takes it at its
    end."""
    with _COUNTS_LOCK:
        return {
            source: dict(sorted(counts.items())) for source, counts in _COUNTS.items() if counts
        }
