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
    NotionalLedger,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from scripts.testnet.run_g5 import _configure_scenario_logging, _extract_account_access, build_context, main


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
