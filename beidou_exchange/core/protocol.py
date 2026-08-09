"""ExchangeAdapter Protocol — 所有交易所适配器必须实现的统一接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 仅在类型检查时导入 — 运行时注解惰性求值（from __future__ import annotations）
    from beidou_exchange.core.error_taxonomy import Result

from beidou_shared.types import (
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
    TimeInForce,
    VenueId,
    VenueInstrument,
)


class Capability(str, Enum):
    SPOT = "SPOT"
    FUTURES_USD_M = "FUTURES_USD_M"
    FUTURES_COIN_M = "FUTURES_COIN_M"
    OPTIONS = "OPTIONS"
    MARGIN = "MARGIN"
    CONDITIONAL_ORDERS = "CONDITIONAL_ORDERS"
    TRAILING_STOP = "TRAILING_STOP"
    BATCH_ORDERS = "BATCH_ORDERS"
    WEBSOCKET_MARKET = "WEBSOCKET_MARKET"
    WEBSOCKET_USER = "WEBSOCKET_USER"
    USER_DATA_STREAM = "USER_DATA_STREAM"
    MULTI_ACCOUNT = "MULTI_ACCOUNT"


@dataclass(frozen=True, slots=True)
class ExchangeInfo:
    venue_id: VenueId
    name: str
    capabilities: frozenset[Capability]
    rate_limits: dict[str, int] = field(default_factory=dict)
    supported_instruments: frozenset[InstrumentId] = field(default_factory=frozenset)
    trading_fee_tiers: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AccountInfo:
    account_ref: AccountRef
    can_trade: bool
    can_deposit: bool
    can_withdraw: bool
    balances: dict[str, MonetaryValue] = field(default_factory=dict)
    positions: dict[str, Quantity] = field(default_factory=dict)
    margin_used: MonetaryValue | None = None
    margin_total: MonetaryValue | None = None


@dataclass(frozen=True, slots=True)
class OrderRequest:
    venue_instrument: VenueInstrument
    account_ref: AccountRef
    side: OrderSide
    order_type: OrderType
    quantity: Quantity
    price: Price | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    client_order_id: str | None = None
    correlation_id: CorrelationId | None = None
    reduce_only: bool = False


@dataclass(frozen=True, slots=True)
class OrderResponse:
    venue_instrument: VenueInstrument
    account_ref: AccountRef
    order_id: str
    client_order_id: str | None
    status: OrderStatus
    side: OrderSide
    order_type: OrderType
    original_quantity: Quantity
    executed_quantity: Quantity
    average_price: Price | None
    commission: MonetaryValue | None
    correlation_id: CorrelationId | None
    raw_response: dict[str, Any] | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class AlgoOrderSnapshot:
    """Typed venue acknowledgement for a conditional/Algo order.

    ``ACTIVE`` is never inferred from local construction.  The adapter must
    return a non-empty venue id, symbol, side, order type, trigger price and
    venue status before an engine can bind ownership to this snapshot.
    """

    algo_id: str
    venue_instrument: VenueInstrument
    account_ref: AccountRef
    side: OrderSide
    order_type: OrderType
    quantity: Quantity
    trigger_price: Price
    status: str
    client_algo_id: str | None = None
    reduce_only: bool | None = None
    close_position: bool | None = None
    position_side: str | None = None
    update_time_ms: int | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserStreamEvent:
    """Normalized user-stream event; missing event identity stays UNKNOWN."""

    event_type: str
    event_id: str
    event_time_ms: int
    transaction_time_ms: int | None
    sequence: int | None
    raw_event: dict[str, Any]


@dataclass(frozen=True, slots=True)
class UserOrderUpdate:
    """Normalized Binance ``ORDER_TRADE_UPDATE`` payload.

    Cumulative quantity is the venue high-water mark; ``last_quantity`` and
    ``trade_id`` identify the incremental execution.  The raw event remains
    attached so an independent replay can re-derive the projection.
    """

    event: UserStreamEvent
    order_id: str
    client_order_id: str
    symbol: InstrumentId
    side: OrderSide
    order_type: OrderType
    order_status: OrderStatus
    execution_type: str
    original_quantity: Quantity
    cumulative_quantity: Quantity
    last_quantity: Quantity
    last_price: Price
    average_price: Price
    trade_id: str | None
    commission: MonetaryValue
    realized_pnl: MonetaryValue | None = None


class ExchangeAdapter(ABC):
    """交易所适配器协议。

    所有方法必须返回标准化的 ResultStatus 语义。
    不支持的能力必须显式返回 UNSUPPORTED，不得静默降级。
    """

    @property
    @abstractmethod
    def venue_id(self) -> VenueId: ...

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[Capability]: ...

    @abstractmethod
    async def get_exchange_info(self) -> ExchangeInfo: ...

    @abstractmethod
    async def check_health(self, venue_id: VenueId) -> HealthStatus: ...

    @abstractmethod
    async def get_account_info(self, account_ref: AccountRef) -> AccountInfo: ...

    @abstractmethod
    async def get_balances(self, account_ref: AccountRef) -> tuple[ResultStatus, dict[str, MonetaryValue]]: ...

    @abstractmethod
    async def get_positions(self, account_ref: AccountRef) -> tuple[ResultStatus, dict[str, Quantity]]: ...

    @abstractmethod
    async def create_order(self, request: OrderRequest) -> OrderResponse: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, venue_instrument: VenueInstrument) -> OrderResponse: ...

    @abstractmethod
    async def get_order_status(self, order_id: str, venue_instrument: VenueInstrument) -> OrderStatus: ...

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities


class MarketDataExchangePort(ABC):
    """BD-T03: 行情数据端口 — Adapter 唯一提供行情数据。"""

    @abstractmethod
    async def get_exchange_info(self, symbol: str | None = None) -> Result: ...

    @abstractmethod
    async def get_closed_klines(self, symbol: str, interval: str, limit: int) -> Result: ...


class AccountExchangePort(ABC):
    """BD-T03: 账户端口 — 余额/仓位查询，失败返回 UNKNOWN 不返回 EMPTY。"""

    @abstractmethod
    async def get_account_snapshot(self) -> Result: ...

    @abstractmethod
    async def get_balances(self) -> Result: ...

    @abstractmethod
    async def get_positions(self) -> Result: ...


class TradingExchangePort(ABC):
    """BD-T03: 交易端口 — 订单创建/查询/取消。"""

    @abstractmethod
    async def create_order(self, command: Any) -> Result: ...

    @abstractmethod
    async def query_order_by_client_id(self, symbol: str, client_id: str) -> Result: ...

    @abstractmethod
    async def cancel_order(self, command: Any) -> Result: ...


class ConditionalOrderPort(ABC):
    """BD-V3: Algo/conditional order lifecycle with explicit venue ACKs."""

    @abstractmethod
    async def get_open_algo_orders(self) -> Result: ...

    @abstractmethod
    async def create_algo_order(self, command: Any) -> Result: ...

    @abstractmethod
    async def cancel_algo_order(self, command: Any) -> Result: ...


class UserStreamPort(ABC):
    """BD-T03: 用户数据流端口 — WebSocket listenKey 管理。"""

    @abstractmethod
    async def stream_user_events(self) -> Result: ...
