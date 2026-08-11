"""
PKG26 (BDS-P1-017/052/053/055/056): 安全与配置完整性测试。

覆盖：
- P1-017: IP 白名单 CIDR 匹配
- P1-052: Rotation 持久化（JSONL 恢复）
- P1-053: 签名域隔离强制
- P1-055: Policy 不可变版本检测
- P1-056: Config hash 嵌套脱敏 + policy_version 绑定
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from beidou_security.credential_validator import (
    Credential,
    CredentialRegistry,
    CredentialStatus,
    CredentialType,
    PolicyRegistry,
    PolicyVersion,
    RotationRecord,
    SigningDomain,
    _deep_strip_secrets,
    compute_full_config_hash,
    sign_with_signer,
    verify_ip_whitelist,
)


# ---------------------------------------------------------------------------
# BDS-P1-017: IP Whitelist CIDR
# ---------------------------------------------------------------------------


class TestIPWhitelistCIDR:
    """BDS-P1-017: IP 白名单 CIDR 匹配。"""

    def test_exact_ip_match(self) -> None:
        """精确 IP 匹配。"""
        assert verify_ip_whitelist("192.168.1.100", ("192.168.1.100",))
        assert not verify_ip_whitelist("192.168.1.101", ("192.168.1.100",))

    def test_cidr_subnet_match(self) -> None:
        """CIDR 子网匹配。"""
        assert verify_ip_whitelist("10.0.0.5", ("10.0.0.0/8",))
        assert verify_ip_whitelist("172.16.5.5", ("172.16.0.0/12",))
        assert verify_ip_whitelist("192.168.1.50", ("192.168.1.0/24",))
        assert not verify_ip_whitelist("10.0.0.5", ("192.168.0.0/16",))

    def test_empty_whitelist_allows_all(self) -> None:
        """空白名单允许所有 IP。"""
        assert verify_ip_whitelist("1.2.3.4", ())
        assert verify_ip_whitelist("10.0.0.1", ())

    def test_multiple_entries_any_match(self) -> None:
        """多个条目任意匹配即可。"""
        assert verify_ip_whitelist("10.0.0.1", ("192.168.0.0/16", "10.0.0.0/8"))
        assert not verify_ip_whitelist("172.16.0.1", ("10.0.0.0/8", "192.168.0.0/16"))

    def test_credential_is_allowed_ip_uses_cidr(self) -> None:
        """Credential.is_allowed_ip 使用 CIDR 匹配。"""
        cred = Credential(
            credential_id="test-cidr",
            credential_type=CredentialType.API_KEY,
            key_hash="abc",
            allowed_ips=("10.0.0.0/8", "192.168.1.0/24"),
        )
        assert cred.is_allowed_ip("10.1.2.3")
        assert cred.is_allowed_ip("192.168.1.50")
        assert not cred.is_allowed_ip("172.16.0.1")

    def test_invalid_ip_rejected(self) -> None:
        """无效 IP 格式被拒绝。"""
        assert not verify_ip_whitelist("not_an_ip", ("10.0.0.0/8",))


# ---------------------------------------------------------------------------
# BDS-P1-052: Rotation Persistent
# ---------------------------------------------------------------------------


class TestRotationPersistence:
    """BDS-P1-052: 轮换持久化。"""

    def test_rotation_persists_to_jsonl(self) -> None:
        """轮换事件持久化到 JSONL 文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            registry = CredentialRegistry(evidence_dir=tmp)
            registry.register(Credential(
                credential_id="key-1", credential_type=CredentialType.SIGNING_KEY,
                key_hash="hash1", status=CredentialStatus.ACTIVE,
            ))

            registry.rotate("key-1", "hash2")

            # 检查 JSONL 文件存在且包含事件
            log_path = os.path.join(tmp, "credential_rotations.jsonl")
            assert os.path.exists(log_path)

            with open(log_path) as f:
                content = f.read()
            assert "key-1" in content
            assert "rotate" in content

    def test_restore_recovers_rotations(self) -> None:
        """从 JSONL 恢复轮换状态。"""
        with tempfile.TemporaryDirectory() as tmp:
            # 创建并写入事件
            registry1 = CredentialRegistry(evidence_dir=tmp)
            registry1.register(Credential(
                credential_id="k1", credential_type=CredentialType.SIGNING_KEY,
                key_hash="h1", status=CredentialStatus.ACTIVE,
            ))
            registry1.rotate("k1", "h2")
            registry1.revoke("k1")

            # 新实例恢复
            registry2 = CredentialRegistry(evidence_dir=tmp)
            registry2.register(Credential(
                credential_id="k1", credential_type=CredentialType.SIGNING_KEY,
                key_hash="h1", status=CredentialStatus.ROTATING,
            ))
            count = registry2.restore_from_log()

            assert count >= 1
            assert "k1" in registry2._revoked

    def test_rotate_with_evidence(self) -> None:
        """带证据哈希的轮换。"""
        registry = CredentialRegistry()
        registry.register(Credential(
            credential_id="cert-key",
            credential_type=CredentialType.CERT_KEY,
            key_hash="h1",
            status=CredentialStatus.ACTIVE,
            key_id="k1",
        ))

        new_cred, record = registry.rotate_with_evidence(
            "cert-key", "h2",
            evidence={"reason": "scheduled", "operator": "auto"},
        )

        assert isinstance(record, RotationRecord)
        assert record.old_credential_id == "cert-key"
        assert record.new_credential_id == "cert-key-gen2"
        assert record.evidence_hash != ""


# ---------------------------------------------------------------------------
# BDS-P1-053: Signing Domain Split
# ---------------------------------------------------------------------------


class TestSigningDomainSplit:
    """BDS-P1-053: 签名域隔离。"""

    def test_credential_has_domain(self) -> None:
        """每种凭据类型映射到独立域。"""
        policy_key = Credential(
            credential_id="pk", credential_type=CredentialType.POLICY_KEY, key_hash="h",
        )
        risk_key = Credential(
            credential_id="rk", credential_type=CredentialType.RISK_KEY, key_hash="h",
        )
        cert_key = Credential(
            credential_id="ck", credential_type=CredentialType.CERT_KEY, key_hash="h",
        )

        assert policy_key.signing_domain == SigningDomain.POLICY
        assert risk_key.signing_domain == SigningDomain.RISK
        assert cert_key.signing_domain == SigningDomain.CERTIFICATION

    def test_different_domains_are_distinct(self) -> None:
        """不同签名域互不相通。"""
        assert SigningDomain.POLICY != SigningDomain.RISK
        assert SigningDomain.CERTIFICATION != SigningDomain.GENERAL

    def test_domain_key_registration(self) -> None:
        """域密钥注册和查询。"""
        registry = CredentialRegistry()
        registry.set_domain_key(SigningDomain.POLICY, "policy-signer-key-1")
        registry.set_domain_key(SigningDomain.RISK, "risk-signer-key-1")

        assert registry.get_domain_key(SigningDomain.POLICY) == "policy-signer-key-1"
        assert registry.get_domain_key(SigningDomain.RISK) == "risk-signer-key-1"
        assert registry.get_domain_key(SigningDomain.CERTIFICATION) == ""

    def test_validate_requires_domain_match(self) -> None:
        """验证时检查签名域匹配。"""
        registry = CredentialRegistry()
        registry.register(Credential(
            credential_id="policy-1",
            credential_type=CredentialType.POLICY_KEY,
            key_hash="h",
            status=CredentialStatus.ACTIVE,
        ))

        # 正确的类型 + 域
        assert registry.validate("policy-1", CredentialType.POLICY_KEY)
        # 不正确的域
        assert not registry.validate(
            "policy-1", CredentialType.POLICY_KEY,
            required_domain=SigningDomain.RISK,
        )


# ---------------------------------------------------------------------------
# BDS-P1-055: Immutable Policy Version
# ---------------------------------------------------------------------------


class TestImmutablePolicyVersion:
    """BDS-P1-055: Policy 不可变版本。"""

    def test_same_id_same_hash_accepted(self) -> None:
        """相同 ID + 相同 hash → 接受。"""
        registry = PolicyRegistry()
        pv = PolicyVersion(policy_id="p1", version="v1", content_hash="abc123")
        ok, reason = registry.register(pv)
        assert ok

    def test_same_id_different_hash_rejected(self) -> None:
        """相同 ID + 不同 hash → 拒绝（不可变）。"""
        registry = PolicyRegistry()
        registry.register(PolicyVersion(policy_id="p1", version="v1", content_hash="hash-a"))

        ok, reason = registry.register(PolicyVersion(policy_id="p1", version="v2", content_hash="hash-b"))
        assert not ok
        assert "immutable" in reason

    def test_different_ids_both_accepted(self) -> None:
        """不同 ID → 都可以注册。"""
        registry = PolicyRegistry()
        assert registry.register(PolicyVersion("p1", "v1", "h1"))[0]
        assert registry.register(PolicyVersion("p2", "v1", "h2"))[0]

    def test_validate_or_reject_catches_mismatch(self) -> None:
        """validate_or_reject 检测 hash 不匹配。"""
        registry = PolicyRegistry()
        registry.register(PolicyVersion(policy_id="p1", version="v1", content_hash="correct-hash"))

        ok, reason = registry.validate_or_reject("p1", "correct-hash")
        assert ok

        ok, reason = registry.validate_or_reject("p1", "wrong-hash")
        assert not ok
        assert "mismatch" in reason

    def test_unknown_policy_rejected(self) -> None:
        """未知策略被拒绝。"""
        registry = PolicyRegistry()
        ok, reason = registry.validate_or_reject("unknown", "hash")
        assert not ok
        assert "unknown_policy" in reason


# ---------------------------------------------------------------------------
# BDS-P1-056: Config Hash Completeness
# ---------------------------------------------------------------------------


class TestConfigHashCompleteness:
    """BDS-P1-056: Config hash 完整性。"""

    def test_deep_strip_removes_nested_secrets(self) -> None:
        """深度脱敏移除嵌套 secrets（完全移除敏感 key，不保留占位符）。"""
        config = {
            "database": {"host": "localhost", "password": "secret123"},
            "api": {"key": "api-key-value"},
            "public_field": "visible",
            "nested": {"inner": {"secret": "nested-secret"}},
        }
        stripped = _deep_strip_secrets(config)
        assert isinstance(stripped, dict)
        # 敏感 key 被完全移除
        assert "password" not in stripped["database"]
        assert stripped["database"]["host"] == "localhost"
        assert "key" not in stripped["api"]
        assert stripped["public_field"] == "visible"
        assert "secret" not in stripped["nested"]["inner"]

    def test_compute_full_config_hash_binds_policy_version(self) -> None:
        """config hash 绑定 policy_version。"""
        config = {"env": "production", "max_notional": 10000}

        hash1 = compute_full_config_hash(config, secrets_version="v1", policy_version="pol-v1")
        hash2 = compute_full_config_hash(config, secrets_version="v1", policy_version="pol-v2")
        hash3 = compute_full_config_hash(config, secrets_version="v1", policy_version="")

        assert hash1 != hash2, "不同 policy_version 应产生不同 hash"
        assert hash1 != hash3, "有无 policy_version 应产生不同 hash"

    def test_top_level_secrets_stripped(self) -> None:
        """顶层 secrets 被完全移除（不保留占位符）。"""
        config_with = {
            "api_key": "sk-12345",
            "api_secret": "super-secret",
            "signing_key": "hex-key",
            "public": "data",
        }
        config_without = {"public": "data"}
        # 不同 secret 值但 hash 应相同（secrets 被移除）
        h1 = compute_full_config_hash(config_with)
        h2 = compute_full_config_hash(config_without)
        assert h1 == h2, "secret key 被完全移除后 hash 应一致"

    def test_deterministic_hash(self) -> None:
        """相同配置产生相同 hash。"""
        config = {"a": 1, "b": 2}
        h1 = compute_full_config_hash(config, "v1")
        h2 = compute_full_config_hash(config, "v1")
        assert h1 == h2

        # 不同顺序也产生相同 hash (sort_keys)
        config2 = {"b": 2, "a": 1}
        h3 = compute_full_config_hash(config2, "v1")
        assert h1 == h3


# ---------------------------------------------------------------------------
# BDS-P1-054: Signer in Payload
# ---------------------------------------------------------------------------


class TestSignerInPayload:
    """BDS-P1-054: signer 纳入签名 payload。"""

    def test_sign_with_signer_includes_identity(self) -> None:
        """签名包含 signer/key_id/algorithm。"""
        sig = sign_with_signer(
            "test-payload", b"my-key", signer="test-signer",
            key_id="key-001", algorithm="HMAC-SHA256",
        )
        assert sig != ""
        assert len(sig) == 64  # SHA-256 hex

    def test_different_signers_produce_different_signatures(self) -> None:
        """不同 signer 产生不同签名。"""
        sig1 = sign_with_signer("data", b"key", signer="alice", key_id="k1")
        sig2 = sign_with_signer("data", b"key", signer="bob", key_id="k1")
        assert sig1 != sig2

    def test_signing_domain_included(self) -> None:
        """签名域包含在签名中。"""
        sig_with = sign_with_signer("data", b"key", signer="s", key_id="k", domain="policy")
        sig_without = sign_with_signer("data", b"key", signer="s", key_id="k")
        assert sig_with != sig_without
