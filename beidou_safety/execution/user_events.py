"""Durable, fail-closed projection of normalized account user-stream events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from beidou_exchange.core.protocol import UserAccountUpdate, UserOrderUpdate
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
        owned_client_order_prefix: str | None = "beidou-",
    ) -> None:
        self._store = store
        self._account_id = account_id or AccountId("default")
        self._venue_id = venue_id or VenueId("BINANCE")
        self._sequencer = UserStreamSequencer()
        self._positions: dict[InstrumentId, Decimal] = {}
        self._balances: dict[str, MonetaryValue] = {}
        self._cumulative_by_order: dict[str, Decimal] = {}
        self._open_orders: set[str] = set()
        # M13 登记兑现: 订单参数明细(order_id → 参数),供三方对账
        # 参数级比较 —— 事件侧此前不携带 detail,detail_degraded 常态
        self._open_order_details: dict[str, dict[str, str]] = {}
        self._last_event_time_ms: int | None = None
        self._applied_event_ids: set[str] = set()
        self._frozen_reason: str | None = None
        self._replay_baseline_verified = False
        self._replay_evidence_hash = ""
        self._replay_approval_id = ""
        self._accepted_events_since_replay = 0
        # BD-FIX: open_orders 所有权过滤 —— 共享 demo 账户上其他用户的
        # 挂单（非 beidou- clientOrderId）不得污染事件侧 open_orders，
        # 否则三方对账 open_orders 严格集合比较恒 MISMATCH（I8 审查）。
        # None 表示不过滤（单用户账户语义）。positions 的 delta 投影
        # 保持全量（共享持仓变化是真实的共享事实）。
        self._owned_client_order_prefix = owned_client_order_prefix
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
        assert self._store is not None, "store required for projection restore"
        row = self._store.restore_user_stream_projection(str(self._account_id), str(self._venue_id))
        if row is None:
            return
        self._positions = {
            InstrumentId(str(symbol)): Decimal(str(amount)) for symbol, amount in dict(row.get("positions", {})).items()
        }
        balance_amount = row.get("balance_amount")
        balance_currency = row.get("balance_currency")
        if balance_amount not in (None, "") and balance_currency not in (None, ""):
            self._balances[str(balance_currency)] = MonetaryValue(
                amount=str(balance_amount),
                currency=str(balance_currency),
                decimals=int(row.get("balance_decimals", 8) or 8),
            )
        self._open_orders = {str(order_id) for order_id in row.get("open_orders", [])}
        self._last_event_time_ms = row.get("last_event_time_ms")
        last_sequence = row.get("last_sequence")
        if last_sequence is not None:
            self._sequencer.restore(int(last_sequence))

        assert self._store is not None
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
                assert self._store is not None
                self._store.mark_user_stream_event_applied(event_id)
                applied_state = "APPLIED"
            if applied_state == "APPLIED" and event_id:
                self._applied_event_ids.add(event_id)
            order_id = str(event.get("order_id", ""))
            cumulative = event.get("cumulative_quantity")
            if order_id and cumulative not in (None, ""):
                value = Decimal(str(cumulative))
                self._cumulative_by_order[order_id] = max(self._cumulative_by_order.get(order_id, Decimal("0")), value)

    @property
    def replay_baseline_verified(self) -> bool:
        return self._replay_baseline_verified

    def authorize_replay_baseline(
        self,
        facts: AccountFactSnapshot,
        *,
        evidence_hash: str,
        approval_id: str,
        last_sequence: int | None = None,
        allow_unsequenced: bool = False,
    ) -> UserProjectionResult:
        """Authorize an independently captured replay baseline.

        This is an explicit recovery boundary.  No REST call is made here;
        the caller must supply complete facts with provenance, evidence hash,
        and an operator/governance approval.  Binance account updates are
        commonly unsequenced, so that mode must be opted into explicitly.
        """

        if self._frozen_reason:
            return UserProjectionResult(UserProjectionStatus.BLOCKED, "REPLAY_BASELINE", reason=self._frozen_reason)
        if (
            facts.account_id != self._account_id
            or facts.venue_id != self._venue_id
            or not facts.complete
            or not str(facts.source).strip()
            or str(facts.source).strip().upper() == "UNKNOWN"
            or not str(facts.fact_version).strip()
            or str(facts.fact_version).strip().upper() == "UNKNOWN"
            or not str(evidence_hash).strip()
            or not str(approval_id).strip()
            or (last_sequence is None and not allow_unsequenced)
        ):
            return UserProjectionResult(
                UserProjectionStatus.BLOCKED,
                "REPLAY_BASELINE",
                reason="complete replay baseline, provenance, approval, and continuity mode are required",
            )
        try:
            if facts.timestamp.tzinfo is None or facts.timestamp > datetime.now(timezone.utc):
                raise ValueError("replay baseline timestamp must be aware and not in the future")
            positions = {
                InstrumentId(str(symbol)): Decimal(str(quantity.amount)) for symbol, quantity in facts.positions.items()
            }
            if any(not str(symbol).strip() for symbol in positions):
                raise ValueError("replay baseline contains empty position symbol")
            if any(not quantity.is_finite() for quantity in positions.values()):
                raise ValueError("replay baseline contains non-finite position")
            balance_amount = Decimal(str(facts.balance.amount))
            if not balance_amount.is_finite():
                raise ValueError("replay baseline contains non-finite balance")
            order_ids = [str(order_id).strip() for order_id in facts.open_orders]
            if any(not order_id for order_id in order_ids) or len(set(order_ids)) != len(order_ids):
                raise ValueError("replay baseline contains invalid open-order identity")

            self._positions = positions
            self._open_orders = set(order_ids)
            self._balances = {
                str(facts.balance.currency): facts.balance,
            }
            self._replay_baseline_verified = True
            self._replay_evidence_hash = str(evidence_hash)
            self._replay_approval_id = str(approval_id)
            self._accepted_events_since_replay = 0
            self._last_event_time_ms = int(facts.timestamp.timestamp() * 1000)
            self._sequencer.mark_replayed(
                last_sequence,
                allow_unsequenced=allow_unsequenced,
                last_event_time_ms=self._last_event_time_ms,
            )
            if self._store is not None:
                self._store.save_user_stream_projection(
                    self.fact_snapshot(),
                    last_sequence=self._sequencer.last_sequence,
                )
        except (InvalidOperation, TypeError, ValueError, RuntimeError) as exc:
            return self._freeze(f"replay baseline rejected: {type(exc).__name__}: {exc}")
        return UserProjectionResult(UserProjectionStatus.ACCEPTED, "REPLAY_BASELINE")

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
                    if row is None or str(row.get("applied_state")) != "PENDING":
                        raise RuntimeError("event insert rejected without a recoverable durable row")

            self._apply_order_update(update)
            self._last_event_time_ms = max(self._last_event_time_ms or 0, int(update.event.event_time_ms))
            if self._store is not None:
                self._store.save_user_stream_projection(
                    self.fact_snapshot(),
                    last_sequence=self._sequencer.last_sequence,
                )
                self._store.mark_user_stream_event_applied(event_id)
            self._applied_event_ids.add(event_id)
            self._accepted_events_since_replay += 1
        except (InvalidOperation, TypeError, ValueError, RuntimeError) as exc:
            # 逻辑错误（cumulative 回退/非有限值等）—— 投影状态已损坏，
            # freeze 是正确的 fail-closed。
            return self._freeze(f"user-stream projection failed for {event_id}: {type(exc).__name__}: {exc}")
        except Exception as exc:
            # BD-FIX: 存储层瞬时错误（sqlite 锁/连接抖动）不冻结投影器
            # —— freeze 无解冻路径，demo 一次抖动即永久拒绝事件流。
            # BLOCKED 由上层 env_guarded 处理（testnet NO_NEW_RISK
            # 可恢复），下一个事件重试即可。
            return UserProjectionResult(
                UserProjectionStatus.BLOCKED,
                event_id,
                reason=f"projection storage error: {type(exc).__name__}",
            )
        return UserProjectionResult(UserProjectionStatus.ACCEPTED, event_id, observation=observation)

    def ingest_account_update(self, update: UserAccountUpdate) -> UserProjectionResult:
        """Apply an ACCOUNT_UPDATE only after replay authorization."""

        event_id = str(update.event.event_id)
        if self._frozen_reason:
            return UserProjectionResult(UserProjectionStatus.BLOCKED, event_id, reason=self._frozen_reason)
        if not self._replay_baseline_verified:
            return UserProjectionResult(
                UserProjectionStatus.BLOCKED,
                event_id,
                reason="REPLAY_BASELINE_REQUIRED before applying account updates",
            )
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
                    if row is None or str(row.get("applied_state")) != "PENDING":
                        raise RuntimeError("account event insert rejected without a recoverable durable row")
            for balance in update.balances:
                self._balances[str(balance.asset)] = balance.wallet_balance
            for position in update.positions:
                amount = Decimal(str(position.position_amount.amount))
                if not amount.is_finite():
                    raise ValueError(f"non-finite account position: {position.symbol}")
                self._positions[InstrumentId(str(position.symbol))] = amount
            self._last_event_time_ms = max(self._last_event_time_ms or 0, int(update.event.event_time_ms))
            if self._store is not None:
                self._store.save_user_stream_projection(
                    self.fact_snapshot(),
                    last_sequence=self._sequencer.last_sequence,
                )
                self._store.mark_user_stream_event_applied(event_id)
            self._applied_event_ids.add(event_id)
            self._accepted_events_since_replay += 1
        except (InvalidOperation, TypeError, ValueError, RuntimeError) as exc:
            # 逻辑错误 → freeze（同 ingest 语义）
            return self._freeze(f"account user-stream projection failed for {event_id}: {type(exc).__name__}: {exc}")
        except Exception as exc:
            # BD-FIX: 存储层瞬时错误 → BLOCKED 可重试（同 ingest 语义）
            return UserProjectionResult(
                UserProjectionStatus.BLOCKED,
                event_id,
                reason=f"projection storage error: {type(exc).__name__}",
            )
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
        # BD-FIX: 只把属于引擎的挂单投进 open_orders（共享账户过滤）
        _owned = self._owned_client_order_prefix is None or str(update.client_order_id).startswith(
            self._owned_client_order_prefix
        )
        if _owned and update.order_status.value in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
            self._open_orders.add(order_id)
            # M13 登记兑现: 记录参数明细(原始 payload 补齐 reduceOnly/
            # stopPrice —— Binance fapi ORDER_TRADE_UPDATE 嵌套在 o.* )
            _raw: Any = getattr(getattr(update, "event", None), "raw_event", None)
            _raw = _raw if isinstance(_raw, dict) else {}
            _nested = _raw.get("o") if isinstance(_raw.get("o"), dict) else {}

            def _raw_opt(top_key: str, nested_key: str) -> str:
                value = _raw.get(top_key)
                if value is None:
                    value = _nested.get(nested_key)
                return str(value) if value is not None else ""

            self._open_order_details[order_id] = {
                "symbol": str(symbol),
                "side": str(update.side.value),
                "qty": str(update.original_quantity.amount),
                "price": str(update.last_price.amount) if update.last_price else "",
                "type": str(update.order_type.value),
                "reduce_only": _raw_opt("reduceOnly", "R"),
                "stop_price": _raw_opt("stopPrice", "sp"),
            }
        else:
            self._open_orders.discard(order_id)
            self._open_order_details.pop(order_id, None)

    def fact_snapshot(self) -> AccountFactSnapshot:
        timestamp = (
            datetime.fromtimestamp(self._last_event_time_ms / 1000, tz=timezone.utc)
            if self._last_event_time_ms is not None
            else datetime.now(timezone.utc)
        )
        balance = self._balances.get("USDT", MonetaryValue(amount="0", currency="USDT"))
        # BD-FIX: complete 不再要求 ≥1 个事件。replay baseline 授权本身即
        # REST 快照独立验证 + listenKey 连接建立，足以作为事件流投影的
        # 完整基线；低频环境（demo 凌晨无活动）等待首个事件会饿死启动
        # 自检（三方 MATCHED 要求 complete）。事件停流的保护不削弱——
        # 运行时 readiness 的 event_age（300s 阈值）仍会在停流后降级。
        complete = self._replay_baseline_verified and "USDT" in self._balances
        source = "BINANCE_USER_STREAM_REPLAY_BASELINE" if complete else "BINANCE_USER_STREAM_ORDER_EVENTS_OR_UNVERIFIED"
        return AccountFactSnapshot(
            account_id=self._account_id,
            venue_id=self._venue_id,
            balance=balance,
            positions={symbol: Quantity(amount=_decimal_text(amount)) for symbol, amount in self._positions.items()},
            open_orders=sorted(self._open_orders),
            # M13 登记兑现: 参数明细随快照携带(两侧齐全时三方对账
            # 参数级比较生效,detail_degraded 仅剩恢复路径)
            open_orders_detail={
                order_id: dict(detail)
                for order_id, detail in self._open_order_details.items()
                if order_id in self._open_orders
            },
            timestamp=timestamp,
            source=source,
            fact_version=(
                f"user-stream-v2/replay:{self._replay_evidence_hash}/approval:{self._replay_approval_id}/"
                f"sequence:{self._sequencer.last_sequence}"
            ),
            complete=complete,
        )


__all__ = ["UserProjectionResult", "UserProjectionStatus", "UserStreamProjector"]
