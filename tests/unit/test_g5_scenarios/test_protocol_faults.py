"""协议组场景二单元测试:判定纯函数、dry_run、注册与假客户端真实路径。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from beidou_certification.g5_scenarios.base import NotionalLedger, ScenarioContext, ScenarioStatus
from beidou_certification.g5_scenarios.protocol import credential_failure as cf_module
from beidou_certification.g5_scenarios.protocol import duplicate_request as dup_module
from beidou_certification.g5_scenarios.protocol import rate_limit as rl_module
from beidou_certification.g5_scenarios.protocol.credential_failure import (
    CredentialFailureScenario,
    classify_auth_error,
)
from beidou_certification.g5_scenarios.protocol.duplicate_request import (
    DuplicateRequestScenario,
    dedupe_verdict,
)
from beidou_certification.g5_scenarios.protocol.rate_limit import RateLimitScenario, classify_rate_limit
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.binance_usdm.endpoints import HIGH_WEIGHT_GET_CACHE_TTL, Endpoint
from beidou_exchange.binance_usdm.rest_client import RateLimitState
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result

# ---- 判定纯函数(brief 单测,verbatim) ----


def test_dedupe_verdict_same_order():
    assert dedupe_verdict(second_response={"orderId": 1}, known=[1]) == (True, "same_order_returned")
    assert dedupe_verdict(second_response={"orderId": 2}, known=[1]) == (False, "new_order_created")
    assert dedupe_verdict(second_response={"code": -4015, "msg": "..."}, known=[1]) == (True, "exchange_rejected")


def test_classify_rate_limit():
    assert classify_rate_limit({"code": -1003}) == "RATE_LIMIT"
    assert classify_rate_limit({"code": -2010}) == "BUSINESS"
    assert classify_rate_limit(None) == "UNKNOWN"


def test_classify_auth_error():
    assert classify_auth_error({"code": -2014}) == "AUTH"
    assert classify_auth_error({"code": -2015}) == "AUTH"
    assert classify_auth_error({"code": -1102}) == "OTHER"


# ---- 判定纯函数边界 ----


def test_dedupe_verdict_unrecognized_response():
    assert dedupe_verdict(None, [1]) == (False, "unrecognized_response")
    assert dedupe_verdict({"orderId": "2"}, [1]) == (False, "new_order_created")  # 字符串 orderId 兼容
    assert dedupe_verdict({"orderId": "1"}, ["1"]) == (True, "same_order_returned")


def test_classify_rate_limit_unknown_branches():
    assert classify_rate_limit({"code": -1009}) == "UNKNOWN"
    assert classify_rate_limit({"msg": "..."}) == "UNKNOWN"
    assert classify_rate_limit({}) == "UNKNOWN"
    assert classify_rate_limit("nope") == "UNKNOWN"
    assert classify_rate_limit({"code": "abc"}) == "UNKNOWN"


def test_classify_auth_error_other_branches():
    assert classify_auth_error(None) == "OTHER"
    assert classify_auth_error({"msg": "..."}) == "OTHER"
    assert classify_auth_error({"code": "-2014"}) == "AUTH"  # 字符串数字兼容


def _ctx(client: Any, tmp_path: Path, *, dry_run: bool = False) -> ScenarioContext:
    return ScenarioContext(
        client=client,
        ledger=NotionalLedger(1000.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=dry_run,
    )


# ---- duplicate_request 场景行为 ----


def test_duplicate_request_dry_run_skips_pg(tmp_path, monkeypatch):
    probe_calls: list[str] = []

    def fake_probe() -> tuple[list[dict[str, Any]], bool, str]:
        probe_calls.append("probe")
        return [], True, "exchange_rejected"

    monkeypatch.setattr(dup_module, "_probe_idempotency", fake_probe)
    ctx = _ctx(None, tmp_path, dry_run=True)
    result = asyncio.run(DuplicateRequestScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert probe_calls == []  # dry_run 不触碰 PG
    assert ctx.ledger.total == 0.0


def test_duplicate_request_unique_rejection_pass(tmp_path, monkeypatch):
    evidence = [
        {"action": "first_insert", "ok": True},
        {"action": "second_insert", "ok": False, "sqlstate": "23505"},
        {"action": "cleanup", "deleted": 1},
    ]
    monkeypatch.setattr(dup_module, "_probe_idempotency", lambda: (evidence, True, "exchange_rejected"))
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(DuplicateRequestScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.error_type == ""
    assert result.evidence["verdict"] == "exchange_rejected"
    assert result.evidence["steps"] == evidence
    assert ctx.ledger.total == 0.0


def test_duplicate_request_duplicate_accepted_fails(tmp_path, monkeypatch):
    evidence = [{"action": "first_insert", "ok": True}, {"action": "second_insert", "ok": True}]
    monkeypatch.setattr(dup_module, "_probe_idempotency", lambda: (evidence, False, "duplicate_accepted"))
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(DuplicateRequestScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "DUPLICATE_ACCEPTED"


def test_duplicate_request_probe_exception_fails_with_evidence(tmp_path, monkeypatch):
    def boom() -> tuple[list[dict[str, Any]], bool, str]:
        raise RuntimeError("pg down")

    monkeypatch.setattr(dup_module, "_probe_idempotency", boom)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(DuplicateRequestScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "pg down" in result.error_message


# ---- rate_limit 场景行为 ----


def test_rate_limit_dry_run(tmp_path):
    ctx = _ctx(None, tmp_path, dry_run=True)
    result = asyncio.run(RateLimitScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert ctx.ledger.total == 0.0


def test_rate_limit_real_client_state_simulation_pass(tmp_path):
    """真实 BinanceRESTClient 状态模拟(无网络):熔断注入 → 断言 → 缓存 GET → 恢复。"""
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(RateLimitScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.error_type == ""
    steps = {s["action"]: s for s in result.evidence["steps"]}
    assert steps["is_circuit_breaker_open"]["is_open"] is True
    assert steps["cached_get_during_circuit_open"]["is_ok"] is True
    assert steps["cached_get_during_circuit_open"]["from_cache"] is True
    assert steps["uncached_get_rejected"]["is_ok"] is False
    assert steps["uncached_get_rejected"]["category"] == ErrorCategory.RATE_LIMIT.value
    assert steps["classify_rate_limit_payload"]["class"] == "RATE_LIMIT"
    assert steps["reset_circuit_breaker"]["is_open_after"] is False
    assert ctx.ledger.total == 0.0


class FakeRateLimitClient:
    """熔断状态面假客户端:缓存/熔断语义与真实客户端一致,不产生网络。"""

    def __init__(self, *, is_open_after_inject: bool = True) -> None:
        self._rate_state = RateLimitState()
        self._get_cache: dict[Any, Any] = {}
        self._is_open = is_open_after_inject
        self.reset_called = False
        self.calls: list[str] = []

    def is_circuit_breaker_open(self) -> bool:
        self.calls.append("is_circuit_breaker_open")
        return self._is_open

    def reset_circuit_breaker(self) -> None:
        self.calls.append("reset_circuit_breaker")
        self.reset_called = True
        self._is_open = False

    async def get_open_orders(self, symbol: str | None = None) -> Result:
        self.calls.append(f"get_open_orders:{symbol or ''}")
        params = {"symbol": symbol} if symbol else {}
        key = (Endpoint.OPEN_ORDERS, tuple(sorted(params.items())))
        cached = self._get_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] <= HIGH_WEIGHT_GET_CACHE_TTL:
            return cached[1]
        return Result.failure(
            "Circuit breaker open",
            category=ErrorCategory.RATE_LIMIT,
            raw={"reason": "CIRCUIT_BREAKER_OPEN"},
        )


def test_rate_limit_fake_client_injection_pass(tmp_path, monkeypatch):
    fake = FakeRateLimitClient()
    monkeypatch.setattr(rl_module, "_build_client", lambda: fake)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(RateLimitScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert fake.reset_called
    assert "reset_circuit_breaker" in fake.calls
    assert ctx.ledger.total == 0.0


def test_rate_limit_circuit_not_open_fails(tmp_path, monkeypatch):
    fake = FakeRateLimitClient(is_open_after_inject=False)  # 注入后熔断断言失败 → 语义不符
    monkeypatch.setattr(rl_module, "_build_client", lambda: fake)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(RateLimitScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "CIRCUIT_SEMANTIC_MISMATCH"


def test_rate_limit_uncached_get_not_rejected_fails(tmp_path, monkeypatch):
    class PermissiveClient(FakeRateLimitClient):
        async def get_open_orders(self, symbol: str | None = None) -> Result:
            self.calls.append(f"get_open_orders:{symbol or ''}")
            return Result.ok([])  # 熔断窗口内请求未被短路 → 语义不符

    fake = PermissiveClient()
    monkeypatch.setattr(rl_module, "_build_client", lambda: fake)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(RateLimitScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "CIRCUIT_SEMANTIC_MISMATCH"


# ---- credential_failure 场景行为 ----


class FakeCredentialClient:
    """只读 get_account 假客户端:按脚本返回结果,不产生网络。"""

    def __init__(self, result: Result) -> None:
        self.result = result
        self.calls: list[str] = []

    async def get_account(self) -> Result:
        self.calls.append("get_account")
        return self.result


def _inject_auth_client(monkeypatch: pytest.MonkeyPatch, result: Result) -> FakeCredentialClient:
    fake = FakeCredentialClient(result)
    monkeypatch.setattr(cf_module, "_build_client", lambda: fake)
    return fake


def test_credential_failure_dry_run_skips_client(tmp_path, monkeypatch):
    calls: list[str] = []

    def factory() -> Any:
        calls.append("build_client")
        raise AssertionError("dry_run 不应构造客户端")

    monkeypatch.setattr(cf_module, "_build_client", factory)
    ctx = _ctx(None, tmp_path, dry_run=True)
    result = asyncio.run(CredentialFailureScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert calls == []
    assert ctx.ledger.total == 0.0


def test_credential_failure_auth_error_pass(tmp_path, monkeypatch):
    res = Result.failure(
        "API-key format invalid",
        category=ErrorCategory.AUTH_FAILURE,
        raw={"code": -2015, "msg": "API-key format invalid"},
    )
    fake = _inject_auth_client(monkeypatch, res)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(CredentialFailureScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.error_type == ""
    assert fake.calls == ["get_account"]
    assert ctx.ledger.total == 0.0
    classify_step = next(s for s in result.evidence["steps"] if s["action"] == "classify_auth_error")
    assert classify_step["class"] == "AUTH"
    assert classify_step["code"] == -2015


def test_credential_failure_venue_unreachable_not_verifiable(tmp_path, monkeypatch):
    res = Result.failure(
        "Connection refused",
        category=ErrorCategory.NETWORK,
        raw={"exception_type": "ConnectError"},
    )
    _inject_auth_client(monkeypatch, res)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(CredentialFailureScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.evidence["reason"] == "venue_unreachable"
    assert result.error_type == "VENUE_UNREACHABLE"
    assert ctx.ledger.total == 0.0


def test_credential_failure_other_error_fails(tmp_path, monkeypatch):
    res = Result.failure(
        "Invalid symbol",
        category=ErrorCategory.UNKNOWN,
        raw={"code": -1102, "msg": "Invalid symbol"},
    )
    _inject_auth_client(monkeypatch, res)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(CredentialFailureScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "UNEXPECTED_ERROR_CLASS"
    assert "OTHER" in result.error_message


def test_credential_failure_unexpected_success_fails(tmp_path, monkeypatch):
    res = Result.ok({"totalWalletBalance": "100.0"})
    _inject_auth_client(monkeypatch, res)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(CredentialFailureScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "INVALID_CREDENTIALS_ACCEPTED"


def test_credential_failure_exception_caught_fails(tmp_path, monkeypatch):
    class BoomClient:
        async def get_account(self) -> Result:
            raise RuntimeError("boom")

    monkeypatch.setattr(cf_module, "_build_client", lambda: BoomClient())
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(CredentialFailureScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "boom" in result.error_message


# ---- 注册 ----


def test_protocol_fault_scenarios_registered():
    assert SCENARIO_REGISTRY["duplicate_request"] is DuplicateRequestScenario
    assert SCENARIO_REGISTRY["rate_limit"] is RateLimitScenario
    assert SCENARIO_REGISTRY["credential_failure"] is CredentialFailureScenario
