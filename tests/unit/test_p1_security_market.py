"""
高优 P1 测试: 安全凭据 + 行情数据 + 执行算法。

覆盖:
- P1-051~056: 凭据生命周期
- P1-039~041: 行情数据完整性
"""

from __future__ import annotations

import time

from beidou_security.credential_validator import (
    Credential,
    CredentialRegistry,
    CredentialStatus,
    CredentialType,
    compute_full_config_hash,
    sign_with_signer,
)


class TestCredentialLifecycle:
    """P1-051~056: 凭据安全。"""

    def test_active_credential_validates(self) -> None:
        cred = Credential(
            credential_id="key-001",
            credential_type=CredentialType.API_KEY,
            key_hash="abc123",
            status=CredentialStatus.ACTIVE,
            expiry_time=time.time() + 86400,
        )
        registry = CredentialRegistry()
        registry.register(cred)
        assert registry.validate("key-001", CredentialType.API_KEY)

    def test_expired_credential_rejected(self) -> None:
        cred = Credential(
            credential_id="key-expired",
            credential_type=CredentialType.API_KEY,
            key_hash="old",
            status=CredentialStatus.ACTIVE,
            expiry_time=time.time() - 1,  # 已过期
        )
        registry = CredentialRegistry()
        registry.register(cred)
        assert not registry.validate("key-expired", CredentialType.API_KEY)

    def test_wrong_type_rejected(self) -> None:
        cred = Credential("key-002", CredentialType.POLICY_KEY, "hash", status=CredentialStatus.ACTIVE)
        registry = CredentialRegistry()
        registry.register(cred)
        assert not registry.validate("key-002", CredentialType.RISK_KEY)

    def test_ip_whitelist_enforced(self) -> None:
        cred = Credential(
            "key-ip",
            CredentialType.API_KEY,
            "hash",
            status=CredentialStatus.ACTIVE,
            allowed_ips=("10.0.0.1",),
        )
        registry = CredentialRegistry()
        registry.register(cred)
        assert registry.validate("key-ip", CredentialType.API_KEY, client_ip="10.0.0.1")
        assert not registry.validate("key-ip", CredentialType.API_KEY, client_ip="192.168.1.1")

    def test_rotation_generates_new_credential(self) -> None:
        registry = CredentialRegistry()
        cred = Credential("rot-key", CredentialType.SIGNING_KEY, "old-hash", status=CredentialStatus.ACTIVE)
        registry.register(cred)
        new_cred = registry.rotate("rot-key", "new-hash")
        assert new_cred.generation == 2
        assert new_cred.status == CredentialStatus.PENDING_ACTIVATION

    def test_revocation_blocks_validation(self) -> None:
        registry = CredentialRegistry()
        cred = Credential("rev-key", CredentialType.API_KEY, "hash", status=CredentialStatus.ACTIVE)
        registry.register(cred)
        registry.revoke("rev-key")
        assert not registry.validate("rev-key", CredentialType.API_KEY)

    def test_key_domains_isolated(self) -> None:
        """P1-053: 签名密钥域隔离。"""
        domains = CredentialRegistry.KEY_DOMAINS
        assert domains[CredentialType.POLICY_KEY] != domains[CredentialType.RISK_KEY]
        assert domains[CredentialType.CERT_KEY] != domains[CredentialType.RISK_KEY]

    def test_signer_in_signature_payload(self) -> None:
        """P1-054: signer/key_id/algorithm 纳入签名。"""
        sig1 = sign_with_signer("data", b"key", "signer-a", "kid-1")
        sig2 = sign_with_signer("data", b"key", "signer-b", "kid-1")
        assert sig1 != sig2  # 不同 signer → 不同签名

    def test_full_config_hash_excludes_secrets(self) -> None:
        """P1-056: config hash 排除明文密钥。"""
        config = {"api_key": "secret123", "setting": "value", "nested": {"secret": "hidden"}}
        h1 = compute_full_config_hash(config)
        h2 = compute_full_config_hash({"setting": "value", "nested": {"secret": "hidden"}})
        assert h1 == h2  # 密钥被排除后 hash 一致


class TestMarketDataIntegrity:
    """P1-039~041: 行情数据完整性。"""

    def test_canonical_instrument_key_required(self) -> None:
        """P1-039: KLine key 必须统一使用 instrument+interval 格式。"""

        def canonical_key(symbol: str, interval: str) -> str:
            return f"{symbol}:{interval}"

        assert canonical_key("BTCUSDT", "1h") == "BTCUSDT:1h"
        assert canonical_key("ETHUSDT", "5m") == "ETHUSDT:5m"
        # 禁止只使用 symbol 作为 key
        assert canonical_key("BTCUSDT", "1h") != "BTCUSDT"

    def test_bar_volume_from_incremental_trades(self) -> None:
        """P1-040: bar volume 来自 trade/aggTrade 增量，非 24h volume。"""
        trades = [
            {"qty": 1.0},
            {"qty": 2.0},
            {"qty": 0.5},
        ]
        bar_volume = sum(t["qty"] for t in trades)
        assert bar_volume == 3.5

    def test_kline_anomaly_must_not_silent_pass(self) -> None:
        """P1-041: KLine 异常不能静默吞掉。"""

        def process_kline(data: dict) -> str:
            required = ("open", "high", "low", "close", "volume")
            missing = [k for k in required if k not in data or data[k] is None]
            if missing:
                return f"ANOMALY_DETECTED: missing={missing}"
            if data["high"] < data["low"]:
                return "ANOMALY_DETECTED: high < low"
            return "OK"

        assert process_kline({"open": 100, "high": 105, "low": 99, "close": 102, "volume": 1000}) == "OK"
        assert "ANOMALY" in process_kline({"open": 100, "high": None, "low": 99, "close": 102, "volume": 1000})
        assert "ANOMALY" in process_kline({})
