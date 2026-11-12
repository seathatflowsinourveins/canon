"""`canon_alpaca.trade_conditions`: which prints become ticks under the venue's bars rule, offline."""

import pytest
from canon_alpaca.trade_conditions import CONDITIONS_PATH
from canon_alpaca.trade_conditions import TABLE
from canon_alpaca.trade_conditions import TAPES
from canon_alpaca.trade_conditions import condition_counts
from canon_alpaca.trade_conditions import count_print
from canon_alpaca.trade_conditions import excluded_by
from canon_alpaca.trade_conditions import read_condition_map
from canon_alpaca.trade_conditions import unknown_codes

# The codes Alpaca's GET /v2/stocks/meta/conditions/trade answered for each tape on 2026-11-12 (keys only).
MAP_CODES = {
    "A": set(" 456789BCEFHIKLMNOPQRTUVXZ"),
    "B": set(" 456789BCEFHIKLMNOPQRTUVXZ"),
    "C": set("1456789@ABCDEFGHIKLMNOPQRSTUVWXYZ"),
}
PLAN_SOURCED = {("A", "8"), ("B", "8"), ("C", "1"), ("C", "S"), ("C", "8"), ("C", "E")}


def test_the_table_knows_exactly_the_codes_the_venues_map_showed() -> None:
    assert {tape: set(rules) for tape, rules in TABLE.items()} == MAP_CODES


def test_every_rule_cites_its_faq_line_or_its_plan_row() -> None:
    for tape, rules in TABLE.items():
        for code, rule in rules.items():
            if (tape, code) in PLAN_SOURCED:
                assert rule.source.startswith("plan-sourced, "), (tape, code)
            else:
                assert rule.source.startswith("FAQ line "), (tape, code)
                assert rule.source.endswith(", minute bar"), (tape, code)


@pytest.mark.parametrize(("tape", "codes"), [("A", [" "]), ("B", [" "]), ("C", ["@"])])
def test_a_regular_print_takes_the_price(tape: str, codes: list[str]) -> None:
    assert excluded_by(tape, codes) == []


@pytest.mark.parametrize(
    ("tape", "codes", "excluded"),
    [
        ("C", ["@", "I"], ["I"]),  # odd lot
        ("A", [" ", "I"], ["I"]),
        ("A", ["B"], ["B"]),  # average price on A and B
        ("C", ["@", "W"], ["W"]),  # average price on C
        ("C", ["@", "Z"], ["Z"]),  # sold out of sequence
        ("B", ["Z"], ["Z"]),
        ("C", ["@", "4", "I"], ["4", "I"]),  # every excluding code is named
    ],
)
def test_odd_lot_average_price_and_out_of_sequence_prints_take_no_price(
    tape: str, codes: list[str], excluded: list[str]
) -> None:
    assert excluded_by(tape, codes) == excluded


@pytest.mark.parametrize(("tape", "codes"), [("A", ["T"]), ("B", [" ", "T"]), ("C", ["@", "T"])])
def test_an_extended_hours_print_takes_the_price(tape: str, codes: list[str]) -> None:
    assert excluded_by(tape, codes) == []


def test_the_plan_sourced_codes_follow_the_lead_s_ruling() -> None:
    assert excluded_by("C", ["@", "1"]) == []
    assert excluded_by("C", ["@", "S"]) == []
    assert excluded_by("C", ["@", "8"]) == ["8"]
    assert excluded_by("C", ["@", "E"]) == ["E"]
    assert excluded_by("A", ["8"]) == ["8"]


def test_an_unknown_code_tape_or_missing_conditions_is_named() -> None:
    assert excluded_by("C", ["@", "?"]) == ["unknown:?"]
    assert excluded_by("O", ["@"]) == ["unknown:tape O"]
    assert excluded_by(None, ["@"]) == ["unknown:tape None"]
    assert excluded_by("C", []) == ["unknown:no conditions"]
    assert excluded_by("C", None) == ["unknown:no conditions"]
    assert excluded_by("C", "@") == [], "alpaca-py allows one condition as a string"


def test_a_code_the_venue_shows_that_the_table_does_not_know_is_named() -> None:
    known = {tape: dict.fromkeys(codes, "name") for tape, codes in MAP_CODES.items()}
    assert unknown_codes(known) == []
    known["C"]["?"] = "Something New"
    assert unknown_codes(known) == ["tape C '?' (Something New)"]


def test_the_process_tally_counts_kept_prints_and_each_excluding_code_per_source() -> None:
    before = condition_counts()
    count_print("stream", [])
    count_print("stream", ["I", "4"])
    count_print("request", ["I"])
    after = condition_counts()

    def grew(source: str, key: str) -> int:
        return after.get(source, {}).get(key, 0) - before.get(source, {}).get(key, 0)

    assert (grew("stream", "kept"), grew("stream", "I"), grew("stream", "4")) == (1, 1, 1)
    assert (grew("request", "kept"), grew("request", "I")) == (0, 1)


@pytest.mark.parametrize(
    "answer", [[], {}, {"@": 1}, {"": "Regular Sale"}, {"AB": "Two characters"}, {"@": ""}, "text"]
)
def test_the_map_reader_refuses_anything_but_one_character_codes_to_names_and_stops(
    answer: object,
) -> None:
    asked: list[str] = []

    def get(path: str, data: dict[str, str]) -> object:
        assert path == CONDITIONS_PATH
        asked.append(data["tape"])
        return answer

    with pytest.raises(ValueError, match="tape A"):
        read_condition_map(get)
    assert asked == ["A"], "nothing more is asked after a malformed answer"


def test_the_map_reader_asks_each_tape_once_after_its_pacer() -> None:
    events: list[str] = []

    def get(path: str, data: dict[str, str]) -> object:
        events.append(data["tape"])
        return {"@": "Regular Sale"}

    answers = read_condition_map(get, lambda: events.append("pace"))
    assert events == ["pace", "A", "pace", "B", "pace", "C"]
    assert answers == {tape: {"@": "Regular Sale"} for tape in TAPES}
