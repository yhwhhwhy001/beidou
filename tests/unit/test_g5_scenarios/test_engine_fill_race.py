"""引擎组场景二单元测试:terminal_monotonic_guard 判定纯函数、partial_fill /
cancel_fill_race 的 dry_run 不碰 PG、注册接线与异常自捕获。

partial_fill 的交易所交互用可编程假客户端;cancel_fill_race 的 PG 交互用
test_postgres_store 同款假连接(记录/事件/事务内存模拟),真实驱动
PostgresPersistentStore 守卫代码路径。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    NotionalLedger,
    ScenarioContext,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.engine.cancel_fill_race import (
    CancelFillRaceScenario,
    terminal_monotonic_guard,
)
from beidou_certification.g5_scenarios.engine.partial_fill import PartialFillScenario
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.core.error_taxonomy import Result

# ---- 判定纯函数(brief 单测,verbatim) ----


def test_terminal_monotonic_guard() -> None:
    # 终态不被非终态覆盖(引擎 fe62da1 语义)
    assert terminal_monotonic_guard("FILLED", "NEW") == "FILLED"
    assert terminal_monotonic_guard("CANCELED", "NEW") == "CANCELED"
    assert terminal_monotonic_guard("NEW", "FILLED") == "FILLED"
    assert terminal_monotonic_guard("PARTIALLY_FILLED", "NEW") == "PARTIALLY_FILLED"
    assert terminal_monotonic_guard("NEW", "PARTIALLY_FILLED") == "PARTIALLY_FILLED"
    assert terminal_monotonic_guard("PARTIALLY_FILLED", "FILLED") == "FILLED"
    assert terminal_monotonic_guard("NEW", "CANCELED") == "CANCELED"


def test_terminal_monotonic_guard_engine_edges() -> None:
    # 与引擎 save_order_state 守卫(beidou_core/store.py:1015-1020、
    # beidou_infra/postgres_store.py:679-684)逐条一致:
    # - 终态集合 {FILLED, CANCELED, EXPIRED, REJECTED} 内,非终态不得覆盖
    # - UNKNOWN 是非终态,不得抹掉已证实终态(引擎契约测试同语义)
    # - 终态→终态(FILLED→CANCELED)引擎放行 —— 守卫只挡非终态回写
    #   与 PARTIALLY_FILLED→NEW(计划期望 FILLED 抗 CANCELED,实测引擎无此规则)
    assert terminal_monotonic_guard("REJECTED", "NEW") == "REJECTED"
    assert terminal_monotonic_guard("EXPIRED", "NEW") == "EXPIRED"
    assert terminal_monotonic_guard("FILLED", "UNKNOWN") == "FILLED"
    assert terminal_monotonic_guard("FILLED", "CANCELED") == "CANCELED"
    assert terminal_monotonic_guard("PARTIALLY_FILLED", "CANCELED") == "CANCELED"
    assert terminal_monotonic_guard("NEW", "EXPIRED") == "EXPIRED"
    assert terminal_monotonic_guard("NEW", "REJECTED") == "REJECTED"
    assert terminal_monotonic_guard("", "NEW") == "NEW"


# ---- 场景执行上下文与可编程假客户端 ----


def _ctx(
    client: Any,
    tmp_path: Path,
    *,
    dry_run: bool = False,
    limit: float = 1000.0,
) -> ScenarioContext:
    return ScenarioContext(
        client=client,
        ledger=NotionalLedger(limit),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=dry_run,
    )


_EXCHANGE_INFO: dict[str, Any] = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            ],
        }
    ]
}

_DEPTH_DEFAULT: dict[str, Any] = {"lastUpdateId": 1, "bids": [["0.49", "5"]], "asks": [["0.50", "10"]]}


class _FakeClient:
    """partial_fill 用假交易所客户端:深度/轮询状态序列/撤单结果可编程。

    MARKET 单(平仓)记录进 close_orders 并带 avgPrice(平仓 notional 记账
    用);FILLED 轮询的 executedQty = 下单量(全成交);fail_close 模拟平仓
    单创建失败。
    """

    def __init__(
        self,
        *,
        exchange_info: dict[str, Any] | None = None,
        depth: dict[str, Any] | None = None,
        order_statuses: list[str] | None = None,
        cancel_status: str = "CANCELED",
        close_avg_price: str = "0.50",
        new_executed_qty: str = "0",
    ) -> None:
        self.exchange_info = exchange_info or _EXCHANGE_INFO
        self.depth = depth or _DEPTH_DEFAULT
        self.statuses = list(order_statuses or ["PARTIALLY_FILLED"])
        self.cancel_status = cancel_status
        self.close_avg_price = close_avg_price
        self.new_executed_qty = new_executed_qty
        self.placed: list[dict[str, Any]] = []
        self.close_orders: list[dict[str, Any]] = []
        self.cancelled: list[int] = []
        self.queries: list[str] = []
        self.fail_create = False
        self.fail_close = False

    async def get_exchange_info(self, symbol: str) -> Result[dict]:
        return Result.success(self.exchange_info)

    async def get_depth(self, symbol: str, limit: int = 20) -> Result[dict]:
        return Result.success(self.depth)

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: str | None = None,
        time_in_force: str | None = None,
        reduce_only: str | None = None,
        client_order_id: str | None = None,
    ) -> Result[dict]:
        if self.fail_create:
            raise AssertionError("流动性不足时不得下单")
        order_id = len(self.placed) + 1
        record: dict[str, Any] = {
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
            "price": price,
            "timeInForce": time_in_force,
            "reduceOnly": reduce_only,
            "clientOrderId": client_order_id,
            "status": "NEW",
        }
        if order_type == "MARKET":
            if self.fail_close:
                raise RuntimeError("close boom")
            record["status"] = "FILLED"
            record["avgPrice"] = self.close_avg_price
            record["executedQty"] = quantity
            self.close_orders.append(record)
        self.placed.append(record)
        return Result.success(record)

    async def get_order(self, symbol: str, order_id: int) -> Result[dict]:
        self.queries.append(str(order_id))
        status = self.statuses[0] if len(self.statuses) > 1 else self.statuses[-1]
        if len(self.statuses) > 1:
            self.statuses.pop(0)
        placed_qty = self.placed[-1]["quantity"] if self.placed else "15"
        executed = {
            "PARTIALLY_FILLED": "10",
            "CANCELED": "10",
            "FILLED": placed_qty,
            "NEW": self.new_executed_qty,
        }.get(status, "0")
        return Result.success(
            {"orderId": order_id, "symbol": symbol, "status": status, "executedQty": executed, "origQty": placed_qty}
        )

    async def cancel_order(self, symbol: str, order_id: int) -> Result[dict]:
        self.cancelled.append(order_id)
        return Result.success({"orderId": order_id, "symbol": symbol, "status": self.cancel_status})


async def _noop_sleep(_: float) -> None:
    return None


# ---- partial_fill: dry_run 早退不碰任何接口 ----


def test_partial_fill_dry_run_does_not_touch_exchange(tmp_path: Path, monkeypatch: Any) -> None:
    def _no_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry_run 不得调用交易所接口")

    fake = _FakeClient()
    for name in ("get_exchange_info", "get_depth", "create_order", "get_order", "cancel_order"):
        monkeypatch.setattr(fake, name, _no_call)
    ctx = _ctx(fake, tmp_path, dry_run=True)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert ctx.ledger.total == 0.0


def test_partial_fill_client_unavailable_not_verifiable(tmp_path: Path) -> None:
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "CLIENT_UNAVAILABLE"


# ---- partial_fill: 流动性门与 notional 记账 ----


def test_partial_fill_liquidity_gate_not_verifiable(tmp_path: Path) -> None:
    # 顶部 ask 量(0.05)≤ min_qty×10(0.1)→ 无对手流动性 → NOT_VERIFIABLE
    depth = {"lastUpdateId": 1, "bids": [["0.49", "5"]], "asks": [["0.50", "0.05"]]}
    fake = _FakeClient(depth=depth)
    fake.fail_create = True  # 不得下单
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert "liquidity_insufficient_or_too_deep" in result.error_message
    assert fake.placed == []
    assert ctx.ledger.total == 0.0


def test_partial_fill_depth_empty_not_verifiable(tmp_path: Path) -> None:
    depth = {"lastUpdateId": 1, "bids": [], "asks": []}
    fake = _FakeClient(depth=depth)
    fake.fail_create = True
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert "liquidity_insufficient_or_too_deep" in result.error_message
    assert fake.placed == []


def test_partial_fill_notional_fallback_uses_min_gate_quantity(tmp_path: Path) -> None:
    # 顶部 ask 1000 ×1.5 = 1500 量,notional 750 > 400 → 改用"最小过门槛量"
    # (0.50 价、stepSize 0.01:ceil(50/0.5/0.01)=100 → qty 100,notional 50)
    depth = {"lastUpdateId": 1, "bids": [["0.49", "5"]], "asks": [["0.50", "1000"]]}
    fake = _FakeClient(depth=depth, order_statuses=["FILLED"])  # 过门槛小单必全成交
    ctx = _ctx(fake, tmp_path, limit=400.0)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE  # 全 FILLED → 无法验证部分成交
    assert fake.placed and fake.placed[0]["quantity"] == "100"
    assert ctx.ledger.total == pytest.approx(100 * 0.50 + 100 * 0.50)  # 入场 + 平仓单金额
    assert result.evidence["notional_usdt"] == pytest.approx(50.0)
    fallback = next(s for s in result.evidence["steps"] if s.get("action") == "notional_fallback")
    assert fallback["fallback_qty"] == "100"
    assert fallback["fallback_notional_usdt"] == pytest.approx(50.0)
    # 全成交产生真实持仓 → 反向市价单平仓清理(认证轮 #2 根因)
    close_step = next(s for s in result.evidence["steps"] if s.get("action") == "close")
    assert close_step["side"] == "SELL" and close_step["qty"] == "100"
    assert close_step["price"] == "0.50" and close_step["price_source"] == "avg_price"
    assert result.evidence["closed"] is True
    assert result.evidence["close_notional_usdt"] == pytest.approx(50.0)


def test_partial_fill_notional_exceeded_fail_fast(tmp_path: Path) -> None:
    # min_qty=1、价 30:过门槛量兜底 = ceil(50/30/1)=2 → notional 60 仍超限(55)
    # → NotionalExceededError 交 runner fail-fast
    info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "1", "stepSize": "1"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                ],
            }
        ]
    }
    depth = {"lastUpdateId": 1, "bids": [["29.9", "5"]], "asks": [["30", "100"]]}
    fake = _FakeClient(exchange_info=info, depth=depth)
    ctx = _ctx(fake, tmp_path, limit=55.0)
    with pytest.raises(NotionalExceededError):
        asyncio.run(PartialFillScenario().run(ctx))


# ---- partial_fill: 轮询判定 ----


def test_partial_fill_full_fill_not_verifiable(tmp_path: Path) -> None:
    fake = _FakeClient(order_statuses=["FILLED"])
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert "liquidity_insufficient_or_too_deep" in result.error_message
    assert fake.cancelled == []  # 全成交,无需撤单清理
    # 全成交产生真实持仓 → 反向市价单平仓(防污染后续场景,认证轮 #2 根因)
    assert fake.close_orders and fake.close_orders[0]["side"] == "SELL"
    assert fake.close_orders[0]["quantity"] == "15"
    assert fake.close_orders[0]["type"] == "MARKET"
    assert fake.close_orders[0]["reduceOnly"] == "true"
    close_step = next(s for s in result.evidence["steps"] if s.get("action") == "close")
    assert close_step["order_id"] == 2 and close_step["status"] == "FILLED"
    assert close_step["executed_qty"] == "15"
    assert result.evidence["closed"] is True
    assert result.evidence["close_needed"] is True
    assert result.evidence["close_notional_usdt"] == pytest.approx(15 * 0.50)
    assert ctx.ledger.total == pytest.approx(15 * 0.50 + 15 * 0.50)  # 入场 + 平仓单金额


def test_partial_fill_stays_new_not_verifiable(tmp_path: Path, monkeypatch: Any) -> None:
    fake = _FakeClient(order_statuses=["NEW"])
    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.partial_fill.asyncio.sleep", _noop_sleep)
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert "liquidity_insufficient_or_too_deep" in result.error_message
    assert len(fake.queries) >= 15  # 30s 窗口(15×2s)轮询满
    assert fake.cancelled == [1]  # 仍挂单 → 撤单清理


# ---- partial_fill: 部分成交成功路径 ----


def test_partial_fill_happy_path(tmp_path: Path) -> None:
    fake = _FakeClient(order_statuses=["PARTIALLY_FILLED", "CANCELED"])
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    evidence = result.evidence
    assert evidence["partial_status"] == "PARTIALLY_FILLED"
    assert fake.placed and fake.placed[0]["price"] == "0.50"  # 价格 = 最优 ask
    assert fake.placed[0]["quantity"] == "15"  # ask 深度 10 × 1.5
    assert fake.cancelled == [1]  # 余量撤单
    # 平掉已成交部分(10),防持仓污染后续场景;反向市价单 + reduceOnly
    assert fake.close_orders and fake.close_orders[0]["side"] == "SELL"
    assert fake.close_orders[0]["quantity"] == "10"
    assert fake.close_orders[0]["type"] == "MARKET"
    assert fake.close_orders[0]["reduceOnly"] == "true"
    close_step = next(s for s in evidence["steps"] if s.get("action") == "close")
    assert close_step["side"] == "SELL" and close_step["qty"] == "10"
    assert close_step["price"] == "0.50" and close_step["price_source"] == "avg_price"
    assert close_step["order_id"] == 2 and close_step["status"] == "FILLED"
    assert evidence["closed"] is True
    assert evidence["close_notional_usdt"] == pytest.approx(10 * 0.50)
    assert ctx.ledger.total == pytest.approx(15 * 0.50 + 10 * 0.50)  # 入场 + 平仓单金额
    # 组件级守卫断言(纯函数)与引擎守卫源码证据
    guard_steps = [s for s in evidence["steps"] if s.get("action") == "guard_component_assert"]
    assert guard_steps and guard_steps[0]["ok"] is True
    source = evidence["monotonic_guard_source"]
    assert source["semantics_consistent"] is True
    assert any(path.endswith("beidou_core/store.py") for path in source["files"])
    assert any(path.endswith("beidou_infra/postgres_store.py") for path in source["files"])
    for entry in source["files"].values():
        assert entry["guard_lines"][0] > 0  # 真实行号
        assert "PARTIALLY_FILLED" in entry["rule_text"]
        assert "NEW" in entry["rule_text"]
    assert evidence["final_status_after_cancel"] == "CANCELED"


# ---- partial_fill: 平仓失败 → close_failed + FAIL(持仓污染不可接受) ----


def test_partial_fill_close_failure_fails(tmp_path: Path) -> None:
    # FILLED 后反向市价平仓单创建失败 → FAIL(CLOSE_FAILED),绝不允许带持仓
    # 离开场景污染后续场景(认证轮 #2 根因)
    fake = _FakeClient(order_statuses=["FILLED"])
    fake.fail_close = True
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "CLOSE_FAILED"
    assert "close boom" in result.error_message
    assert result.evidence["closed"] is False
    close_failed = next(s for s in result.evidence["steps"] if s.get("action") == "close_failed")
    assert close_failed["side"] == "SELL" and close_failed["qty"] == "15"
    assert "close boom" in close_failed["error"]
    assert fake.cancelled == []  # 全成交无需撤单


# ---- partial_fill: 成交后异常路径仍平仓(finally 兜底保证) ----


def test_partial_fill_exception_after_fill_still_closes(tmp_path: Path) -> None:
    # PARTIALLY_FILLED 后撤单抛异常 → FAIL(异常自捕获);finally 先重试撤单
    # 再兜底平仓已成交部分 —— 任何路径不得带持仓离开场景
    fake = _FakeClient(order_statuses=["PARTIALLY_FILLED", "CANCELED"])
    real_cancel = fake.cancel_order
    calls = {"n": 0}

    async def flaky_cancel(symbol: str, order_id: int) -> Result[dict]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("cancel boom")
        return await real_cancel(symbol, order_id)

    fake.cancel_order = flaky_cancel  # type: ignore[method-assign]
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "cancel boom" in result.error_message
    steps = result.evidence["steps"]
    # finally 兜底平仓执行(异常路径也不带持仓离开)
    close_step = next(s for s in steps if s.get("action") == "close")
    assert close_step["side"] == "SELL" and close_step["qty"] == "10"
    assert fake.close_orders and fake.close_orders[0]["quantity"] == "10"
    cleanup = next(s for s in steps if s.get("action") == "cleanup_cancel")
    assert cleanup["ok"] is True  # 撤单重试成功,无挂单残留


# ---- partial_fill: finally 兜底平仓覆盖终态带成交(竞态部分成交后撤单) ----


def test_partial_fill_canceled_with_executed_qty_still_closes(tmp_path: Path) -> None:
    # 撤单竞态部分成交后撤单(CANCELED + executedQty>0 是交易所真实状态):
    # _not_verifiable_terminal 出口后 finally 兜底平仓,不带持仓离开场景
    fake = _FakeClient(order_statuses=["CANCELED"])
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "UNEXPECTED_TERMINAL_WITHOUT_PARTIAL_FILL"
    steps = result.evidence["steps"]
    assert any(s.get("action") == "skip_cleanup_cancel" for s in steps)  # 已终态无需撤单
    close_step = next(s for s in steps if s.get("action") == "close")
    assert close_step["side"] == "SELL" and close_step["qty"] == "10"
    assert fake.close_orders and fake.close_orders[0]["quantity"] == "10"
    assert fake.cancelled == []  # 终态订单不触发撤单


def test_partial_fill_new_with_executed_qty_still_closes(tmp_path: Path, monkeypatch: Any) -> None:
    # 轮询窗口结束仍 NEW 但已有成交(状态查询滞后)→ finally 兜底平仓 + 撤单
    fake = _FakeClient(order_statuses=["NEW"], new_executed_qty="10")
    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.partial_fill.asyncio.sleep", _noop_sleep)
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert "liquidity_insufficient_or_too_deep" in result.error_message
    steps = result.evidence["steps"]
    close_step = next(s for s in steps if s.get("action") == "close")
    assert close_step["side"] == "SELL" and close_step["qty"] == "10"
    assert fake.close_orders and fake.close_orders[0]["quantity"] == "10"
    assert fake.cancelled == [1]  # 未终态订单照常撤单清理


# ---- partial_fill: 撤单竞态成交 → 平仓量取 final executedQty(非 stale) ----


def test_partial_fill_cancel_race_filled_closes_full_executed(tmp_path: Path) -> None:
    # PARTIALLY_FILLED(executed 10)后撤单,撤单查询显示 FILLED(余量竞态成交
    # executed 15)→ 平仓量取 max(10, final 15)=15,不留残余
    fake = _FakeClient(order_statuses=["PARTIALLY_FILLED", "FILLED"])
    ctx = _ctx(fake, tmp_path)
    result = asyncio.run(PartialFillScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "UNEXPECTED_FINAL_STATUS"  # 撤单后终态非 CANCELED
    steps = result.evidence["steps"]
    close_step = next(s for s in steps if s.get("action") == "close")
    assert close_step["qty"] == "15"  # max(轮询 10, final 15)
    assert fake.close_orders and fake.close_orders[0]["quantity"] == "15"
    assert result.evidence["closed"] is True and result.evidence["close_needed"] is True


# ---- cancel_fill_race: 假连接(模拟 PostgresPersistentStore 的 SQL 面) ----


class _FakeRow:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def fetchone(self) -> Any:
        return (self.payload,)

    def fetchall(self) -> list[Any]:
        return []


class _FakeCursor:
    def __init__(self, conn: "_FakeConn", sql: str, params: tuple[Any, ...]) -> None:
        self._conn = conn
        self._sql = sql
        self._params = params
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._conn._fetchone(self._sql, self._params)

    def fetchall(self) -> list[Any]:
        return self._conn._fetchall(self._sql, self._params)


class _FakeConn:
    """内存版 v3_runtime_records:记录 INSERT/UPDATE/SELECT/DELETE 面。"""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.events: list[tuple[str, str, str]] = []
        self.commits = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeCursor:
        normalized = " ".join(sql.split())
        values = tuple(params)
        cursor = _FakeCursor(self, normalized, values)
        if normalized.startswith("INSERT INTO v3_runtime_records"):
            record_type, record_id = str(values[0]), str(values[1])
            payload = json.loads(str(values[2]))
            self.records[(record_type, record_id)] = payload
            cursor.rowcount = 1
        elif normalized.startswith("INSERT INTO v3_runtime_events"):
            self.events.append((str(values[1]), str(values[2]), str(values[3])))
        elif normalized.startswith("DELETE FROM v3_runtime_records"):
            # 场景清理 SQL 把 record_type 写死为字面量,仅传 record_id 一个参数
            record_type = str(values[0]) if len(values) > 1 else "order_state"
            record_id = str(values[1]) if len(values) > 1 else str(values[0])
            cursor.rowcount = int(self.records.pop((record_type, record_id), None) is not None)
        elif normalized.startswith("DELETE FROM v3_runtime_events"):
            record_type = str(values[0]) if len(values) > 1 else "order_state"
            record_id = str(values[1]) if len(values) > 1 else str(values[0])
            before = len(self.events)
            self.events = [e for e in self.events if not (e[0] == record_type and e[1] == record_id)]
            cursor.rowcount = before - len(self.events)
        return cursor

    def _fetchone(self, sql: str, params: tuple[Any, ...]) -> Any:
        if "to_regclass" in sql:
            return ("v3_runtime_records",)
        if "FROM v3_runtime_records" in sql and "record_type=%s AND record_id=%s" in sql:
            payload = self.records.get((str(params[0]), str(params[1])))
            return (json.dumps(payload),) if payload is not None else None
        return None

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        if "FROM schema_migrations" in sql:
            requested = set(params[0]) if params else set()
            return [(version,) for version in requested]
        if "FROM v3_runtime_records" in sql and "WHERE record_type=%s" in sql:
            record_type = str(params[0])
            return [
                (record_id, json.dumps(payload))
                for (kind, record_id), payload in self.records.items()
                if kind == record_type
            ]
        return []

    @contextmanager
    def transaction(self) -> Iterator["_FakeConn"]:
        yield self

    def close(self) -> None:
        return None


def _fake_pg(conn: _FakeConn | None, *, raise_error: Exception | None = None) -> Any:
    def _connect(*args: Any, **kwargs: Any) -> Any:
        if raise_error is not None:
            raise raise_error
        return conn

    return _connect


# ---- cancel_fill_race: dry_run 不碰 PG ----


def test_cancel_fill_race_dry_run_does_not_touch_pg(tmp_path: Path, monkeypatch: Any) -> None:
    def _no_pg(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry_run 不得触碰 PG")

    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.cancel_fill_race.psycopg.connect", _no_pg)
    ctx = _ctx(None, tmp_path, dry_run=True)
    result = asyncio.run(CancelFillRaceScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert ctx.ledger.total == 0.0


def test_cancel_fill_race_pg_failure_self_caught_fail(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "beidou_certification.g5_scenarios.engine.cancel_fill_race.psycopg.connect",
        _fake_pg(None, raise_error=ConnectionError("shared PG unreachable")),
    )
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(CancelFillRaceScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL  # 自捕获,不向上抛
    assert result.error_type == "ConnectionError"
    assert "unreachable" in result.error_message


# ---- cancel_fill_race: 组件级守卫语义验证(真实 store + 内存假连接) ----


def test_cancel_fill_race_happy_path(tmp_path: Path, monkeypatch: Any) -> None:
    conn = _FakeConn()
    monkeypatch.setattr(
        "beidou_certification.g5_scenarios.engine.cancel_fill_race.psycopg.connect",
        _fake_pg(conn),
    )
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(CancelFillRaceScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    evidence = result.evidence
    assert ctx.ledger.total == 0.0  # notional 0,不真实下单
    rows = {s["order_id"]: s for s in evidence["steps"] if s.get("action") == "sequence_done"}

    def _row_for(suffix: str) -> dict[str, Any]:
        return next(row for key, row in rows.items() if key.endswith(suffix))

    # 填单 vs 撤单竞态:NEW → FILLED → 尝试 CANCELED
    race = _row_for("-race")
    assert race["statuses"] == ["NEW", "FILLED", "CANCELED"]
    assert race["all_match"] is True  # 纯函数 == store 直接读
    # 终态不被非终态覆盖:PARTIALLY_FILLED 抗 NEW、FILLED 抗迟到 NEW
    partial = _row_for("-partial")
    assert partial["statuses"] == ["PARTIALLY_FILLED", "NEW"]
    assert partial["all_match"] is True
    late_new = _row_for("-late-new")
    assert late_new["statuses"] == ["NEW", "FILLED", "NEW"]
    assert late_new["all_match"] is True
    # 偏差记录:计划期望 FILLED 抗 CANCELED,实测引擎放行终态→终态
    deviation = evidence["deviation_notes"]
    assert deviation and "CANCELED" in deviation[0]["actual_engine_semantics"]
    # 引擎守卫源码证据
    source = evidence["monotonic_guard_source"]
    assert source["semantics_consistent"] is True
    # 清理零残留
    cleanup = [s for s in evidence["steps"] if s.get("action") == "cleanup_delete"]
    assert cleanup and cleanup[0]["records_deleted"] + cleanup[0]["events_deleted"] > 0
    assert evidence["steps"][-1]["residual"] == []  # cleanup_verify
    residual = [r for r in conn.records if r[0] == "order_state"]
    assert residual == []


# ---- 注册接线(Ruling-5/6:每个任务立即接线注册) ----


def test_fill_race_scenarios_registered() -> None:
    assert SCENARIO_REGISTRY["partial_fill"] is PartialFillScenario
    assert SCENARIO_REGISTRY["cancel_fill_race"] is CancelFillRaceScenario


def test_protocol_subpackage_wired() -> None:
    # Ruling-6:protocol 子包已接线注册(Task 4/5 场景不再游离于注册表之外)
    expected = (
        "create_query_cancel",
        "stable_client_order_id",
        "clock_skew",
        "rate_limit",
        "duplicate_request",
        "credential_failure",
    )
    for sid in expected:
        assert sid in SCENARIO_REGISTRY
