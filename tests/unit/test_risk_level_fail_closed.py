"""Fail-closed recovery contracts for the monotonic risk authority."""

from __future__ import annotations

import pytest

from beidou_safety.risk.risk_level import BreakerScope, RiskLevel, RiskLevelManager, RiskLevelState


def _durable_state(level: RiskLevel = RiskLevel.EXIT_ONLY) -> dict[str, object]:
    state = RiskLevelState(
        level=level,
        reason="durable-risk-state",
        timestamp=1_700_000_000.0,
        generation=7,
        breaker_scope=BreakerScope.SESSION,
        evidence_hash="a" * 64,
    )
    return {
        "level": state.level.name,
        "reason": state.reason,
        "timestamp": state.timestamp,
        "generation": state.generation,
        "breaker_scope": state.breaker_scope.name,
        "evidence_hash": state.evidence_hash,
        "state_hash": state.compute_evidence_hash(),
    }


def test_intraday_reset_clears_trigger_but_never_downgrades_authority() -> None:
    manager = RiskLevelManager()
    manager.escalate(RiskLevel.NO_NEW_RISK, "intraday-volatility", BreakerScope.INTRADAY)
    manager.reset_intraday()
    assert manager.current_level is RiskLevel.NO_NEW_RISK
    assert manager._intraday_breakers == {}
    assert manager.recover(RiskLevel.NORMAL, "signed-recovery")


def test_persistent_breakers_never_expire_and_reset_requires_evidence() -> None:
    manager = RiskLevelManager()
    manager.escalate(RiskLevel.LOCKED, "permanent-breach", BreakerScope.PERMANENT)
    manager._daily_breakers["permanent-breach"] = (BreakerScope.PERMANENT, 0.0)

    with pytest.raises(ValueError, match="跨日断路器"):
        manager.recover(RiskLevel.NORMAL, "signed-recovery")
    with pytest.raises(ValueError, match="签名证据"):
        manager.reset_daily_breaker("permanent-breach", "")
    with pytest.raises(ValueError, match="不存在"):
        manager.reset_daily_breaker("missing", "signed-recovery")

    manager.reset_daily_breaker("permanent-breach", "signed-recovery")
    assert manager.recover(RiskLevel.NORMAL, "signed-recovery").level is RiskLevel.NORMAL


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"level": "NORMAL"},
        {**_durable_state(), "level": "GARBAGE"},
        {**_durable_state(), "reason": ""},
        {**_durable_state(), "breaker_scope": "GARBAGE"},
        {**_durable_state(), "evidence_hash": "bad"},
        {**_durable_state(), "state_hash": None},
        {**_durable_state(), "state_hash": "tampered"},
        {**_durable_state(), "timestamp": float("nan")},
        {**_durable_state(), "generation": 0},
    ],
)
def test_incomplete_corrupt_or_tampered_recovery_state_locks(payload) -> None:
    manager = RiskLevelManager.from_corrupted_state(payload)
    assert manager.current_level is RiskLevel.LOCKED
    history = manager.get_history()
    assert len(history) == 1
    assert history[0].reason.startswith("CORRUPTED_STATE")
    assert history[0].is_blocking


def test_complete_hashed_recovery_state_restores_generation_and_history() -> None:
    manager = RiskLevelManager.from_corrupted_state(_durable_state())
    assert manager.current_level is RiskLevel.EXIT_ONLY
    history = manager.get_history()
    assert len(history) == 1
    assert history[0].compute_evidence_hash() == _durable_state()["state_hash"]
    assert manager.escalate(RiskLevel.LOCKED, "new-breach").generation == 8
    history.clear()
    assert len(manager.get_history()) == 2

    for scope in (BreakerScope.INTRADAY, BreakerScope.PERMANENT):
        payload = _durable_state()
        payload["breaker_scope"] = scope.name
        state = RiskLevelState(
            level=RiskLevel.EXIT_ONLY,
            reason="durable-risk-state",
            timestamp=1_700_000_000.0,
            generation=7,
            breaker_scope=scope,
            evidence_hash="a" * 64,
        )
        payload["state_hash"] = state.compute_evidence_hash()
        restored = RiskLevelManager.from_corrupted_state(payload)
        assert restored.current_level is RiskLevel.EXIT_ONLY
        if scope is BreakerScope.INTRADAY:
            assert restored._intraday_breakers == {"durable-risk-state": 1_700_000_000.0}
        else:
            assert "durable-risk-state" in restored._daily_breakers


def test_risk_transitions_require_nonempty_reason_and_evidence() -> None:
    manager = RiskLevelManager()
    with pytest.raises(ValueError, match="RiskLevel"):
        manager.escalate(1, "breach")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="BreakerScope"):
        manager.escalate(RiskLevel.NO_NEW_RISK, "breach", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="原因"):
        manager.escalate(RiskLevel.NO_NEW_RISK, " ")
    manager.escalate(RiskLevel.LOCKED, "breach", evidence="signed-evidence")
    with pytest.raises(ValueError, match="签名证据"):
        manager.recover(RiskLevel.NORMAL, " ")
    with pytest.raises(ValueError, match="目标等级低于"):
        RiskLevelManager().recover(RiskLevel.NORMAL, "signed-evidence")
    with pytest.raises(ValueError, match="RiskLevel"):
        manager.recover(0, "signed-evidence")  # type: ignore[arg-type]
