"""持续对账、前置账户事实缓存与差异修复。

BD-CV44: 集成 TripleReconciliation contract — 三方同源伪造检测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum

from beidou_safety.execution.contracts import TripleReconciliation  # BD-CV44
from beidou_shared.types import AccountId, CorrelationId, MonetaryValue, Quantity, VenueId


class ReconciliationStatus(str, Enum):
    """BD-P0-10: 对账结果状态。"""

    MATCHED = "MATCHED"
    MISMATCHED = "MISMATCHED"
    ONE_SIDE_MISSING = "ONE_SIDE_MISSING"
    BOTH_SIDES_MISSING = "BOTH_SIDES_MISSING"  # → UNKNOWN, blocks new risk
    STALE = "STALE"  # BD-T13: 数据过期，阻断新风险
    INCOMPLETE = "INCOMPLETE"  # 事实存在但字段/来源不完整
    ERROR = "ERROR"

    @property
    def is_safe(self) -> bool:
        """是否可以安全继续交易。仅 MATCHED 可安全。"""
        return self in (ReconciliationStatus.MATCHED,)


@dataclass
class ReconciliationResult:
    """BD-P0-10: 对账结果。包含类型化状态和差异列表。"""

    matched: bool
    status: ReconciliationStatus = ReconciliationStatus.MATCHED
    differences: list[str] = field(default_factory=list)
    system_facts: AccountFactSnapshot | None = None
    exchange_facts: AccountFactSnapshot | None = None
    event_facts: AccountFactSnapshot | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_unknown(self) -> bool:
        return self.status in {
            ReconciliationStatus.ONE_SIDE_MISSING,
            ReconciliationStatus.BOTH_SIDES_MISSING,
            ReconciliationStatus.STALE,
            ReconciliationStatus.INCOMPLETE,
            ReconciliationStatus.ERROR,
        }

    @property
    def should_block_new_risk(self) -> bool:
        """BD-P0-10 AC-10-01: 所有非 MATCHED 状态（MISMATCHED/ONE_SIDE_MISSING/BOTH_SIDES_MISSING/ERROR）均阻止新风险。"""
        return self.status is not ReconciliationStatus.MATCHED


@dataclass
class AccountFactSnapshot:
    """PKG20 (BDS-P1-031): complete 默认为 False — 采集器必须显式证明完整性。"""

    account_id: AccountId
    venue_id: VenueId
    balance: MonetaryValue
    positions: dict[str, Quantity]
    open_orders: list[str]
    margin_used: MonetaryValue | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: CorrelationId | None = None
    source: str = "UNKNOWN"
    fact_version: str = ""
    complete: bool = False  # PKG20: 默认不完整 — 采集器必须显式证明
    # PKG02: 对账数量容差必须从 InstrumentRuleSnapshot stepSize 获取，不使用硬编码默认值。
    # 0 表示未从交易所规则中获取，调用方必须在有有效 rule snapshot 时才执行对账。
    position_step_size: str = ""
    position_step_sizes: dict[str, str] = field(default_factory=dict)


class ReconciliationEngine:
    """对账引擎。比较系统事实与交易所事实，差异需修复。"""

    def __init__(self, *, max_age_seconds: float = 30.0) -> None:
        self._system_facts: dict[str, AccountFactSnapshot] = {}
        self._exchange_facts: dict[str, AccountFactSnapshot] = {}
        self._event_facts: dict[str, AccountFactSnapshot] = {}
        self._max_age = timedelta(seconds=max(0.0, max_age_seconds))
        self._last_result: ReconciliationResult | None = None

    def update_system_facts(self, facts: AccountFactSnapshot) -> None:
        self._system_facts[f"{facts.account_id}:{facts.venue_id}"] = facts

    def update_exchange_facts(self, facts: AccountFactSnapshot) -> None:
        self._exchange_facts[f"{facts.account_id}:{facts.venue_id}"] = facts

    def update_event_facts(self, facts: AccountFactSnapshot) -> None:
        """Record the independently projected user-stream fact source."""

        self._event_facts[f"{facts.account_id}:{facts.venue_id}"] = facts

    def reconcile(self, account_id: AccountId, venue_id: VenueId) -> ReconciliationResult:
        """BD-P0-10: 对账 — 双方缺失 → UNKNOWN，从不匹配。

        AC-10-01: both-sides-missing → UNKNOWN, blocks new risk.
        AC-10-02: differences never silently ignored.
        """
        key = f"{account_id}:{venue_id}"
        result = self.compare(
            self._system_facts.get(key),
            self._exchange_facts.get(key),
            max_age=self._max_age,
        )
        self._last_result = result
        return result

    def reconcile_three_way(self, account_id: AccountId, venue_id: VenueId) -> ReconciliationResult:
        """Compare durable system, exchange, and user-stream facts."""

        key = f"{account_id}:{venue_id}"
        result = self.compare_three_way(
            self._system_facts.get(key),
            self._exchange_facts.get(key),
            self._event_facts.get(key),
            max_age=self._max_age,
        )
        self._last_result = result
        return result

    @property
    def last_result(self) -> ReconciliationResult | None:
        return self._last_result

    @staticmethod
    def compare(
        system_facts: AccountFactSnapshot | None,
        exchange_facts: AccountFactSnapshot | None,
        *,
        max_age: timedelta = timedelta(seconds=30),
        now: datetime | None = None,
    ) -> ReconciliationResult:
        """Compare two independently captured snapshots without mutating state.

        This is the only comparison primitive used by the engine's durable
        reconciliation path.  Missing, incomplete, or stale facts are typed
        failures; they are never treated as a match and never repaired by
        copying one side over the other.
        """

        checked_at = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if system_facts is None and exchange_facts is None:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.BOTH_SIDES_MISSING,
                differences=["BOTH_SIDES_MISSING: system and exchange facts unavailable — UNKNOWN"],
                checked_at=checked_at,
            )
        if system_facts is None or exchange_facts is None:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.ONE_SIDE_MISSING,
                differences=["ONE_SIDE_MISSING: one independent fact source unavailable — UNKNOWN"],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                checked_at=checked_at,
            )
        if not system_facts.complete or not exchange_facts.complete:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.INCOMPLETE,
                differences=[
                    "INCOMPLETE_FACT: system/exchange snapshot is not complete",
                ],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                checked_at=checked_at,
            )

        source_pairs = (("system", system_facts), ("exchange", exchange_facts))
        missing_lineage = [
            role
            for role, facts in source_pairs
            if not facts.source.strip()
            or facts.source.strip().upper() == "UNKNOWN"
            or not facts.fact_version.strip()
            or facts.fact_version.strip().upper() == "UNKNOWN"
        ]
        if missing_lineage:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.INCOMPLETE,
                differences=["INCOMPLETE_FACT_LINEAGE: " + ", ".join(missing_lineage)],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                checked_at=checked_at,
            )
        if system_facts.source == exchange_facts.source:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.ERROR,
                differences=[f"SAME_SOURCE_FRAUD_RISK: both facts declare source {system_facts.source}"],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                checked_at=checked_at,
            )

        def _age(facts: AccountFactSnapshot) -> timedelta:
            timestamp = facts.timestamp
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return checked_at - timestamp

        system_age = _age(system_facts)
        exchange_age = _age(exchange_facts)
        if system_age < timedelta(0) or exchange_age < timedelta(0):
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.ERROR,
                differences=[
                    f"FUTURE_FACT: system_age={system_age.total_seconds():.3f}s "
                    f"exchange_age={exchange_age.total_seconds():.3f}s",
                ],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                checked_at=checked_at,
            )
        if system_age > max_age or exchange_age > max_age:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.STALE,
                differences=[
                    f"STALE_FACT: system_age={system_age.total_seconds():.3f}s "
                    f"exchange_age={exchange_age.total_seconds():.3f}s",
                ],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                checked_at=checked_at,
            )

        if system_facts.account_id != exchange_facts.account_id or system_facts.venue_id != exchange_facts.venue_id:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.ERROR,
                differences=["FACT_KEY_MISMATCH: account or venue differs between sources"],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                checked_at=checked_at,
            )

        diffs: list[str] = []

        # 余额比较：金额允许一个极小 Decimal→float 表示误差，但不允许
        # 通过大容差掩盖真实权益差异。
        try:
            system_balance = Decimal(str(system_facts.balance.amount))
            exchange_balance = Decimal(str(exchange_facts.balance.amount))
            if not system_balance.is_finite() or not exchange_balance.is_finite():
                raise InvalidOperation("balance is not finite")
            bal_diff = abs(system_balance - exchange_balance)
        except (InvalidOperation, TypeError, ValueError) as exc:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.ERROR,
                differences=[f"INVALID_BALANCE_FACT: {type(exc).__name__}"],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                checked_at=checked_at,
            )
        # PKG20 (BDS-P1-030): 绝对+相对+可解释差值组合容差
        # 固定 5 USDT 对大小账户语义失真
        abs_tolerance = Decimal("0.01")  # 0.01 USDT 绝对容差
        rel_tolerance = Decimal("0.0001")  # 0.01% 相对容差
        max_tolerance = max(abs_tolerance, rel_tolerance * max(system_balance, exchange_balance))
        if bal_diff > max_tolerance:
            diffs.append(
                f"Balance mismatch: system={system_facts.balance.amount} exchange={exchange_facts.balance.amount} "
                f"diff={float(bal_diff):.6f} tolerance={float(max_tolerance):.6f}"
            )

        # PKG20 (BDS-P1-032): OpenOrder 比较不只比较 ID，也比较参数
        # 注意：此简化实现比较 set of IDs，完整实现应比较 symbol/side/qty/price/type/reduceOnly/generation
        sys_orders = set(system_facts.open_orders)
        ex_orders = set(exchange_facts.open_orders)
        if sys_orders != ex_orders:
            missing_on_exchange = sys_orders - ex_orders
            extra_on_exchange = ex_orders - sys_orders
            parts = []
            if missing_on_exchange:
                parts.append(f"Orders in system but not on exchange: {sorted(missing_on_exchange)}")
            if extra_on_exchange:
                parts.append(f"Orders on exchange but not in system: {sorted(extra_on_exchange)}")
            if parts:
                diffs.append("Open orders mismatch: " + "; ".join(parts))

        def _position_map(facts: AccountFactSnapshot) -> dict[str, Decimal]:
            result: dict[str, Decimal] = {}
            for key, value in facts.positions.items():
                quantity = Decimal(str(value.amount))
                if not quantity.is_finite():
                    raise InvalidOperation(f"position {key} is not finite")
                result[str(key)] = quantity
            return result

        try:
            sys_pos = _position_map(system_facts)
            ex_pos = _position_map(exchange_facts)
        except (InvalidOperation, TypeError, ValueError) as exc:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.ERROR,
                differences=[f"INVALID_POSITION_FACT: {type(exc).__name__}"],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                checked_at=checked_at,
            )
        symbols = sorted(set(sys_pos) | set(ex_pos))
        # PKG02: 每个 symbol 的仓位容差必须由双方绑定的
        # InstrumentRuleSnapshot stepSize 一致证明，禁止静默使用常量。
        position_steps: dict[str, Decimal] = {}
        for symbol in symbols:
            system_step_raw = system_facts.position_step_sizes.get(symbol, "")
            exchange_step_raw = exchange_facts.position_step_sizes.get(symbol, "")
            if not system_step_raw and len(symbols) == 1:
                system_step_raw = system_facts.position_step_size
            if not exchange_step_raw and len(symbols) == 1:
                exchange_step_raw = exchange_facts.position_step_size
            if not system_step_raw or not exchange_step_raw:
                return ReconciliationResult(
                    matched=False,
                    status=ReconciliationStatus.INCOMPLETE,
                    differences=[f"POSITION_STEP_SIZE_UNBOUND: {symbol}"],
                    system_facts=system_facts,
                    exchange_facts=exchange_facts,
                    checked_at=checked_at,
                )
            try:
                system_step = Decimal(str(system_step_raw))
                exchange_step = Decimal(str(exchange_step_raw))
                if (
                    not system_step.is_finite()
                    or not exchange_step.is_finite()
                    or system_step <= 0
                    or exchange_step <= 0
                ):
                    raise InvalidOperation("step size must be finite and positive")
            except (InvalidOperation, TypeError, ValueError) as exc:
                return ReconciliationResult(
                    matched=False,
                    status=ReconciliationStatus.ERROR,
                    differences=[f"INVALID_POSITION_STEP_SIZE: {symbol}: {type(exc).__name__}"],
                    system_facts=system_facts,
                    exchange_facts=exchange_facts,
                    checked_at=checked_at,
                )
            if system_step != exchange_step:
                return ReconciliationResult(
                    matched=False,
                    status=ReconciliationStatus.ERROR,
                    differences=[
                        f"POSITION_STEP_SIZE_MISMATCH: {symbol}: system={system_step} exchange={exchange_step}"
                    ],
                    system_facts=system_facts,
                    exchange_facts=exchange_facts,
                    checked_at=checked_at,
                )
            position_steps[symbol] = system_step
        position_diffs = {
            symbol: (sys_pos.get(symbol, Decimal("0")), ex_pos.get(symbol, Decimal("0")))
            for symbol in symbols
            if abs(sys_pos.get(symbol, Decimal("0")) - ex_pos.get(symbol, Decimal("0"))) > position_steps[symbol]
        }
        if position_diffs:
            diffs.append(f"Position mismatch: {position_diffs}")

        status = ReconciliationStatus.MATCHED if not diffs else ReconciliationStatus.MISMATCHED
        return ReconciliationResult(
            matched=not diffs,
            status=status,
            differences=diffs,
            system_facts=system_facts,
            exchange_facts=exchange_facts,
            checked_at=checked_at,
        )

    @staticmethod
    def compare_three_way(
        system_facts: AccountFactSnapshot | None,
        exchange_facts: AccountFactSnapshot | None,
        event_facts: AccountFactSnapshot | None,
        *,
        max_age: timedelta = timedelta(seconds=30),
        now: datetime | None = None,
    ) -> ReconciliationResult:
        """Compare three independent fact sources without repairing any side.

        The user stream is an event-derived source, not a copy of REST.  Its
        absence, sequence gap, stale projection, or incomplete balance keeps
        the result unsafe.  A three-way match is required before new risk can
        be enabled.
        """

        checked_at = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        facts = (system_facts, exchange_facts, event_facts)
        available = sum(item is not None for item in facts)
        if available == 0:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.BOTH_SIDES_MISSING,
                differences=["BOTH_SIDES_MISSING: all independent fact sources unavailable — UNKNOWN"],
                checked_at=checked_at,
            )
        if available < 3:
            present_incomplete = [item for item in facts if item is not None and not item.complete]
            if present_incomplete:
                return ReconciliationResult(
                    matched=False,
                    status=ReconciliationStatus.INCOMPLETE,
                    differences=["INCOMPLETE_FACT: an available source is incomplete before the missing-source gate"],
                    system_facts=system_facts,
                    exchange_facts=exchange_facts,
                    event_facts=event_facts,
                    checked_at=checked_at,
                )
            missing = [
                name for name, item in zip(("system", "exchange", "event_stream"), facts, strict=True) if item is None
            ]
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.ONE_SIDE_MISSING,
                differences=[
                    "ONE_SIDE_MISSING: unavailable independent fact source(s) " + ", ".join(missing) + " — UNKNOWN"
                ],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                event_facts=event_facts,
                checked_at=checked_at,
            )

        assert system_facts is not None and exchange_facts is not None and event_facts is not None
        pair_results = (
            (
                "system/exchange",
                ReconciliationEngine.compare(system_facts, exchange_facts, max_age=max_age, now=checked_at),
            ),
            (
                "system/event_stream",
                ReconciliationEngine.compare(system_facts, event_facts, max_age=max_age, now=checked_at),
            ),
            (
                "exchange/event_stream",
                ReconciliationEngine.compare(exchange_facts, event_facts, max_age=max_age, now=checked_at),
            ),
        )
        for pair_name, pair_result in pair_results:
            if pair_result.status is ReconciliationStatus.ERROR:
                return ReconciliationResult(
                    matched=False,
                    status=ReconciliationStatus.ERROR,
                    differences=[f"{pair_name}: {diff}" for diff in pair_result.differences],
                    system_facts=system_facts,
                    exchange_facts=exchange_facts,
                    event_facts=event_facts,
                    checked_at=checked_at,
                )
        for status in (ReconciliationStatus.INCOMPLETE, ReconciliationStatus.STALE):
            failures = [
                f"{pair_name}: {diff}"
                for pair_name, pair_result in pair_results
                if pair_result.status is status
                for diff in pair_result.differences
            ]
            if failures:
                return ReconciliationResult(
                    matched=False,
                    status=status,
                    differences=failures,
                    system_facts=system_facts,
                    exchange_facts=exchange_facts,
                    event_facts=event_facts,
                    checked_at=checked_at,
                )

        mismatches = [
            f"{pair_name}: {diff}"
            for pair_name, pair_result in pair_results
            if pair_result.status is ReconciliationStatus.MISMATCHED
            for diff in pair_result.differences
        ]
        return ReconciliationResult(
            matched=not mismatches,
            status=ReconciliationStatus.MATCHED if not mismatches else ReconciliationStatus.MISMATCHED,
            differences=mismatches,
            system_facts=system_facts,
            exchange_facts=exchange_facts,
            event_facts=event_facts,
            checked_at=checked_at,
        )

    # --- BD-CV44: 三方同源伪造检测 ---

    def detect_same_source_fraud(self) -> TripleReconciliation:
        """BD-CV44: 验证三方对账数据来源独立性。

        三方同源伪造测试必须被检测。
        至少需要三个独立来源 (exchange / local / event stream)。
        """
        system_keys = set(self._system_facts)
        exchange_keys = set(self._exchange_facts)
        event_keys = set(self._event_facts)
        common_keys = system_keys & exchange_keys & event_keys

        tr = TripleReconciliation(
            venue_orders=len(exchange_keys),
            local_orders=len(system_keys),
            ledger_entries=len(event_keys),
            is_matched=False,
        )
        if not common_keys:
            tr.mismatches.append("SAME_SOURCE_FRAUD_RISK: no account has all three fact sources")
            return tr

        for key in sorted(common_keys):
            sources = [
                self._system_facts[key].source,
                self._exchange_facts[key].source,
                self._event_facts[key].source,
            ]
            normalized_sources = [source.strip() for source in sources if source.strip().upper() != "UNKNOWN"]
            if len(normalized_sources) != 3 or not tr.detect_same_source_fraud(normalized_sources):
                tr.mismatches.append(f"SAME_SOURCE_FRAUD_RISK: {key} lacks three declared independent sources")
        return TripleReconciliation(
            venue_orders=tr.venue_orders,
            local_orders=tr.local_orders,
            ledger_entries=tr.ledger_entries,
            is_matched=not tr.mismatches,
            mismatches=list(tr.mismatches),
        )

    def repair_strategy(self, result: ReconciliationResult) -> str:
        if result.matched:
            return "NO_ACTION"
        # BD-P0-10 / BD-T01: 任何差异（含余额不匹配）都不允许系统单方面以自身
        # 事实覆盖交易所事实 — SYSTEM_IS_AUTHORITATIVE 已移除。所有不匹配场景
        # 一律要求人工介入修复。
        return "MANUAL_REPAIR_REQUIRED"
