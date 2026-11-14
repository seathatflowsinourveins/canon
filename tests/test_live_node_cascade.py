"""A failed case's LiveNode does not fail the next case that builds one.

At the NautilusTrader pin a thread holds one LiveNode until it is dropped
(`crates/live/src/node/mod.rs:344-354`). A failed case's frame holds its node in a cycle, and pytest
keeps the last call-phase exception in `sys.last_*` (`_pytest/runner.py:173-193` at pytest 9.2.0). So
the next case that built a node without collecting first failed with "A LiveNode already exists", and a
run read as many failures what was one. Each case here runs inner cases in a pytest of its own, with no
conftest above its folder: three fail by force with their node disposed (an exception in the call,
`pytest.fail`, and an error in a fixture's teardown), and each is followed by a case that builds a node.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CONFTEST = Path(__file__).with_name("conftest.py")
INNER = """
import pytest
from nautilus_trader.common import Environment
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import TraderId


def build():
    config = LiveNodeConfig(environment=Environment.SANDBOX, trader_id=TraderId("TESTER-001"))
    return LiveNode.build("CASCADE-TEST", config)


def test_a_raises_with_its_node_disposed():
    node = build()
    try:
        raise AssertionError("forced failure")
    finally:
        node.dispose()


def test_b_builds_after_a_raise():
    build().dispose()


def test_c_fails_by_pytest_fail_with_its_node_disposed():
    node = build()
    try:
        pytest.fail("forced failure")
    finally:
        node.dispose()


def test_d_builds_after_pytest_fail():
    build().dispose()


@pytest.fixture
def node_then_teardown_error():
    node = build()
    yield
    node.dispose()
    raise RuntimeError("forced teardown error")


def test_e_errors_in_teardown_with_its_node_disposed(node_then_teardown_error):
    pass


def test_f_builds_after_a_teardown_error():
    build().dispose()
"""
WITH_FIXTURE = f"""
import importlib.util

spec = importlib.util.spec_from_file_location("canon_tests_conftest", {str(CONFTEST)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
pytest_runtest_logreport = module.pytest_runtest_logreport
no_live_node_kept_by_the_last_failure = module.no_live_node_kept_by_the_last_failure
"""
BUILDS = ("test_b_builds_after_a_raise", "test_d_builds_after_pytest_fail")
BUILDS += ("test_f_builds_after_a_teardown_error",)


def inner_run(folder: Path, conftest: str) -> str:
    (folder / "test_inner.py").write_text(INNER)
    (folder / "conftest.py").write_text(conftest)
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rA"]
        + ["--rootdir", str(folder), "--confcutdir", str(folder), "-c", os.devnull, str(folder)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=folder,
        env=env,
        check=False,
    )
    return done.stdout + done.stderr


def test_the_pins_still_keep_a_failed_cases_node_alive(tmp_path) -> None:
    """A canary on pytest and the NautilusTrader pin, not on Canon: with no release, the case after a
    raise fails to build. If a bump makes this pass, the pins no longer keep the node, and the
    release fixture may be retired."""
    out = inner_run(tmp_path, "")
    assert "FAILED test_inner.py::test_a_raises_with_its_node_disposed" in out, out
    assert "FAILED test_inner.py::test_b_builds_after_a_raise" in out, out
    assert "A LiveNode already exists" in out, out


def test_with_the_release_only_the_forced_failures_fail(tmp_path) -> None:
    out = inner_run(tmp_path, WITH_FIXTURE)
    assert "FAILED test_inner.py::test_a_raises_with_its_node_disposed" in out, out
    assert "FAILED test_inner.py::test_c_fails_by_pytest_fail_with_its_node_disposed" in out, out
    assert "ERROR test_inner.py::test_e_errors_in_teardown_with_its_node_disposed" in out, out
    for case in BUILDS:
        assert f"PASSED test_inner.py::{case}" in out, out
    assert "A LiveNode already exists" not in out, out
