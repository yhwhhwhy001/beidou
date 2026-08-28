"""Coverage-gap tests for beidou_launcher.alpha_first_adapter."""

from __future__ import annotations

import pytest

from beidou_launcher.alpha_first_adapter import start_authorized_execution


def test_start_authorized_execution_requires_authorization(monkeypatch) -> None:
    monkeypatch.delenv("BEIDOU_EXECUTION_AUTHORIZATION", raising=False)
    with pytest.raises(PermissionError, match="execution authorization is required"):
        start_authorized_execution(mode="paper", symbols=["BTCUSDT"])


def test_start_authorized_execution_requires_symbols(monkeypatch) -> None:
    monkeypatch.setenv("BEIDOU_EXECUTION_AUTHORIZATION", "EXPLICIT_LOCAL_APPROVAL")
    with pytest.raises(ValueError, match="at least one explicit symbol is required"):
        start_authorized_execution(mode="paper", symbols=[])
