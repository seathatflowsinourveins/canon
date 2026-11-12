"""The arming file's one format and what an absent file means for paper and for live (ADR 0110, (e))."""

from pathlib import Path

import pytest

from canon_trading import arming
from canon_trading.arming import LIVE_ARMING_FILE
from canon_trading.arming import Arming
from canon_trading.arming import read_arming


@pytest.mark.parametrize(
    ("text", "armed", "read"),
    [
        ("on\n# a note under the value\n", True, "on"),
        ("off\n# a note under the value\n", False, "off"),
        ("  on  \n", True, "on"),
        ("ON\n", False, "off: the arming file's first line is neither on nor off: 'ON'"),
        ("# a note first\non\n", False, None),
        ("", False, "off: the arming file's first line is neither on nor off: ''"),
        ("\non\n", False, "off: the arming file's first line is neither on nor off: ''"),
    ],
)
@pytest.mark.parametrize("absent_is_on", [True, False])
def test_a_present_file_is_its_first_line_whatever_the_deployment(
    tmp_path: Path, text: str, armed: bool, read: str | None, absent_is_on: bool
) -> None:
    path = tmp_path / "arming"
    path.write_text(text, encoding="utf-8")
    found = read_arming(path, absent_is_on=absent_is_on)
    assert found.armed is armed
    if read is not None:
        assert found.read == read
    else:
        assert found.read.startswith("off: the arming file's first line is neither on nor off")


def test_an_absent_file_is_on_for_paper_and_off_for_live(tmp_path: Path) -> None:
    missing = tmp_path / "arming"
    assert read_arming(missing, absent_is_on=True) == Arming(True, "on: the arming file is absent")
    assert read_arming(missing, absent_is_on=False) == Arming(
        False, "off: the arming file is absent"
    )
    assert read_arming(None, absent_is_on=True) == Arming(True, "on: no arming file is set")
    assert read_arming(None, absent_is_on=False) == Arming(False, "off: no arming file is set")


@pytest.mark.parametrize("absent_is_on", [True, False])
def test_a_file_that_cannot_be_read_is_off_by_name(tmp_path: Path, absent_is_on: bool) -> None:
    folder = tmp_path / "arming"
    folder.mkdir()  # reading a folder raises IsADirectoryError, an OSError
    found = read_arming(folder, absent_is_on=absent_is_on)
    assert found == Arming(False, "off: the arming file could not be read (IsADirectoryError)")
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff\xfe\x00on")
    found = read_arming(binary, absent_is_on=absent_is_on)
    assert found == Arming(False, "off: the arming file could not be read (UnicodeDecodeError)")


def test_each_read_is_fresh(tmp_path: Path) -> None:
    path = tmp_path / "arming"
    path.write_text("off\n", encoding="utf-8")
    assert not read_arming(path, absent_is_on=True).armed
    path.write_text("on\n", encoding="utf-8")
    assert read_arming(path, absent_is_on=True).armed
    path.unlink()
    assert read_arming(path, absent_is_on=False).armed is False


def test_the_live_file_is_named_once_beside_the_paper_schedule() -> None:
    assert Path("canon/live-arming") == LIVE_ARMING_FILE
    assert LIVE_ARMING_FILE.parent == Path("canon")


def test_a_live_nodes_arming_file_is_an_absolute_path_under_the_main_checkout_it_is_given(
    tmp_path: Path,
) -> None:
    """The live file is read from the checkout of `main`, as `canon/paper-schedule` is, and named
    absolutely, so the folder a node happens to start in never decides which file it reads."""
    found = arming.live_arming_path(tmp_path)
    assert found.is_absolute()
    assert found == tmp_path.resolve() / "canon" / "live-arming"
    assert arming.live_arming_path(Path("some/checkout")).is_absolute()
    found.parent.mkdir()
    assert read_arming(found, absent_is_on=False) == Arming(False, "off: the arming file is absent")
    found.write_text("on\nturned on for the test\n", encoding="utf-8")
    assert read_arming(found, absent_is_on=False) == Arming(True, "on")


@pytest.mark.parametrize(
    ("account", "on"),
    [
        ("tests", True),
        ("windows", True),
        ("protect", False),
        ("live", False),
        ("", False),
        (None, False),
    ],
)
def test_an_absent_file_means_on_only_for_the_two_paper_accounts_that_buy_by_themselves(
    account, on
) -> None:
    """The lead, 2026-11-12: an automatic re-entry is an automatic buy, so on account C and on live
    it is off until the arming file says on. Accounts A and B keep buying with no file named."""
    assert arming.absent_is_on(account) is on
    word = "on" if on else "off"
    assert arming.read_arming_for(account, None) == Arming(on, f"{word}: no arming file is set")


def test_account_c_buys_again_only_once_its_file_says_on(tmp_path: Path) -> None:
    path = tmp_path / "arming"
    assert arming.read_arming_for("protect", path) == Arming(
        False, "off: the arming file is absent"
    )
    path.write_text("on\n", encoding="utf-8")
    assert arming.read_arming_for("protect", path) == Arming(True, "on")
    path.write_text("off\n", encoding="utf-8")
    assert arming.read_arming_for("tests", path) == Arming(False, "off")


def test_a_named_file_whose_folder_is_missing_or_whose_link_is_broken_is_off_by_name(
    tmp_path: Path,
) -> None:
    """A mistyped path must not read as an absent file, which on accounts A and B means on."""
    missing = tmp_path / "no-such-folder" / "arming"
    assert read_arming(missing, absent_is_on=True) == Arming(
        False, "off: the arming file's folder is missing"
    )
    link = tmp_path / "arming"
    link.symlink_to(tmp_path / "gone")
    assert read_arming(link, absent_is_on=True) == Arming(
        False, "off: the arming file is a link to nothing"
    )
    assert read_arming(tmp_path / "absent", absent_is_on=True) == Arming(
        True, "on: the arming file is absent"
    )
