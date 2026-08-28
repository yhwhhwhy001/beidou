"""Fail-closed authority contract for exchange terminal writes.

The transport boundary must never infer that a cancel, reduce-only order, or
emergency action is safe merely from its HTTP method or flags.  A caller must
provide an authority implementation that evaluates a typed request.  Missing,
invalid, or failing authorities deny the write without touching the transport.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Protocol


def normalize_quantity_string(value: object) -> str:
    """Render a quantity string in canonical Decimal form.

    ``quantize_quantity`` can produce trailing zeros (``"0.100"``) while the
    transport serializes the same Decimal as ``"0.1"``.  Both the context-side
    and request-side request hashes must see one canonical form, otherwise the
    write identity check denies a semantically identical order
    (BD-FIX V4 campaign).
    """

    raw = str(value)
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        return raw
    if not parsed.is_finite():
        return raw
    return format(parsed.normalize(), "f")


class TerminalWriteKind(str, Enum):
    """Economic meaning of a venue mutation."""

    INCREASE = "INCREASE"
    CANCEL_OWNED = "CANCEL_OWNED"
    REDUCE_OWNED = "REDUCE_OWNED"
    EMERGENCY = "EMERGENCY"
    SESSION_CONTROL = "SESSION_CONTROL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TerminalWriteContext:
    """Capability scope supplied separately from venue request parameters."""

    task_id: str
    entrypoint: str
    owner_id: str
    generation: str
    approval_id: str
    expires_at: float
    nonce: str
    intent_id: str = ""
    position_id: str = ""
    quantity: str = ""
    dedicated_account: bool = False
    # The following fields are non-secret venue/environment facts.  They are
    # deliberately carried on the typed request instead of being inferred
    # from the HTTP URL or a boolean such as ``reduceOnly``.
    account_id: str = ""
    venue_id: str = ""
    environment: str = ""
    rest_base_url: str = ""
    notional: str = ""
    leverage: str = ""
    side: str = ""
    order_type: str = ""
    reduce_only: bool = False
    close_position: bool = False
    pool_id: str = ""
    pool_version: str = ""
    pool_hash: str = ""
    pool_symbols: tuple[str, ...] = ()
    command_hash: str = ""
    final_request_hash: str = ""
    adaptive_leverage: str = ""
    adaptive_quantity: str = ""
    adaptive_notional: str = ""
    symbol: str = ""
    client_order_id: str = ""
    expected_method: str = ""
    expected_path: str = ""
    account_exposure: str = ""
    projected_account_exposure: str = ""
    order_id: str = ""


@dataclass(frozen=True, slots=True)
class TerminalWriteRequest:
    """Non-secret identity and quantity facts presented to an authority."""

    kind: TerminalWriteKind
    method: str
    path: str
    account_id: str
    symbol: str = ""
    client_order_id: str = ""
    order_id: str = ""
    algo_id: str = ""
    quantity: str = ""
    task_id: str = ""
    entrypoint: str = ""
    owner_id: str = ""
    generation: str = ""
    approval_id: str = ""
    expires_at: float = 0.0
    nonce: str = ""
    intent_id: str = ""
    position_id: str = ""
    dedicated_account: bool = False
    signed: bool = False
    venue_id: str = ""
    environment: str = ""
    rest_base_url: str = ""
    side: str = ""
    order_type: str = ""
    reduce_only: bool = False
    close_position: bool = False
    notional: str = ""
    leverage: str = ""
    pool_id: str = ""
    pool_version: str = ""
    pool_hash: str = ""
    pool_symbols: tuple[str, ...] = ()
    command_hash: str = ""
    final_request_hash: str = ""
    adaptive_leverage: str = ""
    adaptive_quantity: str = ""
    adaptive_notional: str = ""
    account_exposure: str = ""
    projected_account_exposure: str = ""


@dataclass(frozen=True, slots=True)
class TerminalWriteDecision:
    """Pure authorization decision; it must not consume a nonce or write."""

    allowed: bool
    reason_code: str


class TerminalWriteAuthority(Protocol):
    """Authority implementations must make a deterministic, side-effect-free decision."""

    def authorize(self, request: TerminalWriteRequest) -> TerminalWriteDecision: ...


def canonical_final_request_hash(
    method: str,
    path: str,
    params: dict[str, object] | None,
    *,
    account_id: str,
    command_hash: str = "",
    pool_id: str = "",
    pool_version: str = "",
    pool_hash: str = "",
    adaptive_leverage: str = "",
    adaptive_quantity: str = "",
    adaptive_notional: str = "",
) -> str:
    """Hash the exact pre-signature venue request material.

    Binance's HMAC authenticates the HTTP request to Binance, but it does not
    prove that the request was the one approved by Beidou.  This digest is
    deliberately calculated before ``timestamp``, ``recvWindow`` and
    ``signature`` are added.  The command/pool/adaptive fields are included
    explicitly because they are authorization facts rather than Binance
    transport parameters.
    """

    material = {
        "method": str(method).upper(),
        "path": str(path),
        "account_id": str(account_id),
        "params": {
            str(key): (normalize_quantity_string(value) if str(key) == "quantity" else value)
            for key, value in sorted((params or {}).items(), key=lambda item: str(item[0]))
        },
        "command_hash": str(command_hash),
        "pool_id": str(pool_id),
        "pool_version": str(pool_version),
        "pool_hash": str(pool_hash),
        "adaptive_leverage": str(adaptive_leverage),
        "adaptive_quantity": normalize_quantity_string(adaptive_quantity),
        "adaptive_notional": str(adaptive_notional),
    }
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def terminal_write_request_hash(request: TerminalWriteRequest) -> str:
    """Return the deterministic audit digest for a typed write request."""

    data = asdict(request)
    data["kind"] = request.kind.value
    encoded = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluate_terminal_write(
    authority: TerminalWriteAuthority | None,
    request: TerminalWriteRequest,
) -> TerminalWriteDecision:
    """Evaluate a terminal write without allowing UNKNOWN or broken authorities."""

    if request.kind is TerminalWriteKind.UNKNOWN:
        return TerminalWriteDecision(False, "UNCLASSIFIED_TERMINAL_WRITE")
    if authority is None:
        return TerminalWriteDecision(False, "WRITE_AUTHORITY_MISSING")
    required_scope = (
        request.task_id,
        request.entrypoint,
        request.owner_id,
        request.generation,
        request.approval_id,
        request.nonce,
    )
    if not all(str(value).strip() for value in required_scope):
        return TerminalWriteDecision(False, "WRITE_SCOPE_INCOMPLETE")
    if request.account_id.strip().upper() in {"", "UNKNOWN", "DEFAULT"} or not request.dedicated_account:
        return TerminalWriteDecision(False, "WRITE_ACCOUNT_NOT_DEDICATED")
    if not math.isfinite(request.expires_at):
        return TerminalWriteDecision(False, "WRITE_SCOPE_INVALID_EXPIRY")
    if request.expires_at <= time.time():
        return TerminalWriteDecision(False, "WRITE_SCOPE_EXPIRED")
    if (
        request.kind
        in {
            TerminalWriteKind.INCREASE,
            TerminalWriteKind.REDUCE_OWNED,
            TerminalWriteKind.EMERGENCY,
        }
        and request.path != "/fapi/v1/leverage"
        and (not request.intent_id or not request.quantity)
    ):
        return TerminalWriteDecision(False, "WRITE_OBJECT_SCOPE_INCOMPLETE")
    if request.path == "/fapi/v1/leverage" and (not request.intent_id or not request.leverage):
        return TerminalWriteDecision(False, "WRITE_OBJECT_SCOPE_INCOMPLETE")
    if request.kind is TerminalWriteKind.REDUCE_OWNED and not request.position_id:
        return TerminalWriteDecision(False, "WRITE_OBJECT_SCOPE_INCOMPLETE")
    if request.kind is TerminalWriteKind.CANCEL_OWNED and (
        not (request.order_id or request.algo_id)
        or not request.symbol
        or not request.quantity
        or not request.intent_id
        or not request.position_id
    ):
        return TerminalWriteDecision(False, "WRITE_OBJECT_SCOPE_INCOMPLETE")
    try:
        decision = authority.authorize(request)
    except Exception:
        return TerminalWriteDecision(False, "WRITE_AUTHORITY_ERROR")
    if (
        not isinstance(decision, TerminalWriteDecision)
        or type(decision.allowed) is not bool
        or not decision.reason_code
    ):
        return TerminalWriteDecision(False, "WRITE_AUTHORITY_INVALID_DECISION")
    return decision


__all__ = [
    "TerminalWriteAuthority",
    "TerminalWriteContext",
    "TerminalWriteDecision",
    "TerminalWriteKind",
    "TerminalWriteRequest",
    "canonical_final_request_hash",
    "evaluate_terminal_write",
    "terminal_write_request_hash",
]
