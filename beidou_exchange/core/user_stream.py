"""Fail-closed user-stream sequencing and replay boundary.

Binance Futures user-data messages expose event/transaction timestamps but do
not provide a universally monotonic sequence for every account event.  A
reconnect therefore cannot be treated as lossless merely because the socket
is connected.  This module keeps that uncertainty explicit and requires an
independent REST replay/reconciliation step before the stream is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from beidou_exchange.core.protocol import UserStreamEvent


class UserStreamStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DUPLICATE = "DUPLICATE"
    GAP = "GAP"
    SEQUENCE_UNAVAILABLE = "SEQUENCE_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class UserStreamObservation:
    accepted: bool
    status: UserStreamStatus
    previous_sequence: int | None
    observed_sequence: int | None
    reason: str = ""


class UserStreamSequencer:
    """Track a monotonic event sequence when the venue actually supplies one."""

    def __init__(self) -> None:
        self._last_sequence: int | None = None
        self._status = UserStreamStatus.UNKNOWN
        self._unsequenced_allowed = False
        self._last_event_time_ms: int | None = None

    @property
    def last_sequence(self) -> int | None:
        return self._last_sequence

    @property
    def status(self) -> UserStreamStatus:
        return self._status

    def observe(self, event: UserStreamEvent) -> UserStreamObservation:
        """Accept only the next exact sequence; reject gaps and duplicates."""

        previous = self._last_sequence
        if self._status in {UserStreamStatus.GAP, UserStreamStatus.SEQUENCE_UNAVAILABLE}:
            return UserStreamObservation(
                False,
                self._status,
                previous,
                event.sequence,
                "explicit independent replay is required before accepting more events",
            )
        if event.sequence is None:
            if self._unsequenced_allowed:
                # BD-FIX: unsequenced 模式按到达顺序接受，不做跨时钟源
                # 时间比较。交易所事件时间（venue 时钟）与本机基线时间
                # 相比不可靠 —— demo 服务器时钟落后即把真实成交事件判
                # DUPLICATE 拒绝（14:11 实测）。真重复由投影器 event_id
                # 去重拦截，不受影响。
                self._last_event_time_ms = event.event_time_ms
                self._status = UserStreamStatus.HEALTHY
                return UserStreamObservation(True, self._status, previous, None)
            # BD-FIX (TRADE_LITE): 轻量用户流事件(TRADE_LITE)不携带
            # 序号,且与有序的 ORDER_TRADE_UPDATE 混排。拒绝本事件即可,
            # 不得把状态翻成 SEQUENCE_UNAVAILABLE —— 该状态是粘性的,
            # 一次无序号事件会让整条流后续所有有序事件全部被拒(需要
            # 独立 replay 才能恢复)。
            return UserStreamObservation(
                accepted=False,
                status=self._status,
                previous_sequence=previous,
                observed_sequence=None,
                reason="venue event has no monotonic sequence; REST replay required",
            )
        if previous is None:
            self._last_sequence = event.sequence
            self._last_event_time_ms = event.event_time_ms
            self._status = UserStreamStatus.HEALTHY
            return UserStreamObservation(True, self._status, None, event.sequence)
        if event.sequence <= previous:
            self._status = UserStreamStatus.DUPLICATE
            return UserStreamObservation(
                False,
                self._status,
                previous,
                event.sequence,
                "event sequence is not greater than the last accepted sequence",
            )
        if event.sequence != previous + 1:
            self._status = UserStreamStatus.GAP
            return UserStreamObservation(
                False,
                self._status,
                previous,
                event.sequence,
                "event sequence gap; REST replay/reconciliation required",
            )
        self._last_sequence = event.sequence
        self._last_event_time_ms = event.event_time_ms
        self._status = UserStreamStatus.HEALTHY
        return UserStreamObservation(True, self._status, previous, event.sequence)

    def mark_replayed(
        self,
        last_sequence: int | None,
        *,
        allow_unsequenced: bool = False,
        last_event_time_ms: int | None = None,
    ) -> None:
        """Advance state only after an independently verified replay."""

        if last_sequence is None and not allow_unsequenced:
            raise ValueError("unsequenced replay must explicitly allow unsequenced events")
        if last_sequence is not None and last_sequence < 0:
            raise ValueError("last_sequence must be non-negative")
        self._last_sequence = last_sequence
        self._unsequenced_allowed = allow_unsequenced
        if last_event_time_ms is not None and last_event_time_ms < 0:
            raise ValueError("last_event_time_ms must be non-negative")
        self._last_event_time_ms = last_event_time_ms
        self._status = UserStreamStatus.HEALTHY

    def restore(self, last_sequence: int) -> None:
        """Restore a durable high-water mark, but require replay after restart."""

        if last_sequence < 0:
            raise ValueError("last_sequence must be non-negative")
        self._last_sequence = last_sequence
        self._unsequenced_allowed = False
        self._last_event_time_ms = None
        self._status = UserStreamStatus.GAP


__all__ = ["UserStreamObservation", "UserStreamSequencer", "UserStreamStatus"]
