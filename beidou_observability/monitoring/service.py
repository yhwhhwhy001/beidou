"""监控主服务+CLI (FR-MON-012)。"""

import json
from dataclasses import dataclass, field

from beidou_observability.monitoring.contracts import AccountPositionMode
from beidou_observability.monitoring.health_aggregator import health_summary
from beidou_observability.monitoring.rate_limit_budget import RateLimitBudget
from beidou_observability.monitoring.repository import MonitoringRepository


@dataclass(slots=True)
class MonitoringService:
    repo: MonitoringRepository = field(default_factory=MonitoringRepository)
    results: list = field(default_factory=list)
    incidents: list = field(default_factory=list)
    budget: RateLimitBudget = field(default_factory=RateLimitBudget)
    position_mode: AccountPositionMode = AccountPositionMode.UNKNOWN

    def status(self):
        s = health_summary(self.results)
        freq = self.repo.get_frequency_state()
        return {
            "health": s["status"],
            "p0_fails": s["p0_fails"],
            "unknowns": s["unknowns"],
            "total_checks": s["total"],
            "frequency": freq.level.value,
            "interval_s": freq.interval_seconds,
            "position_mode": self.position_mode.value,
            "open_incidents": sum(1 for i in self.incidents if i.status.value not in ("RESOLVED",)),
        }

    def check_deep(self):
        return [
            {"check_id": r.check_id, "status": r.status.value, "severity": r.severity.value, "message": r.message}
            for r in self.results
        ]

    def get_incidents(self, active_only=True):
        incs = [i for i in self.incidents if not active_only or i.status.value not in ("RESOLVED",)]
        return [
            {"id": i.incident_id, "severity": i.severity.value, "status": i.status.value, "title": i.title}
            for i in incs
        ]

    def get_evidence(self, check_id=None, limit=50):
        return self.repo.get_recent_evidence(check_id, limit)

    def get_rate_budget(self):
        return self.budget.summary()

    def get_mode_contract(self):
        return {
            "mode": self.position_mode.value,
            "ONE_WAY": "venue/account/symbol/BOTH",
            "HEDGE": "venue/account/symbol/LONG|SHORT",
        }

    def to_json(self):
        return json.dumps(self.status(), indent=2, ensure_ascii=False)


def create_monitoring_cli(service):
    return {
        "status": service.status,
        "check--deep": service.check_deep,
        "incidents": service.get_incidents,
        "evidence": service.get_evidence,
        "rate-budget": service.get_rate_budget,
        "mode-contract": service.get_mode_contract,
    }
