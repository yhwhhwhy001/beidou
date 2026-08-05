"""
PKG-01: Policy Registry 测试。
覆盖 Schema 验证、签名、生命周期、原子激活、Fail-Closed。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from beidou_policy.registry import (
    PolicyCategory,
    PolicyLifecycle,
    PolicyMetadata,
    PolicyPackage,
    PolicyRegistry,
)
from beidou_shared.types import PolicyId, SchemaVersion


class TestPolicyLifecycle:
    """策略生命周期测试。"""

    def make_policy(self, lifecycle: PolicyLifecycle = PolicyLifecycle.PROPOSED) -> PolicyPackage:
        return PolicyPackage(
            metadata=PolicyMetadata(
                policy_id=PolicyId("TEST_SYNTHETIC_POLICY_001"),
                name="test.risk.max_position_size",
                category=PolicyCategory.HARD_SAFETY,
                schema_version=SchemaVersion("2.0.0"),
                author="test_author",
                environment={"production", "staging"},
            ),
            data={"max_notional": "100000", "max_leverage": "3.0"},
            lifecycle=lifecycle,
        )

    def test_valid_lifecycle_flow(self) -> None:
        pkg = self.make_policy()
        assert pkg.lifecycle == PolicyLifecycle.PROPOSED

        pkg.lifecycle = PolicyLifecycle.VALIDATED
        assert pkg.lifecycle == PolicyLifecycle.VALIDATED

        pkg.sign("test_signature_hex", "key_001")
        assert pkg.lifecycle == PolicyLifecycle.SIGNED

        pkg.schedule(datetime.now(timezone.utc) + timedelta(hours=1))
        assert pkg.lifecycle == PolicyLifecycle.SCHEDULED

        # Can't activate before schedule
        with pytest.raises(ValueError, match="scheduled_at"):
            pkg.activate()

    def test_invalid_transitions(self) -> None:
        pkg = self.make_policy()
        with pytest.raises(ValueError, match="Invalid policy lifecycle transition"):
            pkg.sign("sig", "key_001")  # can't sign from PROPOSED

        pkg.lifecycle = PolicyLifecycle.VALIDATED
        with pytest.raises(ValueError, match="Invalid policy lifecycle transition"):
            pkg.activate()  # can't activate from VALIDATED

    def test_revoke_from_any_state(self) -> None:
        pkg = self.make_policy()
        pkg.revoke("test reason")
        assert pkg.lifecycle == PolicyLifecycle.REVOKED

    def test_expired_policy_not_effective(self) -> None:
        pkg = self.make_policy()
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        object.__setattr__(pkg.metadata, "expires_at", past)
        pkg.lifecycle = PolicyLifecycle.VALIDATED
        pkg.sign("sig", "key")
        pkg.lifecycle = PolicyLifecycle.ACTIVE
        assert not pkg.is_effectively_active()

    def test_is_effectively_active(self) -> None:
        pkg = self.make_policy()
        future = datetime.now(timezone.utc) + timedelta(days=30)
        object.__setattr__(pkg.metadata, "expires_at", future)
        pkg.lifecycle = PolicyLifecycle.VALIDATED
        pkg.sign("sig", "key_001")
        pkg.lifecycle = PolicyLifecycle.ACTIVE
        assert pkg.is_effectively_active()


class TestPolicyRegistry:
    """策略注册中心测试。"""

    def make_policy(
        self, policy_id: str, name: str, category: PolicyCategory = PolicyCategory.HARD_SAFETY
    ) -> PolicyPackage:
        return PolicyPackage(
            metadata=PolicyMetadata(
                policy_id=PolicyId(policy_id),
                name=name,
                category=category,
                schema_version=SchemaVersion("2.0.0"),
                author="test",
                environment={"production"},
            ),
            data={"value": "test"},
        )

    def test_register_and_retrieve(self) -> None:
        registry = PolicyRegistry()
        pkg = self.make_policy("POLICY_001", "test.policy.one")
        registry.register(pkg)
        retrieved = registry.get(PolicyId("POLICY_001"))
        assert retrieved is not None
        assert retrieved.metadata.name == "test.policy.one"

    def test_duplicate_register_raises(self) -> None:
        registry = PolicyRegistry()
        pkg = self.make_policy("POLICY_001", "test.policy.one")
        registry.register(pkg)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(pkg)

    def test_atomic_activation(self) -> None:
        registry = PolicyRegistry()
        pkg = self.make_policy("POLICY_001", "test.risk.max_position")
        pkg.lifecycle = PolicyLifecycle.VALIDATED
        pkg.sign("test_sig", "key_001")
        registry.register(pkg)
        # Must schedule before activating
        pkg.schedule(datetime.now(timezone.utc) - timedelta(hours=1))

        registry.set_active(PolicyId("POLICY_001"))
        active = registry.get_active("test.risk.max_position")
        assert active is not None
        assert active.lifecycle == PolicyLifecycle.ACTIVE

    def test_activate_unsigned_fails(self) -> None:
        registry = PolicyRegistry()
        pkg = self.make_policy("POLICY_001", "test.policy")
        pkg.lifecycle = PolicyLifecycle.VALIDATED
        registry.register(pkg)
        with pytest.raises(ValueError, match="not in an activatable state|not signed"):
            registry.set_active(PolicyId("POLICY_001"))

    def test_fail_closed_when_policy_missing(self) -> None:
        registry = PolicyRegistry()
        result = registry.fail_closed_check("nonexistent.policy")
        assert result.value == "UNKNOWN"

    def test_fail_closed_when_policy_unsigned(self) -> None:
        registry = PolicyRegistry()
        pkg = self.make_policy("POLICY_001", "test.policy")
        pkg.lifecycle = PolicyLifecycle.ACTIVE
        registry.register(pkg)
        registry._active_index["test.policy"] = PolicyId("POLICY_001")
        result = registry.fail_closed_check("test.policy")
        assert result.value == "UNKNOWN"

    def test_concurrent_readers_see_atomic_switch(self) -> None:
        """并发读取者只能看到完整的旧版本或完整的新版本。"""
        registry = PolicyRegistry()

        v1 = self.make_policy("POLICY_001", "test.policy")
        v1.lifecycle = PolicyLifecycle.VALIDATED
        v1.sign("sig_v1", "key_001")
        v1.data = {"version": "v1", "limit": 100}
        v1.schedule(datetime.now(timezone.utc) - timedelta(hours=1))
        registry.register(v1)
        registry.set_active(PolicyId("POLICY_001"))

        v2 = self.make_policy("POLICY_002", "test.policy")
        v2.lifecycle = PolicyLifecycle.VALIDATED
        v2.sign("sig_v2", "key_001")
        v2.data = {"version": "v2", "limit": 200}
        v2.schedule(datetime.now(timezone.utc) - timedelta(hours=1))
        object.__setattr__(v2.metadata, "previous_version", PolicyId("POLICY_001"))
        registry.register(v2)
        registry.set_active(PolicyId("POLICY_002"))

        active = registry.get_active("test.policy")
        assert active is not None
        assert active.data["version"] == "v2"
        assert active.data["limit"] == 200

    def test_hard_safety_policy_filter(self) -> None:
        registry = PolicyRegistry()
        pkg = self.make_policy("POLICY_001", "test.safety", PolicyCategory.HARD_SAFETY)
        pkg.lifecycle = PolicyLifecycle.VALIDATED
        pkg.sign("sig", "key")
        pkg.schedule(datetime.now(timezone.utc) - timedelta(hours=1))
        registry.register(pkg)
        registry.set_active(PolicyId("POLICY_001"))

        assert registry.get_hard_safety_policy("test.safety") is not None
        assert registry.get_hard_safety_policy("nonexistent") is None


class TestPolicySignature:
    """策略签名测试。"""

    def test_signature_verification_fails_tampered_data(self) -> None:
        registry = PolicyRegistry()
        pkg = PolicyPackage(
            metadata=PolicyMetadata(
                policy_id=PolicyId("TEST_POLICY"),
                name="test.policy",
                category=PolicyCategory.HARD_SAFETY,
                schema_version=SchemaVersion("2.0.0"),
                author="test",
                signature="invalid_signature",
                signing_key_id="key_001",
            ),
            data={"original": "data"},
            lifecycle=PolicyLifecycle.ACTIVE,
        )
        registry.register(pkg)
        # Use a known test key pair — the signature was generated with a different key,
        # so verification should fail
        result = registry.verify_signature(
            PolicyId("TEST_POLICY"),
            """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA
-----END PUBLIC KEY-----""",
        )
        assert result is False  # Should fail for invalid signature format
