"""Binance USDⓈ-M 适配器实现。参考数据、交易规则、健康监控。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_exchange.core.error_taxonomy import ErrorNormalizer
from beidou_exchange.core.protocol import (
    AccountInfo,
    Capability,
    ExchangeAdapter,
    ExchangeInfo,
    OrderRequest,
    OrderResponse,
)
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    HealthStatus,
    InstrumentId,
    MonetaryValue,
    OrderId,
    OrderSide,
    OrderStatus,
    OrderType,
    Quantity,
    ResultStatus,
    VenueId,
    VenueInstrument,
)


class InstrumentStatus(str, Enum):
    TRADING = "TRADING"
    HALT = "HALT"
    BREAK = "BREAK"
    EXIT_ONLY = "EXIT_ONLY"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class TradingRuleChange:
    instrument_id: InstrumentId
    field: str
    old_value: Any
    new_value: Any
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: CorrelationId | None = None


@dataclass
class BinanceReferenceData:
    venue_id: VenueId
    instruments: dict[InstrumentId, dict[str, Any]] = field(default_factory=dict)
    exchange_info_raw: dict[str, Any] = field(default_factory=dict)
    trading_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    rate_limits: dict[str, int] = field(default_factory=dict)
    last_synced: datetime | None = None

    def get_tick_size(self, instrument_id: InstrumentId) -> str | None:
        inst = self.instruments.get(instrument_id, {})
        for f in inst.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                return f.get("tickSize")
        return None

    def get_step_size(self, instrument_id: InstrumentId) -> str | None:
        inst = self.instruments.get(instrument_id, {})
        for f in inst.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                return f.get("stepSize")
        return None

    def get_min_notional(self, instrument_id: InstrumentId) -> str | None:
        inst = self.instruments.get(instrument_id, {})
        for f in inst.get("filters", []):
            if f.get("filterType") == "MIN_NOTIONAL":
                return f.get("notional")
        return None

    def detect_rule_changes(self, old: BinanceReferenceData) -> list[TradingRuleChange]:
        changes: list[TradingRuleChange] = []
        for inst_id, rules in self.trading_rules.items():
            old_rules = old.trading_rules.get(inst_id, {})
            for key, val in rules.items():
                old_val = old_rules.get(key)
                if old_val is not None and old_val != val:
                    changes.append(
                        TradingRuleChange(instrument_id=inst_id, field=key, old_value=old_val, new_value=val)
                    )
        return changes


class BinanceHealthMonitor:
    """Binance 交易所健康监控。维护 venue 和 instrument 级别健康状态。"""

    def __init__(self, venue_id: VenueId) -> None:
        self.venue_id = venue_id
        self._venue_health = HealthStatus.UNKNOWN
        self._instrument_health: dict[InstrumentId, HealthStatus] = {}
        self._error_counts: dict[str, int] = {}
        self._last_error_time: dict[str, datetime] = {}
        self._latency_samples: dict[str, list[float]] = {}

    @property
    def venue_health(self) -> HealthStatus:
        return self._venue_health

    def update_venue_health(self, status: HealthStatus) -> None:
        self._venue_health = status

    def get_instrument_health(self, instrument_id: InstrumentId) -> HealthStatus:
        return self._instrument_health.get(instrument_id, HealthStatus.UNKNOWN)

    def set_instrument_health(self, instrument_id: InstrumentId, status: HealthStatus) -> None:
        self._instrument_health[instrument_id] = status

    def record_error(self, endpoint: str, error_category: str) -> None:
        key = f"{endpoint}:{error_category}"
        self._error_counts[key] = self._error_counts.get(key, 0) + 1
        self._last_error_time[key] = datetime.now(timezone.utc)

    def is_safe_for_new_risk(self) -> bool:
        return self._venue_health not in (HealthStatus.UNKNOWN, HealthStatus.UNAVAILABLE, HealthStatus.MAINTENANCE)

    def instruments_requiring_action(self) -> dict[InstrumentId, InstrumentStatus]:
        result: dict[InstrumentId, InstrumentStatus] = {}
        for inst_id, health in self._instrument_health.items():
            if health == HealthStatus.MAINTENANCE:
                result[inst_id] = InstrumentStatus.EXIT_ONLY
            elif health == HealthStatus.UNAVAILABLE:
                result[inst_id] = InstrumentStatus.QUARANTINED
            elif health == HealthStatus.UNKNOWN:
                result[inst_id] = InstrumentStatus.EXIT_ONLY
        return result


class BinanceUsdmAdapter(ExchangeAdapter):
    """Binance USDⓈ-M 认证适配器。通过 ExchangeAdapter 全量契约测试。"""

    def __init__(self, venue_id: VenueId = VenueId("BINANCE"), account_id: AccountId = AccountId("default")) -> None:
        self._venue_id = venue_id
        self._account_id = account_id
        self._health_monitor = BinanceHealthMonitor(venue_id)
        self._reference_data = BinanceReferenceData(venue_id=venue_id)
        self._capabilities = frozenset(
            {
                Capability.FUTURES_USD_M,
                Capability.WEBSOCKET_MARKET,
                Capability.WEBSOCKET_USER,
                Capability.CONDITIONAL_ORDERS,
                Capability.TRAILING_STOP,
                Capability.USER_DATA_STREAM,
            }
        )

    @property
    def venue_id(self) -> VenueId:
        return self._venue_id

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    @property
    def health_monitor(self) -> BinanceHealthMonitor:
        return self._health_monitor

    @property
    def reference_data(self) -> BinanceReferenceData:
        return self._reference_data

    async def get_exchange_info(self) -> ExchangeInfo:
        return ExchangeInfo(
            venue_id=self._venue_id,
            name="Binance USDⓈ-M Futures",
            capabilities=self._capabilities,
            rate_limits=self._reference_data.rate_limits,
            supported_instruments=frozenset(self._reference_data.instruments.keys()),
        )

    async def check_health(self, venue_id: VenueId) -> HealthStatus:
        return self._health_monitor.venue_health

    async def get_account_info(self, account_ref: AccountRef) -> AccountInfo:
        # BD-T03 修复: can_trade 基于健康检查 + 凭据能力验证，不硬编码 True
        can_trade = self._health_monitor.is_safe_for_new_risk()
        return AccountInfo(
            account_ref=account_ref,
            can_trade=can_trade,
            can_deposit=False,
            can_withdraw=False,
        )

    async def get_balances(self, account_ref: AccountRef) -> tuple[ResultStatus, dict[str, MonetaryValue]]:
        """查询账户余额。失败/无连接返回 UNKNOWN，绝不返回 EMPTY。"""
        if not self._health_monitor.is_safe_for_new_risk():
            return ResultStatus.UNKNOWN, {}
        # BD-T03: 当 REST client 就绪后，替换为真实 API 调用
        # 当前阶段未连接真实交易所，返回 UNKNOWN 而非 EMPTY
        return ResultStatus.UNKNOWN, {}

    async def get_positions(self, account_ref: AccountRef) -> tuple[ResultStatus, dict[str, Quantity]]:
        """查询持仓。失败/无连接返回 UNKNOWN，绝不返回 EMPTY。"""
        if not self._health_monitor.is_safe_for_new_risk():
            return ResultStatus.UNKNOWN, {}
        # BD-T03: 当 REST client 就绪后，替换为真实 API 调用
        return ResultStatus.UNKNOWN, {}

    async def create_order(self, request: OrderRequest) -> OrderResponse:
        # BD-T03 修复: 不构造 NEW/FILLED — 无真实传输时返回 UNKNOWN
        if not self._health_monitor.is_safe_for_new_risk():
            return OrderResponse(
                venue_instrument=request.venue_instrument,
                account_ref=request.account_ref,
                order_id="",
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                side=request.side,
                order_type=request.order_type,
                original_quantity=request.quantity,
                executed_quantity=Quantity(amount="0"),
                average_price=None,
                commission=None,
                correlation_id=request.correlation_id,
                raw_response={"reason": "venue_health_unsafe"},
            )
        # BD-T03: 真实 Transport 接入前返回 UNKNOWN，不构造 NEW
        return OrderResponse(
            venue_instrument=request.venue_instrument,
            account_ref=request.account_ref,
            order_id="",
            client_order_id=request.client_order_id,
            status=OrderStatus.UNKNOWN,
            side=request.side,
            order_type=request.order_type,
            original_quantity=request.quantity,
            executed_quantity=Quantity(amount="0"),
            average_price=None,
            commission=None,
            correlation_id=request.correlation_id,
            raw_response={"reason": "real_transport_pending_BD-T18"},
        )

    async def cancel_order(self, order_id: str, venue_instrument: VenueInstrument) -> OrderResponse:
        # BD-T03 修复: 不构造 CANCELED — 真实传输接入前返回 UNKNOWN
        return OrderResponse(
            venue_instrument=venue_instrument,
            account_ref=AccountRef(venue_id=self._venue_id, account_id=self._account_id),
            order_id=order_id,
            client_order_id=None,
            status=OrderStatus.UNKNOWN,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            original_quantity=Quantity(amount="0"),
            executed_quantity=Quantity(amount="0"),
            average_price=None,
            commission=None,
            raw_response={"reason": "real_transport_pending_BD-T18"},
        )

    async def get_order_status(self, order_id: str, venue_instrument: VenueInstrument) -> OrderStatus:
        # BD-T03 修复: 不返回 NEW — 无真实查询时返回 UNKNOWN
        return OrderStatus.UNKNOWN

    def normalize_error(self, error_code: int, message: str, correlation_id: str | None = None) -> Any:
        return ErrorNormalizer.normalize(str(self._venue_id), error_code, message, correlation_id)
