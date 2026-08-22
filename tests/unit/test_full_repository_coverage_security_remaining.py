"""Behavior-backed edge coverage for credential governance."""

from __future__ import annotations

from pathlib import Path

import pytest

from beidou_security import credential_validator as module
from beidou_security.credential_validator import (
    Credential,
    CredentialRegistry,
    CredentialStatus,
    CredentialType,
    PolicyRegistry,
    PolicyVersion,
    SigningDomain,
    _deep_strip_secrets,
    verify_ip_whitelist,
)


def _active(credential_id: str = "c1") -> Credential:
    return Credential(
        credential_id=credential_id,
        credential_type=CredentialType.API_KEY,
        key_hash="hash",
        status=CredentialStatus.ACTIVE,
        allowed_ips=("10.0.0.0/8",),
    )


def test_credential_ip_status_registration_and_domain_edges() -> None:
    assert module._is_ip_in_cidr("not-ip", "10.0.0.0/8") is False
    assert module._is_ip_in_cidr("10.0.0.1", "not-a-network") is False
    assert verify_ip_whitelist("10.0.0.1", ("not-a-network",)) is False
    assert Credential("pending", CredentialType.API_KEY, "h").is_active is False

    registry = CredentialRegistry()
    registry.register(_active())
    with pytest.raises(ValueError, match="generation must increase"):
        registry.register(_active())
    assert registry.validate("missing", CredentialType.API_KEY) is False
    assert registry.validate("c1", CredentialType.POLICY_KEY) is False
    assert registry.validate("c1", CredentialType.API_KEY, client_ip="192.168.1.1") is False
    assert registry.validate("c1", CredentialType.API_KEY, required_domain=SigningDomain.POLICY) is False
    registry.revoke("c1")
    assert registry.validate("c1", CredentialType.API_KEY) is False

    with pytest.raises(ValueError, match="not found"):
        registry.rotate("missing", "new")
    assert registry.get_generation_log() == []
    assert registry.get_rotation_records() == []
    assert registry.verify_domain_membership("missing", SigningDomain.EXCHANGE) is False
    assert registry.verify_domain_membership("c1", SigningDomain.EXCHANGE) is True


def test_credential_restore_and_persistence_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = tmp_path / "evidence"
    registry = CredentialRegistry(evidence_dir=str(evidence))
    registry.register(_active())
    registry.rotate("c1", "new")
    registry.revoke("c1")
    rotation = evidence / "credential_rotations.jsonl"
    revocation = evidence / "credential_revocations.jsonl"
    rotation.write_text(rotation.read_text() + "\nnot-json\n{}\n", encoding="utf-8")
    revocation.write_text(revocation.read_text() + '\n{"action":"revoke"}\n', encoding="utf-8")
    restored = CredentialRegistry(evidence_dir=str(evidence)).restore_from_log()
    assert restored >= 2

    missing_dir = tmp_path / "missing"
    assert CredentialRegistry(evidence_dir=str(missing_dir)).restore_from_log() == 0
    broken = CredentialRegistry(evidence_dir=str(tmp_path / "broken"))
    Path(broken._rotation_log_path).parent.mkdir(parents=True)
    Path(broken._rotation_log_path).touch()
    Path(broken._revocation_log_path).touch()
    monkeypatch.setattr(module, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read")), raising=False)
    assert broken.restore_from_log() == 0

    monkeypatch.setattr(module.os, "makedirs", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("mkdir")))
    broken._persist_rotation_event({"action": "rotate"})
    broken._persist_revocation_event({"action": "revoke"})


def test_policy_versions_and_secret_recursion_edges() -> None:
    policies = PolicyRegistry()
    assert policies.get("missing") is None
    assert policies.is_valid("missing", "hash") is False
    ok, reason = policies.register(PolicyVersion("p1", "v1", "hash"))
    assert ok and reason == "registered"
    assert policies.register(PolicyVersion("p1", "v1", "hash")) == (True, "already_registered")
    assert policies.is_valid("p1", "hash") is True
    assert policies.validate_or_reject("p1", "hash") == (True, "policy_valid")

    nested: object = {"visible": [{"secret": "one", "value": 1}], "key": "hidden"}
    stripped = _deep_strip_secrets(nested)
    assert stripped == {"visible": [{"value": 1}]}
    deep: object = "leaf"
    for _ in range(12):
        deep = {"level": deep}
    assert _deep_strip_secrets(deep) == deep


def test_restore_invalid_json_and_rotation_write_error_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = tmp_path / "logs"
    evidence.mkdir()
    (evidence / "credential_rotations.jsonl").write_text('{"action":\n', encoding="utf-8")
    (evidence / "credential_revocations.jsonl").write_text(
        '{"action":"revoke","credential_id":"x"}\n', encoding="utf-8"
    )
    assert CredentialRegistry(evidence_dir=str(evidence)).restore_from_log() == 1

    registry = CredentialRegistry(evidence_dir=str(tmp_path / "write"))
    monkeypatch.setattr(module.os, "makedirs", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write")))
    registry._persist_rotation_event({"action": "rotate"})
    registry._persist_revocation_event({"action": "revoke"})
