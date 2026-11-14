"""Paper failure reports contain identity and exception type, never frame or exception values.

Contract: pytest 9.2.0's pytest_addoption and pytest_runtest_makereport hook wrapper
(doc/en/example/simple.rst, "Post-process test reports"); TestReport.longrepr accepts a string and
sections hold captured stdout/stderr/log (src/_pytest/reports.py). The terminal and JUnit consume
that same report. paper:test loads this module with `-p canon_trading.paper_safe_output`
(pytest's -p plugins are imported before options are parsed: src/_pytest/config/__init__.py,
consider_preparse) and turns it on with --paper-safe-output; it is not a root conftest.py, which
would shadow the tests' own `conftest` module. Other pytest runs are unchanged.

--no-showlocals alone leaves function arguments visible (src/_pytest/nodes.py:477-484).
--tb=short removes those arguments but keeps exception messages, which can themselves contain a
request's headers. No value-based redaction is used here, so truncation cannot defeat the policy.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--paper-safe-output",
        action="store_true",
        help="Report paper test failures by node ID, phase and exception type only",
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if not item.config.getoption("paper_safe_output"):
        return report

    # Clean the report before terminal/JUnit/other report consumers see it. A teardown report
    # may carry output captured during the failed call, even when teardown itself passed.
    report.sections.clear()
    report.user_properties.clear()
    if hasattr(report, "wasxfail"):
        report.wasxfail = "reason withheld for paper output"
    if report.failed:
        kind = call.excinfo.typename if call.excinfo is not None else "Failed"
        report.longrepr = f"{report.nodeid} [{report.when}]: {kind}"
    elif report.skipped:
        path, line, _ = item.location
        report.longrepr = (path, (line or 0) + 1, "Skipped: reason withheld for paper output")
    return report
