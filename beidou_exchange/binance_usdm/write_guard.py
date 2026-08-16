"""Binance-specific terminal-write classification at the transport boundary."""

from __future__ import annotations

from typing import Any

from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.core.write_authority import TerminalWriteContext, TerminalWriteKind, TerminalWriteRequest


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def classify_terminal_write(
    method: str,
    path: str,
    params: dict[str, Any] | None,
    *,
    account_id: str,
    context: TerminalWriteContext | None = None,
) -> TerminalWriteRequest | None:
    """Return a typed terminal write, ``None`` only for read-only requests.

    Unknown mutating endpoints deliberately produce ``UNKNOWN`` so the shared
    authority evaluator rejects them. Listen-key lifecycle mutates venue
    session state and therefore remains held behind its own capability class.
    """

    method_upper = str(method).upper()
    if method_upper not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if path == Endpoint.LISTEN_KEY:
        kind = TerminalWriteKind.SESSION_CONTROL
        values = dict(params or {})
        context = context or TerminalWriteContext("", "", "", "", "", 0.0, "")
        return TerminalWriteRequest(
            kind=kind,
            method=method_upper,
            path=str(path),
            account_id=str(account_id or "UNKNOWN"),
            task_id=context.task_id,
            entrypoint=context.entrypoint,
            owner_id=context.owner_id,
            generation=context.generation,
            approval_id=context.approval_id,
            expires_at=context.expires_at,
            nonce=context.nonce,
            intent_id=str(values.get("listenKey") or context.intent_id or ""),
            dedicated_account=context.dedicated_account,
        )

    values = dict(params or {})
    if path in {Endpoint.ORDER, Endpoint.ALGO_ORDER}:
        if method_upper == "DELETE":
            kind = TerminalWriteKind.CANCEL_OWNED
        elif _enabled(values.get("reduceOnly")) or _enabled(values.get("closePosition")):
            kind = TerminalWriteKind.REDUCE_OWNED
        else:
            kind = TerminalWriteKind.INCREASE
    elif path == Endpoint.LEVERAGE:
        kind = TerminalWriteKind.INCREASE
    else:
        kind = TerminalWriteKind.UNKNOWN

    context = context or TerminalWriteContext("", "", "", "", "", 0.0, "")
    return TerminalWriteRequest(
        kind=kind,
        method=method_upper,
        path=str(path),
        account_id=str(account_id or "UNKNOWN"),
        symbol=str(values.get("symbol") or ""),
        client_order_id=str(values.get("newClientOrderId") or values.get("clientAlgoId") or ""),
        order_id=str(values.get("orderId") or ""),
        algo_id=str(values.get("algoId") or ""),
        quantity=str(values.get("quantity") or context.quantity or ""),
        task_id=context.task_id,
        entrypoint=context.entrypoint,
        owner_id=context.owner_id,
        generation=context.generation,
        approval_id=context.approval_id,
        expires_at=context.expires_at,
        nonce=context.nonce,
        intent_id=context.intent_id,
        position_id=context.position_id,
        dedicated_account=context.dedicated_account,
    )
