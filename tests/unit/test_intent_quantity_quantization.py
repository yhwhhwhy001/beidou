"""BD-FIX: 订单意图数量必须在创建/签名前按交易对规则快照量化。

回归保护：未量化的原始 sizing 值（如 0.015604628563982322）会在执行器
``_submit_order_slice`` 中被规则快照量化为 0.015，与意图内签名数量不一致，
触发 PLANNED_QUANTITY_NOT_VENUE_EXACT 恒拒（审批绑定语义要求意图创建前
就量化，不能在执行时改数量）。本测试覆盖：
- 量化逻辑本身（与执行器同源的 InstrumentRuleSnapshot.quantize_quantity）
- engine 的 _venue_exact_quantity helper（nearline 路径同款逻辑）
- 紧急平仓路径 enqueue_reduce_only_market 端到端：known 快照 → 意图数量
  venue-exact；unknown/stale 快照 → SKIP（不创建意图）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from beidou_core.engine import AutonomousEngine
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
from beidou_shared.types import RiskDecision


def _known_snapshot() -> InstrumentRuleSnapshot:
    """Binance 风格 LOT_SIZE（stepSize=0.001）的已知规则快照。"""
    return InstrumentRuleSnapshot.from_exchange_info(
        "BTCUSDT",
        {
            "symbol": "BTCUSDT",
            "version": 7,
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "MIN_NOTIONAL", "notional": "100"},
            ],
        },
    )


def _stale_snapshot() -> InstrumentRuleSnapshot:
    """规则字段已知但快照已过期（超过 3600s 新鲜度阈值）。"""
    snap = _known_snapshot()
    return InstrumentRuleSnapshot(
        symbol=snap.symbol,
        tick_size=snap.tick_size,
        step_size=snap.step_size,
        min_qty=snap.min_qty,
        min_notional=snap.min_notional,
        price_precision=snap.price_precision,
        qty_precision=snap.qty_precision,
        rule_version=snap.rule_version,
        observed_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    )


def _engine(*, snapshot: InstrumentRuleSnapshot | None = None) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    if snapshot is not None:
        engine._adapter = SimpleNamespace(get_rule_snapshot=lambda _s: snapshot)
    return engine


# --- 量化逻辑本身（与执行器 _submit_order_slice 同源）---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.015604628563982322", "0.015"),  # 原始自适应 sizing 值
        ("0.0156", "0.015"),
        ("0.001", "0.001"),  # 恰好等于 step
        ("0.0019", "0.001"),  # 向下取整，绝不向上产生额外风险
    ],
)
def test_quantize_quantity_rounds_down_to_venue_step(raw: str, expected: str) -> None:
    assert _known_snapshot().quantize_quantity(raw) == expected


def test_quantize_quantity_raises_when_rounding_to_zero() -> None:
    with pytest.raises(ValueError):
        _known_snapshot().quantize_quantity("0.0004")


# --- engine._venue_exact_quantity（nearline 路径同款逻辑）---


def test_venue_exact_quantity_quantizes_raw_sizing_value() -> None:
    engine = _engine(snapshot=_known_snapshot())
    assert engine._venue_exact_quantity("BTCUSDT", 0.015604628563982322) == 0.015


def test_venue_exact_quantity_returns_none_on_unknown_snapshot() -> None:
    engine = _engine(snapshot=InstrumentRuleSnapshot.unknown("BTCUSDT"))
    assert engine._venue_exact_quantity("BTCUSDT", 0.015604628563982322) is None


def test_venue_exact_quantity_returns_none_on_stale_snapshot() -> None:
    engine = _engine(snapshot=_stale_snapshot())
    assert engine._venue_exact_quantity("BTCUSDT", 0.015604628563982322) is None


def test_venue_exact_quantity_returns_none_on_quantization_failure() -> None:
    engine = _engine(snapshot=_known_snapshot())
    assert engine._venue_exact_quantity("BTCUSDT", 0.0004) is None


def test_venue_exact_quantity_passthrough_without_rule_snapshot_capability() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)  # 无 _adapter
    raw = 0.015604628563982322
    assert engine._venue_exact_quantity("BTCUSDT", raw) == raw


# --- 紧急平仓路径 enqueue_reduce_only_market 端到端 ---


class _ApprovalFake:
    def issue_for_approved_risk(self, *args: object, **kwargs: object) -> str:
        return "sig-fake"

    async def verify(self, *args: object, **kwargs: object) -> bool:
        return True


class _RiskSmFake:
    def approve_if_verified(self, *args: object, **kwargs: object) -> RiskDecision:
        return RiskDecision.APPROVED


class _ControlFake:
    def should_accept(self, intent: object) -> bool:
        return True


class _OutboxFake:
    def __init__(self) -> None:
        self.committed: list[object] = []

    def commit(self, intent: object) -> None:
        self.committed.append(intent)


def _emergency_engine(*, snapshot: InstrumentRuleSnapshot | None) -> AutonomousEngine:
    engine = _engine(snapshot=snapshot)
    engine._approval = _ApprovalFake()
    engine._risk_sm = _RiskSmFake()
    engine._control = _ControlFake()
    engine._outbox = _OutboxFake()
    return engine


@pytest.mark.asyncio
async def test_emergency_close_intent_quantity_is_venue_exact() -> None:
    engine = _emergency_engine(snapshot=_known_snapshot())
    ok = await engine.enqueue_reduce_only_market(
        symbol="BTCUSDT",
        side="SELL",
        quantity=0.015604628563982322,
        correlation_id=None,
        policy_id="policy-1",
        policy_version="v1",
        policy_signature="sig",
    )
    assert ok is True
    assert len(engine._outbox.committed) == 1
    committed = engine._outbox.committed[0]
    assert str(committed.quantity.amount) == "0.015"


@pytest.mark.asyncio
async def test_emergency_close_skips_on_unknown_rule_snapshot() -> None:
    engine = _emergency_engine(snapshot=InstrumentRuleSnapshot.unknown("BTCUSDT"))
    ok = await engine.enqueue_reduce_only_market(
        symbol="BTCUSDT",
        side="SELL",
        quantity=0.015604628563982322,
        correlation_id=None,
        policy_id="policy-1",
        policy_version="v1",
        policy_signature="sig",
    )
    assert ok is False
    assert engine._outbox.committed == []


@pytest.mark.asyncio
async def test_emergency_close_skips_on_stale_rule_snapshot() -> None:
    engine = _emergency_engine(snapshot=_stale_snapshot())
    ok = await engine.enqueue_reduce_only_market(
        symbol="BTCUSDT",
        side="SELL",
        quantity=0.015604628563982322,
        correlation_id=None,
        policy_id="policy-1",
        policy_version="v1",
        policy_signature="sig",
    )
    assert ok is False
    assert engine._outbox.committed == []
