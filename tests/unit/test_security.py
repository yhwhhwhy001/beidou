"""
PKG-03: Security 测试。
覆盖服务身份、权限矩阵、密钥轮换、脱敏、Sec ret 扫描。
"""

from __future__ import annotations

import pytest

from beidou_security.identity import (
    Credential,
    CredentialType,
    KeyRotationStatus,
    KeyRotator,
    Permission,
    SecretSanitizer,
    ServiceIdentity,
)
from beidou_shared.types import ResultStatus


class TestPermissionMatrix:
    """权限矩阵测试。"""

    def test_market_readonly_cannot_trade(self) -> None:
        result = Permission.check("order:create", CredentialType.MARKET_READONLY)
        assert result == ResultStatus.ERROR

    def test_account_readonly_cannot_trade(self) -> None:
        result = Permission.check("order:create", CredentialType.ACCOUNT_READONLY)
        assert result == ResultStatus.ERROR

    def test_trading_key_can_create_order(self) -> None:
        result = Permission.check("order:create", CredentialType.TRADING)
        assert result == ResultStatus.SUCCESS

    def test_trading_key_can_read_account(self) -> None:
        result = Permission.check("account:read", CredentialType.TRADING)
        assert result == ResultStatus.SUCCESS

    def test_withdraw_always_denied(self) -> None:
        """提款权限无条件拒绝，任何 CredentialType 都不行。"""
        for ct in CredentialType:
            result = Permission.check("withdraw", ct)
            assert result == ResultStatus.ERROR, f"Withdraw should be denied for {ct}"

    def test_unknown_action_unsupported(self) -> None:
        result = Permission.check("unknown:action", CredentialType.TRADING)
        assert result == ResultStatus.UNSUPPORTED

    def test_can_create_order_helper(self) -> None:
        assert not Permission.can_create_order(CredentialType.MARKET_READONLY)
        assert not Permission.can_create_order(CredentialType.ACCOUNT_READONLY)
        assert Permission.can_create_order(CredentialType.TRADING)


class TestSecretSanitizer:
    """Secret 脱敏测试。"""

    def test_sanitize_api_key(self) -> None:
        data = {"api_key": "sk-1234567890abcdef", "name": "test"}
        result = SecretSanitizer.sanitize(data)
        assert result["api_key"] == "***REDACTED***"
        assert result["name"] == "test"

    def test_sanitize_nested(self) -> None:
        data = {"config": {"api_secret": "abc123", "endpoint": "https://api.example.com"}}
        result = SecretSanitizer.sanitize(data)
        assert result["config"]["api_secret"] == "***REDACTED***"
        assert result["config"]["endpoint"] == "https://api.example.com"

    def test_sanitize_list_of_dicts(self) -> None:
        data = {"credentials": [{"api_key": "k1"}, {"api_key": "k2"}]}
        result = SecretSanitizer.sanitize(data)
        for cred in result["credentials"]:
            assert cred["api_key"] == "***REDACTED***"

    def test_scan_for_secrets(self) -> None:
        text = "const api_key = 'abc123';"
        found = SecretSanitizer.scan_for_secrets(text)
        assert len(found) > 0
        assert "api_key" in found

    def test_scan_clean_text(self) -> None:
        text = "This is a normal log message about order execution."
        found = SecretSanitizer.scan_for_secrets(text)
        assert len(found) == 0


class TestKeyRotation:
    """密钥轮换测试。"""

    def make_cred(self, cred_id: str) -> Credential:
        return Credential(
            credential_id=cred_id,
            credential_type=CredentialType.TRADING,
            key_reference="vault:secret/test",
        )

    def test_normal_rotation_flow(self) -> None:
        rotator = KeyRotator()
        old = self.make_cred("cred_001")
        rotator.register(old)

        assert rotator.start_rotation("cred_001") == ResultStatus.SUCCESS
        assert old.status == KeyRotationStatus.ROTATING

        new = self.make_cred("cred_002")
        assert rotator.complete_rotation("cred_001", new) == ResultStatus.SUCCESS
        assert old.status == KeyRotationStatus.EXPIRED

        new_cred = rotator._credentials.get("cred_002")
        assert new_cred is not None
        assert new_cred.status == KeyRotationStatus.ACTIVE

    def test_cannot_rotate_already_rotating(self) -> None:
        rotator = KeyRotator()
        cred = self.make_cred("cred_001")
        rotator.register(cred)
        rotator.start_rotation("cred_001")
        result = rotator.start_rotation("cred_001")
        assert result == ResultStatus.ERROR

    def test_rotate_unknown_credential(self) -> None:
        rotator = KeyRotator()
        assert rotator.start_rotation("unknown") == ResultStatus.UNKNOWN

    def test_revoke_credential(self) -> None:
        rotator = KeyRotator()
        cred = self.make_cred("cred_001")
        rotator.register(cred)
        assert rotator.revoke("cred_001", "security incident") == ResultStatus.SUCCESS
        assert cred.status == KeyRotationStatus.REVOKED


class TestServiceIdentity:
    """服务身份测试。"""

    def test_service_identity_creation(self) -> None:
        identity = ServiceIdentity(
            service_name="beidou-safety",
            service_id="svc-safety-001",
            roles=frozenset({"executor", "risk_checker"}),
            allowed_ips=frozenset({"10.0.0.0/8", "172.16.0.0/12"}),
        )
        assert identity.service_name == "beidou-safety"
        assert "executor" in identity.roles
        assert "10.0.0.0/8" in identity.allowed_ips

    def test_credential_withdraw_disabled(self) -> None:
        """所有交易密钥必须禁用提款。"""
        cred = Credential(
            credential_id="cred_001",
            credential_type=CredentialType.TRADING,
            can_withdraw=False,
        )
        assert not cred.can_withdraw


class TestCredentialTypes:
    """凭据类型分离测试。"""

    def test_three_types_distinct(self) -> None:
        types = {CredentialType.MARKET_READONLY, CredentialType.ACCOUNT_READONLY, CredentialType.TRADING}
        assert len(types) == 3
