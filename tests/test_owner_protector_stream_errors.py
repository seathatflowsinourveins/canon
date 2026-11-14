"""Test b (the lead, 2026-11-13 19:01:48): a stream quote with no bid is an upstream named limit, with
no patch to the client, and our side must see and count it. The real alpaca-py stream, offline: one
batch of three quotes on a stub socket, the middle one with no bid. At the pin the parse error ends
the batch (the third quote is lost), is logged as "error during websocket communication" by
`alpaca.data.live.websocket`, and the socket stays up (alpaca/data/live/websocket.py:284-297,
:568-575 at c42add7c). The protector counts each such line, says at the next step for each holding
that quotes may have been lost, and its END line counts them."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from typing import cast

import msgpack
import pytest
from alpaca.data.live import StockDataStream

from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Harness


def quote_message(ns: int, *, bid: bool = True) -> dict[str, Any]:
    message: dict[str, Any] = {
        "T": "q",
        "S": "AAA",
        "bx": "V",
        "bs": 1,
        "ax": "V",
        "ap": 10.02,
        "as": 1,
        "c": ["R"],
        "z": "C",
        "t": msgpack.Timestamp.from_unix_nano(ns),
    }
    if bid:
        message["bp"] = 10.0
    return message


def one_batch_with_no_bid() -> list[float]:
    """Drive the real client through one received batch; returns the bids its handler got."""
    stream = StockDataStream("offline-placeholder", "offline-placeholder")
    handled: list[float] = []

    async def handler(quote: Any) -> None:
        handled.append(quote.bid_price)

    stream.subscribe_quotes(handler, "AAA")

    class Socket:
        received = 0

        async def recv(self) -> bytes:
            self.received += 1
            if self.received == 1:
                batch = [quote_message(1), quote_message(2, bid=False), quote_message(3)]
                return msgpack.packb(batch, datetime=False)
            assert stream._run is not None
            stream._run.request_stop()
            return msgpack.packb([])

        async def close(self) -> None:
            return None

    async def subscribed(run: Any) -> bool:
        del run
        return True

    async def nothing(*_args: Any, **_kwargs: Any) -> None:
        return None

    stream._wait_for_market_subscription = subscribed  # type: ignore[method-assign]
    stream._start_ws = nothing  # type: ignore[method-assign]
    stream._send_subscribe_msg = nothing  # type: ignore[method-assign]
    stream._ws = cast(Any, Socket())
    asyncio.run(asyncio.wait_for(stream._run_forever(), 10))
    return handled


def test_a_quote_with_no_bid_loses_the_rest_of_its_batch_at_the_pin() -> None:
    assert one_batch_with_no_bid() == [10.0], "the pin's batch loss is not as the named limit says"


def test_the_protector_counts_a_lost_batch_and_says_quotes_may_have_been_lost() -> None:
    strategy = Harness()
    strategy.on_start()
    assert AAA in strategy.slots
    one_batch_with_no_bid()
    strategy.test_clock.set_time(T0 + SECOND)
    strategy._on_step(cast(Any, SimpleNamespace()))
    lost = [n for n in strategy.said if n.startswith(f"{AAA}: quotes may have been lost")]
    assert len(lost) == 1, strategy.said
    assert "validation error for Quote" in lost[0] and "1 so far" in lost[0], lost
    strategy._on_step(cast(Any, SimpleNamespace()))
    assert len([n for n in strategy.said if "quotes may have been lost" in n]) == 1
    strategy.on_stop()
    assert any(
        n.startswith(f"{AAA}: END") and "stream-errors (node)=1" in n for n in strategy.said
    ), strategy.said


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Synthetic only, as the sibling owner-protector files: never inspect a credential environment
    or a paper lock (tests/conftest.py's autouse fixture reads the key variables)."""
