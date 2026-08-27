"""
PKG03 (BDS-P1-066): Domain Ports — Engine 去 God Object 第一阶段。

定义核心域的端口协议（interfaces/contracts）。
后续阶段将 AutonomousEngine 拆分为独立的 domain services，
每个 service 只暴露其端口，禁止跨域读取私有字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from beidou_shared.types import StrategyId, VenueId

# ================================================================
# 事实模型 — 不可变数据传输对象
# ================================================================


@dataclass(frozen=True)
class MarketFact:
    """市场数据事实 — 只读，不可变。"""

    symbol: str
    price: float
    timestamp: datetime
    bid: float = 0.0
    ask: float = 0.0
    volume_24h: float = 0.0
    source: str = "MARKET_DATA_AUTHORITY"


@dataclass(frozen=True)
class FactorEvidence:
    """因子证据 — 由 Research 域产出。"""

    strategy_id: StrategyId
    factor_name: str
    value: float
    timestamp: datetime
    generation: int = 1
    is_valid: bool = True


@dataclass(frozen=True)
class StrategyProposal:
    """策略提议 — 由 Strategy 域产出，不可被 Portfolio 层篡改。"""

    strategy_id: StrategyId
    symbol: str
    direction: str  # LONG / SHORT / NO_ACTION
    target_pct: float
    confidence: float
    generation: int = 1
    owner: str = ""
    attribution: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskDecision:
    """风险决策 — Single Risk Authority 的不可变输出。"""

    level: str  # NORMAL / NO_NEW_RISK / EXIT_ONLY / LOCKED / EMERGENCY_FLATTEN
    reason: str
    generation: int
    snapshot_hash: str
    timestamp: datetime


@dataclass(frozen=True)
class OrderIntent:
    """订单意图 — 持久化的不可变订单。"""

    intent_id: str
    symbol: str
    side: str
    quantity: float
    price: float | None = None
    order_type: str = "LIMIT"
    client_order_id: str = ""
    approval_id: str = ""
    generation: int = 1


@dataclass(frozen=True)
class LedgerEntry:
    """账本条目 — 不可变复式记账条目。"""

    transaction_id: str
    account: str
    venue: str
    currency: str
    debit: float = 0.0
    credit: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source_event_id: str = ""


@dataclass(frozen=True)
class PositionFact:
    """仓位事实 — 对账后的权威仓位。"""

    symbol: str
    quantity: float
    avg_price: float
    side: str
    venue: VenueId
    reconciled_at: datetime


# ================================================================
# Domain Port 协议 — 每个域只暴露这些接口
# ================================================================


@runtime_checkable
class MarketDataPort(Protocol):
    """市场数据端口 — 只读市场事实的 Authority。"""

    def get_price(self, symbol: str) -> MarketFact | None: ...
    def get_order_book(self, symbol: str, depth: int = 5) -> dict[str, Any]: ...
    def subscribe(self, symbols: list[str]) -> None: ...


@runtime_checkable
class ResearchPort(Protocol):
    """研究端口 — 因子评估的 Authority。"""

    def evaluate_factor(self, strategy_id: StrategyId, factor_name: str) -> FactorEvidence: ...
    def get_factor_registry(self) -> dict[str, Any]: ...
    def validate_factor(self, factor_name: str) -> bool: ...


@runtime_checkable
class StrategyPort(Protocol):
    """策略端口 — 策略提议的 Authority。"""

    def generate_proposal(self, strategy_id: StrategyId, context: dict[str, Any]) -> StrategyProposal: ...
    def get_active_strategies(self) -> list[StrategyId]: ...
    def validate_proposal(self, proposal: StrategyProposal) -> bool: ...


@runtime_checkable
class RiskPort(Protocol):
    """风险端口 — Single Risk Authority。

    禁止多个 Risk Engine 并存；禁止 Engine 内联第二套风控。
    """

    def evaluate(self, snapshot: dict[str, Any]) -> RiskDecision: ...
    def get_current_level(self) -> str: ...
    def is_trading_allowed(self) -> bool: ...


@runtime_checkable
class ExecutionPort(Protocol):
    """执行端口 — 订单执行的 Authority。

    禁止直接调用交易所 REST client。
    """

    def submit_order(self, intent: OrderIntent) -> str: ...
    def cancel_order(self, order_id: str, symbol: str) -> bool: ...
    def get_order_status(self, order_id: str, symbol: str) -> dict[str, Any]: ...


@runtime_checkable
class LedgerPort(Protocol):
    """账本端口 — 复式记账的 Authority。

    禁止双轨（Legacy + Posting）并存。
    """

    def record_transaction(self, entry: LedgerEntry) -> str: ...
    def get_balance(self, account: str, currency: str) -> float: ...
    def reconcile(self) -> bool: ...


@runtime_checkable
class ProtectionPort(Protocol):
    """保护端口 — 仓位保护的 Authority。

    禁止本地状态先于交易所事实。
    """

    def create_stop_loss(self, position: PositionFact, trigger_price: float) -> str: ...
    def create_take_profit(self, position: PositionFact, trigger_price: float) -> str: ...
    def cancel_protection(self, protection_id: str) -> bool: ...
    def get_active_protections(self) -> dict[str, Any]: ...


@runtime_checkable
class MonitoringPort(Protocol):
    """监控端口 — 运维事实的只读消费者。

    禁止直接发出风险增加订单；禁止读取 Engine 私有字段。
    """

    def report_health(self) -> dict[str, Any]: ...
    def get_operational_facts(self) -> dict[str, Any]: ...


# ================================================================
# Domain Authority 注册表 — 一个事实一个 Authority
# ================================================================


class DomainAuthorityRegistry:
    """域 Authority 注册表。

    每个经济事实只有一个可写 Authority。
    此注册表在运行时强制执行单写原则。
    """

    _instance: DomainAuthorityRegistry | None = None

    def __init__(self) -> None:
        self._authorities: dict[str, Any] = {}
        self._read_models: dict[str, list[Any]] = {}

    @classmethod
    def get_instance(cls) -> DomainAuthorityRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register_authority(self, domain: str, authority: Any) -> None:
        """注册域的 Authority。每个域只能有一个 Authority。"""
        if domain in self._authorities:
            raise ValueError(
                f"域 '{domain}' 已注册 Authority {type(self._authorities[domain]).__name__}，"
                f"不能注册第二个 {type(authority).__name__}"
            )
        self._authorities[domain] = authority

    def register_read_model(self, domain: str, consumer: Any) -> None:
        """注册域的只读消费者。"""
        if domain not in self._read_models:
            self._read_models[domain] = []
        self._read_models[domain].append(consumer)

    def get_authority(self, domain: str) -> Any | None:
        """获取域的 Authority。"""
        return self._authorities.get(domain)

    @property
    def registered_domains(self) -> list[str]:
        """已注册的域列表。"""
        return list(self._authorities.keys())


DOMAINS = [
    "Market",
    "Research",
    "Strategy",
    "Risk",
    "Execution",
    "Ledger",
    "Protection",
    "Monitoring",
]
"""PKG03: 8 个核心域，每个域只有一个 Authority。"""
