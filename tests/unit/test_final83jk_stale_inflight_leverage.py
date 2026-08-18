"""final83j/k 回归:幽灵在途命令治理 + 自适应杠杆同步。

背景:非终态执行子命令(PLANNED/SENDING/ACKED/PARTIALLY_FILLED/UNKNOWN)
持续计入 inflight_signed_quantity,把新订单目标增量挤到交易所最小下单量
(XRP 提案 64 币、实际只下 5.8);自适应杠杆从不同步到交易所(恒 20x)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from beidou_core.engine import AutonomousEngine
from beidou_infra.outbox import PostgresIntentOutbox
from beidou_safety.execution.command_aggregate import ChildCommandState, ExecutionChildCommand


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.fetchone_values: list = []
        self.fetchall_values: list = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append((sql, params))

    def fetchone(self):
        return self.fetchone_values.pop(0) if self.fetchone_values else None

    def fetchall(self):
        return self.fetchall_values.pop(0) if self.fetchall_values else []


class _RecordingTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_args) -> bool:
        return False


class _RecordingConnection:
    def __init__(self) -> None:
        self.cursor_state = _RecordingCursor()

    def cursor(self):
        return self.cursor_state

    def transaction(self):
        return _RecordingTransaction()


def _child(sequence: int = 0) -> ExecutionChildCommand:
    return ExecutionChildCommand.create(
        parent_intent_id="intent-stale-1",
        sequence=sequence,
        symbol="XRPUSDT",
        side="BUY",
        quantity="5.8",
        order_type="LIMIT",
        time_in_force="GTC",
        client_order_id=f"cid-stale-{sequence}",
        limit_price="1.0",
        rule_snapshot_hash="rule-1",
    )


def _outbox(conn: _RecordingConnection) -> PostgresIntentOutbox:
    return PostgresIntentOutbox(connection_factory=lambda: conn, lease_owner="worker-1", fencing_token=7)


def test_stale_child_commands_lists_non_terminal_rows() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchall_values.append(
        [("intent-stale-1", 0, "PLANNED", "XRPUSDT", "cid-stale-0", "", "0", "2026-08-15T00:00:00Z")]
    )
    store = _outbox(conn)
    rows = store.stale_child_commands(min_age_seconds=1800.0)
    assert rows == [
        {
            "parent_intent_id": "intent-stale-1",
            "sequence": 0,
            "state": "PLANNED",
            "symbol": "XRPUSDT",
            "client_order_id": "cid-stale-0",
            "exchange_order_id": "",
            "filled_quantity": "0",
        }
    ]


def test_recover_stale_child_planned_to_rejected() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.append(None)  # 父意图无活跃租约
    conn.cursor_state.fetchall_values.append([(json.dumps(_child().to_payload(), default=str),)])
    conn.cursor_state.fetchone_values.append(None)  # 事件非重复
    store = _outbox(conn)

    store.recover_stale_child(
        "intent-stale-1", 0, ChildCommandState.REJECTED, event_id="stale-resolve:x:0:NOT_FOUND"
    )

    update_sql = next(s for s, _ in conn.cursor_state.statements if s.startswith("UPDATE v3_execution_commands"))
    assert "state=%s" in update_sql
    event_stmt = next((s, params) for s, params in conn.cursor_state.statements if "v3_execution_command_events" in s and "INSERT" in s)
    assert "STALE_CHILD_VENUE_RESOLVED" in json.dumps(event_stmt[1], default=str)


def test_recover_stale_child_blocks_active_parent_lease() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.append(("intent-stale-1",))  # 父意图租约活跃
    store = _outbox(conn)
    with pytest.raises(ValueError, match="STALE_CHILD_PARENT_ACTIVELY_LEASED"):
        store.recover_stale_child(
            "intent-stale-1", 0, ChildCommandState.REJECTED, event_id="e1"
        )


def test_project_order_terminal_fills_acked_child() -> None:
    conn = _RecordingConnection()
    # lookup by exchange_order_id
    conn.cursor_state.fetchone_values.append(("intent-stale-1", 0, "ACKED"))
    acked = _child().transition(ChildCommandState.SENDING, event_id="e-send")
    acked = acked.transition(ChildCommandState.ACKED, event_id="e-ack", exchange_order_id="123456")
    # transition 内部:payload 行 + 事件查重 + UPDATE + INSERT
    conn.cursor_state.fetchall_values.append([(json.dumps(acked.to_payload(), default=str),)])
    conn.cursor_state.fetchone_values.append(None)
    store = _outbox(conn)

    result = store.project_order_terminal("123456", "FILLED", "5.8")

    assert result is not None
    update_sql = next(s for s, _ in conn.cursor_state.statements if s.startswith("UPDATE v3_execution_commands"))
    assert update_sql  # 终态迁移已执行


@pytest.mark.asyncio
async def test_engine_resolves_stale_child_with_venue_facts(monkeypatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    recover = Mock()
    engine._outbox = SimpleNamespace(
        stale_child_commands=Mock(
            return_value=[{
                "parent_intent_id": "intent-x", "sequence": 1, "state": "UNKNOWN",
                "symbol": "XRPUSDT", "client_order_id": "cid-x", "exchange_order_id": "",
            }]
        ),
        recover_stale_child=recover,
    )
    engine._adapter = SimpleNamespace(
        query_order_by_client_id=AsyncMock(
            return_value=SimpleNamespace(
                is_success=lambda: True,
                data={"status": "FILLED", "executedQty": "5.8", "orderId": "123"},
            )
        )
    )

    resolved = await engine._resolve_stale_execution_commands()

    assert resolved == 1
    recover.assert_called_once()
    args, kwargs = recover.call_args
    assert args[2] == ChildCommandState.FILLED
    assert kwargs["cumulative_filled_quantity"] == "5.8"


@pytest.mark.asyncio
async def test_engine_resolves_not_found_planned_as_rejected(monkeypatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    recover = Mock()
    engine._outbox = SimpleNamespace(
        stale_child_commands=Mock(
            return_value=[{
                "parent_intent_id": "intent-x", "sequence": 0, "state": "PLANNED",
                "symbol": "XRPUSDT", "client_order_id": "cid-x", "exchange_order_id": "",
            }]
        ),
        recover_stale_child=recover,
    )
    engine._adapter = SimpleNamespace(
        query_order_by_client_id=AsyncMock(
            return_value=SimpleNamespace(
                is_success=lambda: False,
                data=None,
                error=SimpleNamespace(message="Order does not exist"),
            )
        )
    )

    resolved = await engine._resolve_stale_execution_commands()

    assert resolved == 1
    assert recover.call_args.args[2] == ChildCommandState.REJECTED


@pytest.mark.asyncio
async def test_sync_venue_leverage_env_gated_and_cached(monkeypatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="testnet")
    api = AsyncMock(return_value={"leverage": 3})
    monkeypatch.setattr(engine, "_api_async", api)

    # 默认关闭:不调用
    monkeypatch.delenv("BEIDOU_SYNC_VENUE_LEVERAGE", raising=False)
    assert await engine._sync_venue_leverage("XRPUSDT", 3.0) is False
    api.assert_not_awaited()

    # 开启:同步一次并缓存
    monkeypatch.setenv("BEIDOU_SYNC_VENUE_LEVERAGE", "1")
    assert await engine._sync_venue_leverage("XRPUSDT", 3.0) is True
    assert api.await_count == 1
    assert engine._venue_leverage["XRPUSDT"] == 3
    # 同值不再重复同步
    assert await engine._sync_venue_leverage("XRPUSDT", 3.0) is False
    assert api.await_count == 1
    # 档位变化 → 再次同步;0.5 档钳制为 1
    assert await engine._sync_venue_leverage("XRPUSDT", 0.5) is True
    assert engine._venue_leverage["XRPUSDT"] == 1


@pytest.mark.asyncio
async def test_sync_venue_leverage_never_in_live(monkeypatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="live")
    api = AsyncMock()
    monkeypatch.setattr(engine, "_api_async", api)
    monkeypatch.setenv("BEIDOU_SYNC_VENUE_LEVERAGE", "1")
    assert await engine._sync_venue_leverage("XRPUSDT", 3.0) is False
    api.assert_not_awaited()
