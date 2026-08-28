"""External, bounded authorization for Binance terminal writes.

STATUS: FUTURE_MAINNET / FROZEN for the default Testnet verification path
(PKG-00-M05, PKG-08-M02, V4.0).  The bounded Testnet verifier
(``apps.testnet_verify``) must not import this module; the architecture test
``tests/architecture/test_testnet_verification_boundaries.py`` enforces the
boundary.  This module is retained for historical audit and the future
multi-operator / real-money production path only.

The Binance API signature authenticates a request to Binance.  It does not
authorize the Beidou process to create economic risk.  This module verifies a
separate Ed25519-signed capability issued by an external operator and consumes
its nonce exactly once before a mutating request is sent.

Only the public verification key and the signed capability are read here.  A
private key is intentionally not a runtime input and is never generated,
stored, or logged by this package.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from beidou_exchange.core.write_authority import (
    TerminalWriteContext,
    TerminalWriteDecision,
    TerminalWriteKind,
    TerminalWriteRequest,
)

logger = logging.getLogger(__name__)

MODULE_DEPLOYMENT_STATUS = "FUTURE_MAINNET"  # PKG-00-M05 / PKG-08-M02 frozen marker

_DEFAULT_STATE_DB = ".beidou/write-authority.sqlite3"
_CAPABILITY_PATH_ENV_VARS = (
    "BEIDOU_TESTNET_CAPABILITY_PATH",
    "BEIDOU_WRITE_CAPABILITY_PATH",
    "BEIDOU_TERMINAL_WRITE_CAPABILITY",
)
_PUBLIC_KEY_PATH_ENV_VARS = (
    "BEIDOU_TESTNET_CAPABILITY_PUBLIC_KEY_PATH",
    "BEIDOU_WRITE_AUTHORITY_PUBLIC_KEY_PATH",
    "BEIDOU_CAPABILITY_PUBLIC_KEY_PATH",
)
_ALLOWED_TESTNET_HOSTS = frozenset({"demo-fapi.binance.com", "testnet.binancefuture.com"})


def canonical_capability_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the exact JSON bytes covered by an Ed25519 signature."""

    return json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def capability_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return a stable, non-secret fingerprint for audit messages."""

    return hashlib.sha256(canonical_capability_bytes(payload)).hexdigest()


def _decode_text_bytes(value: str, *, expected_lengths: tuple[int, ...]) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty encoded value")
    try:
        raw = bytes.fromhex(text)
        if len(raw) in expected_lengths:
            return raw
    except ValueError:
        pass
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
        if len(raw) in expected_lengths:
            return raw
    except (ValueError, binascii.Error, UnicodeEncodeError):
        pass
    try:
        raw = base64.urlsafe_b64decode(text.encode("ascii") + b"=" * (-len(text) % 4))
        if len(raw) in expected_lengths:
            return raw
    except (ValueError, binascii.Error, UnicodeEncodeError):
        pass
    raise ValueError("encoded value has an unsupported length or format")


def _load_public_key(path: Path) -> Any:
    """Load an Ed25519 public key from PEM, raw, hex, or base64 bytes."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw = path.read_bytes()
    if b"BEGIN" in raw:
        key = serialization.load_pem_public_key(raw)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("capability verification key is not Ed25519")
        return key
    stripped = b"".join(raw.split())
    if len(stripped) == 32:
        return Ed25519PublicKey.from_public_bytes(stripped)
    decoded = _decode_text_bytes(stripped.decode("ascii"), expected_lengths=(32,))
    return Ed25519PublicKey.from_public_bytes(decoded)


def _decode_signature(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("capability signature must be text")
    return _decode_text_bytes(value, expected_lengths=(64,))


def _as_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_upper(value: Any) -> str:
    return _as_text(value).upper()


def _as_nonempty_set(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(_as_upper(item) for item in value if _as_text(item))


def _finite_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _normalise_url(value: Any) -> str:
    text = _as_text(value).rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_TESTNET_HOSTS or parsed.path not in {"", "/"}:
        return ""
    return f"https://{host}"


def _scope_value(payload: Mapping[str, Any], key: str, default: Any = None) -> Any:
    scope = payload.get("scope")
    if isinstance(scope, Mapping) and key in scope:
        return scope[key]
    return payload.get(key, default)


def _request_digest(request: TerminalWriteRequest) -> str:
    data = asdict(request)
    data["kind"] = request.kind.value
    return hashlib.sha256(canonical_capability_bytes(data)).hexdigest()


class SignedCapabilityAuthority:
    """Verify and consume one externally signed Testnet write capability."""

    def __init__(
        self,
        *,
        capability_path: str | Path,
        public_key_path: str | Path,
        state_db_path: str | Path | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.capability_path = Path(capability_path).expanduser()
        self.public_key_path = Path(public_key_path).expanduser()
        self.state_db_path = Path(state_db_path or os.environ.get("BEIDOU_WRITE_AUTHORITY_STATE_DB", _DEFAULT_STATE_DB))
        self._now = now_fn
        self._error = ""
        self._payload: dict[str, Any] = {}
        self._signature: bytes = b""
        self._payload_hash = ""
        self._current_pool_symbols: tuple[str, ...] = ()
        self._current_pool_scope: tuple[str, str, str] = ("", "", "")
        self._load_and_validate()

    @classmethod
    def from_environment(
        cls,
        *,
        state_db_path: str | Path | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> "SignedCapabilityAuthority | None":
        """Load the configured capability, returning ``None`` when absent.

        The caller can inspect ``load_error`` on the returned instance when a
        path was configured but malformed.  No capability path is inferred
        from a repository file, which keeps writable Testnet opt-in explicit.
        """

        capability_value = next(
            (
                os.environ.get(name, "").strip()
                for name in _CAPABILITY_PATH_ENV_VARS
                if os.environ.get(name, "").strip()
            ),
            "",
        )
        public_key_value = next(
            (
                os.environ.get(name, "").strip()
                for name in _PUBLIC_KEY_PATH_ENV_VARS
                if os.environ.get(name, "").strip()
            ),
            "",
        )
        if not capability_value and not public_key_value:
            return None
        if not capability_value or not public_key_value:
            raise ValueError("WRITE_CAPABILITY_PATH_AND_PUBLIC_KEY_REQUIRED")
        return cls(
            capability_path=capability_value,
            public_key_path=public_key_value,
            state_db_path=state_db_path,
            now_fn=now_fn,
        )

    @property
    def load_error(self) -> str:
        return self._error

    @property
    def is_ready(self) -> bool:
        return not self._error

    @property
    def capability_id(self) -> str:
        return _as_text(self._payload.get("capability_id"))

    @property
    def account_id(self) -> str:
        return _as_text(_scope_value(self._payload, "account_id", ""))

    @property
    def payload_hash(self) -> str:
        return self._payload_hash

    @property
    def context(self) -> TerminalWriteContext:
        pool_id, pool_version, pool_hash = self._current_pool_scope
        if not pool_id:
            raw_scope = _scope_value(self._payload, "pool_scope", {})
            if isinstance(raw_scope, Mapping):
                pool_id = _as_text(raw_scope.get("pool_id"))
                pool_version = _as_text(raw_scope.get("version"))
                pool_hash = _as_text(raw_scope.get("hash"))
        return TerminalWriteContext(
            task_id=_as_text(_scope_value(self._payload, "task_id", "")),
            entrypoint=_as_text(_scope_value(self._payload, "entrypoint", "")),
            owner_id=_as_text(_scope_value(self._payload, "owner_id", "")),
            generation=_as_text(_scope_value(self._payload, "generation", "")),
            approval_id=_as_text(_scope_value(self._payload, "approval_id", "")),
            expires_at=float(_scope_value(self._payload, "expires_at", 0.0) or 0.0),
            nonce=_as_text(_scope_value(self._payload, "nonce", "")),
            intent_id=_as_text(_scope_value(self._payload, "intent_id", "")),
            position_id=_as_text(_scope_value(self._payload, "position_id", "")),
            quantity=_as_text(_scope_value(self._payload, "quantity", "")),
            dedicated_account=_scope_value(self._payload, "dedicated_account", False) is True,
            account_id=self.account_id,
            venue_id=_as_upper(_scope_value(self._payload, "venue_id", "BINANCE_USDM")),
            environment=_as_lower(_scope_value(self._payload, "environment", "testnet")),
            rest_base_url=_normalise_url(_scope_value(self._payload, "rest_base_url", "")),
            pool_id=pool_id,
            pool_version=pool_version,
            pool_hash=pool_hash,
            pool_symbols=self._current_pool_symbols,
        )

    def set_pool_scope(
        self,
        *,
        pool_id: str,
        version: str,
        content_hash: str,
        symbols: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Bind the next request to the latest read-only exchange pool."""

        self._current_pool_scope = (_as_text(pool_id), _as_text(version), _as_text(content_hash))
        self._current_pool_symbols = tuple(sorted({_as_upper(item) for item in (symbols or ()) if _as_text(item)}))

    def authorize(self, request: TerminalWriteRequest) -> TerminalWriteDecision:
        """Perform a pure capability check; nonce consumption is separate."""

        if self._error:
            return TerminalWriteDecision(False, self._error)
        if request.kind is TerminalWriteKind.UNKNOWN:
            return TerminalWriteDecision(False, "UNCLASSIFIED_TERMINAL_WRITE")

        claims = self._payload
        now = self._now()
        try:
            expires_at = float(_scope_value(claims, "expires_at", 0.0))
        except (TypeError, ValueError):
            return TerminalWriteDecision(False, "CAPABILITY_EXPIRY_INVALID")
        if expires_at != expires_at or expires_at <= now:
            return TerminalWriteDecision(False, "CAPABILITY_EXPIRED")
        if request.expires_at != expires_at:
            return TerminalWriteDecision(False, "CAPABILITY_EXPIRY_MISMATCH")

        if request.signed is not True:
            return TerminalWriteDecision(False, "SIGNED_REQUEST_REQUIRED")
        if _as_upper(request.environment) != "TESTNET" or _as_upper(request.venue_id) != "BINANCE_USDM":
            return TerminalWriteDecision(False, "CAPABILITY_ENVIRONMENT_MISMATCH")
        if _normalise_url(request.rest_base_url) != _normalise_url(_scope_value(claims, "rest_base_url", "")):
            return TerminalWriteDecision(False, "CAPABILITY_ENDPOINT_MISMATCH")

        request_identity = {
            "task_id": request.task_id,
            "entrypoint": request.entrypoint,
            "owner_id": request.owner_id,
            "generation": request.generation,
            "approval_id": request.approval_id,
            "nonce": request.nonce,
            "account_id": request.account_id,
        }
        for request_field, claim_field in (
            ("task_id", "task_id"),
            ("entrypoint", "entrypoint"),
            ("owner_id", "owner_id"),
            ("generation", "generation"),
            ("approval_id", "approval_id"),
            ("nonce", "nonce"),
            ("account_id", "account_id"),
        ):
            expected = _as_text(_scope_value(claims, claim_field, ""))
            actual = _as_text(request_identity[request_field])
            if not expected or actual != expected:
                return TerminalWriteDecision(False, f"CAPABILITY_{claim_field.upper()}_MISMATCH")
        if request.dedicated_account is not True or _scope_value(claims, "dedicated_account", False) is not True:
            return TerminalWriteDecision(False, "WRITE_ACCOUNT_NOT_DEDICATED")

        allowed_methods = _as_nonempty_set(_scope_value(claims, "methods", _scope_value(claims, "allowed_methods", [])))
        allowed_paths = frozenset(
            _as_text(item)
            for item in (_scope_value(claims, "paths", _scope_value(claims, "allowed_paths", [])) or [])
            if _as_text(item)
        )
        allowed_kinds = _as_nonempty_set(_scope_value(claims, "kinds", _scope_value(claims, "allowed_kinds", [])))
        if request.method.upper() not in allowed_methods:
            return TerminalWriteDecision(False, "CAPABILITY_METHOD_NOT_ALLOWED")
        if request.path not in allowed_paths:
            return TerminalWriteDecision(False, "CAPABILITY_PATH_NOT_ALLOWED")
        if request.kind.value not in allowed_kinds:
            return TerminalWriteDecision(False, "CAPABILITY_KIND_NOT_ALLOWED")

        pool_scope = _scope_value(claims, "pool_scope", {})
        if not isinstance(pool_scope, Mapping):
            return TerminalWriteDecision(False, "CAPABILITY_POOL_SCOPE_MISSING")
        expected_pool = (
            _as_text(pool_scope.get("pool_id")),
            _as_text(pool_scope.get("version")),
            _as_text(pool_scope.get("hash")),
        )
        actual_pool = (_as_text(request.pool_id), _as_text(request.pool_version), _as_text(request.pool_hash))
        if not all(expected_pool) or actual_pool != expected_pool:
            return TerminalWriteDecision(False, "CAPABILITY_POOL_SCOPE_MISMATCH")
        if not self._current_pool_symbols or _as_upper(request.symbol) not in self._current_pool_symbols:
            return TerminalWriteDecision(False, "SYMBOL_OUTSIDE_ACTIVE_TRADING_POOL")

        configured_symbols = _scope_value(claims, "symbols", _scope_value(claims, "symbol", []))
        if configured_symbols not in (None, "", [], (), {}):
            return TerminalWriteDecision(False, "FIXED_SYMBOL_CAPABILITY_FORBIDDEN")
        if _as_upper(_scope_value(claims, "symbol_scope", "DYNAMIC_POOL")) not in {"DYNAMIC_POOL", "POOL"}:
            return TerminalWriteDecision(False, "DYNAMIC_POOL_SCOPE_REQUIRED")

        if request.side and _as_upper(request.side) not in _as_nonempty_set(
            _scope_value(claims, "sides", _scope_value(claims, "allowed_sides", []))
        ):
            return TerminalWriteDecision(False, "CAPABILITY_SIDE_NOT_ALLOWED")
        if request.order_type and _as_upper(request.order_type) not in _as_nonempty_set(
            _scope_value(claims, "order_types", _scope_value(claims, "allowed_order_types", []))
        ):
            return TerminalWriteDecision(False, "CAPABILITY_ORDER_TYPE_NOT_ALLOWED")
        if request.reduce_only and _scope_value(claims, "allow_reduce_only", False) is not True:
            return TerminalWriteDecision(False, "CAPABILITY_REDUCE_ONLY_NOT_ALLOWED")
        if request.close_position and _scope_value(claims, "allow_close_position", False) is not True:
            return TerminalWriteDecision(False, "CAPABILITY_CLOSE_POSITION_NOT_ALLOWED")
        if request.path == "/fapi/v1/leverage" and _scope_value(claims, "allow_leverage", False) is not True:
            return TerminalWriteDecision(False, "CAPABILITY_LEVERAGE_NOT_ALLOWED")

        optional_request_values = {
            "intent_id": request.intent_id,
            "position_id": request.position_id,
            "client_order_id": request.client_order_id,
        }
        for request_field, claim_field in (
            ("intent_id", "intent_id"),
            ("position_id", "position_id"),
            ("client_order_id", "client_order_id"),
        ):
            expected = _as_text(_scope_value(claims, claim_field, ""))
            if expected and _as_text(optional_request_values[request_field]) != expected:
                return TerminalWriteDecision(False, f"CAPABILITY_{claim_field.upper()}_MISMATCH")

        max_quantity = _finite_decimal(_scope_value(claims, "max_quantity", ""))
        max_notional = _finite_decimal(_scope_value(claims, "max_notional_usdt", ""))
        if max_quantity is None or max_quantity <= 0 or max_notional is None or max_notional <= 0:
            return TerminalWriteDecision(False, "CAPABILITY_LIMITS_INVALID")
        if request.quantity:
            quantity = _finite_decimal(request.quantity)
            if quantity is None or quantity <= 0 or quantity > max_quantity:
                return TerminalWriteDecision(False, "CAPABILITY_QUANTITY_LIMIT_EXCEEDED")
        if request.notional:
            notional = _finite_decimal(request.notional)
            if notional or notional == Decimal("0"):
                if notional is None or notional < 0 or notional > max_notional:
                    return TerminalWriteDecision(False, "CAPABILITY_NOTIONAL_LIMIT_EXCEEDED")
            else:
                return TerminalWriteDecision(False, "CAPABILITY_NOTIONAL_UNKNOWN")
        elif request.kind is TerminalWriteKind.INCREASE and request.path != "/fapi/v1/leverage":
            return TerminalWriteDecision(False, "CAPABILITY_NOTIONAL_UNKNOWN")

        if request.leverage:
            leverage = _finite_decimal(request.leverage)
            max_leverage = _finite_decimal(_scope_value(claims, "max_leverage", ""))
            if leverage is None or leverage <= 0 or max_leverage is None or leverage > max_leverage:
                return TerminalWriteDecision(False, "CAPABILITY_LEVERAGE_LIMIT_EXCEEDED")

        if _scope_value(claims, "allow_withdraw", None) is not False:
            return TerminalWriteDecision(False, "WITHDRAWAL_PERMISSION_MUST_BE_FALSE")
        if _scope_value(claims, "allow_transfer", None) is not False:
            return TerminalWriteDecision(False, "TRANSFER_PERMISSION_MUST_BE_FALSE")
        return TerminalWriteDecision(True, "SIGNED_CAPABILITY_ALLOWED")

    def consume(self, request: TerminalWriteRequest) -> TerminalWriteDecision:
        """Atomically consume the capability nonce immediately before send."""

        decision = self.authorize(request)
        if not decision.allowed:
            return decision
        try:
            self.state_db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.state_db_path), timeout=5.0, isolation_level=None) as connection:
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS terminal_write_nonces (
                        nonce TEXT PRIMARY KEY,
                        capability_id TEXT NOT NULL,
                        request_digest TEXT NOT NULL,
                        consumed_at REAL NOT NULL
                    )
                    """
                )
                nonce = _as_text(request.nonce)
                if connection.execute("SELECT 1 FROM terminal_write_nonces WHERE nonce = ?", (nonce,)).fetchone():
                    connection.rollback()
                    return TerminalWriteDecision(False, "WRITE_NONCE_REPLAY")
                connection.execute(
                    """
                    INSERT INTO terminal_write_nonces(nonce, capability_id, request_digest, consumed_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (nonce, self.capability_id, _request_digest(request), self._now()),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            return TerminalWriteDecision(False, "WRITE_NONCE_REPLAY")
        except (OSError, sqlite3.Error) as exc:
            logger.warning("terminal-write nonce store unavailable: %s", type(exc).__name__)
            return TerminalWriteDecision(False, "WRITE_NONCE_STORE_UNAVAILABLE")
        return TerminalWriteDecision(True, "SIGNED_CAPABILITY_CONSUMED")

    def _load_and_validate(self) -> None:
        try:
            raw = json.loads(self.capability_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("capability envelope must be an object")
            if isinstance(raw.get("payload"), dict):
                payload = dict(raw["payload"])
            elif isinstance(raw.get("claims"), dict):
                payload = dict(raw["claims"])
            else:
                payload = dict(raw)
            signature_value = raw.get("signature") or raw.get("signature_b64") or raw.get("signature_hex")
            payload.pop("signature", None)
            payload.pop("signature_b64", None)
            payload.pop("signature_hex", None)
            if not isinstance(signature_value, str):
                raise ValueError("capability signature is missing")
            signature = _decode_signature(signature_value)
            key = _load_public_key(self.public_key_path)
            key.verify(signature, canonical_capability_bytes(payload))
            self._payload = payload
            self._signature = signature
            self._payload_hash = capability_payload_hash(payload)
            self._validate_claims()
        except Exception as exc:
            self._error = f"CAPABILITY_INVALID:{type(exc).__name__}"

    def _validate_claims(self) -> None:
        payload = self._payload
        required_text = ("capability_id", "task_id", "entrypoint", "owner_id", "generation", "approval_id", "nonce")
        if any(not _as_text(_scope_value(payload, key, "")) for key in required_text):
            raise ValueError("required capability identity is missing")
        if _as_upper(_scope_value(payload, "environment", "")) != "TESTNET":
            raise ValueError("capability environment is not Testnet")
        if _as_upper(_scope_value(payload, "venue_id", "")) != "BINANCE_USDM":
            raise ValueError("capability venue is not Binance USD-M")
        if not _normalise_url(_scope_value(payload, "rest_base_url", "")):
            raise ValueError("capability endpoint is not an allowed Testnet endpoint")
        expiry = _finite_decimal(_scope_value(payload, "expires_at", ""))
        if expiry is None or expiry <= Decimal(str(self._now())):
            raise ValueError("capability is expired or has an invalid expiry")
        if _as_text(_scope_value(payload, "account_id", "")).upper() in {"", "UNKNOWN", "DEFAULT"}:
            raise ValueError("capability account is not dedicated")
        if _scope_value(payload, "dedicated_account", False) is not True:
            raise ValueError("capability does not assert a dedicated account")
        scope = _scope_value(payload, "pool_scope", {})
        if not isinstance(scope, Mapping) or not all(
            _as_text(scope.get(key)) for key in ("pool_id", "version", "hash")
        ):
            raise ValueError("dynamic pool scope is missing")
        configured_symbols = _scope_value(payload, "symbols", _scope_value(payload, "symbol", []))
        if configured_symbols not in (None, "", [], (), {}):
            raise ValueError("fixed-symbol capabilities are forbidden")
        if _as_upper(_scope_value(payload, "symbol_scope", "")) not in {"DYNAMIC_POOL", "POOL"}:
            raise ValueError("dynamic pool scope is required")
        for key in ("methods", "paths", "kinds", "sides", "order_types"):
            if not _scope_value(payload, key, _scope_value(payload, f"allowed_{key}", [])):
                raise ValueError(f"capability {key} is empty")
        for key in ("max_quantity", "max_notional_usdt", "max_leverage"):
            value = _finite_decimal(_scope_value(payload, key, ""))
            if value is None or value <= 0:
                raise ValueError(f"capability limit {key} is invalid")
        if _scope_value(payload, "allow_withdraw", None) is not False:
            raise ValueError("withdrawal capability must be false")
        if _scope_value(payload, "allow_transfer", None) is not False:
            raise ValueError("transfer capability must be false")


def _as_lower(value: Any) -> str:
    return _as_text(value).lower()


__all__ = [
    "SignedCapabilityAuthority",
    "canonical_capability_bytes",
    "capability_payload_hash",
]
