"""统一遥测基础设施 — 贯穿 market event→decision→approval→intent→order→fill→ledger。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from beidou_shared.types import CorrelationId


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
