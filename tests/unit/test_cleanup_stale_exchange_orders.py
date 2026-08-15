"""M00-E01 negative tests for the retired order-cleanup write path."""

from __future__ import annotations

import asyncio
import sys

import pytest

from scripts import cleanup_stale_exchange_orders as cleanup


def test_execute_is_held_before_credentials_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["cleanup", "--execute", "--confirm", cleanup.CONFIRMATION])
    monkeypatch.setattr(
        cleanup,
        "load_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("credentials must not be read")),
    )

    with pytest.raises(RuntimeError, match="WRITE_CAPABILITY_REGISTRY_INCOMPLETE"):
        asyncio.run(cleanup.main())
