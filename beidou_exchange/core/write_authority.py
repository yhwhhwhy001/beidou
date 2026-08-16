"""Fail-closed authority contract for exchange terminal writes.

The transport boundary must never infer that a cancel, reduce-only order, or
emergency action is safe merely from its HTTP method or flags.  A caller must
provide an authority implementation that evaluates a typed request.  Missing,
invalid, or failing authorities deny the write without touching the transport.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


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


@dataclass(frozen=True, slots=True)
class TerminalWriteDecision:
    """Pure authorization decision; it must not consume a nonce or write."""

    allowed: bool
    reason_code: str


class TerminalWriteAuthority(Protocol):
    """Authority implementations must make a deterministic, side-effect-free decision."""

    def authorize(self, request: TerminalWriteRequest) -> TerminalWriteDecision: ...


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
    if request.kind in {
        TerminalWriteKind.INCREASE,
        TerminalWriteKind.REDUCE_OWNED,
        TerminalWriteKind.EMERGENCY,
    } and (not request.intent_id or not request.quantity):
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
