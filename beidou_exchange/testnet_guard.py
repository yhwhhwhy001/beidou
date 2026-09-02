"""Bounded Binance Testnet write authority.

This module is deliberately small and independent from the legacy runtime.
It is the local trust boundary for the Testnet verification application:
public reads may use the normal adapter, while every mutation must carry a
short-lived, identity-bound :class:`TerminalWriteContext` and pass the exact
Testnet URL, cap, and kill-switch checks here.

The guard is not a production authorization system.  Binance HMAC remains the
venue authentication mechanism; this object only constrains the local
Testnet verification process and never permits a Mainnet URL.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlsplit

from beidou_exchange.core.write_authority import (
    TerminalWriteContext,
    TerminalWriteDecision,
    TerminalWriteKind,
    TerminalWriteRequest,
    canonical_final_request_hash,
)

TESTNET_HOSTS = frozenset({"demo-fapi.binance.com", "testnet.binancefuture.com"})
TESTNET_ENVIRONMENT = "TESTNET"
BINANCE_VENUE = "BINANCE"


class TestnetGuardError(ValueError):
    """Raised when a Testnet guard or bounded write context is invalid."""

    __test__ = False


def _decimal(value: Any, *, field_name: str, minimum: Decimal = Decimal("0")) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TestnetGuardError(f"{field_name} must be a finite decimal") from exc
    if not parsed.is_finite() or parsed < minimum:
        raise TestnetGuardError(f"{field_name} must be finite and >= {minimum}")
    return parsed


def _positive_decimal(value: Any, *, field_name: str) -> Decimal:
    parsed = _decimal(value, field_name=field_name, minimum=Decimal("0"))
    if parsed <= 0:
        raise TestnetGuardError(f"{field_name} must be > 0")
    return parsed


def _normalize_host(url: str) -> str:
    """Return an exact lower-case hostname, rejecting URL ambiguity."""

    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise TestnetGuardError("invalid Testnet URL") from exc

    if parsed.scheme.lower() != "https":
        raise TestnetGuardError("Testnet REST URL must use HTTPS")
    if not hostname:
        raise TestnetGuardError("Testnet REST URL must contain a hostname")
    # A user-info component can make logs and operator review disagree with
    # the actual destination.  It is never valid for a REST base URL.
    if parsed.username is not None or parsed.password is not None:
        raise TestnetGuardError("credentials in REST URL are forbidden")
    if port not in (None, 443):
        raise TestnetGuardError("Testnet REST URL must use port 443")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise TestnetGuardError("REST base URL must not contain path, query, or fragment")
    # Do not strip a trailing dot or accept an IDNA spelling here.  The
    # allowlist is intentionally exact to avoid DNS/canonicalization tricks.
    host = hostname.lower()
    if host not in TESTNET_HOSTS:
        raise TestnetGuardError(f"Mainnet or non-allowlisted host denied: {host}")
    return host


def _quantities_equal(left: object, right: object) -> bool:
    """Compare quantities numerically; "0.100" and "0.1" are the same size.

    BD-FIX (V4 campaign): quantize_quantity renders trailing zeros while the
    transport serializes the normalized Decimal; a string comparison would
    deny a semantically identical reduce-only close.
    """

    try:
        left_decimal = Decimal(str(left))
        right_decimal = Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not left_decimal.is_finite() or not right_decimal.is_finite():
        return False
    return left_decimal == right_decimal


@dataclass(frozen=True, slots=True)
class TestnetCap:
    """Absolute local limits applied to one verifier process."""

    max_notional: Decimal
    max_leverage: Decimal
    max_account_exposure: Decimal

    def __post_init__(self) -> None:
        if not self.max_notional.is_finite() or self.max_notional <= 0:
            raise TestnetGuardError("max_notional must be finite and > 0")
        if not self.max_leverage.is_finite() or self.max_leverage <= 0:
            raise TestnetGuardError("max_leverage must be finite and > 0")
        if not self.max_account_exposure.is_finite() or self.max_account_exposure <= 0:
            raise TestnetGuardError("max_account_exposure must be finite and > 0")


class TestnetEnvironmentGuard:
    """Fail-closed local authority for bounded Testnet mutations.

    ``authorize`` implements the existing ``TerminalWriteAuthority`` protocol,
    so the adapter/REST layer can evaluate the same typed request before any
    socket is opened.  The guard allows exits while the kill switch is active,
    but denies all risk-increasing writes synchronously.
    """

    # Pytest should not treat this imported production class as a test class
    # when unit tests import it with its public name.
    __test__ = False

    def __init__(
        self,
        rest_base_url: str,
        *,
        max_notional: float | str | Decimal,
        max_leverage: float | str | Decimal,
        max_account_exposure: float | str | Decimal | None = None,
        account_id: str,
        owner_id: str = "testnet-verifier",
        task_id: str = "testnet-verification",
        entrypoint: str = "apps.testnet_verify",
        writes_enabled: bool = False,
        kill_switch_path: str | Path | None = None,
        context_ttl_seconds: float = 300.0,
        clock: Any = time.time,
    ) -> None:
        self.rest_base_url = self.canonical_base_url(rest_base_url)
        self.host = _normalize_host(self.rest_base_url)
        self.cap = TestnetCap(
            max_notional=_positive_decimal(max_notional, field_name="max_notional"),
            max_leverage=_positive_decimal(max_leverage, field_name="max_leverage"),
            max_account_exposure=_positive_decimal(
                max_notional if max_account_exposure is None else max_account_exposure,
                field_name="max_account_exposure",
            ),
        )
        self.account_id = str(account_id or "").strip()
        if not self.account_id or self.account_id.upper() in {"UNKNOWN", "DEFAULT"}:
            raise TestnetGuardError("a non-default Testnet account_id is required")
        self.owner_id = str(owner_id or "").strip()
        if not self.owner_id:
            raise TestnetGuardError("owner_id is required")
        self.task_id = str(task_id or "").strip()
        self.entrypoint = str(entrypoint or "").strip()
        if not self.task_id or not self.entrypoint:
            raise TestnetGuardError("task_id and entrypoint are required")
        if type(writes_enabled) is not bool:
            raise TestnetGuardError("writes_enabled must be a boolean")
        self.writes_enabled = writes_enabled
        self._kill_switch_path = Path(kill_switch_path) if kill_switch_path is not None else None
        self.context_ttl_seconds = _positive_decimal(context_ttl_seconds, field_name="context_ttl_seconds")
        ttl_float = float(self.context_ttl_seconds)
        if not math.isfinite(ttl_float) or ttl_float > 86_400:
            raise TestnetGuardError("context_ttl_seconds must be <= 86400")
        self._clock = clock
        self._kill_switch = Event()

    @staticmethod
    def canonical_base_url(url: str) -> str:
        """Validate and canonicalize an exact HTTPS Testnet base URL."""

        host = _normalize_host(url)
        # Explicit :443 is normalized so the request hash and audit output are
        # stable, while all other URL components have already been rejected.
        return f"https://{host}"

    @staticmethod
    def is_allowed_url(url: str) -> bool:
        try:
            _normalize_host(url)
        except TestnetGuardError:
            return False
        return True

    is_testnet_url = is_allowed_url

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch.is_set()

    def engage_kill_switch(self) -> None:
        """Synchronously deny new risk-increasing writes."""

        self._kill_switch.set()

    def clear_kill_switch(self) -> None:
        """Clear the local switch; the caller must still create a fresh context."""

        self._kill_switch.clear()

    # Alias useful to a CLI signal handler without exposing an alternate
    # bypass semantics.
    activate_kill_switch = engage_kill_switch
    deactivate_kill_switch = clear_kill_switch

    def build_write_context(
        self,
        *,
        intent_id: str,
        trace_id: str,
        symbol: str = "",
        side: str = "",
        order_type: str = "",
        quantity: str = "",
        notional: str = "",
        leverage: str = "",
        position_id: str = "",
        reduce_only: bool = False,
        close_position: bool = False,
        pool_id: str = "",
        pool_version: str = "",
        pool_hash: str = "",
        pool_symbols: tuple[str, ...] = (),
        command_hash: str = "",
        final_request_hash: str = "",
        client_order_id: str = "",
        account_exposure: str = "",
        projected_account_exposure: str = "",
        method: str = "",
        path: str = "",
        request_params: dict[str, object] | None = None,
        order_id: str = "",
        now: float | None = None,
    ) -> TerminalWriteContext:
        """Create one bounded, identity-bound context for adapter calls."""

        clean_intent = str(intent_id or "").strip()
        clean_trace = str(trace_id or "").strip()
        if not clean_intent or not clean_trace:
            raise TestnetGuardError("intent_id and trace_id are required")
        if not symbol and not (order_type.upper() == "LEVERAGE"):
            # The leverage endpoint also carries a symbol in its params, so a
            # missing symbol is never a valid context for a real mutation.
            raise TestnetGuardError("symbol is required for a Testnet write context")
        timestamp = float(self._clock() if now is None else now)
        if not math.isfinite(timestamp):
            raise TestnetGuardError("context clock is not finite")
        clean_symbol = str(symbol or "").strip().upper()
        clean_order_type = str(order_type or "").strip().upper()
        expected_method = str(method or "POST").strip().upper()
        expected_path = str(path or ("/fapi/v1/leverage" if clean_order_type == "LEVERAGE" else "/fapi/v1/order"))
        clean_client_order_id = str(client_order_id or ("" if expected_method == "DELETE" else clean_intent)).strip()
        if request_params is None:
            if expected_path == "/fapi/v1/leverage":
                request_params = {
                    "symbol": clean_symbol,
                    "leverage": int(_positive_decimal(leverage, field_name="leverage")),
                }
            elif expected_method == "DELETE":
                request_params = {"symbol": clean_symbol, "orderId": int(str(order_id))}
            else:
                request_params = {
                    "symbol": clean_symbol,
                    "side": str(side or "").strip().upper(),
                    "type": clean_order_type,
                    "quantity": str(quantity or ""),
                    "newClientOrderId": clean_client_order_id,
                }
                if reduce_only:
                    request_params["reduceOnly"] = "true"
                if close_position:
                    request_params["closePosition"] = "true"
        computed_request_hash = canonical_final_request_hash(
            expected_method,
            expected_path,
            request_params,
            account_id=self.account_id,
            command_hash=str(command_hash or ""),
            pool_id=str(pool_id or ""),
            pool_version=str(pool_version or ""),
            pool_hash=str(pool_hash or ""),
            adaptive_leverage=str(leverage or ""),
            adaptive_quantity=str(quantity or ""),
            adaptive_notional=str(notional or ""),
        )
        if final_request_hash and str(final_request_hash) != computed_request_hash:
            raise TestnetGuardError("final_request_hash does not bind the supplied request")
        nonce_material = f"{self.account_id}|{clean_intent}|{clean_trace}|{self.owner_id}"
        nonce = hashlib.sha256(nonce_material.encode("utf-8")).hexdigest()[:32]
        return TerminalWriteContext(
            task_id=self.task_id,
            entrypoint=self.entrypoint,
            owner_id=self.owner_id,
            generation=clean_trace,
            approval_id=f"local-testnet-confirmed:{clean_trace}",
            expires_at=timestamp + float(self.context_ttl_seconds),
            nonce=nonce,
            intent_id=clean_intent,
            position_id=str(position_id or ""),
            quantity=str(quantity or ""),
            dedicated_account=True,
            account_id=self.account_id,
            venue_id=BINANCE_VENUE,
            environment=TESTNET_ENVIRONMENT,
            rest_base_url=self.rest_base_url,
            notional=str(notional or ""),
            leverage=str(leverage or ""),
            side=str(side or ""),
            order_type=str(order_type or ""),
            reduce_only=bool(reduce_only),
            close_position=bool(close_position),
            pool_id=str(pool_id or ""),
            pool_version=str(pool_version or ""),
            pool_hash=str(pool_hash or ""),
            pool_symbols=tuple(str(item) for item in pool_symbols),
            command_hash=str(command_hash or ""),
            final_request_hash=computed_request_hash,
            adaptive_leverage=str(leverage or ""),
            adaptive_quantity=str(quantity or ""),
            adaptive_notional=str(notional or ""),
            symbol=clean_symbol,
            client_order_id=clean_client_order_id,
            expected_method=expected_method,
            expected_path=expected_path,
            account_exposure=str(account_exposure or ""),
            projected_account_exposure=str(projected_account_exposure or ""),
            order_id=str(order_id or ""),
        )

    def validate_context(
        self,
        context: TerminalWriteContext,
        request: TerminalWriteRequest | None = None,
    ) -> TerminalWriteDecision:
        """Validate context-level facts before request classification."""

        if str(context.task_id).strip() != self.task_id:
            return TerminalWriteDecision(False, "TESTNET_TASK_MISMATCH")
        if str(context.entrypoint).strip() != self.entrypoint:
            return TerminalWriteDecision(False, "TESTNET_ENTRYPOINT_MISMATCH")
        if str(context.owner_id).strip() != self.owner_id:
            return TerminalWriteDecision(False, "TESTNET_OWNER_MISMATCH")
        if str(context.environment).upper() != TESTNET_ENVIRONMENT:
            return TerminalWriteDecision(False, "TESTNET_ENVIRONMENT_REQUIRED")
        if context.rest_base_url != self.rest_base_url or not self.is_allowed_url(context.rest_base_url):
            return TerminalWriteDecision(False, "TESTNET_URL_MISMATCH")
        if context.account_id != self.account_id:
            return TerminalWriteDecision(False, "TESTNET_ACCOUNT_MISMATCH")
        if request is not None:
            identity_checks = [
                (str(context.expected_method).upper(), str(request.method).upper(), "TESTNET_CONTEXT_METHOD_MISMATCH"),
                (str(context.expected_path), str(request.path), "TESTNET_CONTEXT_PATH_MISMATCH"),
                (str(context.symbol).upper(), str(request.symbol).upper(), "TESTNET_CONTEXT_SYMBOL_MISMATCH"),
                (str(context.pool_id), str(request.pool_id), "TESTNET_CONTEXT_POOL_ID_MISMATCH"),
                (str(context.pool_version), str(request.pool_version), "TESTNET_CONTEXT_POOL_VERSION_MISMATCH"),
                (str(context.pool_hash), str(request.pool_hash), "TESTNET_CONTEXT_POOL_HASH_MISMATCH"),
            ]
            # BD-FIX (V4 campaign): quantities are compared numerically —
            # "0.100" and "0.1" are the same order size; a string comparison
            # would deny a semantically identical reduce-only close.
            if not _quantities_equal(context.quantity, request.quantity):
                return TerminalWriteDecision(False, "TESTNET_CONTEXT_QUANTITY_MISMATCH")
            if request.path != "/fapi/v1/leverage":
                if request.kind is TerminalWriteKind.CANCEL_OWNED:
                    identity_checks.extend(
                        [
                            (str(context.order_id), str(request.order_id), "TESTNET_CONTEXT_ORDER_ID_MISMATCH"),
                        ]
                    )
                else:
                    identity_checks.extend(
                        [
                            (str(context.side).upper(), str(request.side).upper(), "TESTNET_CONTEXT_SIDE_MISMATCH"),
                            (
                                str(context.order_type).upper(),
                                str(request.order_type).upper(),
                                "TESTNET_CONTEXT_ORDER_TYPE_MISMATCH",
                            ),
                            (
                                str(context.client_order_id),
                                str(request.client_order_id),
                                "TESTNET_CONTEXT_CLIENT_ORDER_ID_MISMATCH",
                            ),
                        ]
                    )
            identity_checks.append(
                (
                    str(context.final_request_hash),
                    str(request.final_request_hash),
                    "TESTNET_CONTEXT_REQUEST_HASH_MISMATCH",
                )
            )
            for expected, actual, reason in identity_checks:
                if expected != actual:
                    return TerminalWriteDecision(False, reason)
            if request.kind is TerminalWriteKind.INCREASE and request.path != "/fapi/v1/leverage":
                if not context.pool_id or not context.pool_version or not context.pool_hash:
                    return TerminalWriteDecision(False, "TESTNET_CONTEXT_POOL_SCOPE_INCOMPLETE")
                if request.symbol not in context.pool_symbols:
                    return TerminalWriteDecision(False, "TESTNET_CONTEXT_SYMBOL_NOT_ACTIVE")
        return TerminalWriteDecision(True, "CONTEXT_VALID")

    def _cap_decision(self, request: TerminalWriteRequest) -> TerminalWriteDecision:
        risk_increasing = request.kind is TerminalWriteKind.INCREASE
        if risk_increasing and self.kill_switch_active:
            return TerminalWriteDecision(False, "TESTNET_KILL_SWITCH_ACTIVE")
        if risk_increasing and self._kill_switch_path is not None:
            try:
                self._kill_switch_path.stat()
            except FileNotFoundError:
                pass
            except OSError:
                return TerminalWriteDecision(False, "TESTNET_KILL_SWITCH_STATE_UNKNOWN")
            else:
                return TerminalWriteDecision(False, "TESTNET_KILL_SWITCH_ACTIVE")
        if risk_increasing and not self.writes_enabled:
            return TerminalWriteDecision(False, "TESTNET_CONFIRMATION_REQUIRED")

        if request.kind is TerminalWriteKind.UNKNOWN:
            return TerminalWriteDecision(False, "UNCLASSIFIED_TERMINAL_WRITE")
        if request.environment.upper() != TESTNET_ENVIRONMENT:
            return TerminalWriteDecision(False, "TESTNET_ENVIRONMENT_REQUIRED")
        if request.rest_base_url != self.rest_base_url or not self.is_allowed_url(request.rest_base_url):
            return TerminalWriteDecision(False, "TESTNET_URL_MISMATCH")
        if request.venue_id.upper() not in {"", BINANCE_VENUE}:
            return TerminalWriteDecision(False, "TESTNET_VENUE_MISMATCH")
        if request.account_id != self.account_id:
            return TerminalWriteDecision(False, "TESTNET_ACCOUNT_MISMATCH")
        if not request.signed:
            return TerminalWriteDecision(False, "BINANCE_HMAC_REQUIRED")

        if request.kind is TerminalWriteKind.INCREASE:
            try:
                leverage = _positive_decimal(request.leverage, field_name="leverage")
                notional = _positive_decimal(request.notional, field_name="notional")
            except TestnetGuardError as exc:
                return TerminalWriteDecision(False, str(exc).upper().replace(" ", "_"))
            if leverage > self.cap.max_leverage:
                return TerminalWriteDecision(False, "TESTNET_MAX_LEVERAGE_EXCEEDED")
            if notional > self.cap.max_notional:
                return TerminalWriteDecision(False, "TESTNET_MAX_NOTIONAL_EXCEEDED")
            if request.path != "/fapi/v1/leverage":
                try:
                    current_exposure = _decimal(request.account_exposure, field_name="account_exposure")
                    projected_exposure = _positive_decimal(
                        request.projected_account_exposure,
                        field_name="projected_account_exposure",
                    )
                except TestnetGuardError as exc:
                    return TerminalWriteDecision(False, str(exc).upper().replace(" ", "_"))
                if projected_exposure != current_exposure + notional:
                    return TerminalWriteDecision(False, "TESTNET_PROJECTED_ACCOUNT_EXPOSURE_MISMATCH")
                if projected_exposure > self.cap.max_account_exposure:
                    return TerminalWriteDecision(False, "TESTNET_MAX_ACCOUNT_EXPOSURE_EXCEEDED")

        return TerminalWriteDecision(True, "TESTNET_WRITE_ALLOWED")

    def authorize(self, request: TerminalWriteRequest) -> TerminalWriteDecision:
        """Authorize a classified request without touching the network."""

        return self._cap_decision(request)


__all__ = [
    "BINANCE_VENUE",
    "TESTNET_ENVIRONMENT",
    "TESTNET_HOSTS",
    "TestnetCap",
    "TestnetEnvironmentGuard",
    "TestnetGuardError",
]
