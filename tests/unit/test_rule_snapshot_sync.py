"""BD-FIX: 启动同步 adapter 规则快照（BD-CV10）回归测试。

修复前 BinanceUsdmAdapter._rule_snapshots 恒空（sync_rule_snapshots 无调用点）
→ get_rule_snapshot 恒 UNKNOWN → _submit_order_slice 执行 gate 恒以
VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE 拒绝全部订单。
修复：启动时把 exchangeInfo symbols 同步进 adapter._reference_data 并
调用 sync_rule_snapshots()（beidou_core/engine.py:_sync_adapter_rule_snapshots）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from beidou_core.engine import AutonomousEngine
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
from beidou_shared.types import AccountId, AccountRef, OrderStatus, OrderType, VenueId


def _exchange_info() -> dict[str, object]:
    """最小 BNBUSDT exchangeInfo 条目（与引擎精度加载器同字段）。"""
    return {
        "symbols": [
            {
                "symbol": "BNBUSDT",
                "status": "TRADING",
                "contractSize": "1.000",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.010", "minQty": "0.010"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                ],
            }
        ]
    }


def _engine_with_adapter() -> tuple[AutonomousEngine, BinanceUsdmAdapter]:
    """最小 engine fake：__new__ 跳过 __init__，仅注入真实 adapter。"""
    engine = AutonomousEngine.__new__(AutonomousEngine)
    adapter = BinanceUsdmAdapter()
    engine._adapter = adapter
    return engine, adapter


def test_sync_populates_adapter_rule_snapshots() -> None:
    engine, adapter = _engine_with_adapter()
    assert adapter.rule_snapshot_count == 0

    ok = engine._sync_adapter_rule_snapshots(_exchange_info())

    assert ok is True
    assert adapter.rule_snapshot_count == 1
    snap = adapter.get_rule_snapshot("BNBUSDT")
    assert snap.is_known is True
    assert snap.is_stale is False
    # from_exchange_info 在构造时写入 observed_at → 同步即新鲜，不恒 stale
    assert snap.observed_at != ""
    assert snap.tick_size == "0.10"
    assert snap.step_size == "0.010"


def test_sync_skips_empty_exchange_info() -> None:
    engine, adapter = _engine_with_adapter()

    assert engine._sync_adapter_rule_snapshots({}) is False
    assert engine._sync_adapter_rule_snapshots({"symbols": []}) is False
    assert engine._sync_adapter_rule_snapshots(None) is False
    assert adapter.rule_snapshot_count == 0
    assert adapter.get_rule_snapshot("BNBUSDT").is_known is False


def test_sync_does_not_fabricate_rules_for_unparsable_symbols() -> None:
    engine, adapter = _engine_with_adapter()

    ok = engine._sync_adapter_rule_snapshots({"symbols": [{"symbol": "BOGUSUSDT"}]})

    assert ok is True
    # 无 filters 条目 → is_known False → 执行 gate 仍会 fail-closed 拒绝
    snap = adapter.get_rule_snapshot("BOGUSUSDT")
    assert snap.is_known is False


class _StoreStub:
    def save_order_state(self, *args: object, **kwargs: object) -> None:
        pass


async def _fake_create_order(request: object) -> SimpleNamespace:
    """create_order 打桩：返回 ACK 响应，避免真实传输。"""
    return SimpleNamespace(raw_response={"orderId": 9001, "status": "NEW"}, status=OrderStatus.NEW)


def _gate_engine() -> tuple[AutonomousEngine, BinanceUsdmAdapter]:
    """具备 _submit_order_slice 快乐路径所需全部 stub 的 engine fake。"""
    engine, adapter = _engine_with_adapter()
    adapter.create_order = _fake_create_order
    engine._adapter = adapter
    engine._order_trackers = {}
    engine._order_symbols = {}
    engine._order_count = 0
    engine._close_order_ids = set()
    engine._active_order_ids = set()
    engine._owned_order_ids = set()
    engine._store = _StoreStub()
    return engine, adapter


def _market_intent() -> SimpleNamespace:
    return SimpleNamespace(
        quantity=SimpleNamespace(amount="0.010"),
        price=None,
        order_type=OrderType.MARKET,
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
        correlation_id=None,
        reduce_only=False,
        close_position=False,
    )


def test_submit_order_slice_gate_rejects_unknown_before_sync() -> None:
    """修复前行为：adapter 缓存空 → 执行 gate 以 VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE 拒绝。"""
    engine, _adapter = _gate_engine()

    result = asyncio.run(
        engine._submit_order_slice(
            _market_intent(),
            params={"quantity": "0.010", "newClientOrderId": "test-bnb-1"},
            order_symbol="BNBUSDT",
            side="BUY",
            order_type="MARKET",
            consume_approval=False,
        )
    )

    assert result is not None
    assert result.get("_submit_outcome") == "REJECTED"
    assert result.get("reason") == "VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE"


def test_submit_order_slice_gate_passes_after_sync() -> None:
    """修复后行为：快照同步后同一订单意图通过 gate 并正常 ACK。"""
    engine, adapter = _gate_engine()
    assert engine._sync_adapter_rule_snapshots(_exchange_info()) is True

    result = asyncio.run(
        engine._submit_order_slice(
            _market_intent(),
            params={"quantity": "0.010", "newClientOrderId": "test-bnb-1"},
            order_symbol="BNBUSDT",
            side="BUY",
            order_type="MARKET",
            consume_approval=False,
        )
    )

    assert result is not None
    assert "_submit_outcome" not in result  # 未 REJECTED/UNKNOWN → 返回交易所原始响应
    assert result.get("orderId") == 9001
    assert isinstance(adapter.get_rule_snapshot("BNBUSDT"), InstrumentRuleSnapshot)
