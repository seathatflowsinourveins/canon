"""The live account's request budget comes from a local file outside the repository (ADR 0110, (f)).

Every figure here is made up. The file under test is a throwaway in `tmp_path`; no test reads the config
folder of the machine it runs on.
"""

import os
import traceback
from pathlib import Path

import pytest

from canon_trading import live_budget
from canon_trading.live_budget import LiveSettingsError
from canon_trading.live_budget import read_request_budget
from canon_trading.live_budget import settings_path
from canon_trading.request_budget import TRADING_REQUEST_LIMIT_PER_MINUTE

MADE_UP = 777
"""A made-up limit, unlike the paper figure, so a test can tell which one it was given."""
MADE_UP_LONG = "7770777"
"""A made-up figure long enough that no temporary folder's name holds it by chance."""


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
    assert value not in str(raised.value)
    assert "777" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "text",
    [
        "[limits]\ntrading_requests_per_minute = 777\n",
        '[notes]\nwho = "nobody"\ntrading_requests_per_minute = 777\n',
        "trading_request_per_minute = 777\n",
        "TRADING_REQUESTS_PER_MINUTE = 777\n",
        "trading_requests_per_minute = 777\nburst = 5\n",
        "limits.trading_requests_per_minute = 777\n",
        '"trading_requests_per_minute " = 777\n',
        '777 = "a figure where the name goes"\n',
    ],
)
def test_a_name_the_reader_does_not_know_is_refused_never_read_as_absent(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(LiveSettingsError) as raised:
        read_request_budget(settings(tmp_path, text))
    # a name in the file may be a slip of the figure too (a bare key may be digits): never repeated.
    # The whole message is one fixed sentence, so anything of the file in it fails here, whatever
    # the name was (one name above is the key itself with a space after it)
    assert str(raised.value) == (
        "the local settings file (canon/live/settings.toml) sets a name other than "
        "trading_requests_per_minute at its top level; it holds that one key and nothing else"
    )
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "text",
    [
        "trading_requests_per_minute = \n",
        "= 777\n",
        "[unclosed\n",
        "trading_requests_per_minute = 777 oops\n",
    ],
)
def test_a_file_that_is_not_toml_is_refused_by_name_never_read_as_absent(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(LiveSettingsError, match="is not valid TOML") as raised:
        read_request_budget(settings(tmp_path, text))
    assert raised.value.__cause__ is None
    # the parser's error holds the whole document (`TOMLDecodeError.doc`): it is not kept on ours
    assert raised.value.__context__ is None


def test_a_file_that_cannot_be_read_is_refused_by_name_never_read_as_absent(tmp_path: Path) -> None:
    folder = tmp_path / "settings.toml"
    folder.mkdir()  # a folder of that name: there, and not a file that can be read
    with pytest.raises(LiveSettingsError, match="could not be read") as raised:
        read_request_budget(folder)
    assert raised.value.__context__ is None
    not_text = tmp_path / "bytes.toml"
    not_text.write_bytes(b"trading_requests_per_minute = 777 \xff\n")
    with pytest.raises(LiveSettingsError, match="could not be read") as raised:
        read_request_budget(not_text)
    # the decoder's error holds the file's bytes (`UnicodeDecodeError.object`): it is not kept on ours
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "text",
    [
        "trading_requests_per_minute = "
        + "7" * 5000
        + "\n",  # past Python's digit limit for an integer
        "trading_requests_per_minute = " + "[" * 5000 + "\n",  # nested past its recursion limit
    ],
)
def test_a_file_the_parser_gives_up_on_is_refused_by_name(tmp_path: Path, text: str) -> None:
    with pytest.raises(LiveSettingsError, match="could not be parsed|is not valid TOML") as raised:
        read_request_budget(settings(tmp_path, text))
    assert "7777" not in str(raised.value)
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "text",
    [
        f"trading_requests_per_minute = [{MADE_UP_LONG}]\n",
        f"trading_requests_per_minute = -{MADE_UP_LONG}\n",
        f"trading_request_per_minute = {MADE_UP_LONG}\n",
    ],
)
def test_no_frame_of_the_reader_in_the_error_s_traceback_holds_what_the_file_held(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(LiveSettingsError) as raised:
        read_request_budget(settings(tmp_path, text))
    # a formatter that prints each frame's variables (`capture_locals`) would print these
    frames = traceback.TracebackException.from_exception(raised.value, capture_locals=True).stack
    ours = [frame for frame in frames if frame.filename == live_budget.__file__]
    assert ours, "the reader's own frame is in the traceback"
    for frame in ours:
        assert MADE_UP_LONG not in str(frame.locals)


def test_a_link_whose_target_is_missing_is_refused_never_read_as_absent(tmp_path: Path) -> None:
    link = tmp_path / "settings.toml"
    os.symlink(tmp_path / "gone" / "settings.toml", link)  # the file's own link, to nothing
    with pytest.raises(LiveSettingsError, match="a link whose target is missing"):
        read_request_budget(link)
    os.symlink(tmp_path / "unmounted", tmp_path / "canon")  # a folder above it, linked to nothing
    with pytest.raises(LiveSettingsError, match="a link whose target is missing"):
        read_request_budget(tmp_path / "canon" / "live" / "settings.toml")


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


def test_a_link_that_leads_into_this_checkout_is_refused(tmp_path: Path) -> None:
    checkout = Path(live_budget.__file__).resolve().parents[2]
    os.symlink(checkout / "mise.toml", tmp_path / "settings.toml")
    with pytest.raises(LiveSettingsError, match="inside this checkout"):
        read_request_budget(tmp_path / "settings.toml")


def test_a_file_inside_any_other_git_checkout_is_refused_whether_or_not_it_is_there(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"  # a made-up main checkout: `.git` is a folder
    (main / ".git").mkdir(parents=True)
    linked = tmp_path / "linked"  # a made-up linked worktree: `.git` is a file
    linked.mkdir()
    (linked / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    written = linked / "settings.toml"
    written.write_text(f"trading_requests_per_minute = {MADE_UP}\n", encoding="utf-8")
    for inside in (main / "canon" / "live" / "settings.toml", written):
        with pytest.raises(LiveSettingsError, match="another git checkout"):
            read_request_budget(inside)


def test_this_checkout_is_found_by_name_when_it_cannot_be_compared_by_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = (
        tmp_path / "gone"
    ).resolve()  # a checkout that is not there: nothing to compare, no `.git`
    monkeypatch.setattr(live_budget, "CHECKOUT", gone)
    with pytest.raises(LiveSettingsError, match="inside this checkout"):
        read_request_budget(gone / "canon" / "live" / "settings.toml")


def test_this_checkout_is_found_by_identity_under_another_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"  # a made-up checkout with no `.git` entry
    real.mkdir()
    os.symlink(real, tmp_path / "alias")
    # the checkout known by another name than the one a path under it resolves to
    monkeypatch.setattr(live_budget, "CHECKOUT", tmp_path / "alias")
    written = real / "settings.toml"
    written.write_text(f"trading_requests_per_minute = {MADE_UP}\n", encoding="utf-8")
    assert not written.resolve().is_relative_to(live_budget.CHECKOUT)  # by name it is elsewhere
    for inside in (real / "canon" / "live" / "settings.toml", written):
        with pytest.raises(LiveSettingsError, match="inside this checkout"):
            read_request_budget(inside)


def test_another_git_checkout_is_refused_when_this_one_cannot_be_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_budget, "CHECKOUT", (tmp_path / "gone").resolve())
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    with pytest.raises(LiveSettingsError, match="another git checkout"):
        read_request_budget(other / "settings.toml")


def test_a_file_inside_this_checkout_under_another_spelling_of_its_path_is_refused() -> None:
    checkout = Path(live_budget.__file__).resolve().parents[2]
    respelt = Path(str(checkout).swapcase())
    if not (respelt / "AGENTS.md").is_file():
        pytest.skip("this volume tells upper case from lower: the checkout has one spelling")
    assert not respelt.resolve().is_relative_to(checkout)  # by name it is another folder
    for inside in (respelt / "canon" / "live" / "settings.toml", respelt / "mise.toml"):
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
