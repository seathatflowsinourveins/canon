"""The live account's request budget comes from a local file outside the repository (ADR 0110, (f)).

Every figure here is made up. The file under test is a throwaway in `tmp_path`; no test reads the config
folder of the machine it runs on.
"""

from pathlib import Path

import pytest

from canon_trading import live_budget
from canon_trading.live_budget import LiveSettingsError
from canon_trading.live_budget import read_request_budget
from canon_trading.live_budget import settings_path
from canon_trading.request_budget import TRADING_REQUEST_LIMIT_PER_MINUTE

MADE_UP = 777
"""A made-up limit, unlike the paper figure, so a test can tell which one it was given."""


def settings(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_an_absent_file_is_the_paper_figure_and_says_so(tmp_path: Path) -> None:
    budget = read_request_budget(tmp_path / "settings.toml")
    assert budget.limit_per_minute == TRADING_REQUEST_LIMIT_PER_MINUTE
    assert budget.from_settings is False
    assert budget.source == "the paper figure: no local settings file"


def test_the_file_s_figure_is_the_budget(tmp_path: Path) -> None:
    budget = read_request_budget(
        settings(tmp_path, f"# a note\ntrading_requests_per_minute = {MADE_UP}\n")
    )
    assert budget.limit_per_minute == MADE_UP
    assert budget.from_settings is True
    assert budget.source == "the local settings file"


def test_a_file_that_sets_no_figure_is_the_paper_figure_and_says_so(tmp_path: Path) -> None:
    budget = read_request_budget(settings(tmp_path, "# nothing set yet\n"))
    assert budget.limit_per_minute == TRADING_REQUEST_LIMIT_PER_MINUTE
    assert budget.from_settings is False
    assert budget.source == (
        "the paper figure: the local settings file sets no trading_requests_per_minute"
    )


@pytest.mark.parametrize(
    "value",
    ["0", "-5", "1.5", "true", '"777"', "[777]", "{ per_minute = 777 }"],
)
def test_a_figure_that_is_not_a_positive_whole_number_is_refused_by_name_never_replaced(
    tmp_path: Path, value: str
) -> None:
    path = settings(tmp_path, f"trading_requests_per_minute = {value}\n")
    with pytest.raises(LiveSettingsError, match="must be a positive whole number") as raised:
        read_request_budget(path)
    # what was written may be a slip of the real figure: it is never repeated
    assert "777" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize("text", ["trading_requests_per_minute = \n", "= 777\n", "[unclosed\n"])
def test_a_file_that_is_not_toml_is_refused_by_name_never_read_as_absent(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(LiveSettingsError, match="is not valid TOML") as raised:
        read_request_budget(settings(tmp_path, text))
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_a_file_that_cannot_be_read_is_refused_by_name_never_read_as_absent(tmp_path: Path) -> None:
    folder = tmp_path / "settings.toml"
    folder.mkdir()  # a folder of that name: there, and not a file that can be read
    with pytest.raises(LiveSettingsError, match="could not be read"):
        read_request_budget(folder)
    not_text = tmp_path / "bytes.toml"
    not_text.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(LiveSettingsError, match="could not be read"):
        read_request_budget(not_text)


def test_the_figure_is_in_no_repr_or_message(tmp_path: Path) -> None:
    budget = read_request_budget(settings(tmp_path, f"trading_requests_per_minute = {MADE_UP}\n"))
    assert str(MADE_UP) not in repr(budget)
    assert str(MADE_UP) not in str(budget)
    assert str(MADE_UP) not in budget.source


def test_the_file_is_under_the_config_folder_outside_the_repository(tmp_path: Path) -> None:
    assert settings_path({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == (
        tmp_path / "canon" / "live" / "settings.toml"
    )
    assert settings_path({}) == Path.home() / ".claude" / "canon" / "live" / "settings.toml"
    assert settings_path({"CLAUDE_CONFIG_DIR": ""}) == settings_path({})


def test_a_file_inside_this_checkout_is_refused_whether_or_not_it_is_there() -> None:
    checkout = Path(live_budget.__file__).resolve().parents[2]
    assert (checkout / "AGENTS.md").is_file()
    for inside in (checkout / "canon" / "live" / "settings.toml", checkout / "mise.toml"):
        with pytest.raises(LiveSettingsError, match="inside this checkout"):
            read_request_budget(inside)


def test_with_no_path_it_reads_the_config_folder_s_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert read_request_budget().from_settings is False
    (tmp_path / "canon" / "live").mkdir(parents=True)
    (tmp_path / "canon" / "live" / "settings.toml").write_text(
        f"trading_requests_per_minute = {MADE_UP}\n", encoding="utf-8"
    )
    budget = read_request_budget()
    assert budget.from_settings is True
    assert budget.limit_per_minute == MADE_UP
