"""Check-level Rollout — observe/enforce+locked P0 (MON08/MON14)。"""

from dataclasses import dataclass, field

from beidou_observability.monitoring.contracts import RolloutMode, RolloutState
from beidou_observability.monitoring.repository import MonitoringRepository


@dataclass(slots=True)
class RolloutManager:
    repo: MonitoringRepository = field(default_factory=MonitoringRepository)

    def register(self, check_id, *, mode=RolloutMode.ENFORCE, locked=False):
        s = RolloutState(check_id=check_id, mode=mode, locked_safety_check=locked)
        self.repo.upsert_rollout_state(s)
        return s

    def can_disable(self, check_id, profile):
        return self.repo.can_disable_check(check_id, profile)

    def is_enforce(self, check_id):
        s = self.repo.get_rollout_state(check_id)
        return s is None or (s.enabled and s.mode == RolloutMode.ENFORCE)

    def should_execute_control_action(self, check_id):
        return self.is_enforce(check_id)

    def transition_to_enforce(self, check_id):
        s = self.repo.get_rollout_state(check_id)
        if s is None:
            return False
        s.mode = RolloutMode.ENFORCE
        self.repo.upsert_rollout_state(s)
        return True
