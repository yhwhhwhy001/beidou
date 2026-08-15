"""M00-C01: terminal exchange writes require explicit, typed authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from inspect import signature

import pytest

from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.binance_usdm.write_guard import classify_terminal_write
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result
from beidou_exchange.core.protocol import OrderRequest
from beidou_exchange.core.write_authority import (
    TerminalWriteContext,
    TerminalWriteDecision,
    TerminalWriteKind,
    TerminalWriteRequest,
    evaluate_terminal_write,
)
from beidou_shared.types import (
    AccountId,
    AccountRef,
    HealthStatus,
    InstrumentId,
    OrderSide,
    OrderType,
    Quantity,
    VenueId,
    VenueInstrument,
)


class RecordingAuthority:
    def __init__(self, *, allow: bool) -> None:
        self.allow = allow
        self.requests: list[TerminalWriteRequest] = []

    def authorize(self, request: TerminalWriteRequest) -> TerminalWriteDecision:
        self.requests.append(request)
        return TerminalWriteDecision(
            allowed=self.allow,
            reason_code="TEST_ALLOW" if self.allow else "TEST_DENY",
        )


class InvalidBooleanAuthority:
    def authorize(self, _request: TerminalWriteRequest) -> TerminalWriteDecision:
        return TerminalWriteDecision(allowed="false", reason_code="INVALID_STRING_FALSE")  # type: ignore[arg-type]


class FakeRestClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[str, str, bool, dict[str, object]]] = []

    async def request(
        self,
        method: str,
        path: str,
        signed: bool = False,
        params: dict[str, object] | None = None,
    ) -> Result:
        self.calls.append((method, path, signed, dict(params or {})))
        return Result.success(dict(self.response))


def _order_request(*, reduce_only: bool) -> OrderRequest:
    return OrderRequest(
        venue_instrument=VenueInstrument(
            venue_id=VenueId("BINANCE"),
            instrument_id=InstrumentId("BTCUSDT"),
        ),
        account_ref=AccountRef(
            venue_id=VenueId("BINANCE"),
            account_id=AccountId("dedicated-test-account"),
        ),
        side=OrderSide.SELL if reduce_only else OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="0.01"),
        client_order_id="beidou-owned-intent-1",
        reduce_only=reduce_only,
    )


def _write_context() -> TerminalWriteContext:
    return TerminalWriteContext(
        task_id="TASK-M00-C01-TEST",
        entrypoint="pytest.binance_adapter",
        owner_id="test-owner",
        generation="generation-1",
        approval_id="approval-test-1",
        expires_at=4_102_444_800.0,
        nonce="nonce-test-1",
        intent_id="owned-intent-1",
        position_id="owned-position-1",
        quantity="0.01",
        dedicated_account=True,
    )


def test_rest_transport_denies_all_terminal_writes_without_authority(monkeypatch) -> None:
    transport_calls = 0

    def unexpected_transport(*_args, **_kwargs):
        nonlocal transport_calls
        transport_calls += 1
        raise AssertionError("terminal write reached transport")

    monkeypatch.setattr("beidou_exchange.binance_usdm.rest_client._sync_urlopen", unexpected_transport)
    client = BinanceRESTClient("https://offline.invalid", max_retries=1)

    results = [
        asyncio.run(client.create_order("BTCUSDT", "BUY", "MARKET", "0.01", client_order_id="cid-1")),
        asyncio.run(client.cancel_order("BTCUSDT", 17)),
        asyncio.run(client.create_algo_order({"symbol": "BTCUSDT", "algoType": "STOP"})),
        asyncio.run(client.cancel_algo_order("BTCUSDT", 19)),
    ]

    assert all(result.is_success() is False for result in results)
    assert all(result.error is not None for result in results)
    assert all(result.error.category is ErrorCategory.PERMISSION_DENIED for result in results if result.error)
    assert all(
        result.error.raw["reason"] == "WRITE_CAPABILITY_REGISTRY_INCOMPLETE" for result in results if result.error
    )
    assert transport_calls == 0


def test_production_transport_exposes_no_caller_supplied_authority_surface() -> None:
    rest_parameters = signature(BinanceRESTClient).parameters
    adapter_parameters = signature(BinanceUsdmAdapter).parameters

    assert "write_authority" not in rest_parameters
    assert "write_context" not in rest_parameters
    assert "write_authority" not in adapter_parameters
    assert "write_context" not in adapter_parameters


def test_adapter_does_not_treat_reduce_or_cancel_as_implicitly_authorized() -> None:
    transport = FakeRestClient(
        {
            "orderId": 17,
            "clientOrderId": "beidou-owned-intent-1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "0.01",
            "executedQty": "0",
            "status": "CANCELED",
            "reduceOnly": False,
        }
    )
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)

    reduced = asyncio.run(adapter.create_order(_order_request(reduce_only=True)))
    cancelled = asyncio.run(adapter.cancel_order("17", _order_request(reduce_only=True).venue_instrument))

    assert reduced.status.value == "UNKNOWN"
    assert cancelled.status.value == "UNKNOWN"
    assert transport.calls == []


def test_typed_authority_contract_receives_complete_context() -> None:
    authority = RecordingAuthority(allow=True)
    request = classify_terminal_write(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": "BTCUSDT",
            "quantity": "0.01",
            "newClientOrderId": "beidou-owned-intent-1",
        },
        account_id="dedicated-test-account",
        context=_write_context(),
    )

    assert request is not None
    decision = evaluate_terminal_write(authority, request)
    assert decision.allowed is True
    assert len(authority.requests) == 1
    request = authority.requests[0]
    assert request.kind is TerminalWriteKind.INCREASE
    assert request.account_id == "dedicated-test-account"
    assert request.symbol == "BTCUSDT"
    assert request.client_order_id == "beidou-owned-intent-1"
    assert request.quantity == "0.01"
    assert request.task_id == "TASK-M00-C01-TEST"
    assert request.entrypoint == "pytest.binance_adapter"
    assert request.owner_id == "test-owner"
    assert request.generation == "generation-1"
    assert request.approval_id == "approval-test-1"
    assert request.expires_at == 4_102_444_800.0
    assert request.nonce == "nonce-test-1"
    assert request.intent_id == "owned-intent-1"
    assert request.position_id == "owned-position-1"
    assert request.dedicated_account is True


def test_unknown_mutating_endpoint_is_denied_before_transport() -> None:
    transport = FakeRestClient({"ok": True})
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)

    results = [
        asyncio.run(adapter.request("DELETE", "/unknown/mutation", signed=True, params={})),
        asyncio.run(adapter.request("PATCH", "/future/mutation", signed=True, params={})),
    ]

    assert all(result.is_success() is False for result in results)
    assert all(result.error is not None for result in results)
    assert all(result.error.category is ErrorCategory.PERMISSION_DENIED for result in results if result.error)
    assert all(result.error.raw["reason"] == "UNCLASSIFIED_TERMINAL_WRITE" for result in results if result.error)
    assert transport.calls == []


def test_allowing_authority_cannot_override_missing_scope() -> None:
    authority = RecordingAuthority(allow=True)
    request = classify_terminal_write(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.01",
            "newClientOrderId": "cid-missing-scope",
        },
        account_id="dedicated-test-account",
    )

    assert request is not None
    decision = evaluate_terminal_write(authority, request)
    assert decision.allowed is False
    assert decision.reason_code == "WRITE_SCOPE_INCOMPLETE"
    assert authority.requests == []


@pytest.mark.parametrize(
    ("context", "params", "reason"),
    [
        (
            replace(_write_context(), expires_at=1.0),
            {"symbol": "BTCUSDT", "quantity": "0.01", "newClientOrderId": "cid-expired"},
            "WRITE_SCOPE_EXPIRED",
        ),
        (
            replace(_write_context(), expires_at=float("nan")),
            {"symbol": "BTCUSDT", "quantity": "0.01", "newClientOrderId": "cid-nan-expiry"},
            "WRITE_SCOPE_INVALID_EXPIRY",
        ),
        (
            replace(_write_context(), dedicated_account=False),
            {"symbol": "BTCUSDT", "quantity": "0.01", "newClientOrderId": "cid-shared"},
            "WRITE_ACCOUNT_NOT_DEDICATED",
        ),
        (
            replace(_write_context(), quantity=""),
            {"symbol": "BTCUSDT", "newClientOrderId": "cid-no-quantity"},
            "WRITE_OBJECT_SCOPE_INCOMPLETE",
        ),
        (
            replace(_write_context(), position_id=""),
            {
                "symbol": "BTCUSDT",
                "quantity": "0.01",
                "newClientOrderId": "cid-no-position",
                "reduceOnly": "true",
            },
            "WRITE_OBJECT_SCOPE_INCOMPLETE",
        ),
    ],
)
def test_stale_shared_or_incomplete_object_scope_is_denied(
    context: TerminalWriteContext,
    params: dict[str, str],
    reason: str,
) -> None:
    authority = RecordingAuthority(allow=True)
    request = classify_terminal_write(
        "POST",
        "/fapi/v1/order",
        params,
        account_id="dedicated-test-account",
        context=context,
    )

    assert request is not None
    decision = evaluate_terminal_write(authority, request)
    assert decision.allowed is False
    assert decision.reason_code == reason
    assert authority.requests == []


@pytest.mark.parametrize(
    "context",
    [
        replace(_write_context(), intent_id=""),
        replace(_write_context(), position_id=""),
        replace(_write_context(), quantity=""),
    ],
)
def test_cancel_requires_complete_owned_object_scope(context: TerminalWriteContext) -> None:
    authority = RecordingAuthority(allow=True)
    request = classify_terminal_write(
        "DELETE",
        "/fapi/v1/order",
        {"symbol": "BTCUSDT", "orderId": "17"},
        account_id="dedicated-test-account",
        context=context,
    )

    assert request is not None
    decision = evaluate_terminal_write(authority, request)
    assert decision.allowed is False
    assert decision.reason_code == "WRITE_OBJECT_SCOPE_INCOMPLETE"
    assert authority.requests == []


def test_non_boolean_authority_decision_is_rejected() -> None:
    request = classify_terminal_write(
        "POST",
        "/fapi/v1/order",
        {"symbol": "BTCUSDT", "quantity": "0.01", "newClientOrderId": "cid-bool"},
        account_id="dedicated-test-account",
        context=_write_context(),
    )

    assert request is not None
    decision = evaluate_terminal_write(InvalidBooleanAuthority(), request)
    assert decision.allowed is False
    assert decision.reason_code == "WRITE_AUTHORITY_INVALID_DECISION"
