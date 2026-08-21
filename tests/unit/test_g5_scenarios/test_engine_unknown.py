"""引擎组场景单元测试:unknown_state_verdict 判定纯函数、dry_run 不碰 PG、
注册接线与异常自捕获(ack_loss / timeout_unknown_recovery)。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from beidou_certification.g5_scenarios.base import NotionalLedger, ScenarioContext, ScenarioStatus
from beidou_certification.g5_scenarios.engine.ack_loss import (
    AckLossScenario,
    unknown_state_verdict,
)
from beidou_certification.g5_scenarios.engine.timeout_unknown_recovery import TimeoutUnknownRecoveryScenario
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

# ---- 判定纯函数(brief 单测,verbatim) ----


def test_unknown_state_verdict() -> None:
    # 本地 UNKNOWN + 交易所已无此单 → 可闭合(对账恢复,无重复下单风险)
    assert unknown_state_verdict("UNKNOWN", "GONE") == (True, "recoverable_no_duplicate")
    # 本地 UNKNOWN + 交易所仍有活跃单 → 不可闭合,必须保留锚点
    assert unknown_state_verdict("UNKNOWN", "ACTIVE") == (False, "anchor_must_hold")
    # 本地 FILLED + 交易所 GONE → 正常
    assert unknown_state_verdict("FILLED", "GONE") == (True, "terminal_consistent")


# ---- 判定纯函数边界 ----


def test_unknown_state_verdict_unresolved_fallback() -> None:
    assert unknown_state_verdict("UNKNOWN", "PARTIAL") == (False, "unresolved")
    assert unknown_state_verdict("SENDING", "GONE") == (False, "unresolved")
    assert unknown_state_verdict("", "") == (False, "unresolved")


def _ctx(client: Any, tmp_path: Path, *, dry_run: bool = False) -> ScenarioContext:
    return ScenarioContext(
        client=client,
        ledger=NotionalLedger(1000.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=dry_run,
    )


# ---- dry_run 早退不碰 PG ----


def test_ack_loss_dry_run_does_not_touch_pg(tmp_path: Path, monkeypatch: Any) -> None:
    def _no_pg(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry_run 不得触碰 PG")

    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.ack_loss.psycopg.connect", _no_pg)
    ctx = _ctx(None, tmp_path, dry_run=True)
    result = asyncio.run(AckLossScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert ctx.ledger.total == 0.0  # notional 记账 0


def test_timeout_unknown_recovery_dry_run_does_not_touch_pg(tmp_path: Path, monkeypatch: Any) -> None:
    def _no_pg(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry_run 不得触碰 PG")

    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.timeout_unknown_recovery.psycopg.connect", _no_pg)
    ctx = _ctx(None, tmp_path, dry_run=True)
    result = asyncio.run(TimeoutUnknownRecoveryScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert ctx.ledger.total == 0.0


# ---- 注册接线(Ruling-5:每个任务立即接线注册) ----


def test_engine_scenarios_registered() -> None:
    assert SCENARIO_REGISTRY["ack_loss"] is AckLossScenario
    assert SCENARIO_REGISTRY["timeout_unknown_recovery"] is TimeoutUnknownRecoveryScenario


# ---- run() 自捕获异常与客户端缺失 ----


def test_ack_loss_client_unavailable_not_verifiable(tmp_path: Path, monkeypatch: Any) -> None:
    def _no_pg(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("client 缺失时不得触碰 PG")

    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.ack_loss.psycopg.connect", _no_pg)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(AckLossScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "CLIENT_UNAVAILABLE"


def test_ack_loss_pg_failure_self_caught_fail(tmp_path: Path, monkeypatch: Any) -> None:
    def _pg_down(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("shared PG unreachable")

    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.ack_loss.psycopg.connect", _pg_down)
    fake_client = type("FakeClient", (), {})()
    ctx = _ctx(fake_client, tmp_path)
    result = asyncio.run(AckLossScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL  # 自捕获,不向上抛
    assert result.error_type == "ConnectionError"
    assert "unreachable" in result.error_message


def test_timeout_unknown_recovery_pg_failure_self_caught_fail(tmp_path: Path, monkeypatch: Any) -> None:
    def _pg_down(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("shared PG unreachable")

    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.timeout_unknown_recovery.psycopg.connect", _pg_down)
    fake_client = type("FakeClient", (), {})()
    ctx = _ctx(fake_client, tmp_path)
    result = asyncio.run(TimeoutUnknownRecoveryScenario().run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "ConnectionError"


def test_timeout_unknown_recovery_client_unavailable_not_verifiable(tmp_path: Path, monkeypatch: Any) -> None:
    def _no_pg(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("client 缺失时不得触碰 PG")

    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.timeout_unknown_recovery.psycopg.connect", _no_pg)
    ctx = _ctx(None, tmp_path)
    result = asyncio.run(TimeoutUnknownRecoveryScenario().run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "CLIENT_UNAVAILABLE"
