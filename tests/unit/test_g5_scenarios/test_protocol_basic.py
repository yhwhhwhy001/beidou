"""协议组场景一单元测试:判定纯函数、dry_run、注册与假客户端真实路径。"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from beidou_certification.g5_scenarios.base import NotionalLedger, ScenarioContext, ScenarioStatus
from beidou_certification.g5_scenarios.protocol.clock_skew import ClockSkewScenario, is_timestamp_error
from beidou_certification.g5_scenarios.protocol.create_query_cancel import (
    CreateQueryCancelScenario,
    _resting_buy_price,
    min_order_quantity,
)
from beidou_certification.g5_scenarios.protocol.stable_client_order_id import (
    StableClientOrderIdScenario,
    assert_no_duplicate,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.core.error_taxonomy import Result


def test_min_order_quantity_from_exchange_info():
    info = {
        "symbols": [
            {"symbol": "BTCUSDT", "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"}]}
        ]
    }
    assert min_order_quantity("BTCUSDT", info) == (0.001, "0.001")


def test_min_order_quantity_missing_step():
    info = {"symbols": [{"symbol": "X", "filters": [{"filterType": "LOT_SIZE", "minQty": "0.01"}]}]}
    assert min_order_quantity("X", info) == (0.01, "0.001")


def test_min_order_quantity_missing_filter_raises():
    info = {"symbols": [{"symbol": "X", "filters": []}]}
    with pytest.raises(ValueError):
        min_order_quantity("X", info)


def test_min_order_quantity_symbol_missing_raises():
    with pytest.raises(ValueError):
        min_order_quantity("NOPE", {"symbols": [{"symbol": "X", "filters": []}]})


def test_resting_buy_price_aligned_to_tick():
    # 60000.5 × 0.95 = 57000.475 → 向下取整到 tickSize 0.10 → 57000.40(可挂单精度)
    info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                ],
            }
        ]
    }
    assert _resting_buy_price("BTCUSDT", info, "60000.5") == "57000.40"


def test_resting_buy_price_missing_filter_raises():
    with pytest.raises(ValueError):
        _resting_buy_price("X", {"symbols": [{"symbol": "X", "filters": []}]}, "1.0")


def test_assert_no_duplicate_same_order_returned():
    assert assert_no_duplicate({"orderId": 1}, [{"orderId": 1}]) == (True, "same_order_returned")


def test_assert_no_duplicate_new_order_fails():
    assert assert_no_duplicate({"orderId": 2}, [{"orderId": 1}]) == (False, "new_order_created")


def test_assert_no_duplicate_exchange_rejected():
    assert assert_no_duplicate({"code": -4015, "msg": "..."}, [{"orderId": 1}]) == (True, "exchange_rejected")


def test_is_timestamp_error_codes():
    assert is_timestamp_error({"code": -1021})
    assert is_timestamp_error({"code": -1022})
    assert not is_timestamp_error({"code": -1003})
    assert not is_timestamp_error(None)
    assert not is_timestamp_error({})
    assert not is_timestamp_error({"code": "nope"})


class FakeClient:
    """最小假交易所客户端:记录调用参数并按脚本返回结果。"""

    def __init__(self) -> None:
        self._clock_offset_ms = 0
        self.calls: list[tuple] = []
        self.seen_offsets: list[int] = []
        self.create_order_results: list[Result] = []
        self.final_status: dict[int, str] = {}
        self.simulate_resync = True  # 模拟真实客户端对 -1021 自动 resync 校正偏移

    async def get_server_time(self) -> Result:
        self.calls.append(("get_server_time",))
        return Result.ok({"serverTime": 1780000000000})

    async def get_exchange_info(self, symbol: str | None = None) -> Result:
        self.calls.append(("get_exchange_info", symbol))
        return Result.ok(
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        ],
                    }
                ]
            }
        )

    async def get_ticker(self, symbol: str) -> Result:
        self.calls.append(("get_ticker", symbol))
        return Result.ok({"lastPrice": "60000.5"})

    async def get_open_orders(self, symbol: str | None = None) -> Result:
        self.calls.append(("get_open_orders", symbol))
        return Result.ok([{"orderId": 1, "status": "NEW"}])

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
    ) -> Result:
        self.calls.append(("create_order", symbol, side, order_type, quantity, price, time_in_force, client_order_id))
        self.seen_offsets.append(self._clock_offset_ms)
        if self.simulate_resync:
            self._clock_offset_ms = -500  # 客户端 -1021 后 resync 得到真实校准值
        if self.create_order_results:
            return self.create_order_results.pop(0)
        return Result.ok({"orderId": 1, "status": "NEW"})

    async def get_order(self, symbol: str, order_id: int) -> Result:
        self.calls.append(("get_order", order_id))
        return Result.ok({"orderId": order_id, "status": self.final_status.get(order_id, "NEW")})

    async def cancel_order(self, symbol: str, order_id: int) -> Result:
        self.calls.append(("cancel_order", order_id))
        self.final_status[order_id] = "CANCELED"
        return Result.ok({"orderId": order_id, "status": "CANCELED"})


def _ctx(client: FakeClient, tmp_path: Path, *, dry_run: bool = False) -> ScenarioContext:
    return ScenarioContext(
        client=client,
        ledger=NotionalLedger(1000.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=dry_run,
    )


def test_create_query_cancel_dry_run(tmp_path):
    client = FakeClient()
    ctx = _ctx(client, tmp_path, dry_run=True)
    result = asyncio.run(CreateQueryCancelScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert client.calls == []  # dry_run 不发送任何请求
    assert ctx.ledger.total == 0.0  # dry_run 记账 0


def test_create_query_cancel_real_path_pass(tmp_path):
    client = FakeClient()
    ctx = _ctx(client, tmp_path)
    result = asyncio.run(CreateQueryCancelScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.error_type == ""
    assert result.evidence["order_id"] == 1
    actions = [s["action"] for s in result.evidence["steps"]]
    assert actions == ["order", "query", "cancel", "query_after_cancel"]
    # 真实路径记账 min_qty × 现价 = 0.001 × 60000.5
    assert math.isclose(ctx.ledger.total, 0.001 * 60000.5)


def test_create_query_cancel_order_rejected_fails_cleanly(tmp_path):
    client = FakeClient()
    client.create_order_results = [Result.failure("Insufficient margin", raw={"code": -2019, "msg": "..."})]
    ctx = _ctx(client, tmp_path)
    result = asyncio.run(CreateQueryCancelScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "Insufficient margin" in result.error_message


def test_stable_client_order_id_dry_run(tmp_path):
    client = FakeClient()
    ctx = _ctx(client, tmp_path, dry_run=True)
    result = asyncio.run(StableClientOrderIdScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert client.calls == []
    assert ctx.ledger.total == 0.0


def test_stable_client_order_id_real_path_exchange_rejected_pass(tmp_path):
    client = FakeClient()
    client.create_order_results = [
        Result.ok({"orderId": 1, "status": "NEW"}),
        Result.failure("Client order id already exist", raw={"code": -4015, "msg": "dup"}),
    ]
    ctx = _ctx(client, tmp_path)
    result = asyncio.run(StableClientOrderIdScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.error_type == ""
    second = next(s for s in result.evidence["steps"] if s["action"] == "second_order")
    assert second["verdict"] == "exchange_rejected"
    creates = [c for c in client.calls if c[0] == "create_order"]
    assert len(creates) == 2
    assert creates[0][7] == creates[1][7] and creates[0][7] is not None  # 同 clientOrderId
    assert creates[0][5] == "57000.40"  # resting 限价(现价×0.95 对齐 tick)
    assert any(c[0] == "cancel_order" for c in client.calls)  # 残留挂单清理
    assert math.isclose(ctx.ledger.total, 0.001 * 60000.5)


def test_clock_skew_dry_run(tmp_path):
    client = FakeClient()
    ctx = _ctx(client, tmp_path, dry_run=True)
    result = asyncio.run(ClockSkewScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert client.calls == []
    assert ctx.ledger.total == 0.0


def test_clock_skew_real_path_pass_keeps_resync_calibration(tmp_path):
    client = FakeClient()
    ctx = _ctx(client, tmp_path)
    result = asyncio.run(ClockSkewScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.error_type == ""
    assert client.seen_offsets == [300000]  # 下单时注入 +300000 偏差
    order_step = next(s for s in result.evidence["steps"] if s["action"] == "order")
    assert order_step["resynced"] is True  # resync 校准真实发生(证据不再硬编码)
    # 注入前偏移为 0(无校准),resync 已产生真实校准 → 保留 resync 结果,不还原 0
    assert client._clock_offset_ms == -500
    assert math.isclose(ctx.ledger.total, 0.001 * 60000.5)  # 真实下单即记账
    assert any(c[0] == "cancel_order" for c in client.calls)  # 签名验证后撤单


def test_clock_skew_timestamp_error_fails_without_accounting(tmp_path):
    client = FakeClient()
    client.create_order_results = [
        Result.failure("Timestamp for this request is outside of the recvWindow.", raw={"code": -1021, "msg": "..."})
    ]
    ctx = _ctx(client, tmp_path)
    result = asyncio.run(ClockSkewScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "TIMESTAMP_ERROR"
    assert client._clock_offset_ms == -500  # resync 校准已发生,保留
    assert ctx.ledger.total == 0.0  # 未真实下单不记账


def test_clock_skew_business_error_restores_offset_and_passes(tmp_path):
    # 无 -1021(盘口未校验时间戳或业务拒绝):resync 未发生 → 注入前偏移被还原,不污染后续场景
    client = FakeClient()
    client.simulate_resync = False
    client.create_order_results = [
        Result.failure("Insufficient margin", raw={"code": -2019, "msg": "..."}),
    ]
    ctx = _ctx(client, tmp_path)
    result = asyncio.run(ClockSkewScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS  # 非时间戳类错误即 PASS(签名路径已可用)
    assert result.error_type == ""
    assert client._clock_offset_ms == 0  # 未 resync → 还原注入前偏移
    assert ctx.ledger.total == 0.0  # 未下单不记账


def test_protocol_scenarios_registered():
    # 模块尾部注册:import 协议包即注册进 SCENARIO_REGISTRY
    assert SCENARIO_REGISTRY["create_query_cancel"] is CreateQueryCancelScenario
    assert SCENARIO_REGISTRY["stable_client_order_id"] is StableClientOrderIdScenario
    assert SCENARIO_REGISTRY["clock_skew"] is ClockSkewScenario
