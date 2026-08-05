"""ExchangeAdapter Protocol — 所有交易所适配器必须实现的统一接口。"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import (
    AccountId, AccountRef, CorrelationId, HealthStatus, InstrumentId,
    MonetaryValue, OrderSide, OrderStatus, OrderType, Price, Quantity,
    ResultStatus, TimeInForce, VenueId, VenueInstrument,
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
