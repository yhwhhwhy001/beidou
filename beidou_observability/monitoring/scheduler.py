"""Deep Auditor Scheduler (MON08)。"""

from dataclasses import dataclass, field

from beidou_observability.monitoring.contracts import FrequencyState
from beidou_observability.monitoring.frequency_policy import is_due, update_frequency


@dataclass(slots=True)
class DeepAuditScheduler:
    state: FrequencyState = field(default_factory=FrequencyState)
    check_results: list = field(default_factory=list)

    def tick(self, results, *, restart=False, self_heal=False, clock_reversal=False, p0_failed=False, p1_failed=False):
        self.state = update_frequency(
            self.state,
            results,
            restart_detected=restart,
            self_heal_active=self_heal,
            clock_reversal=clock_reversal,
            p0_failed=p0_failed,
            p1_failed=p1_failed,
        )
        return self.state

    @property
    def current_interval(self):
        return self.state.interval_seconds

    @property
    def level(self):
        return self.state.level

    def should_run_deep_audit(self):
        return is_due(self.state)
