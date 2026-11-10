# Review of 12f19a0b5eac6da29a15bf8512a99d0ba0e4ee35

1. **Instrument-scoped position reports can fail on an unrelated position.** `packages/canon-alpaca/src/canon_alpaca/execution.py:1027`, `_position_status_reports`: the code calls `position_status_report` before checking `command.instrument_id`. Consequently, requesting only `AAPL.ALPACA` raises if the account holds a fractional MSFT position, even though MSFT is outside the requested report. This prevents reporting the requested instrument, including reporting that its position is absent. Filter the position by instrument before converting and validating its quantity; retaining the intentional failure for an unfiltered startup report is compatible with that correction.

   **Reproduction:** In an offline Python invocation through `mise exec -- uv run --locked python`, loaded the commit’s `conftest.py` payloads and `FakeOrdersClient`, set its positions to `[{**POSITION_AAPL, "symbol": "MSFT", "qty": "0.5"}]`, and awaited the actual `AlpacaExecutionClient._position_status_reports` implementation with `command.instrument_id = InstrumentId.from_str("AAPL.ALPACA")`. A minimal receiver supplied the fake REST client, an async `_call` that invokes it, a clock, and a logger. The result was `ValueError('MSFT position quantity 0.5 is fractional; Equity is whole-share only')`, rather than the expected empty AAPL report list. No node or network was required for this reproduction.

## Verification and limits

- Reviewed the parent-to-commit change under `packages/canon-alpaca/` and checked relevant installed pinned upstream Python source.
- `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py packages/canon-alpaca/tests/test_upstream_contract.py -q -p no:cacheprovider`: **78 passed**.
- Broader offline run of `test_provider.py`, `test_data_client.py`, `test_execution_client.py`, and `test_upstream_contract.py`: **124 passed, 1 failed**. The failure was `test_data_client.py::test_stream_ending_while_subscribed_is_logged`. Its cause and whether this change contributes are **UNVERIFIED**; investigation stopped after the failed check in accordance with the repository rule. It is not counted as a confirmed defect above.
- Additional offline probes checked small fill prices and REST/stream rejection ordering; they did not establish additional defects.
- No paper tests, credential-dependent operations, or external network requests were run. No checkout files were intentionally edited; no fixes were made. The confirmed defect and the broader test failure remain unresolved.
