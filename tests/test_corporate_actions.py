"""`canon_trading.corporate_actions`, the GET of Alpaca's split records that
`mise run paper:history <account> corporate-actions` runs, offline: every request is answered by a stand-in
at requests' transport, and the task's routing runs with stand-ins for `security`, `timeout` and `uv`."""

import json
import os
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
import requests
from canon_alpaca import providers
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import ENV_API_KEY
from canon_alpaca.providers import ENV_SECRET_KEY
from requests.adapters import HTTPAdapter

from canon_trading import corporate_actions as ca
from canon_trading.session_window import SOURCE_ROOT

PLACEHOLDER = "placeholder-not-a-key"
FREE_TEXT = "a sentence Alpaca wrote"

REVERSE = {
    "id": "00000000-0000-0000-0000-000000000001",
    "symbol": "AAA",
    "old_cusip": "000000001",
    "new_cusip": "000000002",
    "old_rate": 10,
    "new_rate": 1,
    "ex_date": "2026-09-14",
    "process_date": "2026-09-14",
    "record_date": "2026-09-11",
    "payable_date": None,
    "currency": FREE_TEXT,
    "new_symbol": "AAB",
}
FORWARD = {
    "id": "00000000-0000-0000-0000-000000000002",
    "symbol": "BBB",
    "cusip": "000000003",
    "old_rate": 1,
    "new_rate": 2,
    "ex_date": "2026-09-15",
    "process_date": "2026-09-12",
    "record_date": "2026-09-10",
    "currency": "USD",
}
UNIT = {
    "id": "00000000-0000-0000-0000-000000000003",
    "old_symbol": "CCCU",
    "new_symbol": "CCC",
    "alternate_symbol": "CCCW",
    "old_rate": 1,
    "new_rate": 1,
    "alternate_rate": 0.5,
    "effective_date": "2026-09-10",
    "process_date": "2026-09-10",
}


def date_map(tmp_path: Path, **kinds: str) -> Path:
    """A sim date map: every weekday of 2026-08-31..2026-09-18 development unless named."""
    dates = {}
    for day in range(31, 32):
        dates[f"2026-08-{day}"] = {"kind": "development"}
    for day in (1, 2, 3, 4, 7, 8, 9, 10, 11, 14, 15, 16, 17, 18):
        dates[f"2026-09-{day:02d}"] = {"kind": "development"}
    dates["2026-09-07"] = {"kind": "closed"}
    for iso, kind in kinds.items():
        dates[iso.replace("_", "-")] = {"kind": kind}
    path = tmp_path / "date-map.json"
    path.write_text(json.dumps({"generated": "2026-11-14T00:00:00-05:00", "dates": dates}))
    return path


class Venue:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, list[str]]]] = []
        self.status = 200
        self.pages: list[dict] = [
            {
                "corporate_actions": {
                    "reverse_splits": [REVERSE],
                    "forward_splits": [FORWARD],
                    "unit_splits": [UNIT],
                },
                "next_page_token": None,
            }
        ]
        self.raise_exc: Exception | None = None

    def answer(self, request: requests.PreparedRequest) -> tuple[int, object]:
        url = urlsplit(request.url or "")
        query = parse_qs(url.query)
        self.sent.append((request.method or "", url.path, query))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.status != 200:
            return self.status, {"message": FREE_TEXT}
        token = query.get("page_token", [""])[0]
        return 200, self.pages[int(token or 0)]


@pytest.fixture
def venue(monkeypatch) -> Venue:
    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    monkeypatch.setenv(ENV_API_KEY, PLACEHOLDER)
    monkeypatch.setenv(ENV_SECRET_KEY, PLACEHOLDER)
    monkeypatch.setattr(providers, "REQUESTS", providers.RequestCounter())
    stand_in = Venue()

    def send(self, request, **kwargs):
        status, body = stand_in.answer(request)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return stand_in


def read(venue: Venue, tmp_path: Path, *extra: str, start="2026-09-05", end="2026-09-15") -> dict:
    out = tmp_path / "out"
    code = ca.main(
        [
            "--start",
            start,
            "--end",
            end,
            "--date-map",
            str(date_map(tmp_path)),
            "--catalog",
            str(out),
            *extra,
        ]
    )
    assert code == 0
    written = sorted((out / "corporate-actions").glob("splits-*.json"))
    assert len(written) == 1, written
    return json.loads(written[0].read_text())


def test_one_get_on_v1_corporate_actions_with_the_split_types_and_the_span(
    venue: Venue, tmp_path: Path
) -> None:
    read(venue, tmp_path)
    assert len(venue.sent) == 1
    method, path, query = venue.sent[0]
    assert (method, path) == ("GET", "/v1/corporate-actions")
    assert query["types"] == ["reverse_split,forward_split,unit_split"]
    assert query["start"] == ["2026-09-05"] and query["end"] == ["2026-09-15"]
    assert query["limit"] == [str(ca.PAGE_ROWS)]
    assert "symbols" not in query, "with no symbols named, every symbol is read"


def test_symbols_are_sent_when_named(venue: Venue, tmp_path: Path) -> None:
    names = tmp_path / "names.txt"
    names.write_text("AAA\nBBB\n\n")
    read(venue, tmp_path, "--symbols-file", str(names))
    assert venue.sent[0][2]["symbols"] == ["AAA,BBB"]


def test_the_client_refuses_every_method_but_get(venue: Venue) -> None:
    client = ca.corporate_actions_client()
    with pytest.raises(providers.MethodRefused):
        client.post("/corporate-actions", {})
    assert venue.sent == []


def test_every_page_is_followed_and_paced_after_the_first(
    venue: Venue, tmp_path: Path, monkeypatch
) -> None:
    first = dict(venue.pages[0], next_page_token="1")
    second = {
        "corporate_actions": {"reverse_splits": [dict(REVERSE, symbol="DDD")]},
        "next_page_token": None,
    }
    venue.pages = [first, second]
    slept: list[float] = []
    monkeypatch.setattr(ca, "SLEEP", slept.append)
    monkeypatch.setattr(ca, "CLOCK", lambda: 0.0)
    record = read(venue, tmp_path)
    assert [q.get("page_token") for _, _, q in venue.sent] == [None, ["1"]]
    assert slept == [60 / ca.PACE.data_per_minute]
    assert record["pages"] == 2 and len(record["rows"]) == 4


def test_a_page_cap_with_a_token_left_stops_by_name_and_writes_nothing(
    venue: Venue, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ca, "MAX_PAGES", 2)
    venue.pages = [dict(venue.pages[0], next_page_token=str(i + 1)) for i in range(3)]
    out = tmp_path / "out"
    code = ca.main(
        [
            "--start",
            "2026-09-05",
            "--end",
            "2026-09-15",
            "--date-map",
            str(date_map(tmp_path)),
            "--catalog",
            str(out),
        ]
    )
    assert code == 1
    assert len(venue.sent) == 2
    assert not (out / "corporate-actions").exists()


@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_stops_by_name_without_alpacas_text(
    venue: Venue, tmp_path: Path, status: int, capsys
) -> None:
    venue.status = status
    code = ca.main(
        [
            "--start",
            "2026-09-05",
            "--end",
            "2026-09-15",
            "--date-map",
            str(date_map(tmp_path)),
            "--catalog",
            str(tmp_path),
        ]
    )
    assert code == 1
    printed = capsys.readouterr().out
    assert f"{status}" in printed and "refused" in printed
    assert FREE_TEXT not in printed and PLACEHOLDER not in printed
    assert len(venue.sent) == 1


def test_a_429_stops_and_asks_for_nothing_more(venue: Venue, tmp_path: Path, capsys) -> None:
    venue.status = 429
    code = ca.main(
        [
            "--start",
            "2026-09-05",
            "--end",
            "2026-09-15",
            "--date-map",
            str(date_map(tmp_path)),
            "--catalog",
            str(tmp_path),
        ]
    )
    assert code == 1 and "429" in capsys.readouterr().out
    assert len(venue.sent) == 1


def test_a_read_timeout_stops_by_name(venue: Venue, tmp_path: Path, capsys) -> None:
    venue.raise_exc = requests.exceptions.ReadTimeout(FREE_TEXT)
    code = ca.main(
        [
            "--start",
            "2026-09-05",
            "--end",
            "2026-09-15",
            "--date-map",
            str(date_map(tmp_path)),
            "--catalog",
            str(tmp_path),
        ]
    )
    assert code == 1
    printed = capsys.readouterr().out
    assert "ReadTimeout" in printed and FREE_TEXT not in printed


def test_rows_keep_six_fields_and_mask_the_rest(venue: Venue, tmp_path: Path) -> None:
    record = read(venue, tmp_path)
    for row in record["rows"]:
        assert set(row) <= set(ca.KEPT) | {
            "ex_date_in_read_span",
            "ex_date_withheld",
            "process_date_withheld",
            "mapped_from",
        }
    text = json.dumps(record)
    assert FREE_TEXT not in text and "000000001" not in text and str(REVERSE["id"]) not in text
    by_symbol = {r["symbol"]: r for r in record["rows"]}
    assert by_symbol["AAA"] == {
        "type": "reverse_split",
        "symbol": "AAA",
        "ex_date": "2026-09-14",
        "process_date": "2026-09-14",
        "old_rate": 10,
        "new_rate": 1,
        "ex_date_in_read_span": True,
    }


def test_a_unit_split_maps_old_symbol_and_effective_date_and_says_so(
    venue: Venue, tmp_path: Path
) -> None:
    unit = next(r for r in read(venue, tmp_path)["rows"] if r["type"] == "unit_split")
    assert unit["symbol"] == "CCCU" and unit["ex_date"] == "2026-09-10"
    assert unit["mapped_from"] == {"symbol": "old_symbol", "ex_date": "effective_date"}


def test_a_renamed_symbol_is_counted_as_a_named_gap(venue: Venue, tmp_path: Path) -> None:
    record = read(venue, tmp_path)
    assert record["renamed_rows"] == 2, "the reverse split with new_symbol and the unit split"
    assert any("new_symbol" in g for g in record["gaps"])
    assert "AAB" not in json.dumps(record["rows"])


def test_an_ex_date_outside_the_read_span_is_named(venue: Venue, tmp_path: Path) -> None:
    venue.pages[0]["corporate_actions"]["forward_splits"] = [dict(FORWARD, ex_date="2026-09-16")]
    record = read(venue, tmp_path)
    row = next(r for r in record["rows"] if r["symbol"] == "BBB")
    assert row["ex_date_in_read_span"] is False and row["ex_date"] == "2026-09-16"
    assert record["ex_date_outside_read_span"] == 1
    assert any("process_date" in g for g in record["gaps"])


def test_an_ex_date_on_a_day_the_map_forbids_is_withheld(venue: Venue, tmp_path: Path) -> None:
    venue.pages[0]["corporate_actions"]["forward_splits"] = [dict(FORWARD, ex_date="2026-09-17")]
    out = tmp_path / "out"
    path = date_map(tmp_path, **{"2026_09_17": "held"})
    assert (
        ca.main(
            [
                "--start",
                "2026-09-05",
                "--end",
                "2026-09-15",
                "--date-map",
                str(path),
                "--catalog",
                str(out),
            ]
        )
        == 0
    )
    record = json.loads(
        (out / "corporate-actions" / "splits-2026-09-05_2026-09-15-all.json").read_text()
    )
    row = next(r for r in record["rows"] if r["symbol"] == "BBB")
    assert row["ex_date"] is None and row["ex_date_withheld"] == "held"
    assert "2026-09-17" not in json.dumps(record)


def test_an_ex_date_on_a_weekday_the_map_omits_is_withheld(venue: Venue, tmp_path: Path) -> None:
    venue.pages[0]["corporate_actions"]["forward_splits"] = [dict(FORWARD, ex_date="2026-09-21")]
    record = read(venue, tmp_path)
    row = next(r for r in record["rows"] if r["symbol"] == "BBB")
    assert row["ex_date"] is None and row["ex_date_withheld"] == "not in the map"
    assert "2026-09-21" not in json.dumps(record)


def test_a_process_date_on_a_day_the_map_forbids_is_withheld(venue: Venue, tmp_path: Path) -> None:
    venue.pages[0]["corporate_actions"]["forward_splits"] = [
        dict(FORWARD, process_date="2026-09-17")
    ]
    out = tmp_path / "out"
    path = date_map(tmp_path, **{"2026_09_17": "judgment"})
    args = ["--start", "2026-09-05", "--end", "2026-09-15", "--date-map", str(path)]
    assert ca.main([*args, "--catalog", str(out)]) == 0
    text = (out / "corporate-actions" / "splits-2026-09-05_2026-09-15-all.json").read_text()
    row = next(r for r in json.loads(text)["rows"] if r["symbol"] == "BBB")
    assert row["process_date"] is None and row["process_date_withheld"] == "judgment"
    assert "2026-09-17" not in text


def test_a_named_read_does_not_overwrite_the_span_read_of_every_symbol(
    venue: Venue, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    args = ["--start", "2026-09-05", "--end", "2026-09-15", "--date-map", str(date_map(tmp_path))]
    assert ca.main([*args, "--catalog", str(out)]) == 0
    assert ca.main([*args, "--catalog", str(out), "--symbols", "AAA"]) == 0
    names = sorted(p.name for p in (out / "corporate-actions").glob("splits-*.json"))
    assert len(names) == 2 and "splits-2026-09-05_2026-09-15-all.json" in names
    every = json.loads(
        (out / "corporate-actions" / "splits-2026-09-05_2026-09-15-all.json").read_text()
    )
    assert every["symbols"] == "all" and len(every["rows"]) == 3


def test_a_masked_symbol_is_counted_and_named(venue: Venue, tmp_path: Path) -> None:
    venue.pages[0]["corporate_actions"]["forward_splits"] = [dict(FORWARD, symbol="lower case")]
    record = read(venue, tmp_path)
    assert record["masked_symbols"] == 1
    assert any("masked" in g for g in record["gaps"])
    assert "lower case" not in json.dumps(record)


def test_a_held_catalog_lock_stops_before_any_get(venue: Venue, tmp_path: Path, capsys) -> None:
    import fcntl

    out = tmp_path / "out"
    out.mkdir()
    with (out / ".history-load.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        args = [
            "--start",
            "2026-09-05",
            "--end",
            "2026-09-15",
            "--date-map",
            str(date_map(tmp_path)),
        ]
        assert ca.main([*args, "--catalog", str(out)]) == 1
    assert "another history load holds the catalog" in capsys.readouterr().out
    assert venue.sent == []


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"generated": "g"}, "no dates"),
        ({"generated": "g", "dates": {"2026-09-08": "development"}}, "2026-09-08"),
        ({"generated": "g", "dates": {"2026-09-08": {}}}, "2026-09-08 no kind"),
    ],
)
def test_a_malformed_date_map_is_refused_by_name(
    tmp_path: Path, body: dict, why: str, capsys
) -> None:
    path = tmp_path / "bad-map.json"
    path.write_text(json.dumps(body))
    args = ["--start", "2026-09-08", "--end", "2026-09-08", "--date-map", str(path), "--check-only"]
    assert ca.main(args) == 3
    assert why in capsys.readouterr().out


def test_split_read_feeds_the_rankings_split_test(venue: Venue, tmp_path: Path) -> None:
    from datetime import date

    from canon_trading import movers_rank

    venue.pages[0]["corporate_actions"]["forward_splits"] = [
        FORWARD,
        dict(FORWARD, symbol="EEE", ex_date="2026-09-21"),
    ]
    record = read(venue, tmp_path)
    split_read, left_out = ca.split_read(record)
    assert left_out == {"unit_split": 1, "withheld": 1}
    assert (split_read.start, split_read.end) == (date(2026, 9, 5), date(2026, 9, 15))
    assert movers_rank.split_since(split_read, "BBB", date(2026, 9, 14), date(2026, 9, 15)) is True
    assert movers_rank.split_since(split_read, "AAA", date(2026, 9, 14), date(2026, 9, 15)) is False
    assert movers_rank.split_since(split_read, "AAA", date(2026, 9, 11), date(2026, 9, 14)) is True


def test_the_record_names_its_source_and_when_it_was_read(venue: Venue, tmp_path: Path) -> None:
    record = read(venue, tmp_path)
    assert record["source"] == "GET https://data.alpaca.markets/v1/corporate-actions"
    assert record["read_at"].endswith("-05:00") or record["read_at"].endswith("-04:00")
    assert record["date_map_generated"] == "2026-11-14T00:00:00-05:00"
    assert record["start"] == "2026-09-05" and record["end"] == "2026-09-15"
    assert PLACEHOLDER not in json.dumps(record)


@pytest.mark.parametrize(
    ("kinds", "why"),
    [
        ({"2026_09_09": "held"}, "2026-09-09 held"),
        ({"2026_09_09": "reserved"}, "2026-09-09 reserved"),
        ({"2026_09_09": "forward"}, "2026-09-09 forward"),
    ],
)
def test_a_span_holding_a_day_the_map_forbids_is_refused_before_any_get(
    venue: Venue, tmp_path: Path, kinds: dict, why: str, capsys
) -> None:
    code = ca.main(
        [
            "--start",
            "2026-09-05",
            "--end",
            "2026-09-15",
            "--date-map",
            str(date_map(tmp_path, **kinds)),
            "--catalog",
            str(tmp_path),
        ]
    )
    assert code == 3 and why in capsys.readouterr().out
    assert venue.sent == []


def test_a_weekday_missing_from_the_map_is_refused(venue: Venue, tmp_path: Path, capsys) -> None:
    code = ca.main(
        [
            "--start",
            "2026-09-05",
            "--end",
            "2026-09-22",
            "--date-map",
            str(date_map(tmp_path)),
            "--catalog",
            str(tmp_path),
        ]
    )
    assert code == 3 and "2026-09-21 not in the map" in capsys.readouterr().out
    assert venue.sent == []


def test_a_weekend_inside_the_map_passes_and_a_day_past_its_end_is_refused(
    tmp_path: Path, capsys
) -> None:
    path = str(date_map(tmp_path))
    inside = ["--start", "2026-09-05", "--end", "2026-09-06", "--date-map", path, "--check-only"]
    assert ca.main(inside) == 0
    past = ["--start", "2026-09-18", "--end", "2026-09-19", "--date-map", path, "--check-only"]
    assert ca.main(past) == 3 and "2026-09-19 not in the map" in capsys.readouterr().out


def test_check_only_reads_no_pair_and_sends_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.delenv(ENV_SECRET_KEY, raising=False)
    path = date_map(tmp_path)
    assert (
        ca.main(
            [
                "--start",
                "2026-09-05",
                "--end",
                "2026-09-15",
                "--date-map",
                str(path),
                "--check-only",
            ]
        )
        == 0
    )
    held = date_map(tmp_path, **{"2026_09_10": "judgment"})
    assert (
        ca.main(
            [
                "--start",
                "2026-09-05",
                "--end",
                "2026-09-15",
                "--date-map",
                str(held),
                "--check-only",
            ]
        )
        == 3
    )


def test_an_end_before_the_start_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        ca.main(
            [
                "--start",
                "2026-09-15",
                "--end",
                "2026-09-05",
                "--date-map",
                str(date_map(tmp_path)),
                "--check-only",
            ]
        )


def _route(tmp_path: Path, **usage: str) -> subprocess.CompletedProcess[str]:
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    security = tmp_path / "security"
    security.write_text(f'#!/bin/sh\necho security >> "$SENT.security"\necho {PLACEHOLDER}\n')
    security.chmod(0o700)
    timeout = tmp_path / "timeout"
    timeout.write_text('#!/bin/sh\nif [ "$1" = -s ]; then shift 2; fi\nshift\nexec "$@"\n')
    timeout.chmod(0o700)
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" >> "$SENT"\nprintf "%s\\n" "${CANON_ALPACA_DATA_ONLY:-unset}" >> "$SENT"\n'
        'case "$*" in *--check-only*) exit "${CHECK_EXIT:-0}" ;; esac\n'
    )
    uv.chmod(0o700)
    script = (
        task["run"]
        .replace("/usr/bin/security", str(security))
        .replace("/usr/bin/timeout", str(timeout))
    )
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{tmp_path}:{os.defpath}", "SENT": str(tmp_path / "sent"), **usage},
        capture_output=True,
        text=True,
        check=False,
    )


def test_mise_corporate_actions_mode_checks_the_map_first_then_reads_in_data_only_mode(
    tmp_path: Path,
) -> None:
    done = _route(
        tmp_path,
        usage_account="tests",
        usage_mode="corporate-actions",
        usage_start="2025-10-21",
        usage_end="2025-12-31",
        usage_date_map="/maps/date-map.json",
        usage_symbols_file="/names.txt",
    )
    assert done.returncode == 0, done.stderr
    sent = (tmp_path / "sent").read_text().splitlines()
    args = [
        "--start",
        "2025-10-21",
        "--end",
        "2025-12-31",
        "--date-map",
        "/maps/date-map.json",
        "--catalog",
        "catalog",
        "--symbols-file",
        "/names.txt",
    ]
    module = ["run", "--locked", "python", "-m", "canon_trading.corporate_actions"]
    assert sent == [*module, *args, "--check-only", "unset", *module, *args, "1"]


def test_mise_corporate_actions_mode_stops_on_a_failed_check_before_any_pair(
    tmp_path: Path,
) -> None:
    done = _route(
        tmp_path,
        usage_account="windows",
        usage_mode="corporate-actions",
        usage_start="2026-08-10",
        usage_end="2026-08-20",
        usage_date_map="/maps/date-map.json",
        CHECK_EXIT="3",
    )
    assert done.returncode == 3
    assert not (tmp_path / "sent.security").exists(), "no pair is read after a refused span"
    assert (tmp_path / "sent").read_text().count("canon_trading.corporate_actions") == 1


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("usage_bars", "1-DAY"),
        ("usage_data", "trades"),
        ("usage_feed", "sip"),
        ("usage_session", "2026-09-15"),
    ],
)
def test_mise_corporate_actions_mode_refuses_a_foreign_flag_before_any_pair(
    tmp_path: Path, flag: str, value: str
) -> None:
    done = _route(
        tmp_path,
        usage_account="tests",
        usage_mode="corporate-actions",
        usage_start="2025-10-21",
        usage_end="2025-12-31",
        usage_date_map="/maps/date-map.json",
        **{flag: value},
    )
    assert done.returncode == 3, done.stderr
    assert "corporate-actions mode takes" in done.stderr and "nothing was read" in done.stderr
    assert not (tmp_path / "sent").exists() and not (tmp_path / "sent.security").exists()


def test_mise_corporate_actions_mode_needs_a_date_map(tmp_path: Path) -> None:
    done = _route(
        tmp_path,
        usage_account="tests",
        usage_mode="corporate-actions",
        usage_start="2025-10-21",
        usage_end="2025-12-31",
    )
    assert done.returncode == 3 and "--date-map" in done.stderr
    assert not (tmp_path / "sent").exists()


def test_other_modes_refuse_the_date_map_flag(tmp_path: Path) -> None:
    done = _route(
        tmp_path,
        usage_account="tests",
        usage_mode="conditions",
        usage_date_map="/maps/date-map.json",
    )
    assert done.returncode == 3
    done = _route(
        tmp_path,
        usage_account="tests",
        usage_symbols="AAPL",
        usage_start="2026-11-11T14:30:00Z",
        usage_end="2026-11-11T15:00:00Z",
        usage_date_map="/maps/date-map.json",
    )
    assert done.returncode == 3
