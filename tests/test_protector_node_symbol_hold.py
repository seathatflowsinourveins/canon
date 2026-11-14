"""The node's side of the hold-one-symbol unit: the owner's per-symbol holds are a control of the
task and the runner (default the account's instance folder), read at the start under the locks (never
made: none held; unreadable: refused as a start, not as a hold), named in the node's start line and at
its end, and handed to the protector with the start read, which reads them at every step."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from canon_trading import session_window as window
from canon_trading import symbol_hold
from tests.test_protector_node import ROOT
from tests.test_protector_node import config
from tests.test_protector_node import controls  # noqa: F401 - the fixture
from tests.test_protector_node import runner_arm
from tests.test_protector_node import runtime
from tests.test_protector_node import subject


def test_cli_takes_the_symbol_holds_folder(controls, tmp_path):  # noqa: F811
    module = subject()
    argv = [
        "windows",
        "--row",
        "OWN1",
        "--holds-dir",
        str(controls[0]),
        "--inbox",
        str(controls[1]),
    ]
    args = module.parse_args([*argv, "--symbol-holds-dir", str(tmp_path)])
    assert args.symbol_holds_dir == tmp_path


def test_task_and_runner_pass_the_symbol_holds_folder():
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:protector"]
    assert 'flag "--symbol-holds-dir <symbol_holds_dir>"' in task["usage"]
    assert 'args+=(--symbol-holds-dir "$usage_symbol_holds_dir")' in task["run"]
    runner = (ROOT / "scripts" / "automation").read_text()
    assert '--symbol-holds-dir "$P_INST/symbol-holds"' in runner
    assert 'mkdir -p "$inst/holds" "$inst/inbox" "$inst/symbol-holds"' in runner


def test_the_start_line_and_the_end_name_each_held_symbol(
    controls,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
):
    module, _settings, strategy, _foreign, _reads, built = runtime(monkeypatch, controls)
    holds = tmp_path / "symbol-holds"
    symbol_hold.write_hold(holds, "BBB")
    settings = config(controls, symbol_holds_dir=holds)
    strategy.settings.symbol_holds_dir = holds
    strategy._read_symbol_holds()
    module.run_protector(settings, trading=object(), lock_dir=tmp_path)
    out = capsys.readouterr().out
    assert f"paper:protector: {symbol_hold.HELD}: BBB" in out, out
    (end,) = [line for line in out.splitlines() if line.startswith("BBB: ")]
    assert symbol_hold.HELD in end, end
    (other,) = [line for line in out.splitlines() if line.startswith("AAA: ")]
    assert symbol_hold.HELD not in other
    assert built[0][0].symbol_holds_dir == holds


def test_the_protector_is_handed_the_folder(controls, tmp_path):  # noqa: F811
    holds = tmp_path / "symbol-holds"
    holds.mkdir()
    module = subject()
    from tests.test_protector_node import AAA

    node, strategy = module.protector_node(
        config(controls, symbol_holds_dir=holds), (AAA,), feed="sip"
    )
    try:
        assert strategy.settings.symbol_holds_dir == holds
    finally:
        node.dispose()


def test_agents_row_names_the_task_and_c_is_the_owners_alone():
    agents = (ROOT / "AGENTS.md").read_text()
    row = next(line for line in agents.splitlines() if "mise run paper:hold-symbol" in line)
    assert "owner" in row and "symbol-holds" in row
    assert Path(ROOT / "mise.toml").read_text().count('[tasks."paper:hold-symbol"]') == 1


# Review round (/code-review high, 2026-11-14): each case fails before its fix.


def test_review_f1_an_installed_runner_without_the_flag_reads_its_instance_folder(
    controls,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    argv = ["windows", "--row", "OWN1", "--holds-dir", str(controls[0]), "--inbox"]
    args = subject().parse_args([*argv, str(controls[1])])
    assert args.symbol_holds_dir == symbol_hold.instance_holds("windows")
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:protector"]
    assert 'flag "--symbol-holds-dir <symbol_holds_dir>" required=#true' not in task["usage"]


def test_review_f2_an_unreadable_folder_refuses_without_reading_as_a_hold(
    controls,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    module, _settings, _strategy, _foreign, _reads, built = runtime(monkeypatch, controls)
    unreadable = tmp_path / "symbol-holds"
    unreadable.write_text("not a folder")
    with pytest.raises(window.WindowRefused) as refused:
        module.run_protector(
            config(controls, symbol_holds_dir=unreadable), trading=object(), lock_dir=tmp_path
        )
    assert built == []
    assert runner_arm(str(refused.value)) == "*", "a start refusal, never the runner's hold"


def test_review_f2_and_f4_a_folder_never_made_is_none_held_and_the_read_is_handed_on(
    controls,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
):
    module, _settings, _strategy, _foreign, _reads, built = runtime(monkeypatch, controls)
    module.run_protector(
        config(controls, symbol_holds_dir=tmp_path / "absent"), trading=object(), lock_dir=tmp_path
    )
    assert "none held" in capsys.readouterr().out
    holds = tmp_path / "symbol-holds"
    symbol_hold.write_hold(holds, "BBB")
    module.run_protector(
        config(controls, symbol_holds_dir=holds), trading=object(), lock_dir=tmp_path
    )
    assert built[-1][0].symbol_holds == frozenset({"BBB"}), "the start read is the seed"
