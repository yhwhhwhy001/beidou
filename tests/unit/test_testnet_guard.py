"""PKG-01: bounded Testnet environment and write-authority contracts."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.write_guard import classify_terminal_write
from beidou_exchange.core.write_authority import evaluate_terminal_write
from beidou_exchange.testnet_guard import TestnetEnvironmentGuard, TestnetGuardError


def _guard() -> TestnetEnvironmentGuard:
    return TestnetEnvironmentGuard(
        "https://demo-fapi.binance.com",
        max_notional="25",
        max_leverage="3",
        account_id="dedicated-testnet-account",
        writes_enabled=True,
        clock=time.time,
    )


def test_exact_testnet_allowlist_and_https_are_fail_closed() -> None:
    assert TestnetEnvironmentGuard.is_allowed_url("https://demo-fapi.binance.com")
    assert TestnetEnvironmentGuard.is_allowed_url("https://testnet.binancefuture.com:443")
    for url in (
        "http://demo-fapi.binance.com",
        "https://fapi.binance.com",
        "https://demo-fapi.binance.com.evil.example",
        "https://demo-fapi.binance.com.",
        "https://user:secret@demo-fapi.binance.com",
        "https://demo-fapi.binance.com?redirect=fapi.binance.com",
        "https://demo-fapi.binance.com:8443",
    ):
        assert not TestnetEnvironmentGuard.is_allowed_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://demo-fapi.binance.com",
        "https://fapi.binance.com",
        "https://user:secret@demo-fapi.binance.com",
    ],
)
def test_guard_constructor_rejects_unsafe_destination(url: str) -> None:
    with pytest.raises(TestnetGuardError):
        TestnetEnvironmentGuard(url, max_notional=25, max_leverage=3, account_id="testnet-account")


def test_bounded_context_is_stable_and_contains_no_secret() -> None:
    guard = _guard()
    context = guard.build_write_context(
        intent_id="intent-1",
        trace_id="trace-1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.001",
        notional="20",
        leverage="2",
    )
    assert context.entrypoint == "apps.testnet_verify"
    assert context.environment == "TESTNET"
    assert context.rest_base_url == "https://demo-fapi.binance.com"
    assert context.dedicated_account is True
    assert "secret" not in repr(context).lower()
    same = guard.build_write_context(
        intent_id="intent-1",
        trace_id="trace-1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.001",
        notional="20",
        leverage="2",
    )
    assert context.nonce == same.nonce


def test_valid_increase_passes_caps_and_unknown_endpoint_is_denied() -> None:
    guard = _guard()
    context = guard.build_write_context(
        intent_id="intent-1",
        trace_id="trace-1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.001",
        notional="20",
        leverage="2",
    )
    request = classify_terminal_write(
        "POST",
        Endpoint.ORDER,
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.001",
            "newClientOrderId": "intent-1",
        },
        account_id="dedicated-testnet-account",
        context=context,
        signed=True,
        rest_base_url=guard.rest_base_url,
    )
    assert request is not None
    assert evaluate_terminal_write(guard, request).allowed

    unknown = classify_terminal_write(
        "POST",
        "/fapi/v1/unknown",
        {},
        account_id="dedicated-testnet-account",
        context=context,
        signed=True,
        rest_base_url=guard.rest_base_url,
    )
    assert unknown is not None
    assert evaluate_terminal_write(guard, unknown).reason_code == "UNCLASSIFIED_TERMINAL_WRITE"


def test_increase_requires_explicit_testnet_confirmation() -> None:
    guard = TestnetEnvironmentGuard(
        "https://demo-fapi.binance.com",
        max_notional="25",
        max_leverage="3",
        account_id="dedicated-testnet-account",
        clock=time.time,
    )
    context = guard.build_write_context(
        intent_id="intent-1",
        trace_id="trace-1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.001",
        notional="20",
        leverage="2",
    )
    request = classify_terminal_write(
        "POST",
        Endpoint.ORDER,
        {"symbol": "BTCUSDT", "quantity": "0.001"},
        account_id=guard.account_id,
        context=context,
        signed=True,
        rest_base_url=guard.rest_base_url,
    )
    assert request is not None
    assert evaluate_terminal_write(guard, request).reason_code == "TESTNET_CONFIRMATION_REQUIRED"

    confirmed = TestnetEnvironmentGuard(
        guard.rest_base_url,
        max_notional=25,
        max_leverage=3,
        account_id=guard.account_id,
        writes_enabled=True,
    )
    assert evaluate_terminal_write(confirmed, request).allowed


@pytest.mark.parametrize(
    ("notional", "leverage", "reason"),
    [
        ("25.01", "2", "TESTNET_MAX_NOTIONAL_EXCEEDED"),
        ("20", "3.01", "TESTNET_MAX_LEVERAGE_EXCEEDED"),
    ],
)
def test_absolute_caps_block_risk_increase(notional: str, leverage: str, reason: str) -> None:
    guard = _guard()
    context = guard.build_write_context(
        intent_id="intent-1",
        trace_id="trace-1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.001",
        notional=notional,
        leverage=leverage,
    )
    request = classify_terminal_write(
        "POST",
        Endpoint.ORDER,
        {"symbol": "BTCUSDT", "quantity": "0.001"},
        account_id=guard.account_id,
        context=context,
        signed=True,
        rest_base_url=guard.rest_base_url,
    )
    assert request is not None
    assert evaluate_terminal_write(guard, request).reason_code == reason


def test_kill_switch_blocks_increase_but_allows_owned_reduce() -> None:
    guard = _guard()
    context = guard.build_write_context(
        intent_id="intent-1",
        trace_id="trace-1",
        symbol="BTCUSDT",
        side="SELL",
        order_type="MARKET",
        quantity="0.001",
        notional="20",
        leverage="2",
        position_id="position-1",
        reduce_only=True,
    )
    increase_context = replace(context, reduce_only=False)
    increase = classify_terminal_write(
        "POST",
        Endpoint.ORDER,
        {"symbol": "BTCUSDT", "quantity": "0.001"},
        account_id=guard.account_id,
        context=increase_context,
        signed=True,
        rest_base_url=guard.rest_base_url,
    )
    reduce = classify_terminal_write(
        "POST",
        Endpoint.ORDER,
        {"symbol": "BTCUSDT", "quantity": "0.001", "reduceOnly": "true"},
        account_id=guard.account_id,
        context=context,
        signed=True,
        rest_base_url=guard.rest_base_url,
    )
    assert increase is not None and reduce is not None
    guard.engage_kill_switch()
    assert evaluate_terminal_write(guard, increase).reason_code == "TESTNET_KILL_SWITCH_ACTIVE"
    assert evaluate_terminal_write(guard, reduce).allowed


def test_context_expiry_is_checked_by_shared_authority() -> None:
    guard = _guard()
    context = guard.build_write_context(
        intent_id="intent-1",
        trace_id="trace-1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.001",
        notional="20",
        leverage="2",
        now=1_000.0,
    )
    request = classify_terminal_write(
        "POST",
        Endpoint.ORDER,
        {"symbol": "BTCUSDT", "quantity": "0.001"},
        account_id=guard.account_id,
        context=replace(context, expires_at=999.0),
        signed=True,
        rest_base_url=guard.rest_base_url,
    )
    assert request is not None
    assert evaluate_terminal_write(guard, request).reason_code == "WRITE_SCOPE_EXPIRED"
