"""PKG-01/05/07 contract coverage for the bounded Binance Testnet path."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from beidou_exchange.binance_usdm.adapter import BinanceReferenceData, BinanceUsdmAdapter
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result
from beidou_exchange.core.protocol import OrderRequest
from beidou_exchange.testnet_guard import TestnetEnvironmentGuard
from beidou_shared.errors import ErrorCategory as SharedErrorCategory
from beidou_shared.types import (
    AccountId,
    AccountRef,
    HealthStatus,
    InstrumentId,
    OrderSide,
    OrderType,
    Quantity,
    VenueId,
    VenueInstrument,
)


class _SequenceTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        path: str,
        signed: bool = False,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Result[Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "signed": signed,
                "params": dict(params or {}),
                "kwargs": kwargs,
            }
        )
        if not self.responses:
            raise AssertionError("transport response fixture exhausted")
        response = self.responses.pop(0)
        return response if isinstance(response, Result) else Result.success(response, source="fixture")


def _testnet_guard() -> TestnetEnvironmentGuard:
    return TestnetEnvironmentGuard(
        "https://demo-fapi.binance.com",
        max_notional=25,
        max_leverage=3,
        account_id="dedicated-testnet-account",
        writes_enabled=True,
    )


def _write_context(guard: TestnetEnvironmentGuard):
    return guard.build_write_context(
        intent_id="intent-contract-1",
        trace_id="trace-contract-1",
        symbol="BTCUSDT",
        side="",
        order_type="LEVERAGE",
        quantity="0.001",
        notional="20",
        leverage="2",
        position_id="position-BTCUSDT",
        pool_id="pool-1",
        pool_version="1",
        pool_hash="a" * 64,
        pool_symbols=("BTCUSDT",),
    )


@pytest.mark.asyncio
async def test_leverage_set_ack_and_readback_are_identity_bound() -> None:
    transport = _SequenceTransport(
        [
            {"symbol": "BTCUSDT", "leverage": 2},
            [{"symbol": "BTCUSDT", "leverage": "2"}],
            {"symbol": "ETHUSDT", "leverage": 2},
        ]
    )
    guard = _testnet_guard()
    adapter = BinanceUsdmAdapter(
        account_id=AccountId("dedicated-testnet-account"),
        rest_client=transport,
        testnet_guard=guard,
    )
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    account_ref = AccountRef(venue_id=adapter.venue_id, account_id=AccountId("dedicated-testnet-account"))

    acknowledged = await adapter.set_leverage(
        "BTCUSDT", 2, account_ref=account_ref, write_context=_write_context(guard)
    )
    readback = await adapter.read_leverage("BTCUSDT")
    mismatch = await adapter.set_leverage("BTCUSDT", 2, account_ref=account_ref, write_context=_write_context(guard))

    assert acknowledged.is_success() is True
    assert acknowledged.data == {"symbol": "BTCUSDT", "leverage": 2}
    assert readback.is_success() is True
    assert readback.data is not None and readback.data["leverage"] == 2
    assert mismatch.is_success() is False
    assert mismatch.error is not None
    assert mismatch.error.category is SharedErrorCategory.UNKNOWN
    assert [call["path"] for call in transport.calls] == [Endpoint.LEVERAGE, Endpoint.POSITION_RISK, Endpoint.LEVERAGE]
    assert all(call["signed"] is True for call in transport.calls)
    assert transport.calls[0]["params"] == {"symbol": "BTCUSDT", "leverage": 2}


@pytest.mark.asyncio
async def test_closed_kline_adapter_filters_open_bar_and_normalizes_fields() -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    raw_rows = [
        [1_000, "100", "101", "99", "100.5", "12", now_ms - 10_000, "1206", 7, "6", "0", "0"],
        [2_000, "100", "101", "99", "100.6", "13", now_ms + 60_000, "1307", 8, "7", "0", "0"],
    ]
    transport = _SequenceTransport([raw_rows])
    adapter = BinanceUsdmAdapter(rest_client=transport)

    result = await adapter.get_closed_klines("btcusdt", "1m", 2)

    assert result.is_success() is True
    assert result.data is not None and len(result.data) == 1
    assert result.data[0] == {
        "open_time": 1_000,
        "close_time": now_ms - 10_000,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "volume": "12",
        "quote_volume": "1206",
        "trade_count": 7,
        "taker_buy_volume": "6",
        "is_closed": True,
    }
    assert transport.calls[0]["params"] == {"symbol": "BTCUSDT", "interval": "1m", "limit": 2}


@pytest.mark.asyncio
async def test_exchange_info_supports_min_notional_and_notional_filter_shapes() -> None:
    for filter_row, expected in (
        ({"filterType": "MIN_NOTIONAL", "notional": "5"}, "5"),
        ({"filterType": "NOTIONAL", "minNotional": "7"}, "7"),
    ):
        transport = _SequenceTransport(
            [
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                                filter_row,
                            ],
                        }
                    ]
                }
            ]
        )
        adapter = BinanceUsdmAdapter(rest_client=transport)

        result = await adapter.fetch_exchange_info()
        snapshot = adapter.get_rule_snapshot("BTCUSDT")
        reference = BinanceReferenceData(venue_id=adapter.venue_id)
        reference.instruments[InstrumentId("BTCUSDT")] = {
            "filters": [filter_row],
        }

        assert result.is_success() is True
        assert snapshot.is_known is True
        assert snapshot.min_notional == expected
        assert reference.get_min_notional(InstrumentId("BTCUSDT")) == expected


def _order_request() -> OrderRequest:
    return OrderRequest(
        venue_instrument=VenueInstrument(
            venue_id=VenueId("BINANCE"),
            instrument_id=InstrumentId("BTCUSDT"),
        ),
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("dedicated-testnet-account")),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="0.001"),
        client_order_id="cid-contract-1",
    )


@pytest.mark.asyncio
async def test_deterministic_rejection_is_distinct_from_ambiguous_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "unknown-only")
    deterministic = Result.failure(
        "insufficient balance",
        category=ErrorCategory.INSUFFICIENT_BALANCE,
        raw={"code": -2010, "msg": "insufficient balance"},
    )
    ambiguous = Result.failure(
        "transport unavailable",
        category=ErrorCategory.UNKNOWN,
        raw={"code": -1006, "msg": "unknown"},
    )
    transport = _SequenceTransport([deterministic, ambiguous])
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)

    rejected = await adapter.create_order(_order_request())
    unknown = await adapter.create_order(_order_request())

    assert rejected.status.value == "REJECTED"
    assert rejected.order_id == ""
    assert rejected.raw_response is not None and rejected.raw_response["code"] == -2010
    assert unknown.status.value == "UNKNOWN"
    assert unknown.order_id == ""
    assert unknown.raw_response is not None and unknown.raw_response["code"] == -1006
    assert len(transport.calls) == 2
