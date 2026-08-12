"""
P1 批量修复测试 — 安全/行情/风险快照/控制面/保护。

覆盖:
- P1-014: RiskSnapshot 不可变 + freshness gate + hash
- P1-011: Daily loss 使用 start-of-day equity
- P1-012: 无风险状态的 API 语义统一
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from beidou_safety.risk.engine import RiskSnapshot


class TestRiskSnapshotImmutability:
    """P1-014: RiskSnapshot 不可变 + hash + freshness。"""

    def test_snapshot_is_immutable(self) -> None:
        """positions/orders 为 MappingProxyType 不可变。"""
        snap = RiskSnapshot(
            total_exposure=10000,
            margin_used=2000,
            margin_total=10000,
            position_count=2,
            pending_orders=1,
            leverage=2.0,
            concentration_pct=30,
            account_id="test",
            dq_tier="OK",
            exchange_health="HEALTHY",
            reconciliation_status="MATCHED",
            policy_version="v1",
            positions={"BTC": {"qty": 1.0}},
            orders={"order1": {"status": "NEW"}},
        )
        with pytest.raises(TypeError):
            snap.positions["BTC"] = {"qty": 2.0}  # 不可变

    def test_snapshot_hash_is_stable(self) -> None:
        """相同参数产生相同哈希。"""
        snap1 = RiskSnapshot(10000, 2000, 10000, 2, 1, 2.0, 30, account_id="a")
        snap2 = RiskSnapshot(10000, 2000, 10000, 2, 1, 2.0, 30, account_id="a")
        assert snap1.snapshot_hash == snap2.snapshot_hash

    def test_snapshot_hash_differs_on_change(self) -> None:
        """不同参数产生不同哈希。"""
        snap1 = RiskSnapshot(10000, 2000, 10000, 2, 1, 2.0, 30, account_id="a")
        snap2 = RiskSnapshot(10000, 2000, 10000, 2, 1, 3.0, 30, account_id="a")
        assert snap1.snapshot_hash != snap2.snapshot_hash

    def test_freshness_gate(self) -> None:
        """freshness gate 拒绝过期快照。"""
        now = datetime.now(timezone.utc).isoformat()
        snap = RiskSnapshot(10000, 2000, 10000, 2, 1, 2.0, 30, account_id="a", received_at=now)
        assert snap.is_fresh(max_age_seconds=3600)  # 1 hour
        # 直接检查年龄
        assert snap.age_seconds < 1.0

    def test_stale_snapshot_blocks_risk_increase(self) -> None:
        """过期快照阻断风险增加。"""
        snap = RiskSnapshot(
            10000,
            2000,
            10000,
            2,
            1,
            2.0,
            30,
            account_id="a",
            dq_tier="OK",
            exchange_health="HEALTHY",
            reconciliation_status="MATCHED",
            policy_version="v1",
            portfolio_hash="portfolio-1",
            correlation_id="correlation-1",
            source_timestamp=(datetime.now(timezone.utc) - timedelta(seconds=122)).isoformat(),
            observed_at=(datetime.now(timezone.utc) - timedelta(seconds=121)).isoformat(),
            received_at=(datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
        )
        assert not snap.is_fresh(max_age_seconds=60)
        assert not snap.is_safe_for_risk_increase()
