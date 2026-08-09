"""Durable, fail-closed projection of normalized account user-stream events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from beidou_exchange.core.protocol import UserOrderUpdate
from beidou_exchange.core.user_stream import UserStreamObservation, UserStreamSequencer
from beidou_safety.execution.reconciliation import AccountFactSnapshot
from beidou_shared.types import AccountId, InstrumentId, MonetaryValue, Quantity, VenueId


class UserProjectionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


@dataclass(frozen=True, slots=True)
class UserProjectionResult:
    status: UserProjectionStatus
    event_id: str
    observation: UserStreamObservation | None = None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.status is UserProjectionStatus.ACCEPTED


class UserStreamProjector:
    """Project only contiguous, durable user events into account facts.

    Binance user events do not themselves guarantee a complete account
    balance snapshot.  Consequently ``fact_snapshot().complete`` stays false
    until an independently captured balance/position baseline is explicitly
    supplied; the reconciler must not infer it from order events.
    """

    def __init__(
        self,
        *,
        store: Any | None = None,
        account_id: AccountId | None = None,
        venue_id: VenueId | None = None,
    ) -> None:
        self._store = store
        self._account_id = account_id or AccountId("default")
        self._venue_id = venue_id or VenueId("BINANCE")
        self._sequencer = UserStreamSequencer()
        self._positions: dict[InstrumentId, Decimal] = {}
        self._cumulative_by_order: dict[str, Decimal] = {}
        self._open_orders: set[str] = set()
        self._last_event_time_ms: int | None = None
        self._applied_event_ids: set[str] = set()
        self._frozen_reason: str | None = None
        if store is not None:
            self._restore_projection()

    @property
    def sequencer(self) -> UserStreamSequencer:
        return self._sequencer

    @property
    def frozen_reason(self) -> str | None:
        return self._frozen_reason

    def _freeze(self, reason: str) -> UserProjectionResult:
        self._frozen_reason = reason
        return UserProjectionResult(UserProjectionStatus.ERROR, "", reason=reason)

    def _restore_projection(self) -> None:
        row = self._store.restore_user_stream_projection(str(self._account_id), str(self._venue_id))
        if row is None:
            return
        self._positions = {
            InstrumentId(str(symbol)): Decimal(str(amount))
            for symbol, amount in dict(row.get("positions", {})).items()
        }
        self._open_orders = {str(order_id) for order_id in row.get("open_orders", [])}
        self._last_event_time_ms = row.get("last_event_time_ms")
        last_sequence = row.get("last_sequence")
        if last_sequence is not None:
            self._sequencer.restore(int(last_sequence))

        all_events = self._store.restore_user_stream_events(applied_only=False)
        for event in all_events:
            event_id = str(event.get("event_id", ""))
            applied_state = str(event.get("applied_state", ""))
            pending_already_projected = (
                applied_state == "PENDING"
                and self._last_event_time_ms is not None
                and int(event.get("event_time_ms", 0) or 0) <= self._last_event_time_ms
                and (
                    event.get("sequence") is None
                    or self._sequencer.last_sequence is None
                    or int(event["sequence"]) <= self._sequencer.last_sequence
                )
            )
            if pending_already_projected and event_id:
                self._store.mark_user_stream_event_applied(event_id)
                applied_state = "APPLIED"
            if applied_state == "APPLIED" and event_id:
                self._applied_event_ids.add(event_id)
            order_id = str(event.get("order_id", ""))
            cumulative = event.get("cumulative_quantity")
            if order_id and cumulative not in (None, ""):
                value = Decimal(str(cumulative))
                self._cumulative_by_order[order_id] = max(
                    self._cumulative_by_order.get(order_id, Decimal("0")), value
                )

    def ingest(self, update: UserOrderUpdate) -> UserProjectionResult:
        """Durably append, project, then mark one event applied."""

        event_id = str(update.event.event_id)
        if self._frozen_reason:
            return UserProjectionResult(UserProjectionStatus.BLOCKED, event_id, reason=self._frozen_reason)
        if event_id in self._applied_event_ids:
            return UserProjectionResult(UserProjectionStatus.DUPLICATE, event_id, reason="event already applied")

        observation = self._sequencer.observe(update.event)
        if not observation.accepted:
            return UserProjectionResult(
                UserProjectionStatus.BLOCKED,
                event_id,
                observation=observation,
                reason=observation.reason,
            )

        try:
            if self._store is not None:
                inserted = self._store.save_user_stream_event(
                    update,
                    continuity_status=observation.status.value,
                )
                if not inserted:
                    row = self._store.get_user_stream_event(event_id)
                    if row is not None and str(row.get("applied_state")) == "APPLIED":
                        self._applied_event_ids.add(event_id)
                        return UserProjectionResult(
                            UserProjectionStatus.DUPLICATE,
                            event_id,
                            observation=observation,
                            reason="event already durable and applied",
                        )

            self._apply_order_update(update)
            self._last_event_time_ms = max(self._last_event_time_ms or 0, int(update.event.event_time_ms))
            if self._store is not None:
                self._store.save_user_stream_projection(
                    self.fact_snapshot(),
                    last_sequence=self._sequencer.last_sequence,
                )
                self._store.mark_user_stream_event_applied(event_id)
            self._applied_event_ids.add(event_id)
        except (InvalidOperation, TypeError, ValueError, RuntimeError) as exc:
            return self._freeze(f"user-stream projection failed for {event_id}: {type(exc).__name__}: {exc}")
        return UserProjectionResult(UserProjectionStatus.ACCEPTED, event_id, observation=observation)

    def _apply_order_update(self, update: UserOrderUpdate) -> None:
        order_id = str(update.order_id)
        symbol = InstrumentId(str(update.symbol))
        cumulative = Decimal(str(update.cumulative_quantity.amount))
        previous = self._cumulative_by_order.get(order_id, Decimal("0"))
        if cumulative < previous:
            raise ValueError(f"cumulative quantity regressed for order {order_id}")
        delta = cumulative - previous
        if delta:
            signed_delta = delta if update.side.value == "BUY" else -delta
            self._positions[symbol] = self._positions.get(symbol, Decimal("0")) + signed_delta
        self._cumulative_by_order[order_id] = max(previous, cumulative)
        if update.order_status.value in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
            self._open_orders.add(order_id)
        else:
            self._open_orders.discard(order_id)

    def fact_snapshot(self) -> AccountFactSnapshot:
        timestamp = (
            datetime.fromtimestamp(self._last_event_time_ms / 1000, tz=timezone.utc)
            if self._last_event_time_ms is not None
            else datetime.now(timezone.utc)
        )
        return AccountFactSnapshot(
            account_id=self._account_id,
            venue_id=self._venue_id,
            # Order events do not prove wallet balance.  Zero is a placeholder
            # with complete=False, never an account balance assertion.
            balance=MonetaryValue(amount="0", currency="USDT"),
            positions={symbol: Quantity(amount=_decimal_text(amount)) for symbol, amount in self._positions.items()},
            open_orders=sorted(self._open_orders),
            timestamp=timestamp,
            source="BINANCE_USER_STREAM_ORDER_EVENTS",
            fact_version=f"user-stream-v1/sequence:{self._sequencer.last_sequence}",
            complete=False,
        )


__all__ = ["UserProjectionResult", "UserProjectionStatus", "UserStreamProjector"]
