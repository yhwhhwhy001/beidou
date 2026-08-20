"""Durable parent/child execution command contracts.

The module contains no venue or database side effects.  It defines the exact
state and arithmetic that persistence adapters and the engine must share.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable


class ChildCommandState(str, Enum):
    PLANNED = "PLANNED"
    SENDING = "SENDING"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ParentExecutionState(str, Enum):
    PLANNED = "PLANNED"
    IN_FLIGHT = "IN_FLIGHT"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


_TERMINAL_CHILD_STATES = {
    ChildCommandState.FILLED,
    ChildCommandState.CANCELED,
    ChildCommandState.REJECTED,
}

_ACKNOWLEDGED_CHILD_STATES = {
    ChildCommandState.ACKED,
    ChildCommandState.PARTIALLY_FILLED,
    ChildCommandState.FILLED,
    ChildCommandState.CANCELED,
}

_CHILD_TRANSITIONS: dict[ChildCommandState, set[ChildCommandState]] = {
    ChildCommandState.PLANNED: {ChildCommandState.SENDING, ChildCommandState.REJECTED},
    ChildCommandState.SENDING: {
        ChildCommandState.ACKED,
        ChildCommandState.PARTIALLY_FILLED,
        ChildCommandState.FILLED,
        ChildCommandState.CANCELED,
        ChildCommandState.REJECTED,
        ChildCommandState.UNKNOWN,
    },
    ChildCommandState.ACKED: {
        ChildCommandState.PARTIALLY_FILLED,
        ChildCommandState.FILLED,
        ChildCommandState.CANCELED,
        ChildCommandState.UNKNOWN,
    },
    ChildCommandState.PARTIALLY_FILLED: {
        ChildCommandState.PARTIALLY_FILLED,
        ChildCommandState.FILLED,
        ChildCommandState.CANCELED,
        ChildCommandState.UNKNOWN,
    },
    ChildCommandState.UNKNOWN: {
        ChildCommandState.ACKED,
        ChildCommandState.PARTIALLY_FILLED,
        ChildCommandState.FILLED,
        ChildCommandState.CANCELED,
        ChildCommandState.REJECTED,
        # Governed resend only: the recovery path transitions an UNKNOWN child
        # back to SENDING after a definitive venue-absence fact (Binance -2013
        # by exact clientOrderId) proves no order was ever created.  Without
        # this edge, a transient transport failure on one slice would leave
        # the whole multi-slice plan permanently UNKNOWN and never resendable.
        ChildCommandState.SENDING,
    },
    ChildCommandState.FILLED: set(),
    ChildCommandState.CANCELED: set(),
    ChildCommandState.REJECTED: set(),
}


def _positive_decimal(value: str | Decimal, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


def _nonnegative_decimal(value: str | Decimal, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return result


@dataclass(frozen=True)
class ExecutionChildCommand:
    parent_intent_id: str
    sequence: int
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    time_in_force: str
    client_order_id: str
    limit_price: Decimal | None = None
    reduce_only: bool = False
    rule_snapshot_hash: str = ""
    command_hash: str = ""
    state: ChildCommandState = ChildCommandState.PLANNED
    exchange_order_id: str = ""
    filled_quantity: Decimal = Decimal("0")
    event_ids: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        parent_intent_id: str,
        sequence: int,
        symbol: str,
        side: str,
        quantity: str | Decimal,
        order_type: str,
        time_in_force: str,
        client_order_id: str,
        limit_price: str | Decimal | None = None,
        reduce_only: bool = False,
        rule_snapshot_hash: str = "",
    ) -> ExecutionChildCommand:
        side_value = str(side).upper()
        if side_value not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if sequence < 0:
            raise ValueError("sequence must be nonnegative")
        if not parent_intent_id or not symbol or not client_order_id:
            raise ValueError("parent, symbol and client order identity are required")
        quantity_value = _positive_decimal(quantity, field="quantity")
        price_value = None if limit_price in (None, "") else _positive_decimal(limit_price, field="limit_price")
        economic: dict[str, Any] = {
            "parent_intent_id": str(parent_intent_id),
            "sequence": int(sequence),
            "symbol": str(symbol).upper(),
            "side": side_value,
            "quantity": str(quantity_value),
            "order_type": str(order_type).upper(),
            "time_in_force": str(time_in_force).upper(),
            "client_order_id": str(client_order_id),
            "limit_price": str(price_value) if price_value is not None else None,
            "reduce_only": bool(reduce_only),
            "rule_snapshot_hash": str(rule_snapshot_hash),
        }
        command_hash = hashlib.sha256(json.dumps(economic, sort_keys=True).encode()).hexdigest()
        return cls(
            parent_intent_id=economic["parent_intent_id"],
            sequence=economic["sequence"],
            symbol=economic["symbol"],
            side=economic["side"],
            quantity=quantity_value,
            order_type=economic["order_type"],
            time_in_force=economic["time_in_force"],
            client_order_id=economic["client_order_id"],
            limit_price=price_value,
            reduce_only=economic["reduce_only"],
            rule_snapshot_hash=economic["rule_snapshot_hash"],
            command_hash=command_hash,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "parent_intent_id": self.parent_intent_id,
            "sequence": self.sequence,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "client_order_id": self.client_order_id,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "reduce_only": self.reduce_only,
            "rule_snapshot_hash": self.rule_snapshot_hash,
            "command_hash": self.command_hash,
            "state": self.state.value,
            "exchange_order_id": self.exchange_order_id,
            "filled_quantity": str(self.filled_quantity),
            "event_ids": list(self.event_ids),
        }

    def has_valid_economic_hash(self) -> bool:
        canonical = self.create(
            parent_intent_id=self.parent_intent_id,
            sequence=self.sequence,
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            order_type=self.order_type,
            time_in_force=self.time_in_force,
            client_order_id=self.client_order_id,
            limit_price=self.limit_price,
            reduce_only=self.reduce_only,
            rule_snapshot_hash=self.rule_snapshot_hash,
        )
        return self.command_hash == canonical.command_hash

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ExecutionChildCommand:
        command = cls.create(
            parent_intent_id=str(payload["parent_intent_id"]),
            sequence=int(payload["sequence"]),
            symbol=str(payload["symbol"]),
            side=str(payload["side"]),
            quantity=str(payload["quantity"]),
            order_type=str(payload["order_type"]),
            time_in_force=str(payload["time_in_force"]),
            client_order_id=str(payload["client_order_id"]),
            limit_price=payload.get("limit_price"),
            reduce_only=bool(payload.get("reduce_only", False)),
            rule_snapshot_hash=str(payload.get("rule_snapshot_hash", "")),
        )
        stored_hash = str(payload.get("command_hash", ""))
        if stored_hash and stored_hash != command.command_hash:
            raise ValueError("EXECUTION_COMMAND_HASH_MISMATCH")
        filled = _nonnegative_decimal(payload.get("filled_quantity", "0"), field="filled_quantity")
        if filled > command.quantity:
            raise ValueError("FILLED_QUANTITY_EXCEEDS_COMMAND")
        return replace(
            command,
            state=ChildCommandState(str(payload.get("state", ChildCommandState.PLANNED.value))),
            exchange_order_id=str(payload.get("exchange_order_id", "")),
            filled_quantity=filled,
            event_ids=tuple(str(event_id) for event_id in payload.get("event_ids", [])),
        )

    @property
    def recovery_action(self) -> str:
        if self.state is not ChildCommandState.UNKNOWN:
            return "NONE"
        return f"QUERY_BY_CLIENT_ID:{self.client_order_id}"

    @property
    def signed_remaining_quantity(self) -> Decimal:
        if self.state in {ChildCommandState.CANCELED, ChildCommandState.REJECTED, ChildCommandState.FILLED}:
            return Decimal("0")
        remaining = max(Decimal("0"), self.quantity - self.filled_quantity)
        return remaining if self.side == "BUY" else -remaining

    def transition(
        self,
        state: ChildCommandState,
        *,
        event_id: str,
        exchange_order_id: str = "",
        cumulative_filled_quantity: str | Decimal | None = None,
    ) -> ExecutionChildCommand:
        if not event_id:
            raise ValueError("event_id is required")
        if event_id in self.event_ids:
            return self
        if self.state in _TERMINAL_CHILD_STATES and state is not self.state:
            raise ValueError(f"TERMINAL_CHILD_STATE:{self.state.value}")
        if state is not self.state and state not in _CHILD_TRANSITIONS[self.state]:
            raise ValueError(f"INVALID_CHILD_TRANSITION:{self.state.value}->{state.value}")

        filled = self.filled_quantity
        if cumulative_filled_quantity is not None:
            candidate = _nonnegative_decimal(cumulative_filled_quantity, field="cumulative_filled_quantity")
            if candidate < filled:
                raise ValueError("FILLED_QUANTITY_REGRESSION")
            if candidate > self.quantity:
                raise ValueError("FILLED_QUANTITY_EXCEEDS_COMMAND")
            filled = candidate
        if (
            state is ChildCommandState.FILLED
            and filled != self.quantity
            and not (self.reduce_only and filled > 0)
        ):
            # BD-FIX (venue-capped reduce-only close): 交易所对 reduce-only
            # 平仓单按剩余持仓截断(origQty < 请求量)后回报 FILLED ——
            # 实测 LTCUSDT 紧急平仓本地计划 1.100、venue 实际成交 0.109。
            # 按"满量成交"守卫拒绝会把子命令永久钉在 UNKNOWN:幽灵扫描/
            # 监控投影每次收敛都抛 FILLED_STATE_REQUIRES_FULL_QUANTITY,
            # 在途量(quantity-filled)永久计入 inflight,把新订单挤到
            # 交易所最小下单量门槛之外。截断 FILLED 是终态 venue 事实,
            # 余量对 reduce-only 禁止重发(恒 -2022),必须收敛。
            raise ValueError("FILLED_STATE_REQUIRES_FULL_QUANTITY")
        if state is ChildCommandState.PARTIALLY_FILLED and not (Decimal("0") < filled < self.quantity):
            raise ValueError("PARTIAL_STATE_REQUIRES_PARTIAL_QUANTITY")

        order_id = exchange_order_id or self.exchange_order_id
        if state in _ACKNOWLEDGED_CHILD_STATES and not order_id:
            raise ValueError("ACKNOWLEDGED_CHILD_REQUIRES_EXCHANGE_ORDER_ID")
        return replace(
            self,
            state=state,
            exchange_order_id=order_id,
            filled_quantity=filled,
            event_ids=(*self.event_ids, event_id),
        )


@dataclass(frozen=True)
class ParentExecutionAggregate:
    intent_id: str
    children: tuple[ExecutionChildCommand, ...]

    @classmethod
    def create(cls, intent_id: str, children: Iterable[ExecutionChildCommand]) -> ParentExecutionAggregate:
        child_tuple = tuple(sorted(children, key=lambda child: child.sequence))
        if not intent_id or not child_tuple:
            raise ValueError("parent intent and at least one child are required")
        if any(child.parent_intent_id != intent_id for child in child_tuple):
            raise ValueError("CHILD_PARENT_MISMATCH")
        sequences = [child.sequence for child in child_tuple]
        if sequences != list(range(len(child_tuple))):
            raise ValueError("CHILD_SEQUENCE_MUST_BE_CONTIGUOUS")
        if len({child.client_order_id for child in child_tuple}) != len(child_tuple):
            raise ValueError("DUPLICATE_CHILD_CLIENT_ORDER_ID")
        if not all(child.has_valid_economic_hash() for child in child_tuple):
            raise ValueError("EXECUTION_COMMAND_HASH_MISMATCH")
        return cls(intent_id=intent_id, children=child_tuple)

    @property
    def state(self) -> ParentExecutionState:
        states = {child.state for child in self.children}
        if ChildCommandState.UNKNOWN in states:
            return ParentExecutionState.UNKNOWN
        if ChildCommandState.REJECTED in states:
            return ParentExecutionState.FAILED
        if states == {ChildCommandState.PLANNED}:
            return ParentExecutionState.PLANNED
        if states == {ChildCommandState.FILLED}:
            return ParentExecutionState.FILLED
        if states == {ChildCommandState.CANCELED}:
            return ParentExecutionState.CANCELED
        if all(state in _ACKNOWLEDGED_CHILD_STATES for state in states):
            if ChildCommandState.PARTIALLY_FILLED in states or ChildCommandState.FILLED in states:
                return ParentExecutionState.PARTIALLY_FILLED
            return ParentExecutionState.ACKED
        return ParentExecutionState.IN_FLIGHT

    @property
    def all_children_acknowledged(self) -> bool:
        return all(child.state in _ACKNOWLEDGED_CHILD_STATES for child in self.children)

    @property
    def signed_remaining_quantity(self) -> Decimal:
        return sum((child.signed_remaining_quantity for child in self.children), Decimal("0"))

    def transition_child(
        self,
        sequence: int,
        state: ChildCommandState,
        *,
        event_id: str,
        exchange_order_id: str = "",
        cumulative_filled_quantity: str | Decimal | None = None,
    ) -> ParentExecutionAggregate:
        if sequence < 0 or sequence >= len(self.children):
            raise ValueError("UNKNOWN_CHILD_SEQUENCE")
        updated = list(self.children)
        updated[sequence] = updated[sequence].transition(
            state,
            event_id=event_id,
            exchange_order_id=exchange_order_id,
            cumulative_filled_quantity=cumulative_filled_quantity,
        )
        return ParentExecutionAggregate(intent_id=self.intent_id, children=tuple(updated))


@dataclass(frozen=True)
class TargetDeltaPlan:
    symbol: str
    target_quantity: Decimal
    current_quantity: Decimal
    inflight_quantity: Decimal
    delta_quantity: Decimal
    side: str | None
    order_quantity: Decimal

    @classmethod
    def compute(
        cls,
        *,
        symbol: str,
        target_quantity: str | Decimal,
        current_quantity: str | Decimal,
        inflight_quantity: str | Decimal = "0",
    ) -> TargetDeltaPlan:
        try:
            target = Decimal(str(target_quantity))
            current = Decimal(str(current_quantity))
            inflight = Decimal(str(inflight_quantity))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("target quantities must be decimals") from exc
        if not all(value.is_finite() for value in (target, current, inflight)):
            raise ValueError("target quantities must be finite")
        delta = target - current - inflight
        side = "BUY" if delta > 0 else ("SELL" if delta < 0 else None)
        return cls(
            symbol=str(symbol).upper(),
            target_quantity=target,
            current_quantity=current,
            inflight_quantity=inflight,
            delta_quantity=delta,
            side=side,
            order_quantity=abs(delta),
        )
