"""Offline contract tests for the V4 Testnet verification composition root."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from apps.testnet_verify.config import VerifierConfig
from apps.testnet_verify.runtime import VerificationRuntime
from beidou_data.trading_pool_lifecycle import TradingPool
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result
from beidou_exchange.core.protocol import OrderRequest, OrderResponse
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
from beidou_shared.types import AccountRef, OrderSide, OrderStatus, Quantity


def _bars() -> list[dict[str, Any]]:
    return [
        {
            "open_time": index * 60_000,
            "close_time": (index + 1) * 60_000,
            "close": str(100.0 + index * 0.1),
            "is_closed": True,
        }
        for index in range(100)
    ]


class FakeTestnetAdapter:
    """Adapter-shaped fake; network semantics stay outside the runtime test."""

    validate_order_ack = staticmethod(BinanceUsdmAdapter.validate_order_ack)

    def __init__(self, *, recovery: str = "query", leverage: int = 2) -> None:
        self.recovery = recovery
        self.leverage = leverage
        self.position = 0.0
        self.set_leverage_calls = 0
        self.read_leverage_calls = 0
        self.create_order_calls: list[str] = []
        self.query_calls: list[str] = []
        self._order_counter = 100
        self._unknown_returned = False
        self._pending_request: OrderRequest | None = None

    async def fetch_exchange_info(self) -> Result[dict[str, Any]]:
        return Result.success(
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                    }
                ]
            },
            source="fake-exchange-info",
        )

    async def get_server_time(self) -> Result[dict[str, Any]]:
        return Result.success({"serverTime": 1_700_000_000_000}, source="fake-server-time")

    async def get_position_mode(self) -> Result[dict[str, Any]]:
        return Result.success({"dualSidePosition": False}, source="fake-position-mode")

    async def get_account_snapshot(self) -> Result[dict[str, Any]]:
        return Result.success(
            {
                "totalWalletBalance": "1000",
                "availableBalance": "1000",
                "canTrade": True,
                "canWithdraw": False,
            },
            source="fake-account",
        )

    async def get_ticker(self, symbol: str) -> Result[dict[str, Any]]:
        return Result.success(
            {
                "symbol": symbol,
                "bidPrice": "109.89",
                "askPrice": "109.91",
                "lastPrice": "109.90",
                "quoteVolume": "1000000",
            },
            source="fake-ticker",
        )

    async def get_depth(self, symbol: str, limit: int = 20) -> Result[dict[str, Any]]:
        del limit
        return Result.success(
            {
                "symbol": symbol,
                "bids": [["109.89", "10"]] * 5,
                "asks": [["109.91", "10"]] * 5,
            },
            source="fake-depth",
        )

    async def get_closed_klines(self, symbol: str, interval: str, limit: int) -> Result[list[dict[str, Any]]]:
        del symbol, interval, limit
        return Result.success(_bars(), source="fake-closed-bars")

    async def get_funding_rate(self, symbol: str, limit: int = 1) -> Result[list[dict[str, Any]]]:
        del symbol, limit
        return Result.success([{"fundingRate": "0.0001"}], source="fake-funding")

    def get_rule_snapshot(self, symbol: str) -> InstrumentRuleSnapshot:
        return InstrumentRuleSnapshot(
            symbol=symbol,
            tick_size="0.01",
            step_size="0.001",
            min_qty="0.001",
            min_notional="5",
            price_precision=2,
            qty_precision=3,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )

    async def get_position_risk(self, symbol: str) -> Result[list[dict[str, Any]]]:
        return Result.success(
            [
                {
                    "symbol": symbol,
                    "positionAmt": str(self.position),
                    "entryPrice": "109.90" if self.position else "0",
                    "unRealizedProfit": "0",
                    "leverage": str(self.leverage),
                }
            ],
            source="fake-position-risk",
        )

    async def set_leverage(
        self,
        symbol: str,
        leverage: int,
        *,
        account_ref: AccountRef,
        write_context: Any,
    ) -> Result[dict[str, Any]]:
        del account_ref, write_context
        self.set_leverage_calls += 1
        self.leverage = leverage
        return Result.success({"symbol": symbol, "leverage": leverage}, source="fake-set-leverage")

    async def read_leverage(self, symbol: str) -> Result[dict[str, Any]]:
        self.read_leverage_calls += 1
        readback = self.leverage + 1 if self.recovery == "leverage_mismatch" else self.leverage
        return Result.success({"symbol": symbol, "leverage": readback}, source="fake-read-leverage")

    @staticmethod
    def _order_response(request: OrderRequest, order_id: str, *, executed: str) -> OrderResponse:
        raw = {
            "orderId": order_id,
            "clientOrderId": request.client_order_id,
            "symbol": str(request.venue_instrument.instrument_id),
            "status": "FILLED",
            "side": request.side.value,
            "type": request.order_type.value,
            "origQty": str(request.quantity.amount),
            "executedQty": executed,
            "avgPrice": "109.90",
            "reduceOnly": request.reduce_only,
            "commission": "0.01",
        }
        return OrderResponse(
            venue_instrument=request.venue_instrument,
            account_ref=request.account_ref,
            order_id=order_id,
            client_order_id=request.client_order_id,
            status=OrderStatus.FILLED,
            side=request.side,
            order_type=request.order_type,
            original_quantity=request.quantity,
            executed_quantity=Quantity(amount=executed),
            average_price=None,
            commission=None,
            correlation_id=request.correlation_id,
            raw_response=raw,
        )

    async def create_order(self, request: OrderRequest, *, write_context: Any) -> OrderResponse:
        del write_context
        client_id = str(request.client_order_id)
        self.create_order_calls.append(client_id)
        if self.recovery == "reject" and not request.reduce_only:
            return replace(
                self._order_response(request, "", executed="0"),
                order_id="",
                status=OrderStatus.REJECTED,
                raw_response={"reason": "simulated-deterministic-reject"},
            )
        if self.recovery == "query" and not self._unknown_returned and not request.reduce_only:
            self._unknown_returned = True
            self._pending_request = request
            return OrderResponse(
                venue_instrument=request.venue_instrument,
                account_ref=request.account_ref,
                order_id="",
                client_order_id=request.client_order_id,
                status=OrderStatus.UNKNOWN,
                side=request.side,
                order_type=request.order_type,
                original_quantity=request.quantity,
                executed_quantity=Quantity(amount="0"),
                average_price=None,
                commission=None,
                correlation_id=request.correlation_id,
                raw_response={"reason": "simulated-timeout"},
            )
        if self.recovery == "found" and not self._unknown_returned and not request.reduce_only:
            self._unknown_returned = True
            self._pending_request = request
            return replace(
                self._order_response(request, "", executed="0"),
                order_id="",
                status=OrderStatus.UNKNOWN,
                raw_response={"reason": "simulated-timeout"},
            )

        executed = str(request.quantity.amount)
        amount = float(executed)
        self.position += (
            -amount
            if request.reduce_only and request.side is OrderSide.SELL
            else (amount if request.side is OrderSide.BUY else -amount)
        )
        self._order_counter += 1
        response = self._order_response(request, str(self._order_counter), executed=executed)
        if self.recovery == "bad_ack" and not request.reduce_only:
            raw = dict(response.raw_response or {})
            raw["clientOrderId"] = "wrong-client-id"
            return replace(response, raw_response=raw)
        return response

    async def query_order_by_client_id(self, symbol: str, client_id: str) -> Result[dict[str, Any]]:
        del symbol
        self.query_calls.append(client_id)
        if self.recovery == "found" and self._pending_request is not None:
            request = self._pending_request
            executed = str(request.quantity.amount)
            amount = float(executed)
            self.position += amount if request.side is OrderSide.BUY else -amount
            self._order_counter += 1
            response = self._order_response(request, str(self._order_counter), executed=executed)
            return Result.success(response.raw_response or {}, source="fake-order-recovery")
        if self.recovery == "query":
            return Result.failure(
                "simulated order absent; same-id retry is permitted",
                category=ErrorCategory.UNKNOWN,
                raw={"code": -2013},
                source="fake-order-recovery",
            )
        return Result.failure(
            "simulated order query unavailable",
            category=ErrorCategory.UNKNOWN,
            raw={"code": -1006},
            source="fake-order-recovery",
        )


def _config(tmp_path: Path, *, close_after_verify: bool = True) -> VerifierConfig:
    return VerifierConfig(
        api_key="test-key",
        api_secret="fixture-value",  # noqa: S106 - non-secret test fixture
        account_id="dedicated-testnet-account",
        max_notional=25.0,
        max_leverage=3.0,
        max_instruments=1,
        interval="1m",
        kline_limit=100,
        confirm_testnet=True,
        once=True,
        close_after_verify=close_after_verify,
        trace_path=tmp_path / "trace.jsonl",
        pool_state_path=tmp_path / "pool.json",
        evidence_dir=tmp_path / "evidence",
    )


@pytest.mark.asyncio
async def test_runtime_closes_filled_episode_after_unknown_same_id_recovery(tmp_path: Path) -> None:
    adapter = FakeTestnetAdapter(recovery="query")
    pool = TradingPool(max_instruments=1)
    pool.add("BTCUSDT").min_observation_hours = 1.0
    runtime = VerificationRuntime(_config(tmp_path), adapter=adapter, pool=pool)

    summary = await runtime.run_once()

    assert summary.status == "EPISODE_COMPLETED"
    assert summary.startup["pool"]["active_symbols"] == ["BTCUSDT"]
    assert adapter.set_leverage_calls == 1
    assert adapter.read_leverage_calls == 1
    assert adapter.query_calls == [adapter.create_order_calls[0]]
    assert len(adapter.create_order_calls) == 3
    assert adapter.create_order_calls[0] == adapter.create_order_calls[1]
    assert adapter.create_order_calls[2].endswith("-c")
    assert adapter.position == pytest.approx(0.0)
    traces = runtime.trace_store.all()
    assert {trace.status.value for trace in traces} == {"FILLED", "CLOSED"}
    primary = next(trace for trace in traces if not trace.trace_id.endswith("-c"))
    assert primary.sizing["final_quantity"] == primary.order_request["quantity"]
    assert primary.order_ack["clientOrderId"] == primary.client_order_id
    assert primary.reconciliation["unresolved"] == []
    assert primary.strategy["proposal_hash"]
    assert primary.pool["hash"]
    assert Path(summary.manifest_path).exists()


@pytest.mark.asyncio
async def test_runtime_uses_query_result_without_resubmitting_when_order_is_found(tmp_path: Path) -> None:
    adapter = FakeTestnetAdapter(recovery="found")
    pool = TradingPool(max_instruments=1)
    pool.add("BTCUSDT").min_observation_hours = 1.0
    runtime = VerificationRuntime(_config(tmp_path, close_after_verify=False), adapter=adapter, pool=pool)

    summary = await runtime.run_once()

    assert summary.status == "EPISODE_COMPLETED"
    assert len(adapter.create_order_calls) == 1
    assert adapter.query_calls == adapter.create_order_calls
    assert adapter.position > 0
    assert runtime.trace_store.all()[0].status.value == "FILLED"


@pytest.mark.asyncio
async def test_runtime_does_not_query_after_deterministic_rejection(tmp_path: Path) -> None:
    adapter = FakeTestnetAdapter(recovery="reject")
    pool = TradingPool(max_instruments=1)
    pool.add("BTCUSDT").min_observation_hours = 1.0
    runtime = VerificationRuntime(_config(tmp_path, close_after_verify=False), adapter=adapter, pool=pool)

    summary = await runtime.run_once()

    assert summary.status == "NOT_VERIFIABLE"
    assert len(adapter.create_order_calls) == 1
    assert adapter.query_calls == []
    trace = runtime.trace_store.all()[0]
    assert trace.status.value == "FAILED"
    assert trace.error["reason"] == "ORDER_SUBMIT_REJECTED"


@pytest.mark.asyncio
async def test_runtime_blocks_order_when_leverage_readback_is_mismatched(tmp_path: Path) -> None:
    adapter = FakeTestnetAdapter(recovery="leverage_mismatch")
    pool = TradingPool(max_instruments=1)
    pool.add("BTCUSDT").min_observation_hours = 1.0
    runtime = VerificationRuntime(_config(tmp_path, close_after_verify=False), adapter=adapter, pool=pool)

    summary = await runtime.run_once()

    assert summary.status == "NOT_VERIFIABLE"
    assert adapter.create_order_calls == []
    trace = runtime.trace_store.all()[0]
    assert trace.status.value == "FAILED"
    assert trace.error["reason"] == "LEVERAGE_READBACK_MISMATCH"


@pytest.mark.asyncio
async def test_runtime_records_no_action_and_performs_no_write_without_confirmation(tmp_path: Path) -> None:
    adapter = FakeTestnetAdapter(recovery="found")
    pool = TradingPool(max_instruments=1)
    pool.add("BTCUSDT").min_observation_hours = 1.0
    config = replace(_config(tmp_path, close_after_verify=False), confirm_testnet=False)
    runtime = VerificationRuntime(config, adapter=adapter, pool=pool)

    summary = await runtime.run_once()

    assert summary.status == "NOT_VERIFIABLE"
    assert adapter.set_leverage_calls == 0
    assert adapter.create_order_calls == []
    trace = runtime.trace_store.all()[0]
    assert trace.status.value == "FAILED"
    assert trace.error["reason"] == "CONFIRM_TESTNET_REQUIRED"


@pytest.mark.asyncio
async def test_same_closed_bar_reuses_identity_and_does_not_duplicate_after_restart(tmp_path: Path) -> None:
    first_adapter = FakeTestnetAdapter(recovery="query")
    first_pool = TradingPool(max_instruments=1)
    first_pool.add("BTCUSDT").min_observation_hours = 1.0
    first_runtime = VerificationRuntime(_config(tmp_path), adapter=first_adapter, pool=first_pool)

    first = await first_runtime.run_once()
    assert first.status == "EPISODE_COMPLETED"
    first_client_ids = list(first_adapter.create_order_calls)

    second_adapter = FakeTestnetAdapter(recovery="query")
    second_pool = TradingPool(max_instruments=1)
    second_pool.add("BTCUSDT").min_observation_hours = 1.0
    second_runtime = VerificationRuntime(_config(tmp_path), adapter=second_adapter, pool=second_pool)

    second = await second_runtime.run_once()

    assert second.status == "EPISODE_COMPLETED"
    assert second_adapter.create_order_calls == []
    assert second.trace_ids == first.trace_ids
    assert first_client_ids[0] == first_client_ids[1]
