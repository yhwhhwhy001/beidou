"""原生保护组单元测试:algo_orphan_verdict/fencing_verdict 判定纯函数(brief 单测
verbatim)、native_protection 挂撤往返全流程与孤儿判定边界(假交易所驱动,禁止
真实交易所调用)、double_worker_fencing 真实 InstanceLock 探针(tmp_path,禁止
真实引擎锁操作)、dry_run NOT_VERIFIABLE 且不触碰任何真实资源、fencing 两路径
与异常 FAIL 自捕获。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from beidou_certification.g5_scenarios.base import NotionalLedger, ScenarioContext, ScenarioStatus
from beidou_certification.g5_scenarios.protection.double_worker_fencing import (
    DoubleWorkerFencingScenario,
    fencing_verdict,
)
from beidou_certification.g5_scenarios.protection.native_protection import (
    NativeProtectionScenario,
    algo_orphan_verdict,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.core.error_taxonomy import Result

# ---- 判定纯函数(brief 单测,verbatim) ----


def test_algo_orphan_verdict():
    algos = [{"algoId": 1, "symbol": "BTCUSDT"}, {"algoId": 2, "symbol": "ETHUSDT"}]
    assert algo_orphan_verdict(algos, {"BTCUSDT"}) == [{"algoId": 2, "symbol": "ETHUSDT"}]
    assert algo_orphan_verdict(algos, {"BTCUSDT", "ETHUSDT"}) == []


def test_fencing_verdict():
    assert fencing_verdict(acquired=False, message="北斗实例已运行，PID=123") == (True, "second_instance_fenced")
    assert fencing_verdict(acquired=True, message="") == (False, "second_instance_acquired_lock")


# ---- 判定纯函数边界 ----


def test_algo_orphan_verdict_edges() -> None:
    # 空 algos / 空持仓
    assert algo_orphan_verdict([], set()) == []
    assert algo_orphan_verdict([], {"BTCUSDT"}) == []
    # 全孤儿:无任何持仓 → 全部返回
    algos = [{"algoId": 1, "symbol": "BTCUSDT"}, {"algoId": 2, "symbol": "ETHUSDT"}]
    assert algo_orphan_verdict(algos, set()) == algos
    # 全有:全部 symbol 都有持仓 → 空
    assert algo_orphan_verdict(algos, {"BTCUSDT", "ETHUSDT", "SOLUSDT"}) == []
    # 缺 symbol 字段按孤儿处理(fail-closed)
    assert algo_orphan_verdict([{"algoId": 9}], {"BTCUSDT"}) == [{"algoId": 9}]
    # 返回原 dict 对象引用(供调用侧与交易所事实对拍)
    orphan = algo_orphan_verdict(algos, {"BTCUSDT"})
    assert orphan[0] is algos[1]


def test_fencing_verdict_edges() -> None:
    # acquired=False 且消息含「已运行」→ 第二实例被 fencing
    assert fencing_verdict(False, "北斗实例已运行，PID=80299") == (True, "second_instance_fenced")
    # 纯函数只关心 acquire 结果:其他拒绝消息同样判 fenced
    assert fencing_verdict(False, "实例锁竞争失败") == (True, "second_instance_fenced")
    # acquired=True → 第二实例拿到锁(引擎未运行)
    assert fencing_verdict(True, "PID=12345") == (False, "second_instance_acquired_lock")


# ---- 注册接线 ----


def test_registered() -> None:
    assert SCENARIO_REGISTRY["native_protection"] is NativeProtectionScenario
    assert SCENARIO_REGISTRY["double_worker_fencing"] is DoubleWorkerFencingScenario


# ---- 公共上下文 ----

_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            ],
        }
    ]
}


def _ctx(tmp_path: Path, *, dry_run: bool = False, client: Any = None) -> ScenarioContext:
    return ScenarioContext(
        client=client,
        ledger=NotionalLedger(1000.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=dry_run,
    )


class _FakeClock:
    """native_protection 假时钟:now() 返回假时间,sleep() 直接推进假时间。"""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FakeAlgoExchange:
    """native_protection 假交易所:内存账户持仓 + open algo 列表,记录全部调用。

    create_registers=False 模拟幽灵 ACK(create 返回 algoId 但 open 列表
    不出现),驱动挂撤往返断裂路径;appear_after_polls=N 模拟 algo 挂单
    传播延迟(挂单后第 N 次 open 轮询才出现)。
    """

    def __init__(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        open_algos: list[dict[str, Any]] | None = None,
        next_algo_id: int = 9001,
        create_registers: bool = True,
        appear_after_polls: int = 0,
    ) -> None:
        self.positions = positions if positions is not None else []
        self.open_algos = [dict(a) for a in (open_algos or [])]
        self.next_algo_id = next_algo_id
        self.create_registers = create_registers
        self.appear_after_polls = appear_after_polls
        self.calls: list[str] = []
        self.created_params: list[dict[str, Any]] = []
        self.cancelled: list[tuple[str, int]] = []
        self._pending_algo: dict[str, Any] | None = None
        self._polls_after_create = 0

    async def get_account(self) -> dict[str, Any]:
        self.calls.append("get_account")
        return {"positions": [dict(p) for p in self.positions]}

    async def get_exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append("get_exchange_info")
        return _EXCHANGE_INFO

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        self.calls.append("get_ticker")
        return {"lastPrice": "60000.00"}

    async def get_open_algo_orders(self) -> list[dict[str, Any]]:
        self.calls.append("get_open_algo_orders")
        if self._pending_algo is not None and self.create_registers:
            self._polls_after_create += 1
            if self._polls_after_create >= self.appear_after_polls:
                self.open_algos.append(self._pending_algo)
                self._pending_algo = None
        return [dict(a) for a in self.open_algos]

    async def create_algo_order(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_algo_order")
        self.created_params.append(dict(params))
        algo_id = self.next_algo_id
        self.next_algo_id += 1
        created = {"algoId": algo_id, "algoStatus": "NEW", **params}
        if self.create_registers and self.appear_after_polls == 0:
            self.open_algos.append(created)
        else:
            self._pending_algo = created
        return created

    async def cancel_algo_order(self, symbol: str, algo_id: int) -> dict[str, Any]:
        self.calls.append("cancel_algo_order")
        self.cancelled.append((symbol, algo_id))
        self.open_algos = [a for a in self.open_algos if a.get("algoId") != algo_id]
        return {"algoId": algo_id, "algoStatus": "CANCELLED"}


def _np_scenario(exchange: _FakeAlgoExchange, clock: _FakeClock | None = None) -> NativeProtectionScenario:
    return NativeProtectionScenario(
        get_account=exchange.get_account,
        get_exchange_info=exchange.get_exchange_info,
        get_ticker=exchange.get_ticker,
        get_open_algo_orders=exchange.get_open_algo_orders,
        create_algo_order=exchange.create_algo_order,
        cancel_algo_order=exchange.cancel_algo_order,
        now=clock.now if clock is not None else None,
        sleep=clock.sleep if clock is not None else None,
    )


# ---- native_protection dry_run:NOT_VERIFIABLE 且不触碰任何真实资源 ----


def test_native_protection_dry_run_not_verifiable_no_real_ops(tmp_path: Path) -> None:
    async def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry_run 不得执行任何真实交易所调用")

    scenario = NativeProtectionScenario(
        get_account=_forbidden,
        get_exchange_info=_forbidden,
        get_ticker=_forbidden,
        get_open_algo_orders=_forbidden,
        create_algo_order=_forbidden,
        cancel_algo_order=_forbidden,
    )
    ctx = _ctx(tmp_path, dry_run=True)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "REAL_EXECUTION_REQUIRED"
    assert "requires real execution" in result.error_message
    assert result.evidence["dry_run"] is True
    assert result.evidence["pure_verdict_probe"]["orphans"] == 0
    assert ctx.ledger.total == 0.0


# ---- native_protection 全流程:挂→现→撤→消失→孤儿对拍 → PASS ----


def test_native_protection_roundtrip_pass(tmp_path: Path) -> None:
    # 空账户 + 无存量 algo:场景自挂"孤儿"探针单,挂→现→撤→消失,最终
    # 孤儿对拍为空 → PASS(fail-closed:空账户下任何残留 open algo 都是缺陷)
    exchange = _FakeAlgoExchange(open_algos=[], next_algo_id=9001)
    scenario = _np_scenario(exchange)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=exchange)))
    assert result.status == ScenarioStatus.PASS
    evidence = result.evidence
    assert evidence["verdict"] == "native_protection_roundtrip_ok"
    assert evidence["notional_usdt"] == 0.0
    assert evidence["algo_id"] == 9001
    steps = evidence["steps"]

    snapshot = next(s for s in steps if s.get("action") == "account_snapshot")
    assert snapshot["position_symbols"] == []
    baseline = next(s for s in steps if s.get("action") == "baseline_open_algos")
    assert baseline["count"] == 0 and baseline["algo_ids"] == []
    created = next(s for s in steps if s.get("action") == "algo_created")
    assert created["algo_id"] == 9001 and created["algo_status"] == "NEW"
    appears = next(s for s in steps if s.get("action") == "algo_appears_in_open")
    assert appears["appeared"] is True
    assert appears["polls"] == 1  # 立即出现,单次轮询即闭合
    cancelled = next(s for s in steps if s.get("action") == "algo_cancelled")
    assert cancelled["algo_id"] == 9001 and cancelled["status"] == "CANCELLED"
    gone = next(s for s in steps if s.get("action") == "algo_gone_after_cancel")
    assert gone["gone"] is True
    orphan = next(s for s in steps if s.get("action") == "orphan_verdict")
    assert orphan["orphan_count"] == 0 and orphan["orphans"] == []
    # 恰好一次撤单(非重复),无残留
    assert exchange.cancelled == [("BTCUSDT", 9001)]

    # 写操作意图参数:远价触发 = 现价 × 0.5,quantity = min_qty
    assert evidence["trigger_price"] == "30000.00"
    assert evidence["quantity"] == "0.001000"
    params = exchange.created_params[0]
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "SELL"
    assert params["algoType"] == "CONDITIONAL"
    assert params["type"] == "STOP_MARKET"
    assert params["triggerPrice"] == "30000.00"
    assert params["quantity"] == "0.001000"
    assert params["reduceOnly"] == "true"
    assert params["workingType"] == "CONTRACT_PRICE"
    assert params["clientAlgoId"].startswith("g5-np-")


def test_native_protection_ledger_records_zero(tmp_path: Path) -> None:
    exchange = _FakeAlgoExchange(next_algo_id=9001)
    scenario = _np_scenario(exchange)
    ctx = _ctx(tmp_path, client=exchange)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert ctx.ledger.total == 0.0  # Algo 单不占用即时资金,notional 记账 0


# ---- native_protection 有真实持仓:SL/TP 覆盖 + 孤儿对拍 → PASS ----


def test_native_protection_with_positions_coverage_pass(tmp_path: Path) -> None:
    positions = [{"symbol": "BTCUSDT", "positionAmt": "0.1"}]
    engine_protection = {"algoId": 7001, "symbol": "BTCUSDT", "algoStatus": "NEW"}
    exchange = _FakeAlgoExchange(positions=positions, open_algos=[engine_protection], next_algo_id=8001)
    scenario = _np_scenario(exchange)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=exchange)))
    assert result.status == ScenarioStatus.PASS
    steps = result.evidence["steps"]
    snapshot = next(s for s in steps if s.get("action") == "account_snapshot")
    assert snapshot["position_symbols"] == ["BTCUSDT"]
    coverage = next(s for s in steps if s.get("action") == "protection_coverage")
    assert coverage["position_symbols"] == ["BTCUSDT"] and coverage["missing"] == []
    orphan = next(s for s in steps if s.get("action") == "orphan_verdict")
    assert orphan["orphan_count"] == 0
    # 引擎保护单未被本场景撤掉
    assert all(algo_id != 7001 for _, algo_id in exchange.cancelled)


# ---- native_protection 孤儿判定:空账户下残留 open algo → FAIL ----


def test_native_protection_orphan_algo_fail(tmp_path: Path) -> None:
    # 空账户但交易所存在未归属 algo 单(引擎缺陷或残留)→ 孤儿判定 FAIL
    exchange = _FakeAlgoExchange(open_algos=[{"algoId": 5001, "symbol": "ETHUSDT"}], next_algo_id=6001)
    scenario = _np_scenario(exchange)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=exchange)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "ORPHAN_ALGO_DETECTED"
    steps = result.evidence["steps"]
    orphan = next(s for s in steps if s.get("action") == "orphan_verdict")
    assert orphan["orphan_count"] == 1
    assert orphan["orphans"] == [{"algoId": 5001, "symbol": "ETHUSDT"}]


# ---- native_protection 持仓保护覆盖:有持仓但引擎未挂 SL/TP → FAIL ----


def test_native_protection_missing_coverage_fail(tmp_path: Path) -> None:
    exchange = _FakeAlgoExchange(positions=[{"symbol": "BTCUSDT", "positionAmt": "0.1"}], next_algo_id=6001)
    scenario = _np_scenario(exchange)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=exchange)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "MISSING_PROTECTION_ALGO"
    steps = result.evidence["steps"]
    coverage = next(s for s in steps if s.get("action") == "protection_coverage")
    assert coverage["missing"] == ["BTCUSDT"]


# ---- native_protection 挂单传播延迟:轮询窗口内出现 → 仍 PASS ----


def test_native_protection_delayed_appearance_polls_pass(tmp_path: Path) -> None:
    # algo 挂单传播有延迟:挂单后第 3 次 open 轮询才出现(轮询窗口 ≤5s 内)
    # → appeared=True,挂撤往返闭合,不误伤单次查询 lag
    clock = _FakeClock()
    exchange = _FakeAlgoExchange(open_algos=[], next_algo_id=9001, appear_after_polls=3)
    scenario = _np_scenario(exchange, clock)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=exchange)))
    assert result.status == ScenarioStatus.PASS
    steps = result.evidence["steps"]
    appears = next(s for s in steps if s.get("action") == "algo_appears_in_open")
    assert appears["appeared"] is True
    assert appears["polls"] == 3  # 第 3 次轮询才出现
    assert clock.t == pytest.approx(2.0)  # 两次轮询间隔 1s
    assert exchange.cancelled == [("BTCUSDT", 9001)]


# ---- native_protection 挂撤往返断裂(幽灵 ACK)→ FAIL ----


def test_native_protection_roundtrip_appearance_fail(tmp_path: Path) -> None:
    # create 返回 algoId 但 open 列表在 5s 轮询窗口内始终未出现 → 撤单仍执行,
    # 最终 FAIL(轮询窗口跑满 6 次,假时钟推进 5s)
    clock = _FakeClock()
    exchange = _FakeAlgoExchange(open_algos=[], next_algo_id=6001, create_registers=False)
    scenario = _np_scenario(exchange, clock)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=exchange)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "ALGO_ROUNDTRIP_FAILED"
    steps = result.evidence["steps"]
    appears = next(s for s in steps if s.get("action") == "algo_appears_in_open")
    assert appears["appeared"] is False
    assert appears["polls"] == 6  # t=0..5 共 6 次轮询
    assert appears["poll_window_seconds"] == pytest.approx(5.0)
    warning = next(s for s in steps if s.get("action") == "algo_appearance_warning")
    assert warning is not None
    # 撤单仍执行,不残留
    assert exchange.cancelled == [("BTCUSDT", 6001)]


# ---- native_protection 撤单失败 → 清理撤单,FAIL 自捕获 ----


def test_native_protection_cancel_failure_cleanup_then_fail(tmp_path: Path) -> None:
    exchange = _FakeAlgoExchange(open_algos=[], next_algo_id=6001)
    real_cancel = exchange.cancel_algo_order
    calls = {"n": 0}

    async def flaky_cancel(symbol: str, algo_id: int) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("cancel boom")
        return await real_cancel(symbol, algo_id)

    scenario = NativeProtectionScenario(
        get_account=exchange.get_account,
        get_exchange_info=exchange.get_exchange_info,
        get_ticker=exchange.get_ticker,
        get_open_algo_orders=exchange.get_open_algo_orders,
        create_algo_order=exchange.create_algo_order,
        cancel_algo_order=flaky_cancel,
    )
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=exchange)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "cancel boom" in result.error_message
    steps = result.evidence["steps"]
    cleanup = next(s for s in steps if s.get("action") == "cleanup_cancel_algo")
    assert cleanup["ok"] is True
    # 清理撤单成功,无残留
    assert all(a.get("algoId") != 6001 for a in exchange.open_algos)


# ---- native_protection 挂单失败 → FAIL,未挂单无撤单 ----


def test_native_protection_create_failure_fail(tmp_path: Path) -> None:
    async def _boom_create(params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("create algo boom")

    exchange = _FakeAlgoExchange()
    scenario = NativeProtectionScenario(
        get_account=exchange.get_account,
        get_exchange_info=exchange.get_exchange_info,
        get_ticker=exchange.get_ticker,
        get_open_algo_orders=exchange.get_open_algo_orders,
        create_algo_order=_boom_create,
        cancel_algo_order=exchange.cancel_algo_order,
    )
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=exchange)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "create algo boom" in result.error_message
    steps = result.evidence["steps"]
    assert any(s.get("action") == "baseline_open_algos" for s in steps)
    assert not any(s.get("action") == "algo_created" for s in steps)
    assert exchange.cancelled == []  # 未挂单,无撤单


# ---- native_protection 客户端缺失 → NOT_VERIFIABLE ----


def test_native_protection_client_unavailable_not_verifiable(tmp_path: Path) -> None:
    scenario = NativeProtectionScenario()  # 默认真实依赖:CLIENT_UNAVAILABLE 先行返回
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=None)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "CLIENT_UNAVAILABLE"


# ---- native_protection 默认依赖解包 Result 时 data=None → 显式 RuntimeError ----
# (不用 assert:python -O 下 assert 被剥离,None 会流出为 T)


def test_native_protection_result_none_data_fail(tmp_path: Path) -> None:
    class _NoneDataClient:
        async def get_account(self) -> Any:
            return Result.success(None)

        async def get_exchange_info(self, symbol: str) -> Any:
            raise AssertionError("不应到达 get_exchange_info")

        async def get_ticker(self, symbol: str) -> Any:
            raise AssertionError("不应到达 get_ticker")

        async def get_open_algo_orders(self) -> Any:
            raise AssertionError("不应到达 get_open_algo_orders")

        async def create_algo_order(self, params: dict[str, Any]) -> Any:
            raise AssertionError("不应到达 create_algo_order")

        async def cancel_algo_order(self, symbol: str, algo_id: int) -> Any:
            raise AssertionError("不应到达 cancel_algo_order")

    scenario = NativeProtectionScenario()  # 默认真实依赖:解包 Result 的路径
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=_NoneDataClient())))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "get_account" in result.error_message and "no data" in result.error_message
    assert not any(s.get("action") == "algo_created" for s in result.evidence["steps"])


# ---- double_worker_fencing dry_run:NOT_VERIFIABLE 且不触碰任何真实资源 ----


def test_double_worker_fencing_dry_run_not_verifiable_no_real_ops(tmp_path: Path) -> None:
    def _forbidden_lock(path: Path) -> Any:
        raise AssertionError("dry_run 不得执行任何真实 lock 操作")

    def _forbidden_read(path: Path) -> Any:
        raise AssertionError("dry_run 不得执行任何真实 lock 读取")

    scenario = DoubleWorkerFencingScenario(
        engine_lock_path=tmp_path / "beidou.pid",
        make_lock=_forbidden_lock,
        read_lock_file=_forbidden_read,
    )
    ctx = _ctx(tmp_path, dry_run=True)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "REAL_EXECUTION_REQUIRED"
    assert "requires real execution" in result.error_message
    assert result.evidence["dry_run"] is True
    assert result.evidence["pure_verdict_probe"]["fenced"] is True
    assert ctx.ledger.total == 0.0


# ---- double_worker_fencing 全流程:引擎锁命中 → 第二实例被 fencing → PASS ----


def test_double_worker_fencing_fenced_pass(tmp_path: Path) -> None:
    engine_lock_path = tmp_path / "beidou.pid"
    engine_lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    scenario = DoubleWorkerFencingScenario(engine_lock_path=engine_lock_path, temp_lock_dir=tmp_path)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.PASS
    evidence = result.evidence
    assert evidence["verdict"] == "second_instance_fenced"
    assert evidence["engine_pid"] == str(os.getpid())
    assert evidence["probe_lock_acquired"] is True
    assert evidence["notional_usdt"] == 0.0
    steps = evidence["steps"]

    read = next(s for s in steps if s.get("action") == "engine_lock_read")
    assert read["present"] is True and read["pid"] == str(os.getpid())
    attempt = next(s for s in steps if s.get("action") == "engine_lock_acquire_attempt")
    assert attempt["acquired"] is False and "已运行" in attempt["message"]
    probe = next(s for s in steps if s.get("action") == "probe_lock_acquire")
    assert probe["acquired"] is True
    release = next(s for s in steps if s.get("action") == "probe_lock_release")
    assert release["released"] is True
    verdict = next(s for s in steps if s.get("action") == "verdict")
    assert verdict["fenced"] is True and verdict["reason"] == "second_instance_fenced"

    # acquire 失败不破坏引擎持有的锁文件
    assert engine_lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    # 临时探针锁已清理
    assert not (tmp_path / "probe.pid").exists()


# ---- double_worker_fencing 引擎未运行 → NOT_VERIFIABLE,误取的锁立即归还 ----


def test_double_worker_fencing_engine_not_running_not_verifiable(tmp_path: Path) -> None:
    engine_lock_path = tmp_path / "beidou.pid"
    scenario = DoubleWorkerFencingScenario(engine_lock_path=engine_lock_path, temp_lock_dir=tmp_path)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_lock_not_held"
    steps = result.evidence["steps"]
    attempt = next(s for s in steps if s.get("action") == "engine_lock_acquire_attempt")
    assert attempt["acquired"] is True
    released = next(s for s in steps if s.get("action") == "engine_lock_released")
    assert released is not None
    # 误取的引擎锁已释放,不残留
    assert not engine_lock_path.exists()
    assert not (tmp_path / "probe.pid").exists()


# ---- double_worker_fencing 临时路径锁获取失败 → FAIL ----


class _FakeLock:
    """按预设结果应答 acquire 的假锁,记录 release 调用。"""

    def __init__(self, result: tuple[bool, str]) -> None:
        self._result = result
        self.released = False

    def acquire(self) -> tuple[bool, str]:
        return self._result

    def release(self) -> None:
        self.released = True


def test_double_worker_fencing_probe_lock_failure_fail(tmp_path: Path) -> None:
    engine_lock_path = tmp_path / "beidou.pid"
    engine_lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    engine_lock = _FakeLock((False, "北斗实例已运行，PID=123"))
    probe_lock = _FakeLock((False, "probe lock busy"))

    def _make_lock(path: Path) -> Any:
        if path.name == "beidou.pid":
            return engine_lock
        return probe_lock

    scenario = DoubleWorkerFencingScenario(
        engine_lock_path=engine_lock_path,
        temp_lock_dir=tmp_path,
        make_lock=_make_lock,
    )
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "PROBE_LOCK_ACQUIRE_FAILED"
    assert "probe lock busy" in result.error_message
    # 引擎锁未被误释放(锁本来就属于引擎)
    assert engine_lock.released is False


# ---- double_worker_fencing 拒绝消息不符合约定 → FAIL ----


def test_double_worker_fencing_unexpected_message_fail(tmp_path: Path) -> None:
    scenario = DoubleWorkerFencingScenario(
        engine_lock_path=tmp_path / "beidou.pid",
        make_lock=lambda path: _FakeLock((False, "locked by another service")),
    )
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "UNEXPECTED_LOCK_MESSAGE"


# ---- double_worker_fencing 异常自捕获 → FAIL,框架不崩溃 ----


def test_double_worker_fencing_acquire_exception_fail(tmp_path: Path) -> None:
    def _boom_lock(path: Path) -> Any:
        raise RuntimeError("lock acquire boom")

    scenario = DoubleWorkerFencingScenario(
        engine_lock_path=tmp_path / "beidou.pid",
        make_lock=_boom_lock,
    )
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "lock acquire boom" in result.error_message
