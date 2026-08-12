"""Deterministic recovery must not become active without reconciled evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from beidou_safety.execution.recovery import (
    ReconciliationDiff,
    RecoveryEngine,
    RecoveryPhase,
    RecoveryState,
)


def test_recovery_requires_durable_checkpoint_identity() -> None:
    engine = RecoveryEngine()
    with pytest.raises(ValueError, match="checkpoint id"):
        engine.start_recovery(" ")


def test_recovery_advances_only_in_order_and_requires_final_evidence() -> None:
    engine = RecoveryEngine()
    state = engine.start_recovery("checkpoint-1")
    assert state.phase is RecoveryPhase.CHECKPOINT
    assert not state.is_active
    assert not state.has_blocking_diffs
    assert not engine.advance(RecoveryPhase.RECONCILE)

    for phase in (
        RecoveryPhase.REPLAY,
        RecoveryPhase.EXCHANGE_SNAPSHOT,
        RecoveryPhase.RECONCILE,
        RecoveryPhase.INVARIANT_CHECK,
        RecoveryPhase.VALIDATING,
    ):
        assert engine.advance(phase)

    assert not engine.advance(RecoveryPhase.ACTIVE)
    state.invariants_valid = True
    state.diffs.append(ReconciliationDiff("position", "1", "0", "P0"))
    assert state.has_blocking_diffs
    assert not engine.advance(RecoveryPhase.ACTIVE)
    state.diffs[0].resolved = True
    state.diffs[0].resolved_at = datetime.now(timezone.utc)
    assert engine.advance(RecoveryPhase.ACTIVE)
    assert state.is_active
    assert state.completed_at is not None
    assert engine.can_accept_new_risk()


def test_recovery_timeout_excludes_active_state() -> None:
    engine = RecoveryEngine()
    state = engine.start_recovery("checkpoint-1")
    state.started_at = datetime.now(timezone.utc) - timedelta(seconds=engine.RECOVERY_TIMEOUT_SECONDS + 1)
    assert engine.is_timed_out()
    state.phase = RecoveryPhase.ACTIVE
    assert not engine.is_timed_out()


def test_failed_recovery_records_blocking_difference_and_completion() -> None:
    engine = RecoveryEngine()
    engine.start_recovery("checkpoint-1")
    engine.fail("exchange snapshot unavailable")
    state = engine._state
    assert state.phase is RecoveryPhase.FAILED
    assert state.completed_at is not None
    assert state.has_blocking_diffs
    assert state.diffs[0].field == "recovery"
    assert state.diffs[0].exchange_value == "exchange snapshot unavailable"
    assert not engine.can_accept_new_risk()


def test_default_recovery_state_is_not_authority() -> None:
    state = RecoveryState()
    assert not state.is_active
    assert not state.has_blocking_diffs
