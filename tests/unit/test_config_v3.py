"""BD-V3 configuration provider contracts."""

from __future__ import annotations

import pytest

from beidou_shared.config import ConfigError, ConfigProvider, Environment


def test_unknown_environment_falls_back_to_non_trading_loopback_defaults() -> None:
    first = ConfigProvider().load(environment="not-a-real-environment")
    second = ConfigProvider().load(environment="not-a-real-environment")

    assert first.environment is Environment.SAFETY_ONLY
    assert first.can_write_trades is False
    assert first.exchange.rest_base_url == ""
    assert first.infrastructure.health_host == "127.0.0.1"
    assert first.config_hash == second.config_hash


def test_shadow_is_explicitly_non_trading() -> None:
    assert Environment.SHADOW.can_write_trades is False
    assert Environment.TESTNET.can_write_trades is True


def test_plaintext_credentials_are_rejected(tmp_path) -> None:
    config = tmp_path / "unsafe.yaml"
    config.write_text(
        """
environment: paper
exchange:
  binance_usdm:
    rest_base_url: https://testnet.binancefuture.com
    api_key: plaintext-key
    api_secret: plaintext-secret
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="plaintext api_key"):
        ConfigProvider().load(explicit_path=str(config))
