"""
PKG-04: Observability 测试。
覆盖 Trace 全链追踪、事故生命周期、告警抑制、自动动作。
"""

from __future__ import annotations

from beidou_observability.telemetry import (
    SEVERITY_AUTO_ACTIONS,
    AlertSeverity,
    AlertSuppressor,
    AutoAction,
    Incident,
    IncidentStatus,
    TraceContext,
)
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


class TestIncident:
    """事故生命周期测试。"""

    def test_incident_full_lifecycle(self) -> None:
        incident = Incident(
            incident_id="INC-001",
            severity=AlertSeverity.HIGH,
            title="Exchange connectivity lost",
            description="Binance API unreachable for 30s",
            auto_action=AutoAction.PAUSE_TRADING,
        )
        assert incident.status == IncidentStatus.DETECTED

        incident.acknowledge()
        assert incident.status == IncidentStatus.ACKNOWLEDGED
        assert incident.acknowledged_at is not None

        incident.resolve("Exchange connectivity restored, all systems normal")
        assert incident.status == IncidentStatus.RESOLVED
        assert incident.resolved_at is not None

    def test_capture_evidence(self) -> None:
        incident = Incident(
            incident_id="INC-002",
            severity=AlertSeverity.CRITICAL,
            title="Risk engine failure",
            description="Pre-Risk check not responding",
        )
        incident.capture_evidence({"checkpoint": "pre_risk_timeout", "duration_ms": 5000})
        assert len(incident.evidence_snapshots) == 1
        assert "captured_at" in incident.evidence_snapshots[0]

    def test_incident_with_correlation_id(self) -> None:
        incident = Incident(
            incident_id="INC-003",
            severity=AlertSeverity.WARNING,
            title="Data quality degradation",
            description="Market data gap detected",
            correlation_id=CorrelationId("corr-123"),
        )
        assert incident.correlation_id == "corr-123"


class TestAlertSuppressor:
    """告警抑制测试。"""

    def test_critical_never_suppressed(self) -> None:
        suppressor = AlertSuppressor()
        assert not suppressor.should_suppress(AlertSeverity.CRITICAL, "p0-alert")
        assert not suppressor.should_suppress(AlertSeverity.LOCKDOWN, "lockdown-alert")

    def test_info_suppressed_when_duplicate(self) -> None:
        suppressor = AlertSuppressor(window_seconds=3600)
        assert not suppressor.should_suppress(AlertSeverity.INFO, "info-1")
        assert suppressor.should_suppress(AlertSeverity.INFO, "info-1")

    def test_different_keys_not_suppressed(self) -> None:
        suppressor = AlertSuppressor()
        assert not suppressor.should_suppress(AlertSeverity.WARNING, "warn-1")
        assert not suppressor.should_suppress(AlertSeverity.WARNING, "warn-2")


class TestSeverityAutoActions:
    """自动动作绑定测试。"""

    def test_critical_triggers_exit_only(self) -> None:
        assert SEVERITY_AUTO_ACTIONS[AlertSeverity.CRITICAL] == AutoAction.EXIT_ONLY

    def test_lockdown_triggers_lock(self) -> None:
        assert SEVERITY_AUTO_ACTIONS[AlertSeverity.LOCKDOWN] == AutoAction.LOCK

    def test_high_triggers_pause(self) -> None:
        assert SEVERITY_AUTO_ACTIONS[AlertSeverity.HIGH] == AutoAction.PAUSE_TRADING

    def test_info_noop(self) -> None:
        assert SEVERITY_AUTO_ACTIONS[AlertSeverity.INFO] == AutoAction.NOOP
