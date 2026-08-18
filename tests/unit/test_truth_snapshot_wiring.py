"""BD-FIX: TruthSnapshot 事实哈希接线 — 修复资格恒 NOT_VERIFIABLE 阻塞下单链。

回归保护:
- 10 个事实 hash 全部接线后 build_truth_snapshot 不再缺 hash
- 全部新鲜 + MATCHED/ACTIVE/NORMAL → ELIGIBLE
- 任一 hash 缺失仍 NOT_VERIFIABLE（fail-closed 不弱化）
"""

from __future__ import annotations

import hashlib
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
    """最小 engine 实例 — 模拟全部接线点已运行后的状态。

    protection 的 hash 必须与真实评估记录一致（"ACTIVE" 的 sha256），
    因为 protection_status 跟随记录的 hash（fail-closed 机制）。
    """
    engine = AutonomousEngine.__new__(AutonomousEngine)
    now = time.time()
    for attr, _name in _HASH_ATTRS:
        if attr == "_last_protection_hash":
            setattr(engine, attr, hashlib.sha256(b"ACTIVE").hexdigest())
        else:
            setattr(engine, attr, f"hash-{attr}")
    for attr in _FRESHNESS_ATTRS:
        setattr(engine, attr, now)
    engine._last_reconciliation_result = SimpleNamespace(
        status=ReconciliationStatus.MATCHED,
        matched=True,
        differences=[],
    )
    engine._protection_owner_unknown = False
    engine._control = SimpleNamespace(
        _action=ControlAction.RESUME,
        # M19-F01: build_truth_snapshot 同步快照到控制面 — fake 需提供入口
        update_truth_snapshot=lambda snap: None,
    )  # type: ignore[assignment]  # fake
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


def test_stale_protection_fact_not_verifiable_until_evaluator_refresh() -> None:
    """保护事实由评估方周期刷新(根因修复):快照构建是只读投影,不再
    构建时刷新 hash/时间戳。陈旧事实必须 fail-closed(NOT_VERIFIABLE),
    直到评估方(_update_protection_fact)重新验证并写入。"""
    engine = _wired_engine()
    engine._protection_issues = set()
    engine._protection_owner_unknown = False
    engine._last_protection_fact_at = time.time() - 3600
    # 陈旧事实 → 快照不得自行刷新 → NOT_VERIFIABLE
    assert derive_eligibility(engine.build_truth_snapshot()) == TradingEligibility.NOT_VERIFIABLE
    # 评估方(库存验证通过)刷新 → 恢复 ELIGIBLE
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)
    assert derive_eligibility(engine.build_truth_snapshot()) == TradingEligibility.ELIGIBLE


def test_pending_protection_issues_keep_status_unknown_fail_closed() -> None:
    """问题集未清除时,即使 hash 为 ACTIVE 也不得放行(纯读判定)。

    根因修复:owner_unknown / hash 由写入方维护,快照额外直接检查问题集,
    覆盖"评估方写入间隔内新问题产生"的窗口。
    """
    engine = _wired_engine()
    engine._protection_owner_unknown = False
    engine._protection_issues = {"PROTECTION_OWNER_MAPPING_INCOMPLETE"}
    snap = engine.build_truth_snapshot()
    assert snap.protection_status == "UNKNOWN"
    assert derive_eligibility(snap) == TradingEligibility.NO_NEW_RISK


def test_stale_risk_fact_not_verifiable_then_refresh_eligible() -> None:
    """风险事实 300s 未刷新 → NOT_VERIFIABLE；近线周期刷新后 → ELIGIBLE。"""
    engine = _wired_engine()
    engine._last_risk_fact_at = time.time() - 3600
    assert derive_eligibility(engine.build_truth_snapshot()) == TradingEligibility.NOT_VERIFIABLE
    # 近线 tick 起始处周期风险快照刷新
    engine._last_risk_hash = hashlib.sha256(b"periodic_risk:NORMAL").hexdigest()
    engine._last_risk_fact_at = time.time()
    assert derive_eligibility(engine.build_truth_snapshot()) == TradingEligibility.ELIGIBLE


def test_unknown_protection_hash_keeps_status_unknown_fail_closed() -> None:
    """覆盖评估失败（hash=UNKNOWN）时，即使 owner 标志为 False 也不进入 ACTIVE。

    BD-FIX: testnet 保护状态按 owner_unknown 判定（共享账户语义），
    本测试用 live 语义验证 hash 严格路径不变。
    """
    engine = _wired_engine()
    engine._env_mode = SimpleNamespace(value="live")
    engine._last_protection_hash = hashlib.sha256(b"UNKNOWN").hexdigest()
    snap = engine.build_truth_snapshot()
    assert snap.protection_status == "UNKNOWN"
    assert derive_eligibility(snap) == TradingEligibility.NO_NEW_RISK


def _durable_engine() -> AutonomousEngine:
    """最小 engine 实例 — _durable_fact_status 可通过的持久事实 fake。"""
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace(restore_order_states=lambda: [], restore_protections=lambda: [])  # type: ignore[assignment]  # fake
    engine._outbox = SimpleNamespace(stats=lambda: {"state_counts": {}})  # type: ignore[assignment]  # fake
    engine._active_order_ids = set()
    engine._owned_order_ids = set()
    engine._protection_owner_id = "owner-1"
    engine._protection = SimpleNamespace(all_positions=lambda: {})  # type: ignore[assignment]  # fake
    engine._last_account = {"positions": []}
    return engine


def test_durable_fact_status_refreshes_freshness_without_writing_hash() -> None:
    """覆盖 OK 时 _durable_fact_status 只读判定并刷新新鲜度(根因修复):
    hash 不再由本方法写入 —— 放行唯一入口是 _update_protection_fact,
    消除多写入方竞争造成的 ACTIVE/UNKNOWN 抖动。"""
    engine = _durable_engine()
    engine._last_protection_fact_at = 0.0  # 陈旧
    engine._protection_issues = set()
    ok, reason, _evidence = engine._durable_fact_status()
    assert ok and reason == "DURABLE_FACTS_VERIFIED"
    assert not hasattr(engine, "_last_protection_hash")  # 未写入 hash
    assert time.time() - engine._last_protection_fact_at < 5


def test_durable_fact_status_coverage_gap_fails_closed_without_writing_hash() -> None:
    """覆盖缺失时 _durable_fact_status fail-closed(根因修复):
    判定结果只读返回,不写 hash;门禁状态由问题集 + _update_protection_fact
    维护,快照判定确定且不随调用时点抖动。"""
    engine = _durable_engine()
    engine._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1.0"}]}
    engine._protection_issues = set()
    ok, reason, _evidence = engine._durable_fact_status()
    assert not ok and reason == "DURABLE_PROTECTION_COVERAGE_UNKNOWN"
    assert not hasattr(engine, "_last_protection_hash")  # 未写入 hash
    assert time.time() - engine._last_protection_fact_at < 5
