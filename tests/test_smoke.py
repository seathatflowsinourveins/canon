"""Smoke test: the root package installs and imports. Real tests replace this as code lands."""

import canon_trading


def test_package_imports() -> None:
    assert canon_trading.__doc__
