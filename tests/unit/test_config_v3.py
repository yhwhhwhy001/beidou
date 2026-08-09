"""BD-V3 configuration provider contracts."""

from __future__ import annotations

import pytest

from beidou_shared.config import ConfigError, ConfigProvider, Environment, redact_database_url


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


def test_structured_postgresql_config_becomes_runtime_dsn_without_password(tmp_path, monkeypatch) -> None:
    config = tmp_path / "testnet.yaml"
    config.write_text(
        """
environment: testnet
postgresql:
  host: db.internal
  port: 5433
  database: beidou_testnet
  user: beidou_app
  ssl_mode: require
  application_name: beidou-test
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = ConfigProvider().load(explicit_path=str(config))

    assert settings.database.url == (
        "postgresql://beidou_app@db.internal:5433/beidou_testnet?sslmode=require&application_name=beidou-test"
    )
    assert "beidou_app@" in redact_database_url(settings.database.url)


def test_database_url_environment_override_is_redacted_from_config_hash(tmp_path, monkeypatch) -> None:
    config = tmp_path / "testnet.yaml"
    config.write_text("environment: testnet\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:super-secret@localhost:5432/beidou")

    settings = ConfigProvider().load(explicit_path=str(config))

    assert settings.database.url.endswith("/beidou")
    assert "super-secret" not in redact_database_url(settings.database.url)
    assert "super-secret" not in settings.config_hash
