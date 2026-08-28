"""BD-FIX (V4 campaign): account facts gate must accept demo-fapi reality.

Binance USD-M demo accounts always report ``canWithdraw=true``; the verifier
must not treat that as a blocker because the V4 minimum safety boundary does
not include withdrawal governance and the write guard never authorizes
withdrawal endpoints.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.testnet_verify.config import VerifierConfig
from apps.testnet_verify.runtime import VerificationRuntime
from beidou_exchange.core.error_taxonomy import Result


class _StubAdapter:
    def __init__(self, account: dict[str, Any]) -> None:
        self._account = account

    async def get_account_snapshot(self) -> Result[dict[str, Any]]:
        return Result.success(self._account, source="stub-account")


def _runtime(account: dict[str, Any]) -> VerificationRuntime:
    config = VerifierConfig(account_id="testnet-verification-unit")
    return VerificationRuntime(config, adapter=_StubAdapter(account))


@pytest.mark.asyncio
async def test_account_facts_accept_demo_account_that_can_withdraw() -> None:
    runtime = _runtime(
        {
            "totalWalletBalance": "1000",
            "availableBalance": "1000",
            "canTrade": True,
            "canWithdraw": True,  # demo-fapi always reports this
            "positions": [],
        }
    )
    facts = await runtime._account_facts()
    assert facts is not None
    assert facts[0] == 1000.0


@pytest.mark.asyncio
async def test_account_facts_reject_account_that_cannot_trade() -> None:
    runtime = _runtime(
        {
            "totalWalletBalance": "1000",
            "availableBalance": "1000",
            "canTrade": False,
            "canWithdraw": True,
            "positions": [],
        }
    )
    assert await runtime._account_facts() is None


@pytest.mark.asyncio
async def test_account_facts_reject_missing_balances() -> None:
    runtime = _runtime({"canTrade": True, "canWithdraw": True, "positions": []})
    assert await runtime._account_facts() is None
