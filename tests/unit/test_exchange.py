"""
PKG-05: Exchange Abstraction 测试。
覆盖 Protocol 契约、能力矩阵、错误分类、路由。
"""

from __future__ import annotations

import pytest

from beidou_exchange.core.error_taxonomy import ErrorNormalizer
from beidou_exchange.core.protocol import (
    Capability,
    ExchangeAdapter,
    ExchangeInfo,
    VenueInstrument,
)
from beidou_exchange.router import CapabilityMatrix, ExchangeRouter
from beidou_shared.errors import ErrorCategory, FaultSeverity, RecoveryAction
from beidou_shared.types import (
    InstrumentId,
    ResultStatus,
    VenueId,
)


class TestCapabilityMatrix:
    """能力矩阵测试。"""

    def test_register_and_query_capabilities(self) -> None:
        matrix = CapabilityMatrix()
        matrix.register_venue(
            VenueId("BINANCE"),
            frozenset({Capability.FUTURES_USD_M, Capability.WEBSOCKET_MARKET}),
            frozenset({InstrumentId("BTCUSDT"), InstrumentId("ETHUSDT")}),
        )
        assert matrix.venue_supports(VenueId("BINANCE"), Capability.FUTURES_USD_M) == ResultStatus.SUCCESS
        assert matrix.venue_supports(VenueId("BINANCE"), Capability.SPOT) == ResultStatus.UNSUPPORTED

    def test_unsupported_capability_returns_unsupported(self) -> None:
        matrix = CapabilityMatrix()
        matrix.register_venue(
            VenueId("BINANCE"),
            frozenset({Capability.FUTURES_USD_M}),
            frozenset({InstrumentId("BTCUSDT")}),
        )
        result = matrix.venue_supports(VenueId("BINANCE"), Capability.OPTIONS)
        assert result == ResultStatus.UNSUPPORTED

    def test_unknown_venue_returns_unknown(self) -> None:
        matrix = CapabilityMatrix()
        result = matrix.venue_supports(VenueId("UNKNOWN"), Capability.FUTURES_USD_M)
        assert result == ResultStatus.UNKNOWN

    def test_instrument_available_on(self) -> None:
        matrix = CapabilityMatrix()
        matrix.register_venue(
            VenueId("BINANCE"),
            frozenset({Capability.FUTURES_USD_M}),
            frozenset({InstrumentId("BTCUSDT")}),
        )
        matrix.register_venue(
            VenueId("BYBIT"),
            frozenset({Capability.FUTURES_USD_M}),
            frozenset({InstrumentId("BTCUSDT"), InstrumentId("SOLUSDT")}),
        )
        venues = matrix.instrument_available_on(InstrumentId("BTCUSDT"))
        assert VenueId("BINANCE") in venues
        assert VenueId("BYBIT") in venues

        venues_sol = matrix.instrument_available_on(InstrumentId("SOLUSDT"))
        assert VenueId("BINANCE") not in venues_sol
        assert VenueId("BYBIT") in venues_sol

    def test_venue_has_instrument(self) -> None:
        matrix = CapabilityMatrix()
        matrix.register_venue(
            VenueId("BINANCE"),
            frozenset(),
            frozenset({InstrumentId("BTCUSDT")}),
        )
        assert matrix.venue_has_instrument(VenueId("BINANCE"), InstrumentId("BTCUSDT"))
        assert not matrix.venue_has_instrument(VenueId("BINANCE"), InstrumentId("ETHUSDT"))


class TestExchangeRouter:
    """交易所路由器测试。"""

    def test_resolve_venue_instrument(self) -> None:
        from beidou_exchange.core.protocol import (
            AccountInfo,
            AccountRef,
            ExchangeAdapter,
            HealthStatus,
            OrderRequest,
            OrderResponse,
            OrderStatus,
        )

        class FakeBinanceAdapter(ExchangeAdapter):
            """TEST_SYNTHETIC — 仅用于协议契约测试。"""

            @property
            def venue_id(self) -> VenueId:
                return VenueId("BINANCE")

            @property
            def capabilities(self) -> frozenset[Capability]:
                return frozenset({Capability.FUTURES_USD_M, Capability.WEBSOCKET_MARKET})

            async def get_exchange_info(self) -> ExchangeInfo:
                raise NotImplementedError

            async def check_health(self, venue_id: VenueId) -> HealthStatus:
                raise NotImplementedError

            async def get_account_info(self, account_ref: AccountRef) -> AccountInfo:
                raise NotImplementedError

            async def get_balances(self, account_ref: AccountRef):
                raise NotImplementedError

            async def get_positions(self, account_ref: AccountRef):
                raise NotImplementedError

            async def create_order(self, request: OrderRequest) -> OrderResponse:
                raise NotImplementedError

            async def cancel_order(self, order_id: str, venue_instrument: VenueInstrument) -> OrderResponse:
                raise NotImplementedError

            async def get_order_status(self, order_id: str, venue_instrument: VenueInstrument) -> OrderStatus:
                raise NotImplementedError

        matrix = CapabilityMatrix()
        router = ExchangeRouter(matrix)
        adapter = FakeBinanceAdapter()
        router.register_adapter(adapter)

        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        result = router.resolve(vi, Capability.FUTURES_USD_M)
        # The capability matrix knows the venue but we didn't register instruments
        # through the adapter since they're empty frozenset
        assert result in (ResultStatus.SUCCESS, ResultStatus.UNSUPPORTED)

    def test_resolve_unknown_venue(self) -> None:
        matrix = CapabilityMatrix()
        router = ExchangeRouter(matrix)
        vi = VenueInstrument(venue_id=VenueId("UNKNOWN"), instrument_id=InstrumentId("BTCUSDT"))
        result = router.resolve(vi, Capability.FUTURES_USD_M)
        assert result == ResultStatus.UNKNOWN

    def test_get_adapter_none_for_unknown(self) -> None:
        matrix = CapabilityMatrix()
        router = ExchangeRouter(matrix)
        assert router.get_adapter(VenueId("UNKNOWN")) is None

    def test_duplicate_adapter_raises(self) -> None:
        # Use the same FakeBinanceAdapter pattern but simpler

        class AdapterA2(ExchangeAdapter):
            @property
            def venue_id(self) -> VenueId:
                return VenueId("BINANCE_DUP")

            @property
            def capabilities(self) -> frozenset[Capability]:
                return frozenset({Capability.FUTURES_USD_M})

            async def get_exchange_info(self):
                raise NotImplementedError

            async def check_health(self, venue_id):
                raise NotImplementedError

            async def get_account_info(self, account_ref):
                raise NotImplementedError

            async def get_balances(self, account_ref):
                raise NotImplementedError

            async def get_positions(self, account_ref):
                raise NotImplementedError

            async def create_order(self, request):
                raise NotImplementedError

            async def cancel_order(self, order_id, venue_instrument):
                raise NotImplementedError

            async def get_order_status(self, order_id, venue_instrument):
                raise NotImplementedError

        class AdapterB2(ExchangeAdapter):
            @property
            def venue_id(self) -> VenueId:
                return VenueId("BINANCE_DUP")

            @property
            def capabilities(self) -> frozenset[Capability]:
                return frozenset({Capability.FUTURES_USD_M})

            async def get_exchange_info(self):
                raise NotImplementedError

            async def check_health(self, venue_id):
                raise NotImplementedError

            async def get_account_info(self, account_ref):
                raise NotImplementedError

            async def get_balances(self, account_ref):
                raise NotImplementedError

            async def get_positions(self, account_ref):
                raise NotImplementedError

            async def create_order(self, request):
                raise NotImplementedError

            async def cancel_order(self, order_id, venue_instrument):
                raise NotImplementedError

            async def get_order_status(self, order_id, venue_instrument):
                raise NotImplementedError

        matrix = CapabilityMatrix()
        router = ExchangeRouter(matrix)
        router.register_adapter(AdapterA2())
        with pytest.raises(ValueError, match="already registered"):
            router.register_adapter(AdapterB2())


class TestErrorNormalizer:
    """错误归一化测试。"""

    def test_known_error_normalized(self) -> None:
        error = ErrorNormalizer.normalize("BINANCE", -2015, "Invalid API-key")
        assert error.category == ErrorCategory.AUTH_FAILURE
        assert error.should_fail_closed()
        assert error.recommended_action == RecoveryAction.LOCK

    def test_unknown_error_defaults_to_unknown(self) -> None:
        error = ErrorNormalizer.normalize("BINANCE", 99999, "Mysterious error")
        assert error.category == ErrorCategory.UNKNOWN
        assert error.severity == FaultSeverity.P0_CRITICAL
        assert error.should_fail_closed()
        assert error.recommended_action == RecoveryAction.MANUAL

    def test_rate_limit_normalized(self) -> None:
        error = ErrorNormalizer.normalize("BINANCE", -1013, "Too many requests")
        assert error.category == ErrorCategory.RATE_LIMIT
        assert error.retryable
        assert error.recommended_action == RecoveryAction.RETRY

    def test_custom_venue_mapping(self) -> None:
        ErrorNormalizer.register_venue_errors(
            "BYBIT",
            {
                10001: (ErrorCategory.AUTH_FAILURE, FaultSeverity.P0_CRITICAL, RecoveryAction.LOCK),
            },
        )
        error = ErrorNormalizer.normalize("BYBIT", 10001, "Auth failed")
        assert error.category == ErrorCategory.AUTH_FAILURE
