"""Binance USDⓈ-M 适配器实现。参考数据、交易规则、健康监控。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, cast

from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.write_guard import classify_terminal_write
from beidou_exchange.core.error_taxonomy import ErrorNormalizer, Result
from beidou_exchange.core.protocol import (
    AccountInfo,
    AlgoOrderSnapshot,
    Capability,
    ExchangeAdapter,
    ExchangeInfo,
    OrderRequest,
    OrderResponse,
    UserAccountUpdate,
    UserBalanceUpdate,
    UserOrderUpdate,
    UserPositionUpdate,
    UserStreamEvent,
)
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
from beidou_exchange.core.write_authority import TerminalWriteKind
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


def _format_decimal(value: str) -> str:
    """将数量/价格字符串转为无科学计数法的十进制字符串。"""

    try:
        d = Decimal(value)
        # 去除末尾零但保留至少一位小数
        formatted = f"{d:f}"
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
    except InvalidOperation:
        return value


def _safe_enum(enum_cls: Any, raw_value: str) -> Any:
    """BD-FIX: 未知枚举值降级为 UNKNOWN 而非抛 ValueError。

    共享 demo 账户的非常规订单类型（新类型/新状态）会让合法事件
    解析失败 → fault → 停机（I4/I6 审查）。降级保流；原始值可在
    raw_event 中审计。
    """
    try:
        return enum_cls(raw_value)
    except ValueError:
        return enum_cls("UNKNOWN")


def _strict_bool(value: Any, *, field_name: str) -> bool:
    """Parse venue booleans without Python's truthy-string trap."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{field_name} is not a valid boolean")


def _sanitized_adapter_error(error: Any, *, fallback: str) -> dict[str, Any]:
    """Expose recovery-relevant venue semantics without forwarding arbitrary payloads."""

    raw = getattr(error, "raw", None)
    raw_mapping = raw if isinstance(raw, dict) else {}
    category = getattr(error, "category", ErrorCategory.UNKNOWN)
    return {
        "code": raw_mapping.get("code", -1),
        "msg": str(raw_mapping.get("msg") or getattr(error, "message", fallback))[:500],
        "category": getattr(category, "value", str(category)),
        "retryable": bool(getattr(error, "retryable", False)),
        "http_status": int(getattr(error, "http_status", 0) or 0),
    }


class InstrumentStatus(str, Enum):
    TRADING = "TRADING"
    HALT = "HALT"
    BREAK = "BREAK"
    EXIT_ONLY = "EXIT_ONLY"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class TradingRuleChange:
    instrument_id: InstrumentId
    field_name: str
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
                return cast(str | None, f.get("tickSize"))
        return None

    def get_step_size(self, instrument_id: InstrumentId) -> str | None:
        inst = self.instruments.get(instrument_id, {})
        for f in inst.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                return cast(str | None, f.get("stepSize"))
        return None

    def get_min_notional(self, instrument_id: InstrumentId) -> str | None:
        inst = self.instruments.get(instrument_id, {})
        for f in inst.get("filters", []):
            if f.get("filterType") == "MIN_NOTIONAL":
                return cast(str | None, f.get("notional"))
        return None

    def detect_rule_changes(self, old: BinanceReferenceData) -> list[TradingRuleChange]:
        changes: list[TradingRuleChange] = []
        for inst_id, rules in self.trading_rules.items():
            old_rules = old.trading_rules.get(inst_id, {})
            for key, val in rules.items():
                old_val = old_rules.get(key)
                if old_val is not None and old_val != val:
                    changes.append(
                        TradingRuleChange(
                            instrument_id=InstrumentId(str(inst_id)), field_name=key, old_value=old_val, new_value=val
                        )
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
        # P2 修复 (健康动态更新): 连续传输失败计数 — 达阈值降级 UNAVAILABLE,
        # 任一成功请求恢复 HEALTHY,避免"只升不降"让瞬时不健康永久杀单。
        self._consecutive_transport_failures = 0

    @property
    def venue_health(self) -> HealthStatus:
        return self._venue_health

    def update_venue_health(self, status: HealthStatus) -> None:
        self._venue_health = status

    def record_transport_success(self) -> None:
        """任一真实请求成功 → HEALTHY 并复位连续失败计数。"""
        self._consecutive_transport_failures = 0
        self._venue_health = HealthStatus.HEALTHY

    def record_transport_failure(self) -> None:
        """连续传输失败达阈值 → UNAVAILABLE(下次成功自动恢复)。"""
        self._consecutive_transport_failures += 1
        if self._consecutive_transport_failures >= 3:
            self._venue_health = HealthStatus.UNAVAILABLE

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
        rest_client: Any = None,  # BD-T18: BinanceRESTClient 注入
    ) -> None:
        self._venue_id = venue_id
        self._account_id = account_id
        self._rest_client = rest_client  # BD-T18: 真实传输层
        self._health_monitor = BinanceHealthMonitor(venue_id)
        self._reference_data = BinanceReferenceData(venue_id=venue_id)
        # BD-CV10: InstrumentRuleSnapshot 缓存 — adapter 是唯一规则来源
        self._rule_snapshots: dict[str, InstrumentRuleSnapshot] = {}
        self._rule_snapshot_version: int = 0
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
        write_account_id: str | None = None,
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
        method_upper = method.upper()
        params = params or {}

        write_request = classify_terminal_write(
            method_upper,
            path,
            params,
            account_id=write_account_id or str(self._account_id),
        )
        # 与 rest_client 层保持同一 hold 语义(M22-F03):
        # - 默认 hard: 所有 terminal write 在 transport 层拦截,
        #   等待 write authority 接线
        # - BEIDOU_TERMINAL_WRITE_HOLD=unknown-only(testnet 实装):
        #   仅未分类突变端点(UNKNOWN) hold,已知 kind(下单/取消/
        #   减仓/保护/listen key)放行 —— 写能力不退化
        _hold_mode = os.environ.get("BEIDOU_TERMINAL_WRITE_HOLD", "hard")
        producer_session_write = (
            os.environ.get("BEIDOU_G5_PRODUCER") == "1"
            and write_request is not None
            and write_request.kind is TerminalWriteKind.SESSION_CONTROL
        )
        if write_request is not None and not producer_session_write and (
            _hold_mode == "hard" or write_request.kind is TerminalWriteKind.UNKNOWN
        ):
            reason = (
                "UNCLASSIFIED_TERMINAL_WRITE"
                if write_request.kind is TerminalWriteKind.UNKNOWN
                else "WRITE_CAPABILITY_REGISTRY_INCOMPLETE"
            )
            return Result.failure(
                "Terminal writes are held until the capability registry is complete",
                category=ErrorCategory.PERMISSION_DENIED,
                retryable=False,
                raw={"reason": reason, "kind": write_request.kind.value},
                source="binance_adapter_write_hold",
            )

        def enabled(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes"}

        # A venue-health UNKNOWN must block risk-increasing writes, but it
        # must not remove the only governed path for reducing an already
        # exposed position.  DELETE and explicit reduceOnly/closePosition
        # requests are exit-only; the transport may still return UNKNOWN, but
        # the adapter must not reject them solely because its health probe is
        # stale.  This mirrors the supervisor NO_NEW_RISK/EXIT_ONLY contract.
        exit_only = (
            method_upper == "DELETE" or enabled(params.get("reduceOnly")) or enabled(params.get("closePosition"))
        )
        if (
            method_upper in {"POST", "PUT", "DELETE"}
            and not self._health_monitor.is_safe_for_new_risk()
            and not exit_only
        ):
            return Result.failure(
                "Venue health is not verified for a write",
                category=ErrorCategory.EXCHANGE_UNAVAILABLE,
                source="binance_usdm_adapter",
            )
        result = await self._rest_client.request(method, path, signed=signed, params=params)
        # P2 修复 (venue 健康只升不降): 此前只有 SERVER_TIME 成功才置 HEALTHY,
        # 启动时钟同步失败 → 恒 UNKNOWN → 所有加仓单被健康门永久拒绝。
        # 改为: 任一真实请求成功即 HEALTHY;连续传输失败(网络/交易所不可用)
        # 达阈值才降级 UNAVAILABLE,下次成功自动恢复。
        if result.is_success():
            self._health_monitor.record_transport_success()
        else:
            _err_cat = str(getattr(getattr(result, "error", None), "category", "") or "")
            if _err_cat in {"NETWORK", "EXCHANGE_UNAVAILABLE", "TIMEOUT"}:
                self._health_monitor.record_transport_failure()
        return cast(Result[Any], result)

    def reset_circuit_breaker(self) -> None:
        """通过 Adapter 暴露受控的传输熔断恢复。"""

        if self._rest_client is not None:
            self._rest_client.reset_circuit_breaker()

    def is_circuit_breaker_open(self) -> bool:
        """检查传输断路器是否打开。"""
        if self._rest_client is None:
            return False
        return cast(bool, self._rest_client.is_circuit_breaker_open())

    async def create_user_listen_key(self) -> Result[dict[str, Any]]:
        """Create a user-data listen key through the adapter-owned transport.

        Listen-key lifecycle is a control-plane operation rather than an
        order write.  It still must return a typed, non-empty venue fact and
        must never be replaced by a fabricated key or a REST-only fallback.
        """

        if self._rest_client is None:
            return Result.failure(
                "Exchange transport is not configured",
                category=ErrorCategory.UNKNOWN,
                source="binance_user_stream_adapter",
            )
        result = await self._rest_client.create_listen_key()
        if not result.is_success() or not isinstance(result.data, dict) or not str(result.data.get("listenKey") or ""):
            return Result.failure(
                "User listenKey creation is UNKNOWN",
                category=ErrorCategory.UNKNOWN,
                raw=result.data,
                source="binance_user_stream_adapter",
            )
        return Result.success(dict(result.data), source="binance_user_stream_adapter")

    async def keepalive_user_listen_key(self, listen_key: str) -> Result[dict[str, Any]]:
        """Renew one exact listen key; missing identity fails closed."""

        if self._rest_client is None or not str(listen_key or "").strip():
            return Result.failure(
                "User listenKey identity is unavailable",
                category=ErrorCategory.UNKNOWN,
                source="binance_user_stream_adapter",
            )
        result = await self._rest_client.keepalive_listen_key(str(listen_key))
        if not result.is_success() or not isinstance(result.data, dict):
            return Result.failure(
                "User listenKey keepalive is UNKNOWN",
                category=ErrorCategory.UNKNOWN,
                raw=result.data,
                source="binance_user_stream_adapter",
            )
        return Result.success(dict(result.data or {}), source="binance_user_stream_adapter")

    async def get_exchange_info(self) -> ExchangeInfo:
        return ExchangeInfo(
            venue_id=self._venue_id,
            name="Binance USDⓈ-M Futures",
            capabilities=self._capabilities,
            rate_limits=self._reference_data.rate_limits,
            supported_instruments=frozenset(self._reference_data.instruments.keys()),
        )

    # --- BD-CV10: InstrumentRuleSnapshot — 唯一规则来源 ---

    def get_rule_snapshot(self, symbol: str) -> InstrumentRuleSnapshot:
        """BD-CV10: 获取交易对规则快照。

        从 adapter 缓存返回；规则 UNKNOWN 时返回 InstrumentRuleSnapshot.unknown(symbol)。
        AC-10-02: 未知规则无法产生可执行 OrderSlice/ProtectionOrder。
        """
        return self._rule_snapshots.get(symbol, InstrumentRuleSnapshot.unknown(symbol))

    def sync_rule_snapshots(self) -> int:
        """BD-CV10: 从 BinanceReferenceData 同步所有交易对规则快照。

        支持 exchangeInfo 变更检测和原子版本切换。
        返回新 snapshot_version。
        """
        snapshots: dict[str, InstrumentRuleSnapshot] = {}
        for inst_id, raw_data in self._reference_data.instruments.items():
            symbol = str(inst_id)
            snapshots[symbol] = InstrumentRuleSnapshot.from_exchange_info(symbol, raw_data)
        self._rule_snapshots = snapshots
        self._rule_snapshot_version += 1
        return self._rule_snapshot_version

    @property
    def rule_snapshot_version(self) -> int:
        """BD-CV10: 当前规则快照版本号（单调递增）。"""
        return self._rule_snapshot_version

    @property
    def rule_snapshot_count(self) -> int:
        """BD-CV10: 已缓存的规则快照数量。"""
        return len(self._rule_snapshots)

    async def check_health(self, venue_id: VenueId) -> HealthStatus:
        return self._health_monitor.venue_health

    async def get_account_info(self, account_ref: AccountRef) -> AccountInfo:
        """Read venue permission facts; never infer them from local health.

        ``AccountInfo`` has no ``UNKNOWN`` enum, so a missing/invalid venue
        response is represented conservatively as ``can_trade=False`` and
        ``can_withdraw=True``.  The latter intentionally fails the R9
        no-withdrawal rule instead of manufacturing a safe-looking ``False``.
        """

        can_trade = False
        can_withdraw = True
        can_deposit = False
        if self._rest_client is not None:
            try:
                result = await self.request("GET", Endpoint.ACCOUNT, signed=True)
                account = result.data if result.is_success() else None
                if isinstance(account, dict):
                    venue_can_trade = account.get("canTrade")
                    venue_can_withdraw = account.get("canWithdraw")
                    venue_can_deposit = account.get("canDeposit")
                    if isinstance(venue_can_trade, bool) and isinstance(venue_can_withdraw, bool):
                        can_trade = venue_can_trade and self._health_monitor.is_safe_for_new_risk()
                        can_withdraw = venue_can_withdraw
                        can_deposit = venue_can_deposit if isinstance(venue_can_deposit, bool) else False
            except Exception as exc:
                # Preserve the fail-closed defaults on transport/parser errors,
                # but retain an auditable reason instead of swallowing the
                # exception with a no-op handler.
                logger.warning(
                    "account capability query failed; returning fail-closed defaults: %s", type(exc).__name__
                )
        return AccountInfo(
            account_ref=account_ref,
            can_trade=can_trade,
            can_deposit=can_deposit,
            can_withdraw=can_withdraw,
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
                        if not isinstance(b, dict) or not b.get("asset") or b.get("balance") in (None, ""):
                            return ResultStatus.UNKNOWN, {}
                        try:
                            balance = Decimal(str(b["balance"]))
                        except (InvalidOperation, TypeError, ValueError):
                            return ResultStatus.UNKNOWN, {}
                        if not balance.is_finite():
                            return ResultStatus.UNKNOWN, {}
                        if balance != 0:
                            # Preserve a verified signed balance instead of
                            # silently dropping negative or malformed rows and
                            # turning a partial account response into EMPTY.
                            balances[str(b["asset"])] = MonetaryValue(amount=str(balance))
                    return ResultStatus.SUCCESS, balances
            except Exception as exc:
                self._health_monitor.record_error(Endpoint.BALANCE, type(exc).__name__)
                logger.warning("balance query failed; returning UNKNOWN: %s", type(exc).__name__)
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
                        if not isinstance(p, dict) or not p.get("symbol") or "positionAmt" not in p:
                            return ResultStatus.UNKNOWN, {}
                        amt = Decimal(str(p["positionAmt"]))
                        if not amt.is_finite():
                            return ResultStatus.UNKNOWN, {}
                        if amt != 0:
                            # Preserve Binance's signed position amount; the
                            # sign is the only authoritative direction fact
                            # for ONE_WAY mode and must not be erased at the
                            # Adapter boundary.
                            positions[str(p["symbol"])] = Quantity(amount=str(amt))
                    return ResultStatus.SUCCESS, positions
            except Exception as exc:
                self._health_monitor.record_error(Endpoint.ACCOUNT, type(exc).__name__)
                logger.warning("position query failed; returning UNKNOWN: %s", type(exc).__name__)
        return ResultStatus.UNKNOWN, {}

    @staticmethod
    def _validate_order_ack(request: OrderRequest, response: Any) -> tuple[bool, str]:
        """Validate that a venue ACK is the exact order that was requested.

        An ``orderId`` alone is not proof of acknowledgement: a malformed or
        mismatched response must remain UNKNOWN rather than being projected as
        a local order.  This is especially important after an ambiguous POST,
        where retrying with a different client id could duplicate exposure.
        """

        if not isinstance(response, dict):
            return False, "ACK_NOT_OBJECT"
        required = ("orderId", "status", "symbol", "side", "type", "origQty")
        if any(response.get(field) in (None, "") for field in required):
            return False, "ACK_IDENTITY_MISSING"
        # P2 修复: executedQty 缺失会产出 "FILLED + executed=0" 的自相矛盾
        # ACK —— 该字段是成交事实的必备成分,缺失即 UNKNOWN。
        if response.get("executedQty") in (None, ""):
            return False, "ACK_EXECUTED_QTY_MISSING"

        if str(response["symbol"]).upper() != str(request.venue_instrument.instrument_id).upper():
            return False, "ACK_SYMBOL_MISMATCH"
        expected_side = request.side.value if hasattr(request.side, "value") else str(request.side)
        if str(response["side"]).upper() != expected_side.upper():
            return False, "ACK_SIDE_MISMATCH"
        expected_type = request.order_type.value if hasattr(request.order_type, "value") else str(request.order_type)
        if str(response["type"]).upper() != expected_type.upper():
            return False, "ACK_ORDER_TYPE_MISMATCH"
        try:
            OrderStatus(str(response["status"]))
        except ValueError:
            return False, "ACK_STATUS_UNKNOWN"

        if request.client_order_id:
            if response.get("clientOrderId") in (None, ""):
                return False, "ACK_CLIENT_ORDER_ID_MISSING"
            if str(response["clientOrderId"]) != str(request.client_order_id):
                return False, "ACK_CLIENT_ORDER_ID_MISMATCH"

        try:
            requested_quantity = Decimal(str(request.quantity.amount))
            venue_quantity = Decimal(str(response["origQty"]))
            executed_quantity = Decimal(str(response["executedQty"]))
        except (InvalidOperation, TypeError, ValueError):
            return False, "ACK_QUANTITY_INVALID"
        # P2 修复 (venue-capped reduce-only close): 交易所把减仓平仓单按
        # 剩余持仓截断(origQty < 请求量)后回报 FILLED 是合法终态,不是
        # mismatch。采纳 venue 事实(engine 侧 reduce-only 余量禁止重发)。
        _truncated_close = (
            request.reduce_only
            and str(response.get("status", "")).upper() == "FILLED"
            and venue_quantity < requested_quantity
            and executed_quantity == venue_quantity
            and executed_quantity > 0
        )
        if (
            not requested_quantity.is_finite()
            or requested_quantity <= 0
            or (venue_quantity != requested_quantity and not _truncated_close)
            or not executed_quantity.is_finite()
            or executed_quantity < 0
            or executed_quantity > venue_quantity
        ):
            return False, "ACK_QUANTITY_MISMATCH"

        if request.reduce_only:
            reduce_only = response.get("reduceOnly")
            enabled = reduce_only is True or str(reduce_only).strip().lower() in {"1", "true", "yes"}
            if not enabled:
                return False, "ACK_REDUCE_ONLY_UNPROVEN"
        return True, "OK"

    async def create_order(self, request: OrderRequest) -> OrderResponse:
        # BD-T03 修复: 不构造 NEW/FILLED — 无真实传输时返回 UNKNOWN
        if not self._health_monitor.is_safe_for_new_risk() and not request.reduce_only:
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
                    "type": request.order_type.value
                    if hasattr(request.order_type, "value")
                    else str(request.order_type),
                    "quantity": _format_decimal(str(request.quantity.amount)),
                }
                # P2 修复: client_order_id 为空时省略参数(与 rest_client.create_order
                # 行为一致),避免向交易所发送空值 newClientOrderId(-1102)。
                if request.client_order_id:
                    order_params["newClientOrderId"] = request.client_order_id
                # MARKET/STOP_MARKET/TAKE_PROFIT_MARKET/TRAILING_STOP_MARKET
                # 均不允许 timeInForce 参数(P2 修复: 此前只排除普通 MARKET,
                # 市价型条件单会被错误附加 timeInForce → -1106)。
                _market_types = {"MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}
                _is_market = str(order_params.get("type", "")).upper() in _market_types
                if not _is_market:
                    order_params["timeInForce"] = (
                        request.time_in_force.value
                        if hasattr(request.time_in_force, "value")
                        else str(request.time_in_force or "GTC")
                    )
                if request.price:
                    order_params["price"] = _format_decimal(str(request.price.amount))
                if request.reduce_only:
                    # Binance ONE_WAY safety invariant: reduce-only must be
                    # sent to the venue, not merely kept in local intent data.
                    order_params["reduceOnly"] = "true"
                transport_result = await self.request(
                    "POST",
                    Endpoint.ORDER,
                    signed=True,
                    params=order_params,
                    write_account_id=str(request.account_ref.account_id),
                )
                if not transport_result.is_success():
                    _err = transport_result.error
                    failure = _sanitized_adapter_error(_err, fallback="Order acknowledgement is UNKNOWN")
                    logger.warning(
                        "ORDER FAILED: cat=%s msg=%s code=%s",
                        failure["category"],
                        failure["msg"][:200],
                        failure["code"],
                    )
                    # P2 修复 (确定性拒绝被误标 UNKNOWN): -2010 余额不足/
                    # -2019 保证金/-2022 减仓超限/参数拒绝等是确定性业务事实
                    # (交易所保证未建单),应映射 REJECTED 让意图干净终结;
                    # 限频/时钟偏差等 retryable 拒绝打标 retryable_rejection
                    # (engine 转 UNKNOWN 裁决后按确定性缺席 re-arm 重发);
                    # 只有网络/5xx/未知才是真正歧义 → UNKNOWN。
                    # PERMISSION_DENIED(写 hold 门)不是 venue 拒绝 —— 保持
                    # UNKNOWN:hold 是可能解除的配置状态,不得确定性终结意图。
                    _cat = str(failure.get("category", "")).upper()
                    _retryable = bool(failure.get("retryable"))
                    _definitive = (
                        _cat
                        in {
                            "INSUFFICIENT_BALANCE",
                            "INSUFFICIENT_MARGIN",
                            "ORDER_REJECTED",
                            "POSITION_LIMIT",
                            "AUTH_FAILURE",
                        }
                        and not _retryable
                    )
                    _status = OrderStatus.REJECTED if (_definitive or _retryable) else OrderStatus.UNKNOWN
                    _reason = (
                        f"retryable_rejection:{_cat}"
                        if _retryable
                        else (f"venue_rejected:{_cat}:{failure.get('code')}" if _definitive else "api_call_failed")
                    )
                    return OrderResponse(
                        venue_instrument=request.venue_instrument,
                        account_ref=request.account_ref,
                        order_id="",
                        client_order_id=request.client_order_id,
                        status=_status,
                        side=request.side,
                        order_type=request.order_type,
                        original_quantity=request.quantity,
                        executed_quantity=Quantity(amount="0"),
                        average_price=None,
                        commission=None,
                        correlation_id=request.correlation_id,
                        raw_response={"reason": _reason, **failure},
                    )
                result = transport_result.data
                if isinstance(result, dict) and "orderId" in result:
                    ack_valid, ack_reason = self._validate_order_ack(request, result)
                    if not ack_valid:
                        logger.error("order acknowledgement rejected at adapter boundary: %s", ack_reason)
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
                            raw_response={"reason": ack_reason, "venue_response": result},
                        )
                    return OrderResponse(
                        venue_instrument=request.venue_instrument,
                        account_ref=request.account_ref,
                        order_id=str(result.get("orderId", "")),
                        client_order_id=(
                            str(result.get("clientOrderId")) if result.get("clientOrderId") else request.client_order_id
                        ),
                        status=OrderStatus(str(result.get("status", OrderStatus.NEW.value))),
                        side=request.side,
                        order_type=request.order_type,
                        original_quantity=request.quantity,
                        executed_quantity=Quantity(amount=str(result.get("executedQty", "0"))),
                        average_price=_Price(amount=str(result.get("avgPrice", "0")))
                        if result.get("avgPrice")
                        else None,
                        commission=None,
                        correlation_id=request.correlation_id,
                        raw_response=result,
                    )
            except Exception as exc:
                self._health_monitor.record_error(Endpoint.ORDER, type(exc).__name__)
                logger.warning("order submission failed; returning UNKNOWN: %s: %s", type(exc).__name__, str(exc)[:200])
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
                # P2 修复: 语义明确化 —— 该分支只可能是"未配置传输层"或
                # 请求处理期间抛出未捕获异常,旧 reason 文本易误读为特性
                # 开关未到位。
                "reason": "transport_not_configured" if self._rest_client is None else "api_call_failed"
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
                valid, reason = self._validate_cancel_ack(order_id, venue_instrument, result)
                if valid and isinstance(result, dict):
                    status = OrderStatus(str(result["status"]))
                    return OrderResponse(
                        venue_instrument=venue_instrument,
                        account_ref=AccountRef(venue_id=self._venue_id, account_id=self._account_id),
                        order_id=str(result["orderId"]),
                        client_order_id=result.get("clientOrderId"),
                        # A cancel response may race with a fill.  Preserve
                        # the venue's terminal state instead of claiming
                        # CANCELED merely because the DELETE request returned.
                        status=status,
                        side=OrderSide(result.get("side", OrderSide.BUY.value)),
                        order_type=OrderType(result.get("type", OrderType.MARKET.value)),
                        original_quantity=Quantity(amount=str(result.get("origQty", "0"))),
                        executed_quantity=Quantity(amount=str(result.get("executedQty", "0"))),
                        average_price=None,
                        commission=None,
                        correlation_id=None,
                        raw_response=result,
                    )
                logger.error("cancel acknowledgement rejected at adapter boundary: %s", reason)
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
                    raw_response={"reason": reason, "venue_response": result},
                )
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning("cancel order response could not be normalized: %s", exc)
        return OrderResponse(
            venue_instrument=venue_instrument,
            account_ref=AccountRef(venue_id=self._venue_id, account_id=self._account_id),
            order_id=order_id,
            client_order_id=None,
            correlation_id=None,
            status=OrderStatus.UNKNOWN,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            original_quantity=Quantity(amount="0"),
            executed_quantity=Quantity(amount="0"),
            average_price=None,
            commission=None,
            raw_response={"reason": "real_transport_pending_BD-T18"},
        )

    @staticmethod
    def _validate_cancel_ack(
        order_id: str,
        venue_instrument: VenueInstrument,
        response: Any,
    ) -> tuple[bool, str]:
        """Require an identity-bound, terminal cancel response.

        DELETE success is not itself a cancellation fact: the order may have
        filled concurrently, or the venue may return an unrelated/malformed
        row.  The caller must preserve the exact venue terminal status and
        keep UNKNOWN on any ambiguity.
        """

        if not isinstance(response, dict):
            return False, "CANCEL_ACK_NOT_OBJECT"
        required = ("orderId", "symbol", "side", "type", "origQty", "executedQty", "status")
        if any(response.get(field) in (None, "") for field in required):
            return False, "CANCEL_ACK_IDENTITY_MISSING"
        if str(response["orderId"]) != str(order_id):
            return False, "CANCEL_ACK_ORDER_ID_MISMATCH"
        if str(response["symbol"]).upper() != str(venue_instrument.instrument_id).upper():
            return False, "CANCEL_ACK_SYMBOL_MISMATCH"
        try:
            status = OrderStatus(str(response["status"]))
        except ValueError:
            return False, "CANCEL_ACK_STATUS_UNKNOWN"
        if status not in {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.FILLED}:
            return False, "CANCEL_ACK_NOT_TERMINAL"
        try:
            original = Decimal(str(response["origQty"]))
            executed = Decimal(str(response["executedQty"]))
        except (InvalidOperation, TypeError, ValueError):
            return False, "CANCEL_ACK_QUANTITY_INVALID"
        if not original.is_finite() or original <= 0 or not executed.is_finite() or executed < 0:
            return False, "CANCEL_ACK_QUANTITY_INVALID"
        if executed > original:
            return False, "CANCEL_ACK_EXECUTED_QTY_INVALID"
        if status in {OrderStatus.CANCELED, OrderStatus.EXPIRED} and executed > 0:
            return False, "CANCEL_ACK_PARTIAL_FILL_RECONCILIATION_REQUIRED"
        try:
            OrderSide(str(response["side"]))
            OrderType(str(response["type"]))
        except ValueError:
            return False, "CANCEL_ACK_ORDER_SEMANTICS_UNKNOWN"
        return True, "OK"

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
                    if str(result.get("orderId", "")) != str(order_id):
                        return OrderStatus.UNKNOWN
                    if str(result.get("symbol", "")).upper() != str(venue_instrument.instrument_id).upper():
                        return OrderStatus.UNKNOWN
                    return OrderStatus(str(result.get("status", OrderStatus.UNKNOWN.value)))
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning("order status response could not be normalized: %s", exc)
        return OrderStatus.UNKNOWN

    async def query_order_by_client_id(self, symbol: str, client_id: str) -> Result[dict[str, Any]]:
        """Query one order by durable client id and bind the identity."""

        symbol_value = str(symbol).strip().upper()
        client_value = str(client_id).strip()
        if not symbol_value or not client_value or self._rest_client is None:
            return Result.failure(
                "Client-order lookup requires transport, symbol and client id",
                category=ErrorCategory.UNKNOWN,
                source="binance_order_recovery",
            )
        try:
            response = await self.request(
                "GET",
                Endpoint.ORDER,
                signed=True,
                params={"symbol": symbol_value, "origClientOrderId": client_value},
            )
        except Exception as exc:
            self._health_monitor.record_error(Endpoint.ORDER, type(exc).__name__)
            return Result.failure(
                f"Client-order lookup failed: {type(exc).__name__}",
                category=ErrorCategory.UNKNOWN,
                source="binance_order_recovery",
            )
        if not response.is_success() or not isinstance(response.data, dict):
            # P2 修复: 保留底层 raw 错误(code/msg)—— -2013"Order does not
            # exist" 是 query-before-retry 的确定性缺席信号,此前被丢弃后
            # 调用方无法区分"从未成交"与"传输失败"。
            _raw = getattr(response.error, "raw", None) if response.error is not None else None
            return Result.failure(
                "Client-order lookup is UNKNOWN",
                category=ErrorCategory.UNKNOWN,
                raw=_raw if isinstance(_raw, dict) else response.data,
                source="binance_order_recovery",
            )
        raw = response.data
        required = (
            "orderId",
            "clientOrderId",
            "symbol",
            "status",
            "side",
            "type",
            "origQty",
            "executedQty",
        )
        if any(raw.get(field) in (None, "") for field in required):
            return Result.failure(
                "Client-order lookup response is incomplete",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_order_recovery",
            )
        if str(raw["clientOrderId"]) != client_value or str(raw["symbol"]).upper() != symbol_value:
            return Result.failure(
                "Client-order lookup identity mismatch",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_order_recovery",
            )
        try:
            OrderStatus(str(raw["status"]))
            OrderSide(str(raw["side"]))
            OrderType(str(raw["type"]))
            original_quantity = Decimal(str(raw["origQty"]))
            executed_quantity = Decimal(str(raw["executedQty"]))
            if (
                not original_quantity.is_finite()
                or original_quantity <= 0
                or not executed_quantity.is_finite()
                or executed_quantity < 0
                or executed_quantity > original_quantity
            ):
                raise ValueError("invalid recovery quantities")
        except (InvalidOperation, TypeError, ValueError):
            return Result.failure(
                "Client-order lookup semantics are UNKNOWN",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_order_recovery",
            )
        return Result.success(dict(raw), source="binance_order_recovery")

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
                order_type=_safe_enum(OrderType, str(raw["orderType"])),
                quantity=Quantity(amount=str(raw.get("quantity", "0"))),
                trigger_price=Price(amount=str(raw["triggerPrice"])),
                status=str(raw["algoStatus"]),
                client_algo_id=(str(raw["clientAlgoId"]) if raw.get("clientAlgoId") else None),
                reduce_only=(_strict_bool(raw["reduceOnly"], field_name="reduceOnly") if "reduceOnly" in raw else None),
                close_position=(
                    _strict_bool(raw["closePosition"], field_name="closePosition") if "closePosition" in raw else None
                ),
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
            # BD-DIAG: 透传底层失败原因,供 nearline 诊断打印定位
            _underlying = response.error.message if response.error else "non-list data"
            return Result.failure(
                f"Open Algo inventory is UNKNOWN: {_underlying}",
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
            error = response.error
            if error is None:
                return Result.failure(
                    "Algo order acknowledgement is UNKNOWN",
                    category=ErrorCategory.UNKNOWN,
                    raw=response.data,
                    source=response.source or "binance_algo_adapter",
                    observed_at=response.observed_at,
                    correlation_id=response.correlation_id,
                )
            return Result.failure(
                error.message,
                http_status=error.http_status,
                category=error.category,
                retryable=error.retryable,
                raw=error.raw,
                source=response.source or "binance_algo_adapter",
                observed_at=response.observed_at,
                correlation_id=response.correlation_id,
            )
        account_ref = AccountRef(venue_id=self._venue_id, account_id=self._account_id)
        parsed = self.parse_algo_order_snapshot(response.data, account_ref)
        if not parsed.is_success() or parsed.data is None:
            return parsed

        # A venue id alone is not proof that the requested protection was
        # installed.  Bind the typed acknowledgement to the exact command at
        # this boundary so callers cannot mark a mismatched/ non-reduce-only
        # conditional order ACTIVE from a partial response.
        raw = parsed.data.raw_response
        expected_symbol = str(params.get("symbol", "")).strip().upper()
        expected_side = str(params.get("side", "")).strip().upper()
        expected_type = str(params.get("type", "")).strip().upper()
        actual_symbol = str(raw.get("symbol", "")).strip().upper()
        actual_side = str(raw.get("side", "")).strip().upper()
        actual_type = str(raw.get("orderType", "")).strip().upper()
        mismatches: list[str] = []
        if not expected_symbol or actual_symbol != expected_symbol:
            mismatches.append("symbol")
        if not expected_side or actual_side != expected_side:
            mismatches.append("side")
        if not expected_type or actual_type != expected_type:
            mismatches.append("orderType")

        expected_client_algo_id = str(params.get("clientAlgoId", "")).strip()
        actual_client_algo_id = str(raw.get("clientAlgoId", "")).strip()
        if expected_client_algo_id and actual_client_algo_id != expected_client_algo_id:
            mismatches.append("clientAlgoId")

        expected_trigger = params.get("triggerPrice")
        actual_trigger = raw.get("triggerPrice")
        if expected_trigger not in (None, ""):
            try:
                if Decimal(str(expected_trigger)) <= 0 or Decimal(str(actual_trigger)) != Decimal(
                    str(expected_trigger)
                ):
                    mismatches.append("triggerPrice")
            except (InvalidOperation, TypeError, ValueError):
                mismatches.append("triggerPrice")

        expected_working_type = str(params.get("workingType", "")).strip().upper()
        if expected_working_type and str(raw.get("workingType", "")).strip().upper() != expected_working_type:
            mismatches.append("workingType")

        expected_position_side = str(params.get("positionSide", "")).strip().upper()
        if expected_position_side and str(raw.get("positionSide", "")).strip().upper() != expected_position_side:
            mismatches.append("positionSide")

        def _enabled(value: Any) -> bool:
            return value is True or str(value).strip().lower() in {"1", "true", "yes"}

        if _enabled(params.get("reduceOnly")) and not _enabled(raw.get("reduceOnly")):
            mismatches.append("reduceOnly")
        if _enabled(params.get("closePosition")) and not _enabled(raw.get("closePosition")):
            mismatches.append("closePosition")

        requested_quantity = params.get("quantity")
        response_quantity = raw.get("quantity")
        if requested_quantity not in (None, "") and not _enabled(params.get("closePosition")):
            try:
                if Decimal(str(requested_quantity)) <= 0 or Decimal(str(response_quantity)) != Decimal(
                    str(requested_quantity)
                ):
                    mismatches.append("quantity")
            except (InvalidOperation, TypeError, ValueError):
                mismatches.append("quantity")

        if mismatches:
            return Result.failure(
                "Algo acknowledgement does not match request: " + ",".join(mismatches),
                category=ErrorCategory.UNKNOWN,
                raw=response.data,
                source="binance_algo_adapter",
            )
        return parsed

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
        assert isinstance(execution, dict)
        if execution.get("i") is not None and execution.get("I") is not None:
            event_id = f"{event_type}:{execution.get('i')}:{execution.get('I')}"
        else:
            # Binance account events do not expose a unique id.  Event time
            # alone can collide within one millisecond, so retain a stable
            # content digest while keeping exact replay idempotent.
            canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
            event_id = f"{event_type}:{event_time_ms}:{digest}"
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
        if event_result.data.event_type not in {"ORDER_TRADE_UPDATE", "TRADE_LITE"} or not isinstance(raw, dict):
            # TRADE_LITE 是轻量用户流:成交事实字段在顶层(无 o 包裹),
            # 且不携带订单类型/状态/累计成交量 —— 只表达"一笔成交发生了"。
            return Result.failure(
                "Expected ORDER_TRADE_UPDATE/TRADE_LITE envelope",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        # BD-FIX (TRADE_LITE): ORDER_TRADE_UPDATE 的执行字段嵌套在 ``o``;
        # TRADE_LITE 的字段直接在顶层(s/q/p/m/c/S/L/l/t/i)。此前统一按
        # ``o`` 解析,真实 TRADE_LITE 事件 100% 解析失败 → 成交事实
        # (保护单平仓/外部订单)永不进入投影与账本。两种包络分别处理。
        if event_result.data.event_type == "TRADE_LITE":
            lite_order = raw
            lite_required = ("i", "c", "s", "S", "q", "L", "l", "t")
            if any(lite_order.get(key) in (None, "") for key in lite_required):
                return Result.failure(
                    "TRADE_LITE missing execution fields",
                    category=ErrorCategory.UNKNOWN,
                    raw=raw,
                    source="binance_user_stream_adapter",
                )
            try:
                numeric_values = {key: Decimal(str(lite_order[key])) for key in ("q", "l", "L")}
                if any(value < 0 for value in numeric_values.values()) or numeric_values["l"] <= 0:
                    raise ValueError("TRADE_LITE quantity/price fields must be valid")
                commission_asset = str(lite_order.get("N") or "USDT")
                update = UserOrderUpdate(
                    event=event_result.data,
                    order_id=str(lite_order["i"]),
                    client_order_id=str(lite_order["c"]),
                    symbol=InstrumentId(str(lite_order["s"])),
                    side=OrderSide(str(lite_order["S"])),
                    # TRADE_LITE 不携带订单类型;缺省值显式 UNKNOWN,不得伪造。
                    order_type=_safe_enum(OrderType, str(lite_order.get("o") or "UNKNOWN")),
                    # TRADE_LITE 不携带订单状态/累计成交量。成交事实由
                    # last_quantity/last_price + trade_id 表达,消费方必须
                    # 按 trade_id 幂等累加,不得把 UNKNOWN 状态解释为
                    # FILLED/CANCELED。
                    order_status=OrderStatus.UNKNOWN,
                    execution_type="TRADE",
                    original_quantity=Quantity(amount=str(lite_order["q"])),
                    cumulative_quantity=Quantity(amount="0"),
                    last_quantity=Quantity(amount=str(lite_order["l"])),
                    last_price=Price(amount=str(lite_order["L"])),
                    average_price=Price(amount=str(lite_order["L"])),
                    trade_id=str(lite_order["t"]),
                    commission=MonetaryValue(amount=str(lite_order.get("n", "0")), currency=commission_asset),
                    realized_pnl=None,
                )
            except (TypeError, ValueError, InvalidOperation) as exc:
                return Result.failure(
                    f"TRADE_LITE invalid: {exc}",
                    category=ErrorCategory.UNKNOWN,
                    raw=raw,
                    source="binance_user_stream_adapter",
                )
            return Result.success(update, source="binance_user_stream_adapter")

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
                order_type=_safe_enum(OrderType, str(order["o"])),
                order_status=_safe_enum(OrderStatus, str(order["X"])),
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

    @staticmethod
    def parse_user_account_update(raw: Any) -> Result[UserAccountUpdate]:
        """Parse Binance ``ACCOUNT_UPDATE`` without inventing a full snapshot."""

        event_result = BinanceUsdmAdapter.parse_user_stream_event(raw)
        if not event_result.is_success() or event_result.data is None:
            return Result.failure(
                "User account update has no valid event envelope",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        if event_result.data.event_type != "ACCOUNT_UPDATE" or not isinstance(raw, dict):
            return Result.failure(
                "Expected ACCOUNT_UPDATE envelope",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        account = raw.get("a")
        if (
            not isinstance(account, dict)
            or not isinstance(account.get("B"), list)
            or not isinstance(account.get("P"), list)
        ):
            return Result.failure(
                "User account update missing balance or position arrays",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )

        def _number(value: Any, field_name: str) -> str:
            if value in (None, ""):
                raise ValueError(f"missing {field_name}")
            parsed = Decimal(str(value))
            if not parsed.is_finite():
                raise ValueError(f"{field_name} must be finite")
            return str(value)

        try:
            balances: list[UserBalanceUpdate] = []
            for row in account["B"]:
                if not isinstance(row, dict):
                    raise ValueError("malformed balance row")
                asset = str(row.get("a") or "").strip()
                if not asset:
                    raise ValueError("balance row missing asset")
                wallet_balance = _number(row.get("wb"), "wallet balance")
                cross_wallet_balance = _number(row.get("cw"), "cross wallet balance")
                available = (
                    MonetaryValue(amount=_number(row["ab"], "available balance"), currency=asset)
                    if row.get("ab") not in (None, "")
                    else None
                )
                balances.append(
                    UserBalanceUpdate(
                        asset=asset,
                        wallet_balance=MonetaryValue(amount=wallet_balance, currency=asset),
                        cross_wallet_balance=MonetaryValue(amount=cross_wallet_balance, currency=asset),
                        available_balance=available,
                    )
                )

            positions: list[UserPositionUpdate] = []
            for row in account["P"]:
                if not isinstance(row, dict):
                    raise ValueError("malformed position row")
                symbol = str(row.get("s") or "").strip()
                if not symbol:
                    raise ValueError("position row missing symbol")
                position_amount = _number(row.get("pa"), "position amount")
                entry_price = _number(row.get("ep"), "entry price")
                break_even_price = _number(row.get("bep"), "break-even price")
                unrealized_pnl = _number(row.get("up"), "unrealized pnl")
                positions.append(
                    UserPositionUpdate(
                        symbol=InstrumentId(symbol),
                        position_amount=Quantity(amount=position_amount),
                        entry_price=Price(amount=entry_price),
                        break_even_price=Price(amount=break_even_price),
                        unrealized_pnl=MonetaryValue(amount=unrealized_pnl, currency="USDT"),
                        margin_type=str(row.get("mt") or "UNKNOWN"),
                        position_side=str(row.get("ps") or "UNKNOWN"),
                    )
                )
            reason = str(account.get("m") or "UNKNOWN")
            if reason == "UNKNOWN":
                raise ValueError("account update missing reason")
            update = UserAccountUpdate(
                event=event_result.data,
                reason=reason,
                balances=tuple(balances),
                positions=tuple(positions),
            )
        except (TypeError, ValueError, InvalidOperation) as exc:
            return Result.failure(
                f"User account update invalid: {exc}",
                category=ErrorCategory.UNKNOWN,
                raw=raw,
                source="binance_user_stream_adapter",
            )
        return Result.success(update, source="binance_user_stream_adapter")

    def normalize_error(self, error_code: int, message: str, correlation_id: str | None = None) -> Any:
        return ErrorNormalizer.normalize(str(self._venue_id), error_code, message, correlation_id)
