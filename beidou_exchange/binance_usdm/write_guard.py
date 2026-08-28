"""Binance-specific terminal-write classification at the transport boundary."""

from __future__ import annotations

from typing import Any

from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.core.write_authority import (
    TerminalWriteContext,
    TerminalWriteKind,
    TerminalWriteRequest,
    canonical_final_request_hash,
)


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
    signed: bool = False,
    rest_base_url: str = "",
) -> TerminalWriteRequest | None:
    """Return a typed terminal write, ``None`` only for read-only requests.

    Unknown mutating endpoints deliberately produce ``UNKNOWN`` so the shared
    authority evaluator rejects them. Listen-key lifecycle mutates venue
    session state and therefore remains held behind its own capability class.
    """

    method_upper = str(method).upper()
    if method_upper not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    context = context or TerminalWriteContext("", "", "", "", "", 0.0, "")
    if path == Endpoint.LISTEN_KEY:
        kind = TerminalWriteKind.SESSION_CONTROL
        values = dict(params or {})
        command_hash = str(values.get("_command_hash") or context.command_hash or "")
        final_request_hash = str(values.get("_final_request_hash") or context.final_request_hash or "")
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
            signed=signed,
            venue_id=context.venue_id,
            environment=context.environment,
            rest_base_url=rest_base_url or context.rest_base_url,
            pool_id=context.pool_id,
            pool_version=context.pool_version,
            pool_hash=context.pool_hash,
            pool_symbols=context.pool_symbols,
            command_hash=command_hash,
            final_request_hash=final_request_hash,
            adaptive_leverage=context.adaptive_leverage,
            adaptive_quantity=context.adaptive_quantity,
            adaptive_notional=context.adaptive_notional,
        )

    values = dict(params or {})
    command_hash = str(values.get("_command_hash") or context.command_hash or "")
    adaptive_leverage = str(values.get("_adaptive_leverage") or context.adaptive_leverage or "")
    adaptive_quantity = str(values.get("_adaptive_quantity") or context.adaptive_quantity or "")
    adaptive_notional = str(values.get("_adaptive_notional") or context.adaptive_notional or "")
    final_request_hash = canonical_final_request_hash(
        method_upper,
        str(path),
        {key: value for key, value in values.items() if not str(key).startswith("_")},
        account_id=str(account_id or "UNKNOWN"),
        command_hash=command_hash,
        pool_id=context.pool_id,
        pool_version=context.pool_version,
        pool_hash=context.pool_hash,
        adaptive_leverage=adaptive_leverage,
        adaptive_quantity=adaptive_quantity,
        adaptive_notional=adaptive_notional,
    )
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
        signed=signed,
        venue_id=context.venue_id,
        environment=context.environment,
        rest_base_url=rest_base_url or context.rest_base_url,
        side=str(values.get("side") or context.side or ""),
        order_type=str(values.get("type") or context.order_type or ""),
        reduce_only=_enabled(values.get("reduceOnly")) or context.reduce_only,
        close_position=_enabled(values.get("closePosition")) or context.close_position,
        notional=str(values.get("notional") or context.notional or ""),
        leverage=str(values.get("leverage") or context.leverage or ""),
        pool_id=str(values.get("_pool_id") or context.pool_id or ""),
        pool_version=str(values.get("_pool_version") or context.pool_version or ""),
        pool_hash=str(values.get("_pool_hash") or context.pool_hash or ""),
        pool_symbols=context.pool_symbols,
        command_hash=command_hash,
        final_request_hash=final_request_hash,
        adaptive_leverage=adaptive_leverage,
        adaptive_quantity=adaptive_quantity,
        adaptive_notional=adaptive_notional,
        account_exposure=context.account_exposure,
        projected_account_exposure=context.projected_account_exposure,
    )
