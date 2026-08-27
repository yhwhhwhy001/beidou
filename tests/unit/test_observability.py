"""
PKG-04: Observability 测试。
覆盖 Trace 全链追踪。
"""

from __future__ import annotations

from beidou_observability.telemetry import TraceContext
from beidou_shared.types import CorrelationId


class TestTraceContext:
    """全链追踪测试。"""

    def test_full_trace_lifecycle(self) -> None:
        """cover: market event → decision → approval → intent → order → fill → ledger"""
        ctx = TraceContext(correlation_id=CorrelationId("trace-001"))

        spans = []
        for op in [
            "market_event",
            "pre_risk_decision",
            "risk_approval",
            "order_intent",
            "order_submit",
            "order_fill",
            "ledger_write",
        ]:
            span = ctx.start_span(op)
            ctx.end_span(span.span_id, "OK")
            spans.append(span)

        assert len(spans) == 7
        assert all(s.status == "OK" for s in spans)
        otel = ctx.to_otel_format()
        assert len(otel) == 7

    def test_parent_child_relationship(self) -> None:
        ctx = TraceContext(correlation_id=CorrelationId("trace-002"))
        parent = ctx.start_span("parent_operation")
        child = ctx.start_span("child_operation")

        assert child.parent_span_id == parent.span_id
        assert parent.parent_span_id is None

    def test_failed_span_status(self) -> None:
        ctx = TraceContext(correlation_id=CorrelationId("trace-003"))
        span = ctx.start_span("failing_operation")
        ctx.end_span(span.span_id, "ERROR")
        assert span.status == "ERROR"

    def test_span_stack_maintained(self) -> None:
        ctx = TraceContext(correlation_id=CorrelationId("trace-004"))
        s1 = ctx.start_span("op1")
        s2 = ctx.start_span("op2")
        ctx.end_span(s2.span_id)
        ctx.end_span(s1.span_id)
        assert len(ctx._span_stack) == 0
