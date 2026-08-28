"""final83g 回归:SL/TP 保护创建缺口修复。

复现:15:53 AVAXUSDT 入场单成交事实经用户流/部分成交路径先入账,
_process_fill 的已提交分支直接返回,保护投影从未建立 → 持仓裸露 →
protection_coverage blocker 防抖升级 LOCKED → 停机。

覆盖:
- _process_fill 已提交分支补建 SL/TP(venue 累计成交事实)
- 平仓订单不补建入场保护
- _ensure_entry_protection 幂等(同量同向投影跳过,防重复下单)
- _get_open_algo_inventory 真值空/抖动空标志
- _retry_missing_protections 仅对抖动空 defer;真值空时近线自愈投影
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from beidou_control.plane import ControlAction
from beidou_core.engine import AutonomousEngine
from beidou_safety.execution.order_state import OrderEvent, OrderStateTracker
from beidou_shared.types import OrderId, OrderSide


class _FakeStore:
    def __init__(self, *, fill_rows: dict | None = None, protections: list | None = None) -> None:
        self.fill_rows = dict(fill_rows or {})
        self._protections = list(protections or [])
        self.order_state_writes: list[tuple] = []

    def get_fill_event(self, event_id: str):
        return self.fill_rows.get(event_id)

    def restore_protections(self):
        return list(self._protections)

    def save_order_state(self, order_id: str, *args: object, **kwargs: object) -> None:
        self.order_state_writes.append((order_id, args, kwargs))


def _engine() -> AutonomousEngine:
    return AutonomousEngine.__new__(AutonomousEngine)


@pytest.mark.asyncio
async def test_process_fill_already_committed_creates_protection(monkeypatch) -> None:
    """已提交成交事实的 FILLED 轮询必须补建入场保护(根因回归)。"""
    engine = _engine()
    engine._store = _FakeStore(fill_rows={"trade:516462877:t1": {"processing_state": "COMMITTED"}})
    tracker = OrderStateTracker(order_id=OrderId("516462877"))
    tracker.apply(OrderEvent.SENT)
    tracker.apply(OrderEvent.ACKED)
    engine._order_trackers = {"516462877": tracker}
    engine._order_symbols = {"516462877": "AVAXUSDT"}
    engine._active_order_ids = {"516462877"}
    engine._close_order_ids = set()
    engine._filled_quantities_by_order = {"516462877": 1.0}
    ensure = AsyncMock()
    monkeypatch.setattr(engine, "_ensure_entry_protection", ensure)

    result = {
        "orderId": "516462877",
        "symbol": "AVAXUSDT",
        "side": "BUY",
        "status": "FILLED",
        "executedQty": "1",
        "avgPrice": "6.31",
        "tradeId": "t1",
    }
    await engine._process_fill("516462877", "AVAXUSDT", result)

    ensure.assert_awaited_once()
    kwargs = ensure.call_args.kwargs
    assert kwargs["qty"] == 1.0
    assert kwargs["entry_price"] == 6.31
    assert "516462877" not in engine._order_trackers
    assert "516462877" not in engine._active_order_ids
    # 2026-08-29 回归: 已提交分支必须落 order_state FILLED 终态,
    # 否则 NEW 残留行制造 system 侧挂单 → 对账 MISMATCHED 锁盘。
    assert engine._store.order_state_writes
    _order_id, _args, kwargs = engine._store.order_state_writes[-1]
    assert _order_id == "516462877"
    assert _args[5] == "FILLED"  # status 是第 6 个位置参数
    assert _args[6] == "1.0"  # filled_qty 采用 venue 累计成交事实


@pytest.mark.asyncio
async def test_process_fill_already_committed_close_order_skips_entry_protection(monkeypatch) -> None:
    """平仓单的已提交成交不得触发入场保护创建。"""
    engine = _engine()
    engine._store = _FakeStore(fill_rows={"trade:99:t1": {"processing_state": "COMMITTED"}})
    tracker = OrderStateTracker(order_id=OrderId("99"))
    tracker.apply(OrderEvent.SENT)
    tracker.apply(OrderEvent.ACKED)
    engine._order_trackers = {"99": tracker}
    engine._order_symbols = {"99": "AVAXUSDT"}
    engine._active_order_ids = {"99"}
    engine._close_order_ids = {"99"}
    engine._filled_quantities_by_order = {"99": 1.0}
    ensure = AsyncMock()
    monkeypatch.setattr(engine, "_ensure_entry_protection", ensure)

    result = {
        "orderId": "99",
        "symbol": "AVAXUSDT",
        "side": "SELL",
        "status": "FILLED",
        "executedQty": "1",
        "avgPrice": "6.31",
        "tradeId": "t1",
    }
    await engine._process_fill("99", "AVAXUSDT", result)

    ensure.assert_not_awaited()
    assert "99" not in engine._order_trackers


@pytest.mark.asyncio
async def test_ensure_entry_protection_idempotent_for_same_projection() -> None:
    """同 symbol 同量同向已有投影时跳过,不重复创建/下单。"""
    engine = _engine()
    engine._protection = SimpleNamespace(
        all_positions=lambda: {"p1": SimpleNamespace(instrument_id="AVAXUSDT", quantity=1.0, side=OrderSide.BUY)},
        create_protection=Mock(side_effect=AssertionError("must not create")),
    )

    await engine._ensure_entry_protection(
        "516462877", "AVAXUSDT", {"side": "BUY"}, qty=1.0, entry_price=6.31, submit=False
    )

    assert engine._protection.create_protection.call_count == 0


@pytest.mark.asyncio
async def test_open_algo_inventory_genuine_flag() -> None:
    """查询成功(含真空)置 genuine=True;异常/UNKNOWN 置 False。"""
    engine = _engine()
    engine._env_mode = SimpleNamespace(value="testnet")

    engine._adapter = SimpleNamespace(
        get_open_algo_orders=AsyncMock(return_value=SimpleNamespace(is_success=lambda: True, data=[]))
    )
    assert await engine._get_open_algo_inventory() == []
    assert engine._last_algo_inventory_genuine is True

    engine._adapter = SimpleNamespace(get_open_algo_orders=AsyncMock(side_effect=RuntimeError("boom")))
    assert await engine._get_open_algo_inventory() == []
    assert engine._last_algo_inventory_genuine is False

    engine._adapter = SimpleNamespace(
        get_open_algo_orders=AsyncMock(return_value=SimpleNamespace(is_success=lambda: False, data=None, error=None))
    )
    assert await engine._get_open_algo_inventory() == []
    assert engine._last_algo_inventory_genuine is False


def _retry_engine() -> AutonomousEngine:
    engine = _engine()
    engine._can_write = True
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._protection = SimpleNamespace(all_positions=lambda: {})
    engine._store = _FakeStore()
    engine._active_algo_ids = {}
    engine._protection_owner_id = "owner-1"
    return engine


@pytest.mark.asyncio
async def test_retry_missing_protections_defers_only_on_non_genuine_empty(monkeypatch) -> None:
    """抖动空 defer(不自愈);真值空继续近线自愈投影。"""
    ensure = AsyncMock()

    # 抖动空:查询失败回落 [] → defer,_ensure_entry_protection 不被调用
    engine = _retry_engine()
    engine._last_algo_inventory_genuine = False
    engine._position_projection = {"AVAXUSDT": {"signed_quantity": "1", "entry_price": "6.31"}}
    monkeypatch.setattr(engine, "_get_open_algo_inventory", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine, "_ensure_entry_protection", ensure)
    await engine._retry_missing_protections({"AVAXUSDT"})
    ensure.assert_not_awaited()

    # 真值空:查询成功但无单 → 用持久化投影自愈重建(submit=False 交回受治理循环)
    ensure.reset_mock()
    engine2 = _retry_engine()
    engine2._last_algo_inventory_genuine = True
    engine2._position_projection = {"AVAXUSDT": {"signed_quantity": "1", "entry_price": "6.31"}}
    monkeypatch.setattr(engine2, "_get_open_algo_inventory", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine2, "_ensure_entry_protection", ensure)
    await engine2._retry_missing_protections({"AVAXUSDT"})
    ensure.assert_awaited_once()
    kwargs = ensure.call_args.kwargs
    assert kwargs["submit"] is False
    assert kwargs["qty"] == 1.0
    assert kwargs["entry_price"] == 6.31


@pytest.mark.asyncio
async def test_retry_missing_protections_sweep_skips_existing_projection(monkeypatch) -> None:
    """保护投影已存在时不重复自愈。"""
    ensure = AsyncMock()
    engine = _retry_engine()
    engine._last_algo_inventory_genuine = True
    engine._protection = SimpleNamespace(
        all_positions=lambda: {
            "p1": SimpleNamespace(
                instrument_id="AVAXUSDT",
                quantity=1.0,
                side=OrderSide.BUY,
                entry_price=6.31,
                stop_loss=SimpleNamespace(status=SimpleNamespace(value="PENDING")),
                take_profits=(),
            )
        },
    )
    engine._position_projection = {"AVAXUSDT": {"signed_quantity": "1", "entry_price": "6.31"}}
    engine._symbol_precision = {}
    engine._protection_retries = {}
    monkeypatch.setattr(engine, "_get_open_algo_inventory", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine, "_ensure_entry_protection", ensure)
    await engine._retry_missing_protections({"AVAXUSDT"})
    ensure.assert_not_awaited()


def _sl_projection(*, status_value: str = "ACTIVE") -> SimpleNamespace:
    """构造带止损单的保护投影(跳过 S33 深路径所需字段)。"""
    stop_loss = SimpleNamespace(
        protection_id="sl-pos-recovered-AVAXUSDT",
        position_id="pos-recovered-AVAXUSDT",
        instrument_id="AVAXUSDT",
        side=OrderSide.SELL,
        trigger_price=SimpleNamespace(amount="6.20"),
        order_price=None,
        quantity=SimpleNamespace(amount="1.0"),
        order_type="STOP_MARKET",
        reduce_only=True,
        status=SimpleNamespace(value=status_value),
        stop_type=None,
        take_profit_type=None,
        reason="Stop Loss: ATR_BASED",
        owner_id="owner-1",
        position_generation=1,
        session_id="s1",
        created_at=None,
        triggered_at=None,
        correlation_id=None,
        exchange_order_id="1000000171282363",
    )
    return SimpleNamespace(
        instrument_id="AVAXUSDT",
        quantity=1.0,
        side=OrderSide.BUY,
        entry_price=6.31,
        stop_loss=stop_loss,
        take_profits=(),
    )


@pytest.mark.asyncio
async def test_retry_sl_skips_when_durable_active_and_inventory_genuine_empty(monkeypatch) -> None:
    """库存真空 + durable ACTIVE 行已覆盖 → 不重复补挂止损(venue 延迟防重)。"""
    engine = _retry_engine()
    engine._last_algo_inventory_genuine = True
    engine._store = _FakeStore(
        protections=[
            {
                "protection_id": "sl-pos-recovered-AVAXUSDT",
                "position_id": "pos-recovered-AVAXUSDT",
                "symbol": "AVAXUSDT",
                "side": "SELL",
                "trigger_price": "6.20",
                "quantity": "1.0",
                "order_type": "STOP_MARKET",
                "status": "ACTIVE",
                "owner_id": "owner-1",
                "position_generation": 1,
                "session_id": "s1",
                "exchange_order_id": "1000000171282363",
            }
        ]
    )
    engine._protection = SimpleNamespace(all_positions=lambda: {"pos-recovered-AVAXUSDT": _sl_projection()})
    engine._symbol_precision = {}
    engine._protection_retries = {}
    monkeypatch.setattr(engine, "_get_open_algo_inventory", AsyncMock(return_value=[]))
    create_algo = AsyncMock()
    monkeypatch.setattr(engine, "_create_algo_order", create_algo)
    await engine._retry_missing_protections({"AVAXUSDT"})
    create_algo.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_sl_places_when_no_durable_rows(monkeypatch) -> None:
    """库存真空且无 durable ACTIVE 行(新建投影) → 照常补挂止损。"""
    engine = _retry_engine()
    engine._last_algo_inventory_genuine = True
    engine._store = _FakeStore(protections=[])
    engine._protection = SimpleNamespace(all_positions=lambda: {"pos-recovered-AVAXUSDT": _sl_projection()})
    engine._symbol_precision = {}
    engine._protection_retries = {}
    monkeypatch.setattr(engine, "_get_open_algo_inventory", AsyncMock(return_value=[]))
    create_algo = AsyncMock(return_value={"algoId": "123"})
    monkeypatch.setattr(engine, "_create_algo_order", create_algo)
    monkeypatch.setattr(engine, "_protection_algo_params", Mock(return_value={}))
    monkeypatch.setattr(engine, "_persist_protection_order", Mock())
    await engine._retry_missing_protections({"AVAXUSDT"})
    create_algo.assert_awaited_once()
