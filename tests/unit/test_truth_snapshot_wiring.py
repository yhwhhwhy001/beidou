"""BD-FIX: TruthSnapshot 事实哈希接线 — 修复资格恒 NOT_VERIFIABLE 阻塞下单链。

回归保护:
- 10 个事实 hash 全部接线后 build_truth_snapshot 不再缺 hash
- 全部新鲜 + MATCHED/ACTIVE/NORMAL → ELIGIBLE
- 任一 hash 缺失仍 NOT_VERIFIABLE（fail-closed 不弱化）
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from beidou_control.plane import ControlAction
from beidou_control.truth import TradingEligibility, derive_eligibility
from beidou_core.engine import AutonomousEngine
from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationResult,
    ReconciliationStatus,
)
from beidou_shared.types import AccountId, MonetaryValue, Quantity, VenueId

_HASH_ATTRS: list[tuple[str, str]] = [
    ("_last_market_hash", "market"),
    ("_last_account_hash", "account"),
    ("_last_order_hash", "order"),
    ("_last_position_hash", "position"),
    ("_last_ledger_hash", "ledger"),
    ("_last_reconciliation_hash", "reconciliation"),
    ("_last_protection_hash", "protection"),
    ("_last_risk_hash", "risk"),
    ("_config_hash", "config"),
    ("_policy_hash", "policy"),
]

_FRESHNESS_ATTRS: list[str] = [
    "_last_market_fact_at",
    "_last_account_fact_at",
    "_last_order_fact_at",
    "_last_position_fact_at",
    "_last_ledger_fact_at",
    "_last_reconciliation_fact_at",
    "_last_protection_fact_at",
    "_last_risk_fact_at",
    "_config_observed_at",
    "_policy_observed_at",
]


def _wired_engine() -> AutonomousEngine:
    """最小 engine 实例 — 模拟全部接线点已运行后的状态。"""
    engine = AutonomousEngine.__new__(AutonomousEngine)
    now = time.time()
    for attr, _name in _HASH_ATTRS:
        setattr(engine, attr, f"hash-{attr}")
    for attr in _FRESHNESS_ATTRS:
        setattr(engine, attr, now)
    engine._last_reconciliation_result = SimpleNamespace(
        status=ReconciliationStatus.MATCHED,
        matched=True,
        differences=[],
    )
    engine._protection_owner_unknown = False
    engine._control = SimpleNamespace(_action=ControlAction.RESUME)  # type: ignore[assignment]  # fake
    engine._env_mode = SimpleNamespace(value="testnet")  # type: ignore[assignment]  # fake
    return engine


def _facts(timestamp: datetime | None = None) -> AccountFactSnapshot:
    return AccountFactSnapshot(
        account_id=AccountId("default"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount="100.0"),
        positions={"BTCUSDT": Quantity(amount="0.1")},
        open_orders=["o-1"],
        timestamp=timestamp or datetime.now(timezone.utc),
        complete=True,
    )


def test_build_truth_snapshot_reads_all_wired_hashes() -> None:
    engine = _wired_engine()
    snap = engine.build_truth_snapshot()
    assert not snap.is_empty()
    assert snap.missing_hashes() == []
    assert not snap.is_stale(max_age_seconds=300.0)


def test_fully_wired_fresh_facts_are_eligible() -> None:
    """完整正向: 全部事实新鲜 + MATCHED/ACTIVE/NORMAL → ELIGIBLE。"""
    engine = _wired_engine()
    snap = engine.build_truth_snapshot()
    assert snap.reconciliation_status == "MATCHED"
    assert snap.protection_status == "ACTIVE"
    assert snap.risk_status == "NORMAL"
    assert derive_eligibility(snap) == TradingEligibility.ELIGIBLE


@pytest.mark.parametrize("attr", [a for a, _name in _HASH_ATTRS])
def test_missing_any_hash_still_not_verifiable(attr: str) -> None:
    """负向保留: 任一 hash 缺失仍 NOT_VERIFIABLE（fail-closed 不弱化）。"""
    engine = _wired_engine()
    delattr(engine, attr)
    snap = engine.build_truth_snapshot()
    assert snap.missing_hashes()
    assert derive_eligibility(snap) == TradingEligibility.NOT_VERIFIABLE


def test_stale_facts_not_verifiable() -> None:
    engine = _wired_engine()
    for attr in _FRESHNESS_ATTRS:
        setattr(engine, attr, time.time() - 3600)
    assert derive_eligibility(engine.build_truth_snapshot()) == TradingEligibility.NOT_VERIFIABLE


def test_record_reconciliation_truth_wires_fact_hashes() -> None:
    """对账助手直接把结果与两侧事实接线为 TruthSnapshot 属性。"""
    engine = AutonomousEngine.__new__(AutonomousEngine)
    now = datetime.now(timezone.utc)
    system_facts = _facts(now)
    exchange_facts = _facts(now)
    result = ReconciliationResult(
        matched=True,
        status=ReconciliationStatus.MATCHED,
        differences=[],
        checked_at=now,
    )
    engine._record_reconciliation_truth(
        result,
        system_facts=system_facts,
        exchange_facts=exchange_facts,
    )

    assert engine._last_reconciliation_hash
    assert engine._last_reconciliation_fact_at > 0
    assert engine._last_account_hash
    assert engine._last_account_fact_at > 0
    assert engine._last_position_hash
    assert engine._last_position_fact_at > 0
    assert engine._last_order_hash
    assert engine._last_order_fact_at > 0
    assert engine._last_ledger_hash
    assert engine._last_ledger_fact_at > 0


def test_record_reconciliation_truth_without_facts_keeps_hashes_empty() -> None:
    """facts 为 None 时不赋值 — 缺证据保持 fail-closed。"""
    engine = AutonomousEngine.__new__(AutonomousEngine)
    result = ReconciliationResult(
        matched=False,
        status=ReconciliationStatus.ONE_SIDE_MISSING,
        differences=["ONE_SIDE_MISSING: ACCOUNT snapshot unavailable"],
        checked_at=datetime.now(timezone.utc),
    )
    engine._record_reconciliation_truth(result)

    assert engine._last_reconciliation_hash  # 对账结果本身仍被记录
    assert not hasattr(engine, "_last_account_hash")
    assert not hasattr(engine, "_last_position_hash")
    assert not hasattr(engine, "_last_order_hash")
    assert not hasattr(engine, "_last_ledger_hash")


def test_reconciliation_wiring_then_eligibility_flow() -> None:
    """对账助手接线 → build_truth_snapshot → ELIGIBLE 全链路。"""
    engine = _wired_engine()
    now = datetime.now(timezone.utc)
    facts = _facts(now)
    engine._record_reconciliation_truth(
        ReconciliationResult(
            matched=True,
            status=ReconciliationStatus.MATCHED,
            differences=[],
            checked_at=now,
        ),
        system_facts=facts,
        exchange_facts=facts,
    )
    assert derive_eligibility(engine.build_truth_snapshot()) == TradingEligibility.ELIGIBLE
