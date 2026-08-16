"""run_g5 CLI 薄入口测试:build_context 与 --list 参数。

--list 路径在 plan 校验/网络访问之前短路返回,因此用 monkeypatch
注入假场景注册表后 in-process 调用 main() 即可断言输出,不需要真实环境。
"""

from __future__ import annotations

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
from scripts.testnet.run_g5 import build_context, main


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


def test_list_flag_prints_registry(capsys, monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "SCENARIO_REGISTRY", {"fake": _FakeScenario})
    monkeypatch.setattr(sys, "argv", ["run_g5.py", "--list"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "fake" in out
