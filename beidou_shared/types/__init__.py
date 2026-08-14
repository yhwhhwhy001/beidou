"""
核心领域类型定义。

所有市场、订单、账户、交易池和风险对象必须包含 venue_id、account_id、instrument_id。
核心领域不得暴露任何交易所专有类型。
"""

from __future__ import annotations

from enum import Enum
from typing import NewType
from uuid import uuid4

from pydantic import BaseModel, Field

# --- 身份类型 ---

VenueId = NewType("VenueId", str)
AccountId = NewType("AccountId", str)
InstrumentId = NewType("InstrumentId", str)
CorrelationId = NewType("CorrelationId", str)
CausationId = NewType("CausationId", str)
PolicyId = NewType("PolicyId", str)
StrategyId = NewType("StrategyId", str)
FactorId = NewType("FactorId", str)
ModelId = NewType("ModelId", str)
OrderId = NewType("OrderId", str)
ExecutionId = NewType("ExecutionId", str)
RiskApprovalId = NewType("RiskApprovalId", str)
BatchId = NewType("BatchId", str)
SchemaVersion = NewType("SchemaVersion", str)


class ResultStatus(str, Enum):
    SUCCESS = "SUCCESS"
    EMPTY = "EMPTY"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"
    UNSUPPORTED = "UNSUPPORTED"


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    MAINTENANCE = "MAINTENANCE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    READ_ONLY = "READ_ONLY"
    SUSPENDED = "SUSPENDED"
    LOCKED = "LOCKED"
    UNKNOWN = "UNKNOWN"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    TRAILING_STOP = "TRAILING_STOP"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"
    # BD-FIX（I6/I4 审查）: Binance USD-M 实际订单类型补全 ——
    # 缺失枚举导致合法事件解析 ValueError → fault → 停机。
    TRAILING_STOP_MARKET = "TRAILING_STOP_MARKET"
    LIQUIDATION = "LIQUIDATION"
    INSURANCE = "INSURANCE"
    ADL = "ADL"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    PENDING_CANCEL = "PENDING_CANCEL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    # BD-FIX（I4 审查）: Binance 强平/保险/ADL 单的合法状态
    NEW_INSURANCE = "NEW_INSURANCE"
    NEW_ADL = "NEW_ADL"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTX = "GTX"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class RiskDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"


class GateResult(str, Enum):
    PASS = "PASS"
    CONDITIONAL_PASS = "CONDITIONAL_PASS"
    FAIL = "FAIL"
    UNVERIFIABLE = "UNVERIFIABLE"


class ClockDomain(str, Enum):
    REALTIME = "REALTIME"
    NEARLINE = "NEARLINE"
    OFFLINE = "OFFLINE"


class DataQualityTier(str, Enum):
    PASS = "PASS"
    CONDITIONAL = "CONDITIONAL"
    FAIL = "FAIL"


class VenueInstrument(BaseModel):
    venue_id: VenueId
    instrument_id: InstrumentId

    def __hash__(self) -> int:
        return hash((self.venue_id, self.instrument_id))


class AccountRef(BaseModel):
    venue_id: VenueId
    account_id: AccountId


class MonetaryValue(BaseModel):
    amount: str
    currency: str = "USDT"
    decimals: int = 8
    model_config = {"frozen": True}


class Quantity(BaseModel):
    amount: str
    decimals: int = 8
    model_config = {"frozen": True}


class Price(BaseModel):
    amount: str
    decimals: int = 8
    model_config = {"frozen": True}


class PerformanceBudget(BaseModel):
    path_name: str
    operation: str
    clock_domain: ClockDomain
    p50_ms: float
    p95_ms: float
    p99_ms: float
    throughput_per_second: float | None = None
    max_memory_mb: float | None = None
    reference_hardware: str
    measurement_method: str
    degradation_threshold_pct: float = 20.0
