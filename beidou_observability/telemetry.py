"""统一遥测基础设施 — 贯穿 market event→decision→approval→intent→order→fill→ledger。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import CorrelationId


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    LOCKDOWN = "LOCKDOWN"


class IncidentStatus(str, Enum):
    DETECTED = "DETECTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    INVESTIGATING = "INVESTIGATING"
    MITIGATING = "MITIGATING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class AutoAction(str, Enum):
    NOOP = "NOOP"
    ALERT = "ALERT"
    DEGRADE = "DEGRADE"
    PAUSE_TRADING = "PAUSE_TRADING"
    EXIT_ONLY = "EXIT_ONLY"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    LOCK = "LOCK"


SEVERITY_AUTO_ACTIONS: dict[AlertSeverity, AutoAction] = {
    AlertSeverity.INFO: AutoAction.NOOP,
    AlertSeverity.WARNING: AutoAction.ALERT,
    AlertSeverity.HIGH: AutoAction.PAUSE_TRADING,
    AlertSeverity.CRITICAL: AutoAction.EXIT_ONLY,
    AlertSeverity.LOCKDOWN: AutoAction.LOCK,
}


@dataclass(frozen=True, slots=True)
class Span:
    span_id: str
    trace_id: str
    parent_span_id: str | None
    operation: str
    correlation_id: CorrelationId | None
    start_time: datetime
    end_time: datetime | None = None
    status: str = "OK"
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class Incident:
    incident_id: str
    severity: AlertSeverity
    title: str
    description: str
    correlation_id: CorrelationId | None = None
    root_cause_category: str | None = None
    status: IncidentStatus = IncidentStatus.DETECTED
    auto_action: AutoAction = AutoAction.NOOP
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone(timedelta(hours=8))))
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    evidence_snapshots: list[dict[str, Any]] = field(default_factory=list)
    related_incidents: list[str] = field(default_factory=list)
    # 可观测性修复: 保护覆盖缺口 reason 明细, 供 supervisor A/B 分流消费
    # (get_active_incidents 以 gap_reasons 字段暴露)。
    gap_reasons: list[str] = field(default_factory=list)
    # M21: 运行时字段 —— 去重更新时由 AlertManager 就地刷新
    _last_updated: datetime | None = None

    def acknowledge(self) -> None:
        if self.status == IncidentStatus.DETECTED:
            self.status = IncidentStatus.ACKNOWLEDGED
            self.acknowledged_at = datetime.now(timezone.utc)

    def resolve(self, resolution: str) -> None:
        self.status = IncidentStatus.RESOLVED
        self.resolved_at = datetime.now(timezone.utc)

    def capture_evidence(self, snapshot: dict[str, Any]) -> None:
        snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()
        self.evidence_snapshots.append(snapshot)


class TraceContext:
    """全链追踪上下文。每个请求链维护一个 Trace。"""

    def __init__(self, correlation_id: CorrelationId) -> None:
        self.trace_id = correlation_id
        self.spans: list[Span] = []
        self._span_stack: list[str] = []

    def start_span(self, operation: str) -> Span:
        parent_id = self._span_stack[-1] if self._span_stack else None
        span = Span(
            span_id=f"{self.trace_id}-{len(self.spans)}",
            trace_id=self.trace_id,
            parent_span_id=parent_id,
            operation=operation,
            correlation_id=CorrelationId(self.trace_id),
            start_time=datetime.now(timezone.utc),
        )
        self.spans.append(span)
        self._span_stack.append(span.span_id)
        return span

    def end_span(self, span_id: str, status: str = "OK") -> None:
        for span in self.spans:
            if span.span_id == span_id:
                object.__setattr__(span, "end_time", datetime.now(timezone.utc))
                object.__setattr__(span, "status", status)
                break
        if self._span_stack and self._span_stack[-1] == span_id:
            self._span_stack.pop()

    def to_otel_format(self) -> list[dict[str, Any]]:
        return [
            {
                "span_id": s.span_id,
                "trace_id": s.trace_id,
                "parent_span_id": s.parent_span_id,
                "operation": s.operation,
                "correlation_id": s.correlation_id,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat() if s.end_time else None,
                "status": s.status,
                "attributes": s.attributes,
            }
            for s in self.spans
        ]


class AlertSuppressor:
    """告警抑制器 — 指纹去重+聚合相关告警，但不得隐藏 P0。

    BD-FIX: 使用稳定指纹(fingerprint)代替精确字符串匹配，
    同一根因的多次复发在窗口内聚合计数而非创建新 incident。
    """

    def __init__(self, window_seconds: float = 300.0) -> None:
        self._window_seconds = window_seconds
        self._recent_alerts: list[tuple[datetime, AlertSeverity, str]] = []
        self._fingerprints: dict[str, list[datetime]] = {}  # fingerpint → occurrences

    def _fingerprint(self, category: str, title: str) -> str:
        """生成稳定指纹: 同category+同根因title(去除时间戳/ID等变化部分)。"""
        import hashlib

        # 去除标题中的动态部分 (orderId, 时间戳, 数字)
        import re

        stable = re.sub(r"\d+", "N", f"{category}:{title}")
        return hashlib.sha256(stable.encode()).hexdigest()[:12]

    def should_suppress(self, severity: AlertSeverity, alert_key: str, category: str = "", title: str = "") -> bool:
        if severity in (AlertSeverity.CRITICAL, AlertSeverity.LOCKDOWN):
            return False  # P0 永不抑制
        now = datetime.now(timezone.utc)
        # 指纹去重
        fp = self._fingerprint(category, title)
        if fp not in self._fingerprints:
            self._fingerprints[fp] = []
        self._fingerprints[fp] = [t for t in self._fingerprints[fp] if (now - t).total_seconds() < self._window_seconds]
        count = len(self._fingerprints[fp])
        self._fingerprints[fp].append(now)
        # 窗口内超过3次 → 抑制（聚合而非重复告警）
        if count >= 3:
            return True
        # 精确key去重(兼容旧逻辑)
        self._recent_alerts = [
            (t, s, k) for t, s, k in self._recent_alerts if (now - t).total_seconds() < self._window_seconds
        ]
        for _t, _s, k in self._recent_alerts:
            if k == alert_key:
                return True
        self._recent_alerts.append((now, severity, alert_key))
        return False

    def get_fingerprint_count(self, category: str, title: str) -> int:
        """返回指定指纹在窗口内的发生次数。"""
        now = datetime.now(timezone.utc)
        fp = self._fingerprint(category, title)
        if fp not in self._fingerprints:
            return 0
        self._fingerprints[fp] = [t for t in self._fingerprints[fp] if (now - t).total_seconds() < self._window_seconds]
        return len(self._fingerprints[fp])

    def get_auto_action(self, severity: AlertSeverity) -> AutoAction:
        return SEVERITY_AUTO_ACTIONS.get(severity, AutoAction.NOOP)
