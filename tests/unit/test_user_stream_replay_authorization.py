"""BD-FIX: testnet 自动授权 user stream replay baseline。

回归保护：Binance user stream 事件（ORDER_TRADE_UPDATE/ACCOUNT_UPDATE）
没有单调序列号，``UserStreamSequencer`` 要求显式 replay 授权
（``allow_unsequenced=True``）才接受事件。``authorize_user_stream_replay``
历史上没有任何调用点 → 下单后第一个成交事件必被拒
（USER_EVENT_REJECTED:ORDER_TRADE_UPDATE）→ fault → STOPPED →
DEGRADED → LOCKED → 停机。

修复语义（用户批准）：recon MATCHED（REST/系统/事件三方独立对拍）后，
testnet 自动授权 baseline；live/canary 保持人工治理授权语义不变。

覆盖：
- sequencer 处于 SEQUENCE_UNAVAILABLE 时 helper 授权成功，之后无序列
  事件可被 ingest（核心回归：复现 02:27 下单后停机场景）
- sequencer 已 HEALTHY 时幂等跳过（不重复授权）
- live 环境 / 不可写环境永不自动授权
- 授权失败不冻结账本、不发 incident（自动路径不得触发 fail-closed）
- _reconcile MATCHED 路径集成调用 helper
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from beidou_core.engine import AutonomousEngine
from beidou_exchange.core.protocol import UserOrderUpdate, UserStreamEvent
from beidou_exchange.core.user_stream import UserStreamStatus
from beidou_safety.execution.reconciliation import AccountFactSnapshot
from beidou_safety.execution.user_events import UserProjectionStatus, UserStreamProjector
from beidou_shared.types import (
    AccountId,
    InstrumentId,
    MonetaryValue,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    VenueId,
)


def _baseline_facts(*, complete: bool = True, source: str = "BINANCE_ACCOUNT_AND_OPEN_ORDERS") -> AccountFactSnapshot:
    """recon 事实快照（默认交易所侧来源）。"""
    return AccountFactSnapshot(
        account_id=AccountId("default"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount="10544.5", currency="USDT", decimals=8),
        positions={},
        open_orders=[],
        timestamp=datetime.now(timezone.utc),
        source=source,
        fact_version="rest-123",
        complete=complete,
    )


def _engine(
    *,
    env_mode: str = "testnet",
    can_write: bool = True,
    projector: UserStreamProjector | None = None,
) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value=env_mode, can_write_trades=can_write)
    engine._can_write = can_write
    engine._user_stream_projector = projector
    engine._user_stream_runtime = {}
    engine._event_stream_facts = None
    return engine


def _projector_requiring_replay() -> UserStreamProjector:
    """投影器 + sequencer 已进入 SEQUENCE_UNAVAILABLE（复现事故现场）。"""
    projector = UserStreamProjector(account_id=AccountId("default"), venue_id=VenueId("BINANCE"), store=None)
    sequencer = projector.sequencer
    event = UserStreamEvent(
        event_type="ORDER_TRADE_UPDATE",
        event_id="evt-1",
        event_time_ms=1786645000000,
        transaction_time_ms=None,
        sequence=None,
        raw_event={},
    )
    observation = sequencer.observe(event)  # 无序列事件 → SEQUENCE_UNAVAILABLE
    assert not observation.accepted
    assert sequencer.status is UserStreamStatus.SEQUENCE_UNAVAILABLE
    return projector


def _unsequenced_order_update(*, event_time_ms: int | None = None) -> UserOrderUpdate:
    if event_time_ms is None:
        # baseline 授权时间戳 = datetime.now()，事件必须在其之后
        event_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + 1000
    return UserOrderUpdate(
        event=UserStreamEvent(
            event_type="ORDER_TRADE_UPDATE",
            event_id="evt-2",
            event_time_ms=event_time_ms,
            transaction_time_ms=None,
            sequence=None,
            raw_event={},
        ),
        order_id="2623101001",
        client_order_id="beidou-bnbusdt-entry-1786645635",
        symbol=InstrumentId("BNBUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        order_status=OrderStatus.FILLED,
        execution_type="TRADE",
        original_quantity=Quantity(amount="0.01"),
        cumulative_quantity=Quantity(amount="0.01"),
        last_quantity=Quantity(amount="0.01"),
        last_price=Price(amount="608.29"),
        average_price=Price(amount="608.29"),
        trade_id="1001",
        commission=MonetaryValue(amount="0.0004", currency="BNB", decimals=8),
    )


# --- helper 单元行为 ---


def test_authorizes_baseline_when_sequencer_blocked() -> None:
    projector = _projector_requiring_replay()
    engine = _engine(projector=projector)

    assert engine._maybe_authorize_user_stream_baseline(_baseline_facts(), "recon-abc") is True
    assert projector.sequencer.status is UserStreamStatus.HEALTHY
    assert projector.sequencer._unsequenced_allowed is True
    # 核心回归：授权后无序列成交事件可被接受（02:27 停机场景的镜像）
    result = projector.ingest(_unsequenced_order_update())
    assert result.status is UserProjectionStatus.ACCEPTED


def test_skips_when_sequencer_healthy() -> None:
    projector = UserStreamProjector(account_id=AccountId("default"), venue_id=VenueId("BINANCE"), store=None)
    projector.sequencer.mark_replayed(None, allow_unsequenced=True, last_event_time_ms=1786645000000)
    engine = _engine(projector=projector)
    spy = Mock(wraps=projector.authorize_replay_baseline)
    projector.authorize_replay_baseline = spy  # type: ignore[method-assign]

    assert engine._maybe_authorize_user_stream_baseline(_baseline_facts(), "recon-abc") is False
    spy.assert_not_called()


def test_live_env_never_auto_authorizes() -> None:
    projector = _projector_requiring_replay()
    engine = _engine(env_mode="live", projector=projector)

    assert engine._maybe_authorize_user_stream_baseline(_baseline_facts(), "recon-abc") is False
    assert projector.sequencer.status is UserStreamStatus.SEQUENCE_UNAVAILABLE


def test_canary_env_never_auto_authorizes() -> None:
    projector = _projector_requiring_replay()
    engine = _engine(env_mode="canary", projector=projector)

    assert engine._maybe_authorize_user_stream_baseline(_baseline_facts(), "recon-abc") is False
    assert projector.sequencer.status is UserStreamStatus.SEQUENCE_UNAVAILABLE


def test_non_writable_env_never_authorizes() -> None:
    projector = _projector_requiring_replay()
    engine = _engine(env_mode="testnet", can_write=False, projector=projector)

    assert engine._maybe_authorize_user_stream_baseline(_baseline_facts(), "recon-abc") is False
    assert projector.sequencer.status is UserStreamStatus.SEQUENCE_UNAVAILABLE


def test_authorization_failure_does_not_freeze_or_incident() -> None:
    """自动授权失败只是 defer（保持人工授权语义），绝不触发 fail-closed。"""
    projector = _projector_requiring_replay()
    engine = _engine(projector=projector)
    ledger = Mock()
    alerts = Mock()
    engine._ledger = ledger
    engine._alerts = alerts

    incomplete = _baseline_facts(complete=False)  # complete=False → BLOCKED
    assert engine._maybe_authorize_user_stream_baseline(incomplete, "recon-abc") is False
    assert projector.sequencer.status is UserStreamStatus.SEQUENCE_UNAVAILABLE
    ledger.freeze.assert_not_called()
    alerts.send_incident.assert_not_called()


def test_returns_false_without_projector() -> None:
    engine = _engine(projector=None)
    assert engine._maybe_authorize_user_stream_baseline(_baseline_facts(), "recon-abc") is False


# --- 两方一致性守卫（helper result 参数）---


def _recon_result(*, differences: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        differences=differences,
        checked_at=datetime.now(timezone.utc),
        matched=not differences,
    )


def test_two_way_conflict_blocks_authorization() -> None:
    projector = _projector_requiring_replay()
    engine = _engine(projector=projector)

    result = _recon_result(differences=["system/exchange: BALANCE_MISMATCH"])
    assert engine._maybe_authorize_user_stream_baseline(_baseline_facts(), "recon-x", result=result) is False
    assert projector.sequencer.status is UserStreamStatus.SEQUENCE_UNAVAILABLE


def test_event_stream_only_incompleteness_does_not_block() -> None:
    """授权前 event_stream 侧必然 INCOMPLETE（鸡生蛋）——不构成授权障碍。"""
    projector = _projector_requiring_replay()
    engine = _engine(projector=projector)

    result = _recon_result(
        differences=[
            "system/event_stream: INCOMPLETE_FACT: system/exchange snapshot is not complete",
            "exchange/event_stream: INCOMPLETE_FACT: system/exchange snapshot is not complete",
        ]
    )
    assert engine._maybe_authorize_user_stream_baseline(_baseline_facts(), "recon-x", result=result) is True
    assert projector.sequencer.status is UserStreamStatus.HEALTHY


# --- _reconcile 死锁打破集成（真实事故场景）---


@pytest.mark.asyncio
async def test_reconcile_incomplete_event_stream_still_authorizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """三方对账 INCOMPLETE（event_stream 未授权）时两方一致 → 仍自动授权。

    镜像 02:42 现场：sequencer 拒绝态使 event_stream 永远 INCOMPLETE，
    若只在三方 MATCHED 后授权将永久死锁。授权后下一轮 recon 三方齐备。
    """
    projector = _projector_requiring_replay()
    engine = _engine(projector=projector)
    engine._last_account = {}
    engine._last_reconciliation_result = None
    engine._active_order_ids = set()
    # 投影器未授权 → 事件流事实 incomplete（真实死锁现场）
    engine._event_stream_facts = projector.fact_snapshot()

    account_payload = {
        "totalWalletBalance": 10544.5,
        "updateTime": 1786645000000,
        "positions": [{"symbol": "BNBUSDT", "positionAmt": 0}],
    }

    async def fake_api(endpoint: object, *, signed: bool) -> tuple[object, bool]:
        ep = str(endpoint).lower()
        if "account" in ep and "openorders" not in ep and "config" not in ep:
            return account_payload, True
        if "openorders" in ep:
            return [], True
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    engine._api_async_safe = fake_api  # type: ignore[method-assign]
    system_facts = _baseline_facts(source="LOCAL_DURABLE_PROJECTION")
    engine._build_system_reconciliation_facts = Mock(return_value=system_facts)  # type: ignore[method-assign]
    engine._store = SimpleNamespace(
        save_reconciliation_snapshot=Mock(),
        save_reconciliation_result=Mock(),
    )
    from beidou_safety.execution.reconciliation import ReconciliationEngine

    recon = ReconciliationEngine()
    engine._recon = recon
    engine._record_reconciliation_failure = Mock(return_value=False)  # type: ignore[method-assign]
    monkeypatch.setattr(engine, "_record_reconciliation_truth", Mock())

    ok = await engine._reconcile()
    # 本轮仍 INCOMPLETE（事件流在授权后才完整）——但授权已生效
    assert ok is False
    assert projector.sequencer.status is UserStreamStatus.HEALTHY
    assert projector.sequencer._unsequenced_allowed is True
    # 基线已建立即 complete（授权 = REST 独立验证 + listenKey 连接）；
    # 事件停流保护由运行时 event_age 检查承担
    assert projector.replay_baseline_verified is True
    assert engine._event_stream_facts.complete is True
