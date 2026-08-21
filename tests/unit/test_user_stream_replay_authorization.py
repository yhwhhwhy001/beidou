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

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from beidou_core.engine import AutonomousEngine
from beidou_exchange.core.protocol import UserOrderUpdate, UserStreamEvent
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
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


def _baseline_facts(
    *,
    complete: bool = True,
    source: str = "BINANCE_ACCOUNT_AND_OPEN_ORDERS",
    timestamp: datetime | None = None,
) -> AccountFactSnapshot:
    """recon 事实快照（默认交易所侧来源）。"""
    return AccountFactSnapshot(
        account_id=AccountId("default"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount="10544.5", currency="USDT", decimals=8),
        positions={},
        open_orders=[],
        timestamp=timestamp or datetime.now(timezone.utc),
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
    observation = sequencer.observe(event)  # 无序列事件 → 逐条拒绝
    assert not observation.accepted
    # BD-FIX (TRADE_LITE): 无序列事件不再把 sequencer 永久翻成
    # SEQUENCE_UNAVAILABLE;事故现场(需要 replay 才能继续的阻塞态)
    # 显式构造,保证下游 fail-closed 断言仍有意义。
    sequencer._status = UserStreamStatus.SEQUENCE_UNAVAILABLE
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


def test_unsequenced_event_older_than_baseline_is_accepted() -> None:
    """跨时钟源比较不可靠：demo 服务器时钟落后于本机，事件时间早于
    baseline 授权时间，旧逻辑判 DUPLICATE 拒绝真实成交事件（14:11 现场）。
    unsequenced 模式按到达顺序接受，event_id 去重防真重复。"""
    projector = UserStreamProjector(account_id=AccountId("default"), venue_id=VenueId("BINANCE"), store=None)
    result = projector.authorize_replay_baseline(
        _baseline_facts(),
        evidence_hash="hash123",
        approval_id="test-approval",
        last_sequence=None,
        allow_unsequenced=True,
    )
    assert result.status is UserProjectionStatus.ACCEPTED
    # 事件时间早于 baseline（交易所时钟落后 2 分钟）
    past_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 120_000
    accepted = projector.ingest(_unsequenced_order_update(event_time_ms=past_ms))
    assert accepted.status is UserProjectionStatus.ACCEPTED
    assert projector.sequencer.status is UserStreamStatus.HEALTHY


def test_true_duplicate_event_id_still_rejected_after_authorization() -> None:
    """event_id 去重保留 —— 时间比较移除不削弱防重复。"""
    projector = UserStreamProjector(account_id=AccountId("default"), venue_id=VenueId("BINANCE"), store=None)
    projector.authorize_replay_baseline(
        _baseline_facts(),
        evidence_hash="hash123",
        approval_id="test-approval",
        last_sequence=None,
        allow_unsequenced=True,
    )
    update = _unsequenced_order_update()
    assert projector.ingest(update).status is UserProjectionStatus.ACCEPTED
    again = projector.ingest(update)  # 同 event_id 重放
    assert again.status is UserProjectionStatus.DUPLICATE


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


# --- 对账事实新鲜度阈值环境化 ---


def test_recon_max_age_testnet_relaxed_to_300s() -> None:
    """testnet 低频事件流：30s 无新事件即 STALE 恒失败；放宽到 300s，
    与运行时 readiness 的 event_age 阈值（CONNECTED 时 300s）一致。"""
    from beidou_core.engine import _reconciliation_max_age_seconds

    assert _reconciliation_max_age_seconds("testnet") == 300.0


def test_recon_max_age_strict_for_production_envs() -> None:
    from beidou_core.engine import _reconciliation_max_age_seconds

    assert _reconciliation_max_age_seconds("live") == 30.0
    assert _reconciliation_max_age_seconds("canary") == 30.0
    assert _reconciliation_max_age_seconds("paper") == 30.0


def test_recently_matched_reconciliation_false_when_never_matched() -> None:
    engine = _engine()
    assert engine._recently_matched_reconciliation(max_age_seconds=300.0) is False


def test_recently_matched_reconciliation_window_bounds() -> None:
    import time

    engine = _engine()
    engine._last_matched_reconciliation_mono = time.monotonic() - 120.0
    assert engine._recently_matched_reconciliation(max_age_seconds=300.0) is True
    assert engine._recently_matched_reconciliation(max_age_seconds=60.0) is False  # 超窗


def test_recently_matched_reconciliation_survives_recent_failure() -> None:
    """瞬时 ONE_SIDE_MISSING 不清空"近期 MATCHED"时间戳(EXEMPT-06 语义)。

    失败路径只更新 _last_reconciliation_result(最近一次),不触碰
    _last_matched_reconciliation_mono —— 快照风控门(testnet)在失败窗口内
    仍按"近期有过 MATCHED"放行;严格语义(cleanup/live)按最近一次判定不变。
    """
    import time

    engine = _engine()
    engine._last_matched_reconciliation_mono = time.monotonic() - 30.0
    engine._last_reconciliation_result = SimpleNamespace(matched=False, checked_at=datetime.now(timezone.utc))
    assert engine._recently_matched_reconciliation(max_age_seconds=300.0) is True
    assert engine._fresh_matched_reconciliation(max_age_seconds=300.0) is False


def test_recon_max_age_300s_accepts_32s_old_event_facts() -> None:
    """镜像 03:04 现场：授权后投影冻结 32s → 30s 阈值 STALE；
    300s 阈值下对账正常比较（不再 STALE）。"""
    from datetime import timedelta

    from beidou_safety.execution.reconciliation import ReconciliationEngine

    engine = ReconciliationEngine(max_age_seconds=300.0)
    old_timestamp = datetime.now(timezone.utc) - timedelta(seconds=32)
    stale_facts = _baseline_facts(timestamp=old_timestamp)
    result = engine.compare(stale_facts, _baseline_facts())
    assert result.status.value != "STALE"


# --- 三方对账事件侧免 stale（transport 证明流活性）---


def test_compare_three_way_event_side_stale_exempt() -> None:
    """事件流侧时间戳冻结（低频无事件）不构成 STALE —— 流活性由
    transport readiness 证明；三方一致性仍完整比较。"""
    from datetime import timedelta

    from beidou_safety.execution.reconciliation import ReconciliationEngine

    old_event = _baseline_facts(
        source="BINANCE_USER_STREAM_REPLAY_BASELINE",
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=32),
    )
    result = ReconciliationEngine.compare_three_way(
        _baseline_facts(source="LOCAL_DURABLE_PROJECTION"),
        _baseline_facts(),
        old_event,
    )
    assert result.status.value != "STALE"
    assert result.matched is True  # 三侧余额/持仓一致


def test_compare_three_way_system_side_stale_still_fails() -> None:
    """system/exchange 侧新鲜度检查保留 —— 豁免仅限事件流侧。"""
    from datetime import timedelta

    from beidou_safety.execution.reconciliation import ReconciliationEngine

    old_system = _baseline_facts(
        source="LOCAL_DURABLE_PROJECTION",
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=32),
    )
    result = ReconciliationEngine.compare_three_way(
        old_system,
        _baseline_facts(),
        _baseline_facts(source="BINANCE_USER_STREAM_REPLAY_BASELINE"),
    )
    assert result.status.value == "STALE"


# --- readiness: testnet transport 模式 / live 严格 event_age ---


def _authorized_projector() -> UserStreamProjector:
    projector = UserStreamProjector(account_id=AccountId("default"), venue_id=VenueId("BINANCE"), store=None)
    result = projector.authorize_replay_baseline(
        _baseline_facts(),
        evidence_hash="hash123",
        approval_id="test-approval",
        last_sequence=None,
        allow_unsequenced=True,
    )
    assert result.status is UserProjectionStatus.ACCEPTED
    return projector


def _ready_engine(
    *,
    env_mode: str,
    transport_status: str,
    projector: UserStreamProjector,
    last_event_mono: float | None = None,
) -> AutonomousEngine:
    import time

    engine = _engine(env_mode=env_mode, projector=projector)
    engine._user_stream_runtime = {
        "status": transport_status,
        "listen_key_active": True,
        "last_error": "",
    }
    if last_event_mono is not None:
        engine._user_stream_runtime["last_event_mono"] = last_event_mono
    engine._event_stream_facts = projector.fact_snapshot()
    engine._startup_mono = time.monotonic() - 700.0  # 已过启动宽限期
    return engine


def test_readiness_testnet_ready_without_recent_events() -> None:
    import time

    engine = _ready_engine(
        env_mode="testnet",
        transport_status="CONNECTED",
        projector=_authorized_projector(),
        last_event_mono=time.monotonic() - 1000.0,  # 停流 1000s
    )
    ready, _ = engine._user_stream_readiness()
    assert ready is True


def test_readiness_live_requires_recent_events() -> None:
    import time

    engine = _ready_engine(
        env_mode="live",
        transport_status="CONNECTED",
        projector=_authorized_projector(),
        last_event_mono=time.monotonic() - 1000.0,  # 停流 1000s → 严格 FAIL
    )
    ready, _ = engine._user_stream_readiness()
    assert ready is False


def test_readiness_testnet_stopped_transport_still_fails() -> None:
    """testnet 放宽的只是事件新鲜度 —— transport 断开仍 fail-closed。"""
    engine = _ready_engine(
        env_mode="testnet",
        transport_status="STOPPED",
        projector=_authorized_projector(),
    )
    ready, _ = engine._user_stream_readiness()
    assert ready is False


def test_readiness_testnet_unprojected_sequencer_still_fails() -> None:
    """testnet 放宽的只是事件新鲜度 —— 未授权 sequencer 仍 fail-closed。"""
    engine = _ready_engine(
        env_mode="testnet",
        transport_status="CONNECTED",
        projector=_projector_requiring_replay(),  # SEQUENCE_UNAVAILABLE
    )
    ready, _ = engine._user_stream_readiness()
    assert ready is False


# --- 切片数量量化（修复 PLANNED_QUANTITY_NOT_VENUE_EXACT 复发）---


def _snapshot_with_step(step: str) -> InstrumentRuleSnapshot:
    return InstrumentRuleSnapshot.from_exchange_info(
        "BNBUSDT",
        {
            "symbol": "BNBUSDT",
            "version": 7,
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": step, "stepSize": step},
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        },
    )


def test_slice_quantity_quantized_to_venue_step() -> None:
    """算法切片浮点乘积（0.02×0.8=0.016）必须量化到 venue step。"""
    engine = _engine()
    engine._adapter = SimpleNamespace(get_rule_snapshot=lambda _s: _snapshot_with_step("0.01"))
    assert engine._quantize_slice_quantity("BNBUSDT", "0.016") == "0.01"


def test_slice_quantity_unchanged_when_already_exact() -> None:
    engine = _engine()
    engine._adapter = SimpleNamespace(get_rule_snapshot=lambda _s: _snapshot_with_step("0.001"))
    assert engine._quantize_slice_quantity("BNBUSDT", "0.016") == "0.016"


def test_slice_quantity_none_on_unknown_snapshot() -> None:
    engine = _engine()
    engine._adapter = SimpleNamespace(get_rule_snapshot=lambda _s: InstrumentRuleSnapshot.unknown("BNBUSDT"))
    assert engine._quantize_slice_quantity("BNBUSDT", "0.016") is None


def test_slice_quantity_none_when_quantization_to_zero() -> None:
    engine = _engine()
    engine._adapter = SimpleNamespace(get_rule_snapshot=lambda _s: _snapshot_with_step("0.1"))
    assert engine._quantize_slice_quantity("BNBUSDT", "0.016") is None


def test_slice_quantity_none_without_adapter() -> None:
    engine = _engine()  # 无 _adapter
    assert engine._quantize_slice_quantity("BNBUSDT", "0.016") is None


# --- user stream 故障与账本冻结解耦（testnet 可恢复）---


def test_user_stream_fault_does_not_freeze_ledger_on_testnet() -> None:
    """demo ws 抖动（临时连接失败）不得永久冻结账本 —— freeze 无解冻路径。"""
    engine = _engine(env_mode="testnet")
    engine._user_stream_runtime = {}
    engine._ledger = Mock()
    engine._alerts = Mock()
    engine._control = Mock(get_status=Mock(return_value="NO_NEW_RISK"))
    engine._user_stream_restart_attempts = 99  # 超出上限，不调度重启任务

    engine._user_stream_fault("TEST_CONNECTION_FAILED", terminal=True)

    engine._ledger.freeze.assert_not_called()
    # user_stream incident 仍发送（自身 category）
    user_stream_calls = [
        c for c in engine._alerts.send_incident.call_args_list if c.kwargs.get("category") == "user_stream"
    ]
    assert user_stream_calls, "user_stream incident must still be raised"


def test_user_stream_fault_freezes_ledger_on_live() -> None:
    """live/canary 保留 freeze 语义 —— 真实资金下成交事件丢失不可接受。"""
    engine = _engine(env_mode="live")
    engine._user_stream_runtime = {}
    engine._ledger = Mock()
    engine._alerts = Mock()
    engine._control = Mock(get_status=Mock(return_value="NO_NEW_RISK"))
    engine._user_stream_restart_attempts = 99

    engine._user_stream_fault("TEST_CONNECTION_FAILED", terminal=True)

    engine._ledger.freeze.assert_called_once()
    assert engine._ledger.freeze.call_count == 1


# --- 事件拒绝与账本冻结解耦（testnet 可恢复）---


def _engine_with_reject_path(env_mode: str) -> tuple[AutonomousEngine, Mock, Mock]:
    engine = _engine(env_mode=env_mode)
    ledger = Mock()
    alerts = Mock()
    engine._ledger = ledger
    engine._alerts = alerts
    return engine, ledger, alerts


def test_ingest_rejection_does_not_freeze_ledger_on_testnet() -> None:
    """投影器拒绝事件（流问题）不冻结账本 —— freeze 无解冻路径。

    镜像 14:11 现场：ORDER_TRADE_UPDATE 被 sequencer 判 DUPLICATE →
    ingest 返回 False → 旧代码 _record_execution_fact_failure → 账本
    永久冻结。testnet 只降级控制面，不 freeze；live/canary 保留。
    """
    engine, ledger, _alerts = _engine_with_reject_path("testnet")
    engine._control = Mock(get_status=Mock(return_value="NO_NEW_RISK"))
    engine._event_stream_facts = None
    engine._recon = Mock()
    projector = _projector_requiring_replay()
    engine._user_stream_projector = projector
    engine._update_user_stream_runtime = Mock()  # type: ignore[method-assign]

    result = projector.ingest(_unsequenced_order_update())
    assert result.status is UserProjectionStatus.BLOCKED  # sequencer 拒绝态

    # ingest_user_order_update 在 BLOCKED 时走记录路径
    engine.ingest_user_order_update(_unsequenced_order_update())
    ledger.freeze.assert_not_called()


def test_ingest_rejection_freezes_ledger_on_live() -> None:
    engine, ledger, _alerts = _engine_with_reject_path("live")
    engine._control = Mock(get_status=Mock(return_value="NO_NEW_RISK"))
    engine._event_stream_facts = None
    engine._recon = Mock()
    engine._user_stream_projector = _projector_requiring_replay()

    engine.ingest_user_order_update(_unsequenced_order_update())
    ledger.freeze.assert_called_once()
    assert ledger.freeze.call_count == 1


# --- 事故自动清理（与 RESUME 动作解耦）---


def _incident(category: str) -> Mock:
    return Mock(root_cause_category=category)


def test_auto_resolve_execution_fact_regardless_of_control_state() -> None:
    """事实干净即 resolve —— 控制面已 RESUME 不再阻塞事故清理（旧死角）。"""
    engine = _engine()
    engine._user_stream_runtime = {"status": "CONNECTED"}
    alerts = Mock()
    alerts._active_incidents = {"i1": _incident("execution_fact"), "i2": _incident("execution_fact")}
    engine._alerts = alerts

    engine._maybe_auto_resolve_incidents()

    assert alerts.resolve_incident.call_count == 2


def test_auto_resolve_user_stream_requires_healthy_transport() -> None:
    engine = _engine()
    engine._user_stream_runtime = {"status": "CONNECTED"}
    alerts = Mock()
    alerts._active_incidents = {"i1": _incident("user_stream")}
    engine._alerts = alerts

    engine._maybe_auto_resolve_incidents()
    alerts.resolve_incident.assert_called_once()
    assert alerts.resolve_incident.call_count == 1

    # transport 未恢复 → 不 resolve
    engine2 = _engine()
    engine2._user_stream_runtime = {"status": "FAILED"}
    alerts2 = Mock()
    alerts2._active_incidents = {"i1": _incident("user_stream")}
    engine2._alerts = alerts2
    engine2._maybe_auto_resolve_incidents()
    alerts2.resolve_incident.assert_not_called()
    assert alerts2.resolve_incident.call_count == 0


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
    # testnet 下事件侧差异降 WARN（用户批准）：两方一致即 MATCHED，
    # 事件侧 INCOMPLETE 不阻断；自动授权仍发生
    assert ok is True
    assert projector.sequencer.status is UserStreamStatus.HEALTHY
    assert projector.sequencer._unsequenced_allowed is True
    # 基线已建立即 complete（授权 = REST 独立验证 + listenKey 连接）；
    # 事件停流保护由运行时 event_age 检查承担
    assert projector.replay_baseline_verified is True
    assert engine._event_stream_facts.complete is True


# --- 杠杆推导清算价（demo 快照缺 liquidationPrice）---


def test_derive_liquidation_price_long_and_short() -> None:
    from beidou_core.engine import _derive_liquidation_price

    # 3x 多头：entry 608.43 → liq = 608.43 × 2/3 = 405.62
    long_liq = _derive_liquidation_price(0.01, 608.43, 3.0)
    assert long_liq is not None
    assert abs(long_liq - 608.43 * 2 / 3) < 1e-9
    # 3x 空头：entry 608.43 → liq = 608.43 × 4/3 = 811.24
    short_liq = _derive_liquidation_price(-0.01, 608.43, 3.0)
    assert short_liq is not None
    assert abs(short_liq - 608.43 * 4 / 3) < 1e-9


def test_derive_liquidation_price_invalid_inputs_return_none() -> None:
    from beidou_core.engine import _derive_liquidation_price

    assert _derive_liquidation_price(0.0, 608.43, 3.0) is None  # 无持仓
    assert _derive_liquidation_price(0.01, 0.0, 3.0) is None  # 非法 entry
    assert _derive_liquidation_price(0.01, 608.43, 0.0) is None  # 非法杠杆
    assert _derive_liquidation_price(0.01, float("nan"), 3.0) is None


def test_external_order_terminal_event_syncs_order_state() -> None:
    """外部订单终态事件回落 order_state(G5 partial_fill 锁盘回归)。

    镜像现场: 共享 demo 账户的认证探针单(不在 _active_order_ids)部分
    成交后 CANCELED —— fill 记账已写 order_state=PARTIALLY_FILLED,
    终态事件必须把它翻成 CANCELED,否则 system 侧恒多出 open order
    → 对账 MISMATCHED 锁盘(LABUSDT 1525093545 实证)。
    """
    projector = _authorized_projector()
    engine = _engine(projector=projector)
    engine._outbox = Mock(project_user_order_update=Mock())
    store = Mock()
    engine._store = store
    engine._active_order_ids = set()
    engine._recon = Mock()

    terminal = replace(
        _unsequenced_order_update(),
        order_status=OrderStatus.CANCELED,
        cumulative_quantity=Quantity(amount="0.5"),
        original_quantity=Quantity(amount="1.0"),
    )
    assert engine.ingest_user_order_update(terminal) is True
    store.save_order_state.assert_called_once()
    args, _kwargs = store.save_order_state.call_args
    assert args[0] == "2623101001"
    assert args[6] == "CANCELED"  # status 终态,store 单调守卫安全
    assert args[7] == "0.5"  # filled_qty 保留成交事实


def test_external_order_fill_event_does_not_touch_terminal_sync() -> None:
    """非终态事件不触发 order_state 终态同步(写放大防护)。"""
    projector = _authorized_projector()
    engine = _engine(projector=projector)
    engine._outbox = Mock(project_user_order_update=Mock())
    store = Mock()
    engine._store = store
    engine._active_order_ids = set()
    engine._recon = Mock()

    partial = replace(
        _unsequenced_order_update(),
        order_status=OrderStatus.PARTIALLY_FILLED,
        cumulative_quantity=Quantity(amount="0.5"),
    )
    assert engine.ingest_user_order_update(partial) is True
    # PARTIALLY_FILLED 走 fill 记账路径(_consume_cumulative_fill),不写
    # 终态同步分支;该路径需要更完整的引擎状态,此处仅断言不因缺失
    # 组件崩溃、且不误写终态。
    store.save_order_state.assert_not_called()
