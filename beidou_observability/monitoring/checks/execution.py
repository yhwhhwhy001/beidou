"""PKG-MON-05: Order Trace Monitor — stage FSM, stuck/duplicate/timeout。"""

import time
from dataclasses import dataclass, field

from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, MonitoringCheckResult, TraceStage


@dataclass(slots=True)
class OrderTraceState:
    correlation_id: str
    client_order_id: str
    symbol: str = ""
    current_stage: TraceStage = TraceStage.UNKNOWN
    stages_completed: list = field(default_factory=list)
    stuck_since: float = 0.0
    duplicate_count: int = 0
    started_at: float = 0.0

    @property
    def is_stuck(self):
        return self.stuck_since > 0 and (time.time() - self.stuck_since) > 30.0


STAGE_TIMEOUTS = {
    TraceStage.INTENT_CREATED: 5.0,
    TraceStage.RISK_APPROVED: 3.0,
    TraceStage.OUTBOX_ENQUEUED: 2.0,
    TraceStage.EXECUTOR_SENT: 10.0,
    TraceStage.EXCHANGE_ACKED: 10.0,
    TraceStage.FILLED: 60.0,
    TraceStage.LEDGER_POSTED: 30.0,
}


def check_order_trace(traces):
    results = []
    # PKG02 (BDS-P0-001): 所有环境统一订单追踪检查标准。
    _sev = CheckSeverity.P0
    _status = CheckStatus.FAIL
    for t in traces:
        if t.is_stuck:
            results.append(
                MonitoringCheckResult(
                    check_id="runtime.execution.order_trace",
                    entity_type="order",
                    entity_id=t.correlation_id,
                    status=_status,
                    severity=_sev,
                    message=f"STUCK at {t.current_stage.value}",
                    observed_at=time.time(),
                )
            )
        elif t.duplicate_count > 0:
            results.append(
                MonitoringCheckResult(
                    check_id="runtime.execution.order_trace",
                    entity_type="order",
                    entity_id=t.correlation_id,
                    status=_status,
                    severity=_sev,
                    message=f"DUPLICATE UNKNOWN ({t.duplicate_count}x)",
                    observed_at=time.time(),
                )
            )
    return results
