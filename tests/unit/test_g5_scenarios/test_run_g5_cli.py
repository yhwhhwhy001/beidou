"""run_g5 CLI 薄入口测试:build_context 与 --list 参数。

--list 路径在 plan 校验/网络访问之前短路返回,因此用 monkeypatch
注入假场景注册表后 in-process 调用 main() 即可断言输出,不需要真实环境。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from beidou_certification.g5_scenarios import runner as runner_module
from beidou_certification.g5_scenarios.base import (
    EvidenceWriteError,
    NotionalLedger,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.runner import G5Runner
from scripts.testnet import run_g5 as run_g5_module
from scripts.testnet.run_g5 import (
    _configure_scenario_logging,
    _extract_account_access,
    build_context,
    main,
    write_scenario_evidence_all,
)


class _FakeScenario(ScenarioBase):
    """注册表注入用假场景,仅用于断言 --list 输出。"""

    scenario_id = "fake"

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        return ScenarioResult(scenario_id="fake", status=ScenarioStatus.PASS, evidence={}, duration=0.0)


def test_build_context_shape() -> None:
    ctx = build_context(
        client=None,
        ledger=NotionalLedger(20.0),
        evidence_dir=Path("/tmp/e"),
        symbol="BTCUSDT",
        dry_run=True,
    )
    assert isinstance(ctx, ScenarioContext)
    assert ctx.client is None
    assert ctx.ledger.limit_usdt == 20.0
    assert ctx.evidence_dir == Path("/tmp/e")
    assert ctx.symbol == "BTCUSDT"
    assert ctx.dry_run


def test_extract_account_access_measured() -> None:
    observations = {
        "account_access": {
            "status": "PASS",
            "can_trade": True,
            "can_withdraw": False,
            "permission_status": "OK",
            "has_balance": True,
        }
    }
    assert _extract_account_access(observations) == {"can_trade": True, "can_withdraw": False, "has_balance": True}


def test_extract_account_access_missing_keys_returns_none() -> None:
    # 三键任一缺失(S2 失败/部分字段)即视为未测量,返回 None → build_certificate 保守默认
    assert _extract_account_access({}) is None
    assert _extract_account_access({"account_access": {"status": "FAIL", "error": "boom"}}) is None
    assert _extract_account_access({"account_access": {"status": "PASS", "can_trade": True}}) is None


def test_list_flag_prints_registry(capsys, monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "SCENARIO_REGISTRY", {"fake": _FakeScenario})
    monkeypatch.setattr(sys, "argv", ["run_g5.py", "--list"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "fake" in out


def test_evidence_write_failure_fails_scenario_and_continues(tmp_path: Path, monkeypatch) -> None:
    """证据写失败 → 该场景判 FAIL(error_type 记证据写错误类型,error_message
    记路径),后续场景继续落盘,证书照常生成(spec §4:没有 durable evidence
    的结果不算结果)。"""
    evidence_dir = tmp_path / "evidence"
    results = {
        "alpha": ScenarioResult("alpha", ScenarioStatus.PASS, {"step": 1}, 0.1),
        "beta": ScenarioResult("beta", ScenarioStatus.PASS, {"step": 2}, 0.1),
        "gamma": ScenarioResult("gamma", ScenarioStatus.PASS, {"step": 3}, 0.1),
    }
    written: list[str] = []

    def _flaky_write(ctx: ScenarioContext, result: ScenarioResult) -> Path:
        written.append(result.scenario_id)
        target = evidence_dir / f"{result.scenario_id}.json"
        if result.scenario_id == "alpha":
            raise EvidenceWriteError(f"场景证据写入失败 {target}: disk full")
        if result.scenario_id == "gamma":
            raise OSError("simulated raw OSError")
        evidence_dir.mkdir(parents=True, exist_ok=True)  # 与真实 write_scenario_evidence 同语义
        target.write_text("{}", encoding="utf-8")
        return target

    monkeypatch.setattr(run_g5_module, "write_scenario_evidence", _flaky_write)
    ledger = NotionalLedger(1000.0)
    ctx = build_context(client=None, ledger=ledger, evidence_dir=evidence_dir, symbol="BTCUSDT", dry_run=True)
    out = write_scenario_evidence_all(results, lambda: ctx, evidence_dir)

    # alpha/gamma 改判 FAIL:error_type 记错误类型,error_message 记目标路径
    assert out["alpha"].status == ScenarioStatus.FAIL
    assert out["alpha"].error_type == "EVIDENCE_WRITE_EvidenceWriteError"
    assert str(evidence_dir / "alpha.json") in out["alpha"].error_message
    assert out["gamma"].status == ScenarioStatus.FAIL
    assert out["gamma"].error_type == "EVIDENCE_WRITE_OSError"
    # 后续场景照常执行与落盘(未被首个失败中断)
    assert out["beta"].status == ScenarioStatus.PASS
    assert (evidence_dir / "beta.json").exists()
    assert written == ["alpha", "beta", "gamma"]
    # 证书照常生成:FAIL 进入证书汇总而非中断主流程
    runner = G5Runner(
        plan_path=Path("/nonexistent/plan.yaml"),
        commit="test-commit",
        testnet_url="https://testnet.binancefuture.com",
        evidence_dir=evidence_dir,
        ledger=ledger,
        symbol="BTCUSDT",
        make_context=lambda: ctx,
    )
    cert = runner.build_certificate(
        out,
        started_at="2026-08-17T00:00:00+00:00",
        ended_at="2026-08-17T00:00:01+00:00",
    )
    assert cert["status"] == "FAIL"
    assert cert["scenarios"]["alpha"]["status"] == "FAIL"
    assert cert["scenarios"]["beta"]["status"] == "PASS"


def test_configure_scenario_logging_enables_info_emission() -> None:
    """_configure_scenario_logging 后 root 达 INFO:场景 logger.info 意图消息真实可达(设计§4)。"""
    root = logging.getLogger()
    old_level = root.level
    old_handlers = list(root.handlers)
    captured: list[str] = []
    probe = logging.Handler()
    probe.setLevel(logging.INFO)

    def _capture(record: logging.LogRecord) -> None:
        captured.append(record.getMessage())

    probe.emit = _capture
    try:
        root.setLevel(logging.WARNING)  # 先造出「INFO 被丢弃」的默认状态
        for handler in list(root.handlers):
            root.removeHandler(handler)
        _configure_scenario_logging()
        assert root.level == logging.INFO  # INFO 级不再被 lastResort 丢弃
        root.addHandler(probe)
        logging.getLogger("beidou_certification.g5_scenarios.protocol.create_query_cancel").info("intent-probe")
        assert "intent-probe" in captured  # INFO 消息真实到达 handler
    finally:
        root.setLevel(old_level)
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in old_handlers:
            root.addHandler(handler)
