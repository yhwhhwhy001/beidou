"""Fail-closed approval signing, persistence and lifecycle contracts."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from beidou_safety.risk.engine import RiskApprovalSignerImpl, RiskApprovalStateMachine
from beidou_shared.types import RiskApprovalId, RiskDecision


def _signer() -> RiskApprovalSignerImpl:
    return RiskApprovalSignerImpl("unit-test-signing-key")


def _binding() -> dict[str, str]:
    return {
        "proposal_hash": "proposal-hash",
        "intent_hash": "intent-hash",
        "account_snapshot_hash": "account-hash",
        "risk_snapshot_hash": "risk-hash",
        "policy_version": "policy-1",
        "nonce": "nonce-1",
    }


@pytest.mark.parametrize("expires_at", [0, float("nan"), float("inf")])
def test_sign_rejects_invalid_expiry(expires_at: float) -> None:
    with pytest.raises(ValueError, match="expiry"):
        _signer().sign(RiskApprovalId("approval-1"), nonce="nonce-1", expires_at=expires_at)


def test_sign_and_issue_require_nonempty_identity_nonce_and_full_binding() -> None:
    signer = _signer()
    with pytest.raises(ValueError, match="identity"):
        signer.sign(RiskApprovalId(""), nonce="nonce-1")
    with pytest.raises(ValueError, match="nonce"):
        signer.sign(RiskApprovalId("approval-1"))
    with pytest.raises(ValueError, match="binding"):
        signer.issue_for_approved_risk(RiskApprovalId("approval-1"), risk_approved=True, nonce="nonce-1")


def test_nonce_is_not_consumed_when_durable_write_fails(monkeypatch) -> None:
    signer = _signer()
    signature = signer.sign(RiskApprovalId("approval-1"), **_binding())
    monkeypatch.setattr(signer, "_persist_nonce", lambda _nonce: False)
    assert not asyncio.run(signer.verify(RiskApprovalId("approval-1"), signature=signature, **_binding()))
    assert "nonce-1" not in signer._nonces


def test_verify_rejects_explicit_expiry_that_differs_from_signed_metadata() -> None:
    signer = _signer()
    expires_at = time.time() + 60
    signature = signer.sign(RiskApprovalId("approval-1"), expires_at=expires_at, **_binding())
    assert not asyncio.run(
        signer.verify(
            RiskApprovalId("approval-1"),
            signature=signature,
            expires_at=expires_at + 1,
            **_binding(),
        )
    )


def test_verify_rejects_expired_explicit_and_stored_expiry(monkeypatch) -> None:
    signer = _signer()
    expires_at = time.time() + 60
    signature = signer.sign(RiskApprovalId("approval-1"), expires_at=expires_at, **_binding())
    assert not asyncio.run(
        signer.verify(
            RiskApprovalId("approval-1"),
            signature=signature,
            expires_at=time.time() - 1,
            **_binding(),
        )
    )
    monkeypatch.setitem(signer._signed_expiry, signature, time.time() - 1)
    assert not asyncio.run(signer.verify(RiskApprovalId("approval-1"), signature=signature, **_binding()))


def test_restore_signature_rejects_nonfinite_revoked_or_expired_metadata() -> None:
    signer = _signer()
    signature = signer.sign(RiskApprovalId("approval-1"), **_binding())
    assert not signer.restore_signature("", time.time() + 60)
    assert not signer.restore_signature(signature, float("nan"))
    assert signer.revoke(signature)
    assert not signer.restore_signature(signature, time.time() + 60)


def test_revocation_reports_persistence_failure_but_still_blocks_in_process(monkeypatch) -> None:
    signer = _signer()
    signature = signer.sign(RiskApprovalId("approval-1"), **_binding())
    monkeypatch.setattr(signer, "_persist_revocation", lambda _signature: False)
    assert not signer.revoke(signature)
    assert not asyncio.run(signer.verify(RiskApprovalId("approval-1"), signature=signature, **_binding()))


def test_restore_log_ignores_malformed_or_empty_identities(tmp_path) -> None:
    nonce_log = tmp_path / "nonces.jsonl"
    revoke_log = tmp_path / "revocations.jsonl"
    nonce_log.write_text(
        "\n".join(
            [
                "",
                json.dumps({"action": "consume_nonce", "nonce": "nonce-ok"}),
                json.dumps({"action": "consume_nonce", "nonce": ""}),
                json.dumps({"action": "wrong", "nonce": "nonce-wrong"}),
                "not-json",
            ]
        )
    )
    revoke_log.write_text(json.dumps({"action": "revoke", "signature": "sig-ok"}) + "\n")
    signer = _signer()
    signer._nonce_log_path = str(nonce_log)
    signer._revocation_log_path = str(revoke_log)
    assert signer._restore_from_log() == 2
    assert signer._nonces == {"nonce-ok"}
    assert signer._revoked_sigs == {"sig-ok"}


def test_persistence_and_restore_io_failures_are_fail_closed(tmp_path) -> None:
    signer = _signer()
    signer._nonce_log_path = str(tmp_path)
    signer._revocation_log_path = str(tmp_path)
    assert not signer._persist_nonce("nonce")
    assert not signer._persist_revocation("signature")
    assert signer._restore_from_log() == 0


def test_signer_legacy_approval_query_and_empty_revocation_never_approve() -> None:
    signer = _signer()
    assert not signer.is_approved(RiskApprovalId("approval-1"))
    assert not signer.revoke("")


def test_approval_terminal_states_are_explicit_irreversible_and_not_usable() -> None:
    machine = RiskApprovalStateMachine(default_ttl_seconds=60)

    consumed = RiskApprovalId("consumed")
    assert machine.approve(consumed, nonce="n1", risk_snapshot_hash="r1", policy_version="p1") is RiskDecision.APPROVED
    assert machine.consume(consumed) is RiskDecision.APPROVED
    assert machine.get_metadata(consumed)["status"] == "CONSUMED"
    assert not machine.is_valid_for_use(consumed)
    assert machine.approve(consumed, nonce="n1", risk_snapshot_hash="r1", policy_version="p1") is RiskDecision.REJECTED

    revoked = RiskApprovalId("revoked")
    machine.approve(revoked, nonce="n2", risk_snapshot_hash="r2", policy_version="p2")
    assert machine.revoke(revoked) is RiskDecision.APPROVED
    assert machine.get_metadata(revoked)["status"] == "REVOKED"
    assert not machine.is_valid_for_use(revoked)
    assert machine.approve(revoked, nonce="n2", risk_snapshot_hash="r2", policy_version="p2") is RiskDecision.REJECTED

    expired = RiskApprovalId("expired")
    machine.approve(expired, nonce="n3", ttl=0.001, risk_snapshot_hash="r3", policy_version="p3")
    time.sleep(0.002)
    assert not machine.is_valid_for_use(expired)
    assert machine.get_metadata(expired)["status"] == "EXPIRED"
    assert machine.approve(expired, nonce="n3", risk_snapshot_hash="r3", policy_version="p3") is RiskDecision.REJECTED


def test_approval_rejects_invalid_ttl_and_conflicting_idempotent_context() -> None:
    with pytest.raises(ValueError, match="positive"):
        RiskApprovalStateMachine(0)
    machine = RiskApprovalStateMachine()
    aid = RiskApprovalId("approval-1")
    machine.approve(aid, nonce="n1", risk_snapshot_hash="r1", policy_version="p1")
    with pytest.raises(ValueError, match="conflicting"):
        machine.approve(aid, nonce="n2", risk_snapshot_hash="r1", policy_version="p1")


def test_approval_identity_ttl_idempotency_and_explicit_transitions() -> None:
    machine = RiskApprovalStateMachine()
    with pytest.raises(ValueError, match="identity"):
        machine.approve(RiskApprovalId(""), nonce="n", risk_snapshot_hash="r", policy_version="p")
    with pytest.raises(ValueError, match="positive"):
        machine.approve(RiskApprovalId("bad-ttl"), ttl=float("nan"))

    approved = RiskApprovalId("approved")
    assert (
        machine.approve(approved, nonce="n", ttl=10, risk_snapshot_hash="r", policy_version="p")
        is RiskDecision.APPROVED
    )
    assert (
        machine.approve(approved, nonce="n", ttl=10, risk_snapshot_hash="r", policy_version="p")
        is RiskDecision.APPROVED
    )
    assert machine.reject(approved) is RiskDecision.APPROVED

    unknown = RiskApprovalId("unknown")
    assert machine.consume(unknown) is RiskDecision.PENDING
    assert machine.revoke(unknown) is RiskDecision.PENDING
    machine.expire(unknown)

    revoked = RiskApprovalId("revoked-again")
    machine.approve(revoked, nonce="n", risk_snapshot_hash="r", policy_version="p")
    machine.revoke(revoked)
    assert machine.get(revoked) is RiskDecision.REJECTED

    expired = RiskApprovalId("explicit-expiry")
    machine.approve(expired, nonce="n", risk_snapshot_hash="r", policy_version="p")
    machine.expire(expired)
    assert machine.get_metadata(expired)["status"] == "EXPIRED"


def test_approval_use_requires_exact_bound_context_when_supplied() -> None:
    machine = RiskApprovalStateMachine()
    aid = RiskApprovalId("bound")
    machine.approve(aid, nonce="n1", risk_snapshot_hash="r1", policy_version="p1")

    assert machine.is_valid_for_use(aid, nonce="n1", risk_snapshot_hash="r1", policy_version="p1")
    assert not machine.is_valid_for_use(aid, nonce="n2", risk_snapshot_hash="r1", policy_version="p1")
    assert not machine.is_valid_for_use(aid, nonce="n1", risk_snapshot_hash="r2", policy_version="p1")
    assert not machine.is_valid_for_use(aid, nonce="n1", risk_snapshot_hash="r1", policy_version="p2")


def test_consume_and_metadata_lazily_mark_expired_approval() -> None:
    machine = RiskApprovalStateMachine()
    consumed = RiskApprovalId("expired-at-consume")
    machine.approve(consumed, ttl=0.001)
    metadata = RiskApprovalId("expired-at-metadata")
    machine.approve(metadata, ttl=0.001)
    time.sleep(0.002)

    assert machine.consume(consumed) is RiskDecision.PENDING
    assert machine.get_metadata(consumed)["status"] == "EXPIRED"
    assert machine.get_metadata(metadata)["status"] == "EXPIRED"
