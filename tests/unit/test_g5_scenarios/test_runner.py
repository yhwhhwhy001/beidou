"""G5 runner 单元测试:注册表执行、汇总、证书生成与名义超限 fail-fast。"""

from __future__ import annotations

from pathlib import Path

from beidou_certification.g5_scenarios import runner as runner_module
from beidou_certification.g5_scenarios.base import (
    NotionalLedger,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.runner import (
    RESTART_GROUP,
    G5Runner,
    aggregate_evidence_hash,
    certificate_status,
    summarize,
)


def _r(sid, status):
    return ScenarioResult(scenario_id=sid, status=status, evidence={"n": 1}, duration=0.1)


def test_summarize_counts():
    s = summarize(
        {
            "a": _r("a", ScenarioStatus.PASS),
            "b": _r("b", ScenarioStatus.FAIL),
            "c": _r("c", ScenarioStatus.NOT_VERIFIABLE),
            "d": _r("d", ScenarioStatus.PASS),
        }
    )
    assert s == {"total": 4, "pass": 2, "warn": 0, "fail": 1, "not_verifiable": 1}


def test_summarize_counts_warn():
    s = summarize({"w": _r("w", ScenarioStatus.WARN)})
    assert s == {"total": 1, "pass": 0, "warn": 1, "fail": 0, "not_verifiable": 0}


def test_certificate_status_rules():
    assert certificate_status({"fail": 0, "not_verifiable": 0}, []) == "PASS"
    assert certificate_status({"fail": 1, "not_verifiable": 0}, []) == "FAIL"
    assert certificate_status({"fail": 0, "not_verifiable": 1}, []) == "NOT_VERIFIABLE"
    assert certificate_status({"fail": 0, "not_verifiable": 0}, ["process_restart"]) == "NOT_VERIFIABLE"


def test_aggregate_evidence_hash_stable_and_distinct():
    h1 = aggregate_evidence_hash({"a": _r("a", ScenarioStatus.PASS), "b": _r("b", ScenarioStatus.PASS)})
    h2 = aggregate_evidence_hash({"a": _r("a", ScenarioStatus.PASS), "b": _r("b", ScenarioStatus.PASS)})
    assert h1 == h2 and len(h1) == 64
    h3 = aggregate_evidence_hash({"a": _r("a", ScenarioStatus.FAIL), "b": _r("b", ScenarioStatus.PASS)})
    assert h3 != h1


def test_restart_group_names():
    assert {"process_restart", "database_restart", "user_stream_reconnect"} <= RESTART_GROUP


def _make_fake(sid: str) -> type[ScenarioBase]:
    """构造返回 PASS 的假场景;evidence 携带 ctx 字段以验证 ctx 透传。"""

    class Fake(ScenarioBase):
        scenario_id = sid

        async def run(self, ctx: ScenarioContext) -> ScenarioResult:
            return ScenarioResult(
                scenario_id=sid,
                status=ScenarioStatus.PASS,
                evidence={"symbol": ctx.symbol},
                duration=0.01,
            )

    return Fake


def _make_over(sid: str) -> type[ScenarioBase]:
    """构造记账超限的假场景:ledger.record 超限时抛 NotionalExceededError。"""

    class Over(ScenarioBase):
        scenario_id = sid

        async def run(self, ctx: ScenarioContext) -> ScenarioResult:
            ctx.ledger.record(sid, 9999.0)
            raise AssertionError("unreachable")

    return Over


def _make_runner(tmp_path: Path, *, limit: float = 1000.0) -> tuple[G5Runner, list[ScenarioContext]]:
    ledger = NotionalLedger(limit_usdt=limit)
    made: list[ScenarioContext] = []

    def make_context() -> ScenarioContext:
        ctx = ScenarioContext(
            client=None,
            ledger=ledger,
            evidence_dir=tmp_path / "evidence",
            symbol="BTCUSDT",
            dry_run=True,
        )
        made.append(ctx)
        return ctx

    runner = G5Runner(
        plan_path=tmp_path / "plan.md",
        commit="abc1234",
        testnet_url="https://testnet.binancefuture.com",
        evidence_dir=tmp_path / "evidence",
        ledger=ledger,
        symbol="BTCUSDT",
        make_context=make_context,
    )
    return runner, made


def test_run_selected_registry_order_and_per_scenario_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "SCENARIO_REGISTRY", {"a": _make_fake("a"), "b": _make_fake("b")})
    runner, made = _make_runner(tmp_path)
    results = runner.run_selected()
    assert list(results) == ["a", "b"]
    assert len(made) == 2  # 每个场景独立构造 ctx
    assert results["a"].evidence == {"symbol": "BTCUSDT"}
    assert results["b"].status == ScenarioStatus.PASS


def test_run_selected_only_and_skip_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_module,
        "SCENARIO_REGISTRY",
        {"process_restart": _make_fake("process_restart"), "a": _make_fake("a")},
    )
    runner, _ = _make_runner(tmp_path)
    results = runner.run_selected(skip_restart=True)
    assert "process_restart" not in results and "a" in results
    assert runner.restart_skipped == ["process_restart"]
    results = runner.run_selected(only="process_restart")
    assert list(results) == ["process_restart"]


def test_run_one_marks_notional_exceeded_fail(tmp_path):
    runner, _ = _make_runner(tmp_path, limit=10.0)
    ctx = ScenarioContext(
        client=None,
        ledger=runner.ledger,
        evidence_dir=tmp_path / "evidence",
        symbol="BTCUSDT",
        dry_run=True,
    )
    result = runner._run_one(_make_over("over"), ctx)
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "NOTIONAL_EXCEEDED"
    assert "exceeds limit" in result.error_message
    assert runner.ledger.total > runner.ledger.limit_usdt  # 超限后不回滚(Task 1 语义)


def test_run_selected_stops_after_notional_exceeded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_module,
        "SCENARIO_REGISTRY",
        {"a": _make_fake("a"), "over": _make_over("over"), "c": _make_fake("c")},
    )
    runner, _ = _make_runner(tmp_path, limit=10.0)
    results = runner.run_selected()
    assert list(results) == ["a", "over"]  # 资金保护优先,后续场景不再执行
    assert results["over"].status == ScenarioStatus.FAIL
    assert results["over"].error_type == "NOTIONAL_EXCEEDED"


def test_build_certificate_shape_and_account_access_override(tmp_path):
    runner, _ = _make_runner(tmp_path)
    results = {"a": _r("a", ScenarioStatus.PASS), "b": _r("b", ScenarioStatus.WARN)}
    cert = runner.build_certificate(results, started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:01:00Z")
    assert cert["status"] == "PASS"
    assert cert["gate"] == "G5" and cert["mainnet_prohibited"] is True
    assert cert["commit"] == "abc1234"
    assert cert["max_notional_usdt"] == 1000.0
    assert cert["account_access"] == {"can_trade": False, "can_withdraw": None, "has_balance": False}
    assert cert["summary"] == {"total": 2, "pass": 1, "warn": 1, "fail": 0, "not_verifiable": 0, "p0": 0}
    assert cert["scenarios"]["a"]["status"] == "PASS"
    assert cert["evidence_hash"] == aggregate_evidence_hash(results)
    overridden = runner.build_certificate(
        results,
        started_at="t",
        ended_at="t",
        account_access={"can_trade": False, "can_withdraw": True, "has_balance": False},
    )
    assert overridden["account_access"] == {"can_trade": False, "can_withdraw": True, "has_balance": False}


def test_build_certificate_account_access_default_is_conservative(tmp_path):
    """账户事实未测量时,占位默认必须是保守声明,不得肯定性声称可交易/有余额。"""
    runner, _ = _make_runner(tmp_path)
    cert = runner.build_certificate({"a": _r("a", ScenarioStatus.PASS)}, started_at="t", ended_at="t")
    assert cert["account_access"] == {"can_trade": False, "can_withdraw": None, "has_balance": False}


def test_build_certificate_status_reflects_fail_and_restart_skip(tmp_path):
    runner, _ = _make_runner(tmp_path)
    cert = runner.build_certificate({"a": _r("a", ScenarioStatus.FAIL)}, started_at="t", ended_at="t")
    assert cert["status"] == "FAIL"
    runner.restart_skipped = ["user_stream_reconnect"]
    cert = runner.build_certificate({"a": _r("a", ScenarioStatus.PASS)}, started_at="t", ended_at="t")
    assert cert["status"] == "NOT_VERIFIABLE"


def _make_boom(sid: str, exc: Exception) -> type[ScenarioBase]:
    """构造 run() 抛通用异常(非 NotionalExceededError)的假场景。"""

    class Boom(ScenarioBase):
        scenario_id = sid

        async def run(self, ctx: ScenarioContext) -> ScenarioResult:
            raise exc

    return Boom


def test_run_selected_resets_restart_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "SCENARIO_REGISTRY", {"process_restart": _make_fake("process_restart")})
    runner, _ = _make_runner(tmp_path)
    runner.run_selected(skip_restart=True)
    assert runner.restart_skipped == ["process_restart"]
    # 再次运行且注册表无重启组场景时,残留跳过记录必须清空(否则污染证书状态)
    monkeypatch.setattr(runner_module, "SCENARIO_REGISTRY", {"a": _make_fake("a")})
    runner.run_selected(skip_restart=True)
    assert runner.restart_skipped == []


def test_run_one_constructor_failure_uses_class_scenario_id(tmp_path):
    class Exploding(ScenarioBase):
        scenario_id = "exploding"

        def __init__(self) -> None:
            raise RuntimeError("ctor boom")

        async def run(self, ctx: ScenarioContext) -> ScenarioResult:
            raise AssertionError("unreachable")

    runner, _ = _make_runner(tmp_path)
    ctx = ScenarioContext(
        client=None,
        ledger=runner.ledger,
        evidence_dir=tmp_path / "evidence",
        symbol="BTCUSDT",
        dry_run=True,
    )
    result = runner._run_one(Exploding, ctx)
    assert result.status == ScenarioStatus.FAIL
    # 构造异常时实例未产生,scenario_id 必须取类属性(否则 UnboundLocalError)
    assert result.scenario_id == "exploding"
    assert result.error_type == "RuntimeError"
    assert "ctor boom" in result.error_message


def test_run_selected_continues_after_generic_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_module,
        "SCENARIO_REGISTRY",
        {"boom": _make_boom("boom", ValueError("boom")), "after": _make_fake("after")},
    )
    runner, _ = _make_runner(tmp_path)
    results = runner.run_selected()
    assert list(results) == ["boom", "after"]  # 通用异常记 FAIL 后继续后续场景
    assert results["boom"].status == ScenarioStatus.FAIL
    assert results["boom"].error_type == "ValueError"
    assert "boom" in results["boom"].error_message
    assert results["after"].status == ScenarioStatus.PASS
