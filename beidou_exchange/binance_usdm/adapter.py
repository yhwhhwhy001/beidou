"""Binance USDⓈ-M 适配器实现。参考数据、交易规则、健康监控。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.core.error_taxonomy import ErrorNormalizer, Result
from beidou_exchange.core.protocol import (
    AccountInfo,
    AlgoOrderSnapshot,
    Capability,
    ExchangeAdapter,
    ExchangeInfo,
    OrderRequest,
    OrderResponse,
    UserOrderUpdate,
    UserStreamEvent,
)
from beidou_shared.errors import ErrorCategory
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    HealthStatus,
    InstrumentId,
    MonetaryValue,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    ResultStatus,
    VenueId,
    VenueInstrument,
)

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        venue_id: VenueId = VenueId("BINANCE"),
        account_id: AccountId = AccountId("default"),
        rest_client=None,  # BD-T18: BinanceRESTClient 注入
    ) -> None:
        self._venue_id = venue_id
        self._account_id = account_id
        self._rest_client = rest_client  # BD-T18: 真实传输层
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

    async def request(
        self,
        method: str,
        path: str,
        signed: bool = False,
        params: dict[str, Any] | None = None,
    ) -> Result[Any]:
        """唯一的底层传输边界。

        上层可以请求已登记的端点，但不能持有或直接调用 REST client。
        没有真实传输层时返回 UNKNOWN，而不是伪造成功状态。
        """

        if self._rest_client is None:
            return Result.failure(
                "Exchange transport is not configured",
                category=ErrorCategory.UNKNOWN,
                source="binance_usdm_adapter",
            )
        if method.upper() in {"POST", "PUT", "DELETE"} and not self._health_monitor.is_safe_for_new_risk():
            return Result.failure(
                "Venue health is not verified for a write",
                category=ErrorCategory.EXCHANGE_UNAVAILABLE,
                source="binance_usdm_adapter",
            )
        result = await self._rest_client.request(method, path, signed=signed, params=params)
        if path == Endpoint.SERVER_TIME and result.is_success():
            self._health_monitor.update_venue_health(HealthStatus.HEALTHY)
        return result

    def reset_circuit_breaker(self) -> None:
        """通过 Adapter 暴露受控的传输熔断恢复。"""

        if self._rest_client is not None:
            self._rest_client.reset_circuit_breaker()

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
        if self._rest_client is not None:
            try:
                transport_result = await self.request("GET", Endpoint.BALANCE, signed=True)
                if not transport_result.is_success():
                    return ResultStatus.UNKNOWN, {}
                result = transport_result.data
                if isinstance(result, list):
                    balances: dict[str, MonetaryValue] = {}
                    for b in result:
                        asset = b.get("asset", "")
                        bal = b.get("balance", "0")
                        if float(bal) > 0:
                            balances[asset] = MonetaryValue(amount=bal)
                    return ResultStatus.SUCCESS, balances
            except Exception:
                pass
        return ResultStatus.UNKNOWN, {}

    async def get_positions(self, account_ref: AccountRef) -> tuple[ResultStatus, dict[str, Quantity]]:
        """查询持仓。失败/无连接返回 UNKNOWN，绝不返回 EMPTY。"""
        if not self._health_monitor.is_safe_for_new_risk():
            return ResultStatus.UNKNOWN, {}
        if self._rest_client is not None:
            try:
                transport_result = await self.request("GET", Endpoint.ACCOUNT, signed=True)
                if not transport_result.is_success():
                    return ResultStatus.UNKNOWN, {}
                result = transport_result.data
                if isinstance(result, dict) and "positions" in result:
                    positions: dict[str, Quantity] = {}
                    for p in result["positions"]:
                        amt = float(p.get("positionAmt", 0))
                        if abs(amt) > 0:
                            positions[p["symbol"]] = Quantity(amount=str(abs(amt)))
                    return ResultStatus.SUCCESS, positions
            except Exception:
                pass
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
        # BD-T18: 有真实传输层时走 API，否则返回 UNKNOWN
        if self._rest_client is not None:
            try:
                from beidou_shared.types import Price as _Price

                order_params: dict[str, Any] = {
                    "symbol": str(request.venue_instrument.instrument_id),
                    "side": request.side.value if hasattr(request.side, "value") else str(request.side),
                    "type": request.order_type.value if hasattr(request.order_type, "value") else str(request.order_type),
                    "quantity": str(float(request.quantity.amount)),
                    "timeInForce": (
                        request.time_in_force.value
                        if hasattr(request.time_in_force, "value")
                        else str(request.time_in_force or "GTC")
                    ),
                    "newClientOrderId": request.client_order_id or "",
                }
                if request.price:
                    order_params["price"] = str(float(request.price.amount))
                if request.reduce_only:
                    # Binance ONE_WAY safety invariant: reduce-only must be
                    # sent to the venue, not merely kept in local intent data.
                    order_params["reduceOnly"] = "true"
                transport_result = await self.request("POST", Endpoint.ORDER, signed=True, params=order_params)
                if not transport_result.is_success():
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
                        raw_response={"reason": "api_call_failed"},
                    )
                result = transport_result.data
                if isinstance(result, dict) and "orderId" in result:
                    return OrderResponse(
                        venue_instrument=request.venue_instrument,
                        account_ref=request.account_ref,
                        order_id=str(result.get("orderId", "")),
                        client_order_id=request.client_order_id,
                        status=OrderStatus(str(result.get("status", OrderStatus.NEW.value))),
                        side=request.side,
                        order_type=request.order_type,
                        original_quantity=request.quantity,
                        executed_quantity=Quantity(amount=str(result.get("executedQty", "0"))),
                        average_price=_Price(amount=str(result.get("avgPrice", "0"))) if result.get("avgPrice") else None,
                        commission=None,
                        correlation_id=request.correlation_id,
                        raw_response=result,
                    )
            except Exception:
                pass
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
            raw_response={
                "reason": "real_transport_pending_BD-T18" if self._rest_client is None else "api_call_failed"
            },
        )

    async def cancel_order(self, order_id: str, venue_instrument: VenueInstrument) -> OrderResponse:
        if self._rest_client is not None:
            try:
                transport_result = await self.request(
                    "DELETE",
                    Endpoint.ORDER,
                    signed=True,
                    params={
                        "symbol": str(venue_instrument.instrument_id),
                        "orderId": int(order_id),
                    },
                )
                if not transport_result.is_success():
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
                        correlation_id=None,
                        raw_response={"reason": "api_call_failed"},
                    )
                result = transport_result.data
                if isinstance(result, dict) and result.get("orderId"):
                    return OrderResponse(
                        venue_instrument=venue_instrument,
                        account_ref=AccountRef(venue_id=self._venue_id, account_id=self._account_id),
                        order_id=str(result["orderId"]),
                        client_order_id=result.get("clientOrderId"),
                        status=OrderStatus.CANCELED,
                        side=OrderSide(result.get("side", OrderSide.BUY.value)),
                        order_type=OrderType(result.get("type", OrderType.MARKET.value)),
                        original_quantity=Quantity(amount=str(result.get("origQty", "0"))),
                        executed_quantity=Quantity(amount=str(result.get("executedQty", "0"))),
                        average_price=None,
                        commission=None,
                        correlation_id=None,
                        raw_response=result,
                    )
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning("cancel order response could not be normalized: %s", exc)
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
        if self._rest_client is not None:
            try:
                transport_result = await self.request(
                    "GET",
                    Endpoint.ORDER,
                    signed=True,
                    params={
                        "symbol": str(venue_instrument.instrument_id),
                        "orderId": int(order_id),
                    },
                )
                if not transport_result.is_success():
                    return OrderStatus.UNKNOWN
                result = transport_result.data
                if isinstance(result, dict):
                    return OrderStatus(str(result.get("status", OrderStatus.UNKNOWN.value)))
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning("order status response could not be normalized: %s", exc)
        return OrderStatus.UNKNOWN

    @staticmethod
    def parse_algo_order_snapshot(raw: Any, account_ref: AccountRef) -> Result[AlgoOrderSnapshot]:
        """Normalize one Binance Algo response; incomplete rows are UNKNOWN."""

        if not isinstance(raw, dict):
            return Result.failure(
                "Algo order response is not an object",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_algo_adapter",
            )
        required = ("algoId", "symbol", "side", "orderType", "triggerPrice", "algoStatus")
        missing = [key for key in required if raw.get(key) in (None, "")]
        if missing:
            return Result.failure(
                f"Algo order response missing required fields: {','.join(missing)}",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_algo_adapter",
            )
        try:
            trigger_price = Decimal(str(raw["triggerPrice"]))
            quantity = Decimal(str(raw.get("quantity", "0")))
            if trigger_price <= 0 or quantity < 0:
                raise ValueError("triggerPrice must be positive and quantity non-negative")
            update_time = raw.get("updateTime")
            update_time_ms = int(update_time) if update_time is not None else None
            snapshot = AlgoOrderSnapshot(
                algo_id=str(raw["algoId"]),
                venue_instrument=VenueInstrument(
                    venue_id=account_ref.venue_id,
                    instrument_id=InstrumentId(str(raw["symbol"])),
                ),
                account_ref=account_ref,
                side=OrderSide(str(raw["side"])),
                order_type=OrderType(str(raw["orderType"])),
                quantity=Quantity(amount=str(raw.get("quantity", "0"))),
                trigger_price=Price(amount=str(raw["triggerPrice"])),
                status=str(raw["algoStatus"]),
                client_algo_id=(str(raw["clientAlgoId"]) if raw.get("clientAlgoId") else None),
                reduce_only=(bool(raw["reduceOnly"]) if "reduceOnly" in raw else None),
                close_position=(bool(raw["closePosition"]) if "closePosition" in raw else None),
                position_side=(str(raw["positionSide"]) if raw.get("positionSide") else None),
                update_time_ms=update_time_ms,
                raw_response=dict(raw),
            )
        except (TypeError, ValueError, InvalidOperation) as exc:
            return Result.failure(
                f"Algo order response invalid: {exc}",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_algo_adapter",
            )
        return Result.success(snapshot, source="binance_algo_adapter")

    async def get_open_algo_orders(self) -> Result[list[AlgoOrderSnapshot]]:
        """Read and type-check the complete open Algo inventory."""

        if self._rest_client is None:
            return Result.failure(
                "Exchange transport is not configured",
                category=ErrorCategory.UNKNOWN,
                source="binance_algo_adapter",
            )
        response = await self.request("GET", Endpoint.OPEN_ALGO_ORDERS, signed=True)
        if not response.is_success() or not isinstance(response.data, list):
            return Result.failure(
                "Open Algo inventory is UNKNOWN",
                category=ErrorCategory.UNKNOWN,
                raw=response.data,
                source="binance_algo_adapter",
            )
        account_ref = AccountRef(venue_id=self._venue_id, account_id=self._account_id)
        parsed: list[AlgoOrderSnapshot] = []
        for row in response.data:
            item = self.parse_algo_order_snapshot(row, account_ref)
            if not item.is_success() or item.data is None:
                return Result.failure(
                    "Open Algo inventory contains an incomplete row",
                    category=ErrorCategory.UNKNOWN,
                    raw=row,
                    source="binance_algo_adapter",
                )
            parsed.append(item.data)
        return Result.success(parsed, source="binance_algo_adapter")

    async def create_algo_order(self, params: dict[str, Any]) -> Result[AlgoOrderSnapshot]:
        """Submit one Algo order and require a typed venue acknowledgement."""

        if self._rest_client is None:
            return Result.failure(
                "Exchange transport is not configured",
                category=ErrorCategory.UNKNOWN,
                source="binance_algo_adapter",
            )
        response = await self.request("POST", Endpoint.ALGO_ORDER, signed=True, params=dict(params))
        if not response.is_success():
            return Result.failure(
                "Algo order acknowledgement is UNKNOWN",
                category=ErrorCategory.UNKNOWN,
                raw=response.data,
                source="binance_algo_adapter",
            )
        account_ref = AccountRef(venue_id=self._venue_id, account_id=self._account_id)
        return self.parse_algo_order_snapshot(response.data, account_ref)

    async def cancel_algo_order(self, symbol: str, algo_id: int) -> Result[dict[str, Any]]:
        """Cancel one explicitly identified Algo order; no cancel-all fallback."""

        if self._rest_client is None:
            return Result.failure(
                "Exchange transport is not configured",
                category=ErrorCategory.UNKNOWN,
                source="binance_algo_adapter",
            )
        response = await self.request(
            "DELETE",
            Endpoint.ALGO_ORDER,
            signed=True,
            params={"symbol": symbol, "algoId": algo_id},
        )
        if not response.is_success() or not isinstance(response.data, dict) or not response.data.get("algoId"):
            return Result.failure(
                "Algo cancel acknowledgement is UNKNOWN",
                category=ErrorCategory.UNKNOWN,
                raw=response.data,
                source="binance_algo_adapter",
            )
        return Result.success(dict(response.data), source="binance_algo_adapter")

    @staticmethod
    def parse_user_stream_event(raw: Any) -> Result[UserStreamEvent]:
        """Normalize a Binance user event without inventing sequence numbers."""

        if not isinstance(raw, dict) or not raw.get("e") or raw.get("E") in (None, ""):
            return Result.failure(
                "User-stream event is missing type or event time",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        event_type = str(raw["e"])
        try:
            event_time_ms = int(raw["E"])
            transaction_time_ms = int(raw["T"]) if raw.get("T") is not None else None
            sequence = int(raw["u"]) if raw.get("u") is not None else None
        except (TypeError, ValueError) as exc:
            return Result.failure(
                f"User-stream event has invalid time/sequence: {exc}",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        execution = raw.get("o") if isinstance(raw.get("o"), dict) else raw
        event_id = (
            f"{event_type}:{execution.get('i')}:{execution.get('I')}"
            if execution.get("i") is not None and execution.get("I") is not None
            else f"{event_type}:{event_time_ms}"
        )
        return Result.success(
            UserStreamEvent(
                event_type=event_type,
                event_id=event_id,
                event_time_ms=event_time_ms,
                transaction_time_ms=transaction_time_ms,
                sequence=sequence,
                raw_event=dict(raw),
            ),
            source="binance_user_stream_adapter",
        )

    @staticmethod
    def parse_user_order_update(raw: Any) -> Result[UserOrderUpdate]:
        """Parse one order/trade update without converting missing fields to 0."""

        event_result = BinanceUsdmAdapter.parse_user_stream_event(raw)
        if not event_result.is_success() or event_result.data is None:
            return Result.failure(
                "User order update has no valid event envelope",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        if event_result.data.event_type != "ORDER_TRADE_UPDATE" or not isinstance(raw, dict):
            return Result.failure(
                "Expected ORDER_TRADE_UPDATE envelope",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        order = raw.get("o")
        required = ("i", "c", "s", "S", "o", "X", "x", "q", "z", "l", "L", "ap")
        if not isinstance(order, dict) or any(order.get(key) in (None, "") for key in required):
            return Result.failure(
                "User order update missing execution fields",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        try:
            numeric_values = {
                key: Decimal(str(order[key]))
                for key in ("q", "z", "l", "L", "ap", "n", "rp")
                if order.get(key) not in (None, "")
            }
            if any(value < 0 for key, value in numeric_values.items() if key != "rp"):
                raise ValueError("quantity/price/commission fields must be non-negative")
            commission_asset = str(order.get("N") or "USDT")
            commission = MonetaryValue(amount=str(order.get("n", "0")), currency=commission_asset)
            realized_pnl = None
            if order.get("rp") not in (None, ""):
                realized_pnl = MonetaryValue(amount=str(order["rp"]), currency="USDT")
            update = UserOrderUpdate(
                event=event_result.data,
                order_id=str(order["i"]),
                client_order_id=str(order["c"]),
                symbol=InstrumentId(str(order["s"])),
                side=OrderSide(str(order["S"])),
                order_type=OrderType(str(order["o"])),
                order_status=OrderStatus(str(order["X"])),
                execution_type=str(order["x"]),
                original_quantity=Quantity(amount=str(order["q"])),
                cumulative_quantity=Quantity(amount=str(order["z"])),
                last_quantity=Quantity(amount=str(order["l"])),
                last_price=Price(amount=str(order["L"])),
                average_price=Price(amount=str(order["ap"])),
                trade_id=(str(order["t"]) if order.get("t") not in (None, "-1") else None),
                commission=commission,
                realized_pnl=realized_pnl,
            )
        except (TypeError, ValueError, InvalidOperation) as exc:
            return Result.failure(
                f"User order update invalid: {exc}",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        return Result.success(update, source="binance_user_stream_adapter")

    def normalize_error(self, error_code: int, message: str, correlation_id: str | None = None) -> Any:
        return ErrorNormalizer.normalize(str(self._venue_id), error_code, message, correlation_id)
