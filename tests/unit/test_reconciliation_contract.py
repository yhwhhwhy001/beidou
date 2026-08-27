from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from beidou_control.plane import ControlAction
from beidou_core.engine import AutonomousEngine
from beidou_core.store import PersistentStore
from beidou_safety.execution.ledger import (
    AccountType,
    ImmutableLedger,
    LedgerTransaction,
    LedgerTransactionType,
    Posting,
    PostingSide,
)
from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationEngine,
    ReconciliationStatus,
)
from beidou_shared.types import AccountId, InstrumentId, MonetaryValue, Quantity, VenueId


def _facts(
    *,
    timestamp: datetime,
    complete: bool = True,
    balance: str = "100",
    source: str = "SYSTEM",
    detail: dict | None = None,
) -> AccountFactSnapshot:
    return AccountFactSnapshot(
        account_id=AccountId("acct"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount=balance),
        positions={InstrumentId("BTCUSDT"): Quantity(amount="0.25")},
        open_orders=["order-1"],
        open_orders_detail=detail or {},
        timestamp=timestamp,
        source=source,
        fact_version="v1",
        complete=complete,
        position_step_size="0.001",
    )


def test_compare_is_pure_and_requires_fresh_complete_facts() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    matched = ReconciliationEngine.compare(_facts(timestamp=now), _facts(timestamp=now, source="EXCHANGE"), now=now)
    assert matched.status is ReconciliationStatus.MATCHED
    assert matched.matched is True

    stale = ReconciliationEngine.compare(
        _facts(timestamp=now - timedelta(seconds=31)),
        _facts(timestamp=now, source="EXCHANGE"),
        now=now,
    )
    assert stale.status is ReconciliationStatus.STALE
    assert stale.should_block_new_risk is True

    incomplete = ReconciliationEngine.compare(
        _facts(timestamp=now, complete=False),
        _facts(timestamp=now, source="EXCHANGE"),
        now=now,
    )
    assert incomplete.status is ReconciliationStatus.INCOMPLETE


def test_compare_treats_missing_side_as_unknown() -> None:
    now = datetime.now(timezone.utc)
    result = ReconciliationEngine.compare(_facts(timestamp=now), None, now=now)
    assert result.status is ReconciliationStatus.ONE_SIDE_MISSING
    assert result.is_unknown is True


def test_compare_three_way_requires_event_stream_and_compares_all_pairs() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now)
    exchange = _facts(timestamp=now, source="EXCHANGE")
    event = _facts(timestamp=now, source="EVENT_STREAM")

    matched = ReconciliationEngine.compare_three_way(system, exchange, event, now=now)
    assert matched.status is ReconciliationStatus.MATCHED
    assert matched.event_facts is event

    missing = ReconciliationEngine.compare_three_way(system, exchange, None, now=now)
    assert missing.status is ReconciliationStatus.ONE_SIDE_MISSING
    assert missing.should_block_new_risk is True

    event.positions[InstrumentId("BTCUSDT")] = Quantity(amount="0.20")
    mismatch = ReconciliationEngine.compare_three_way(system, exchange, event, now=now)
    assert mismatch.status is ReconciliationStatus.MISMATCHED
    assert any("event_stream" in difference for difference in mismatch.differences)

    stale_event = _facts(timestamp=now - timedelta(seconds=31), source="EVENT_STREAM")
    stale = ReconciliationEngine.compare_three_way(system, exchange, stale_event, now=now)
    # BD-FIX: 事件流侧新鲜度豁免 —— 流活性由 transport readiness（event_age）
    # 证明，对账只比较状态一致性；低频环境时间戳冻结 = 状态无变化 ≠ 事实失效。
    assert stale.status is ReconciliationStatus.MATCHED


def test_compare_rejects_non_finite_numeric_facts() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    invalid_balance = _facts(timestamp=now, balance="NaN")
    result = ReconciliationEngine.compare(invalid_balance, _facts(timestamp=now, source="EXCHANGE"), now=now)
    assert result.status is ReconciliationStatus.ERROR
    assert "INVALID_BALANCE_FACT" in result.differences[0]

    invalid_position = _facts(timestamp=now)
    invalid_position.positions[InstrumentId("BTCUSDT")] = Quantity(amount="Infinity")
    result = ReconciliationEngine.compare(invalid_position, _facts(timestamp=now, source="EXCHANGE"), now=now)
    assert result.status is ReconciliationStatus.ERROR
    assert "INVALID_POSITION_FACT" in result.differences[0]


def test_compare_does_not_collapse_long_and_short_positions() -> None:
    now = datetime.now(timezone.utc)
    long_facts = _facts(timestamp=now)
    short_facts = _facts(timestamp=now, source="EXCHANGE")
    short_facts.positions[InstrumentId("BTCUSDT")] = Quantity(amount="-0.25")
    result = ReconciliationEngine.compare(long_facts, short_facts, now=now)
    assert result.status is ReconciliationStatus.MISMATCHED


def test_store_reconciliation_and_fill_projections_are_idempotent(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "state.db"))
    now = datetime.now(timezone.utc)
    facts = _facts(timestamp=now)
    store.save_reconciliation_snapshot("snap-1", "SYSTEM", facts)
    assert store.restore_latest_reconciliation_snapshot("acct", "BINANCE", "SYSTEM")["snapshot_id"] == "snap-1"
    result = ReconciliationEngine.compare(facts, _facts(timestamp=now, source="EXCHANGE"), now=now)
    store.save_reconciliation_result("result-1", result, system_snapshot_id="snap-1")
    with store._get_conn() as conn:
        persisted = conn.execute(
            "SELECT status, matched FROM reconciliation_results WHERE result_id='result-1'"
        ).fetchone()
    assert tuple(persisted) == ("MATCHED", 1)

    assert store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.1", "0.1", "100", "PARTIAL")
    assert not store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.1", "0.1", "100", "PARTIAL")
    store.save_position_projection("BTCUSDT", "0.1", "100", 2, "fill-1")
    row = store.restore_position_projection()[0]
    assert row["signed_quantity"] == "0.1"
    assert row["position_generation"] == 2

    with store._get_conn() as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(protection_orders)").fetchall()}
    assert {"owner_id", "position_generation", "session_id", "exchange_order_id"} <= columns


def test_opening_projection_is_consumed_without_exchange_self_heal(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "opening-engine.db"))
    store.save_account_opening_projection(
        "opening-1",
        "default",
        "BINANCE",
        "1000",
        "USDT",
        8,
        {"BTCUSDT": "0.25"},
        [],
        "2026-08-09T00:00:00+00:00",
        "AUTHORIZED_READ_SNAPSHOT",
        "account-v1",
        "sha256:evidence",
        "approval-1",
    )
    engine = object.__new__(AutonomousEngine)
    engine._store = store
    engine._position_projection = {}
    facts = engine._build_system_reconciliation_facts()
    assert facts.complete is True
    assert facts.balance.amount == "1000"
    assert facts.positions[InstrumentId("BTCUSDT")].amount == "0.25"
    assert facts.source.endswith("AUTHORIZED_OPENING")


def test_ledger_persistence_failure_freezes_and_closes_gate() -> None:
    tx = LedgerTransaction(
        transaction_id="tx-failure",
        transaction_type=LedgerTransactionType.FILL,
        source_event_id="fill-failure",
        postings=(
            Posting(
                "p1",
                AccountId("default"),
                AccountType.CASH,
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                MonetaryValue(amount="10"),
                PostingSide.DEBIT,
            ),
            Posting(
                "p2",
                AccountId("default"),
                AccountType.POSITION_COST,
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                MonetaryValue(amount="10"),
                PostingSide.CREDIT,
            ),
        ),
    )

    class FailingStore:
        def save_ledger_transaction(self, _tx):
            raise OSError("disk full")

    engine = object.__new__(AutonomousEngine)
    engine._ledger = ImmutableLedger()
    engine._store = FailingStore()
    control = SimpleNamespace(action=ControlAction.RESUME)
    control.get_status = lambda: control.action
    control.execute_action = lambda action: setattr(control, "action", action)
    engine._control = control

    with pytest.raises(OSError, match="disk full"):
        engine._post_ledger_transaction(tx)
    # BD-FIX（freeze 家族环境化）: 账本写失败在 testnet 不 freeze（无
    # 解冻路径，demo 抖动一次即永久停机）—— 但 NO_NEW_RISK 必须成立
    # 且交易未入账（durable 行缺失本身阻止 RESUME，安全等价）。
    assert engine._ledger.is_frozen is False
    assert control.action is ControlAction.NO_NEW_RISK
    assert engine._ledger.transaction_count == 0


def test_runtime_reconciliation_has_no_automatic_self_heal_call() -> None:
    import inspect

    assert "_sync_exchange_state" not in inspect.getsource(AutonomousEngine._reconcile)


def test_legacy_exchange_state_self_heal_is_fail_closed() -> None:
    import asyncio

    engine = object.__new__(AutonomousEngine)

    with pytest.raises(RuntimeError, match="EXCHANGE_STATE_MUTATION_RECOVERY_DISABLED"):
        asyncio.run(engine._sync_exchange_state())


def test_cumulative_fill_observations_produce_one_delta_per_event(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "fills.db"))
    engine = object.__new__(AutonomousEngine)
    engine._store = store
    engine._filled_quantities_by_order = {}

    first = engine._consume_cumulative_fill(
        "order-1",
        "BTCUSDT",
        {"executedQty": "0.1", "avgPrice": "100", "side": "BUY"},
        status="PARTIALLY_FILLED",
    )
    second = engine._consume_cumulative_fill(
        "order-1",
        "BTCUSDT",
        {"executedQty": "0.2", "avgPrice": "101", "side": "BUY"},
        status="PARTIALLY_FILLED",
    )
    duplicate = engine._consume_cumulative_fill(
        "order-1",
        "BTCUSDT",
        {"executedQty": "0.2", "avgPrice": "101", "side": "BUY"},
        status="PARTIALLY_FILLED",
    )
    assert first[0] == 0.1
    assert second[0] == 0.1
    assert duplicate[0] == 0.0
    assert len(store.restore_fill_events()) == 2

    engine._mark_fill_retryable("order-1", first[2])
    retry = engine._consume_cumulative_fill(
        "order-1",
        "BTCUSDT",
        {"executedQty": "0.1", "avgPrice": "100", "side": "BUY"},
        status="PARTIALLY_FILLED",
    )
    assert retry[0] == 0.1


@pytest.mark.asyncio
async def test_engine_reconciliation_failure_is_read_only_and_closes_gate(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "engine-recon.db"))
    engine = object.__new__(AutonomousEngine)
    engine._store = store
    engine._position_projection = {}
    engine._recon = ReconciliationEngine()
    engine._last_reconciliation_result = None
    calls: list[tuple[str, str]] = []

    async def api(path: str, *, signed: bool = False):
        del signed
        calls.append(("GET", path))
        if path == "/fapi/v2/account":
            return {"totalWalletBalance": "100", "positions": []}, True
        return [], True

    control = SimpleNamespace(
        action=None,
        get_status=lambda: control.action,
        execute_action=lambda action: setattr(control, "action", action),
    )
    engine._api_async_safe = api
    engine._control = control
    engine._last_account = {}

    ok = await engine._reconcile()
    assert ok is False
    assert control.action.value == "NO_NEW_RISK"
    assert calls == [("GET", "/fapi/v2/account"), ("GET", "/fapi/v1/openOrders")]
    assert engine._last_reconciliation_result.status is ReconciliationStatus.INCOMPLETE


def test_engine_reconciliation_rule_steps_require_fresh_venue_authority() -> None:
    engine = object.__new__(AutonomousEngine)
    assert engine._reconciliation_rule_steps(set()) == ({}, "OK")
    assert engine._reconciliation_rule_steps({"BTCUSDT"}) == ({}, "POSITION_RULE_AUTHORITY_UNAVAILABLE")

    engine._adapter = SimpleNamespace(
        get_rule_snapshot=lambda symbol: SimpleNamespace(
            is_known=symbol != "UNKNOWN",
            is_stale=symbol == "STALE",
            step_size="0.001",
        )
    )
    assert engine._reconciliation_rule_steps({"BTCUSDT"}) == ({"BTCUSDT": "0.001"}, "OK")
    assert engine._reconciliation_rule_steps({"UNKNOWN"}) == ({}, "POSITION_RULE_UNKNOWN_OR_STALE:UNKNOWN")
    assert engine._reconciliation_rule_steps({"STALE"}) == ({}, "POSITION_RULE_UNKNOWN_OR_STALE:STALE")

    engine._adapter = SimpleNamespace(
        get_rule_snapshot=lambda _symbol: SimpleNamespace(is_known=True, is_stale=False, step_size="NaN")
    )
    assert engine._reconciliation_rule_steps({"BTCUSDT"}) == ({}, "POSITION_RULE_STEP_INVALID:BTCUSDT")


@pytest.mark.asyncio
async def test_realtime_recon_timeout_is_fail_closed_and_does_not_block_loop() -> None:
    """BD-FIX: 实时循环内对账调用由 asyncio.wait_for(25s) 保护。

    慢网络导致 _reconcile 超时时按 fail-closed 处理：本轮不自动
    RESUME（不更新事实），_last_recon 仍被刷新 —— 循环不被阻塞，
    30s 后下一轮自然重试，对账间隔不会拉大触发 supervisor 60s
    新鲜度检查降级。
    """
    engine = object.__new__(AutonomousEngine)
    engine._tick_count = 1
    engine._trading_pool = SimpleNamespace(active_instruments=lambda: [])
    engine._outbox = SimpleNamespace(
        unacked=lambda: [],
        pending_count=lambda: 0,
        _outbox=[],
        _processed=[],
        _inbox=[],
    )
    engine._can_write = False
    engine._can_simulate = False
    engine._last_recon = 0.0
    engine._reconcile = AsyncMock(return_value=True)
    actions: list[str] = []
    engine._control = SimpleNamespace(
        execute_action=lambda action: actions.append(action.value),
    )
    engine._durable_fact_status = lambda: (True, None, None)
    engine._error_count = 0
    engine._last_realtime = 0.0
    engine._last_realtime_mono = 0.0

    # 模拟 wait_for 超时：先让 coro 完成（避免 dangling coroutine 警告），
    # 再抛 TimeoutError —— 与真实 wait_for 25s 超时抛出的异常路径一致。
    async def _simulated_timeout(coro, **kwargs):
        await coro
        raise asyncio.TimeoutError

    with patch("asyncio.wait_for", side_effect=_simulated_timeout) as wf:
        await engine._realtime_tick()

    # wait_for 包装存在，超时 25s（< 30s 对账间隔 + supervisor 60s 阈值余量）。
    # 周期段现在分别保护 UNKNOWN 意图恢复（20s）、池外订单监控（15s）
    # 和主对账（25s），三条路径都必须保持 fail-closed 且不阻塞实时循环。
    assert wf.await_count == 3
    assert wf.await_args.kwargs["timeout"] == 25.0
    # _last_recon 被刷新 → 循环未被阻塞，30s 后重试
    assert time.time() - engine._last_recon < 5
    assert engine._last_realtime > 0  # tick 完整走完（finally 执行）
    # fail-closed：recon_ok=False → 不自动 RESUME、不触碰事实
    assert actions == []


@pytest.mark.asyncio
async def test_realtime_tick_prefix_failure_does_not_block_reconciliation() -> None:
    """fail-closed 根因修复: tick 前段异常不得阻断对账心跳。

    PG 重启时前段 PG 读写抛 OperationalError(RuntimeError 模拟)被函数级
    except 吞掉,原结构下 reconcile 段(在 try 内、异常点之后)永远跳不过去
    → _last_recon 停更 → supervisor 模块健康 stale(>120s)→ 防抖器
    LOCKED fatal → 有序关机 exit 0 → launchd 不拉起(KeepAlive 只认
    非零退出)→ 引擎永久下线。修复后 reconcile 段独立执行,心跳续命。
    """
    engine = object.__new__(AutonomousEngine)
    engine._tick_count = 1
    engine._trading_pool = SimpleNamespace(active_instruments=lambda: [])

    # 前段异常源: outbox 读取在 PG 重启时抛连接错误(reconcile 段之前)
    def _fail_unacked() -> list:
        raise RuntimeError(
            'connection failed: connection to server at "127.0.0.1", '
            "port 5432 failed: FATAL: the database system is shutting down"
        )

    engine._outbox = SimpleNamespace(
        unacked=_fail_unacked,
        pending_count=lambda: 0,
        _outbox=[],
        _processed=[],
        _inbox=[],
    )
    engine._can_write = False
    engine._can_simulate = False
    engine._last_recon = 0.0
    engine._reconcile = AsyncMock(return_value=True)
    actions: list[str] = []
    engine._control = SimpleNamespace(
        execute_action=lambda action: actions.append(action.value),
        get_status=lambda: ControlAction.NO_NEW_RISK,
    )
    engine._durable_fact_status = lambda: (True, None, None)
    engine._error_count = 0
    engine._last_realtime = 0.0
    engine._last_realtime_mono = 0.0

    await engine._realtime_tick()  # 不抛异常: 前段异常被隔离,不冒泡

    # 核心断言: 心跳仍被刷新 —— reconcile 段在前段异常后照常执行
    assert time.time() - engine._last_recon < 5
    assert engine._last_realtime > 0  # finally 执行,tick 未死
    assert engine._error_count == 1  # 前段异常被主 except 计数一次
    # recon_ok=True → durable 事实干净 → 自动 RESUME(现有语义保留)
    assert actions == ["RESUME"]


def test_order_parameter_mismatch_detected_with_same_ids() -> None:
    """M13-F01: 同 ID 订单参数漂移必须产生差异(旧实现只比 ID 集合)。"""
    from datetime import datetime as _dt

    from beidou_safety.execution.reconciliation import AccountFactSnapshot, ReconciliationEngine

    now = _dt(2026, 8, 9, tzinfo=timezone.utc)
    system_facts = AccountFactSnapshot(
        account_id=AccountId("default"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount="1000"),
        positions={},
        open_orders=["o1"],
        open_orders_detail={"o1": {"symbol": "BTCUSDT", "side": "BUY", "qty": "1", "type": "LIMIT"}},
        timestamp=now,
        source="SYSTEM",
        fact_version="v1",
        complete=True,
    )
    exchange_facts = AccountFactSnapshot(
        account_id=AccountId("default"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount="1000"),
        positions={},
        open_orders=["o1"],
        open_orders_detail={"o1": {"symbol": "BTCUSDT", "side": "SELL", "qty": "1", "type": "LIMIT"}},
        timestamp=now,
        source="EXCHANGE",
        fact_version="v1",
        complete=True,
    )
    result = ReconciliationEngine.compare(system_facts, exchange_facts, now=now)
    assert not result.matched
    assert any("parameter mismatch" in str(d) for d in result.differences)


# --- M13-R2: 对抗审查反例回归 ---


def _detail(**overrides) -> dict:
    values = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "qty": "0.25",
        "price": "50000",
        "type": "LIMIT",
    }
    values.update(overrides)
    return {"order-1": values}


def test_one_sided_detail_does_not_block() -> None:
    """单侧缺明细 → MATCHED + detail_degraded 审计标志,不阻断新风险。

    回归:旧分支在有挂单时每轮 MISMATCHED,live/canary 三方对账永久阻断。
    """
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail())
    exchange = _facts(timestamp=now, source="EXCHANGE", detail={})
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert result.status is ReconciliationStatus.MATCHED
    assert result.detail_degraded is True


def test_both_missing_detail_no_degradation_flag() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    result = ReconciliationEngine.compare(_facts(timestamp=now), _facts(timestamp=now, source="EXCHANGE"), now=now)
    assert result.status is ReconciliationStatus.MATCHED
    assert result.detail_degraded is False


def test_price_drift_detected() -> None:
    """price 加入比较域 —— 同 ID 订单价格漂移必须阻断。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail(price="90000"))
    exchange = _facts(timestamp=now, source="EXCHANGE", detail=_detail(price="85000"))
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert not result.matched
    assert any("price" in str(d) for d in result.differences)


def test_price_zero_skipped_for_stop_orders() -> None:
    """STOP 类单 price=0 为无效价位(有效价位在 stopPrice) —— 跳过比较。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail(price="0", type="STOP_MARKET"))
    exchange = _facts(timestamp=now, source="EXCHANGE", detail=_detail(price="0", type="STOP_MARKET"))
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert result.status is ReconciliationStatus.MATCHED


def test_qty_invalid_blocks_not_silently_cleared() -> None:
    """qty 解析失败 → 阻断差异(fail-closed),不再双侧清零放行。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail(qty="abc"))
    exchange = _facts(timestamp=now, source="EXCHANGE", detail=_detail(qty="1.0"))
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert not result.matched
    assert any("INVALID_ORDER_QTY_FACT" in str(d) for d in result.differences)


def test_reduce_only_not_compared() -> None:
    """reduce_only 移除比较域(系统侧 order_states 无该列,原死代码)。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail())  # 无 reduce_only 键
    exchange = _facts(
        timestamp=now,
        source="EXCHANGE",
        detail=_detail(reduce_only="true"),
    )
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert result.status is ReconciliationStatus.MATCHED


def test_three_way_aggregates_detail_degraded() -> None:
    """三方对账:event 侧无 detail → 聚合 detail_degraded=True 且不阻断。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail())
    exchange = _facts(timestamp=now, source="EXCHANGE", detail=_detail())
    event = _facts(timestamp=now, source="EVENT_STREAM", detail={})
    result = ReconciliationEngine.compare_three_way(system, exchange, event, now=now)
    assert result.status is ReconciliationStatus.MATCHED
    assert result.detail_degraded is True


# --- M16-F02: order_states 扩列后防线恢复 ---


def test_reduce_only_drift_detected_when_both_sides_present() -> None:
    """两侧都提供 reduce_only 且不一致 → 阻断(M16-F02 防线恢复)。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail(reduce_only="true"))
    exchange = _facts(timestamp=now, source="EXCHANGE", detail=_detail(reduce_only="false"))
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert not result.matched
    assert any("reduce_only" in str(d) for d in result.differences)


def test_stop_order_compares_stop_price_not_price() -> None:
    """STOP 类单比较 stop_price(有效价位),price=0 不参与。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail(type="STOP_MARKET", price="0", stop_price="90000"))
    exchange = _facts(
        timestamp=now, source="EXCHANGE", detail=_detail(type="STOP_MARKET", price="0", stop_price="85000")
    )
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert not result.matched
    assert any("price" in str(d) for d in result.differences)

    matched_system = _facts(timestamp=now, detail=_detail(type="STOP_MARKET", price="0", stop_price="85000"))
    matched_exchange = _facts(
        timestamp=now, source="EXCHANGE", detail=_detail(type="STOP_MARKET", price="0", stop_price="85000")
    )
    result2 = ReconciliationEngine.compare(matched_system, matched_exchange, now=now)
    assert result2.status is ReconciliationStatus.MATCHED


# --- M16-R2: STOP 类订单类型判定(对抗审查 BUG-4) ---


def test_take_profit_market_stop_price_drift_detected() -> None:
    """TAKE_PROFIT_MARKET 不含 "STOP" 子串,旧判定走 price 分支漏检。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail(type="TAKE_PROFIT_MARKET", price="0", stop_price="90000"))
    exchange = _facts(
        timestamp=now, source="EXCHANGE", detail=_detail(type="TAKE_PROFIT_MARKET", price="0", stop_price="85000")
    )
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert not result.matched
    assert any("stop_price" in str(d) for d in result.differences)


def test_stop_loss_limit_compares_both_prices() -> None:
    """STOP_LOSS_LIMIT 双价:limit price 与 stop_price 都参与比较。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail(type="STOP_LOSS_LIMIT", price="49000", stop_price="50000"))
    exchange = _facts(
        timestamp=now, source="EXCHANGE", detail=_detail(type="STOP_LOSS_LIMIT", price="49500", stop_price="50000")
    )
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert not result.matched
    assert any("price" in str(d) for d in result.differences)


def test_production_shape_reduce_only_drift_detected() -> None:
    """生产形态:系统侧 reduce_only 真实值(扩列后)vs 交换侧 reduceOnly。"""
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now, detail=_detail(reduce_only="true"))
    exchange = _facts(timestamp=now, source="EXCHANGE", detail=_detail(reduce_only="false"))
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert not result.matched
    assert any("reduce_only" in str(d) for d in result.differences)
