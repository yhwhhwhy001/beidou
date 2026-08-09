"""控制面 FastAPI — BD-12 items 1,2,3,5,7。

提供 /health, /ready, /trading-eligibility, /facts, /incidents, /evidence 和紧急动作。
health=进程存活, ready=依赖可用, trading-eligibility=基于账户/DQ/对账/保护/Gate。
单操作员本地模式只监听 localhost。日志结构化 correlation/causation ID。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class SystemStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


@dataclass
class HealthResponse:
    status: SystemStatus
    uptime_seconds: float
    version: str = "2.0.0"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ReadinessResponse:
    ready: bool
    dependencies: dict[str, bool]
    checks: dict[str, str]


@dataclass
class TradingEligibilityResponse:
    eligible: bool
    reason: str
    checks: dict[str, bool] = field(default_factory=dict)
    account_facts_age_seconds: float = 0.0
    data_quality: str = "UNKNOWN"
    reconciliation_clean: bool = False
    protection_coverage_pct: float = 0.0
    gate_status: str = "NOT_VERIFIABLE"


@dataclass
class EmergencyAction:
    action: str  # NO_NEW_RISK, EXIT_ONLY, EMERGENCY_FLATTEN, LOCK
    operator: str
    correlation_id: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    success: bool = False
    reason: str = ""


class ControlPlaneAPI:
    """BD-12: 控制面 API。

    使用 FastAPI 提供 structured endpoints。
    单操作员模式只监听 localhost。
    远程访问必须 OIDC + TLS。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9090):
        self._host = host
        self._port = port
        self._start_time = datetime.now(timezone.utc)
        self._emergency_actions: list[EmergencyAction] = []
        self._metrics: dict[str, float] = {}
        self._control_plane = None  # BD-T14: wired at engine startup

    @property
    def bind_address(self) -> str:
        return f"{self._host}:{self._port}"

    @property
    def is_remote_enabled(self) -> bool:
        return self._host not in ("127.0.0.1", "localhost", "::1")

    # === Health ===

    def health(self) -> HealthResponse:
        return HealthResponse(
            status=SystemStatus.HEALTHY,
            uptime_seconds=(datetime.now(timezone.utc) - self._start_time).total_seconds(),
        )

    # === Readiness ===

    def readiness(self, checks: dict[str, bool] | None = None) -> ReadinessResponse:
        deps = {
            "database": checks.get("db", False) if checks else False,
            "exchange": checks.get("exchange", False) if checks else False,
            "redis": checks.get("redis", False) if checks else False,
        }
        return ReadinessResponse(
            ready=all(deps.values()),
            dependencies=deps,
            checks={k: "OK" if v else "FAIL" for k, v in deps.items()},
        )

    # === Trading Eligibility ===

    def trading_eligibility(
        self,
        account_facts: dict | None = None,
        dq_tier: str = "UNKNOWN",
        reconciliation_clean: bool = False,
        protection_coverage: float = 0.0,
        gate_result: str = "NOT_VERIFIABLE",
    ) -> TradingEligibilityResponse:
        checks = {
            "account_facts_fresh": account_facts is not None,
            "data_quality_pass": dq_tier in ("PASS",),
            "reconciliation_clean": reconciliation_clean,
            "protection_full_coverage": protection_coverage >= 100.0,
            "gate_passed": gate_result == "PASS",
        }
        eligible = all(checks.values())
        reason = "All checks passed" if eligible else f"Failed: {[k for k, v in checks.items() if not v]}"

        return TradingEligibilityResponse(
            eligible=eligible,
            reason=reason,
            checks=checks,
            data_quality=dq_tier,
            reconciliation_clean=reconciliation_clean,
            protection_coverage_pct=protection_coverage,
            gate_status=gate_result,
        )

    # === Emergency Actions ===

    def emergency(self, action: str, operator: str, correlation_id: str) -> EmergencyAction:
        """BD-FIX: 紧急动作实际执行控制面操作（之前仅记录不执行）。"""
        valid = {"NO_NEW_RISK", "EXIT_ONLY", "EMERGENCY_FLATTEN", "LOCK"}
        if action not in valid:
            return EmergencyAction(
                action=action,
                operator=operator,
                correlation_id=correlation_id,
                success=False,
                reason=f"Invalid action: {action}",
            )
        # 执行控制面操作
        from beidou_control.plane import ControlAction

        try:
            ctrl_action = ControlAction(action)
            if self._control_plane is None:
                raise RuntimeError("control_plane not wired")
            self._control_plane.execute_action(ctrl_action)
        except Exception as exc:
            ea = EmergencyAction(
                action=action,
                operator=operator,
                correlation_id=correlation_id,
                success=False,
                reason=f"control action failed: {type(exc).__name__}: {exc}",
            )
            self._emergency_actions.append(ea)
            return ea
        ea = EmergencyAction(action=action, operator=operator, correlation_id=correlation_id, success=True)
        self._emergency_actions.append(ea)
        return ea

    def get_emergency_history(self) -> list[EmergencyAction]:
        return list(self._emergency_actions)

    # === Metrics ===

    def update_metrics(self, metrics: dict[str, float]) -> None:
        self._metrics.update(metrics)

    def get_metrics(self) -> dict[str, float]:
        return {
            **self._metrics,
            "uptime_seconds": (datetime.now(timezone.utc) - self._start_time).total_seconds(),
            "emergency_actions_count": len(self._emergency_actions),
        }

    # === Control Actions (BD-T14) ===

    def resume_trading(self) -> dict:
        """BD-T14: 手动 RESUME — 验证通过后恢复交易能力。"""
        if self._control_plane is not None:
            from beidou_control.plane import ControlAction

            result = self._control_plane.execute_action(ControlAction.RESUME)
            return {"action": "RESUME", "success": result, "new_status": str(self._control_plane.get_status())}
        return {"action": "RESUME", "success": False, "error": "control_plane not wired"}

    def wire_control_plane(self, control_plane) -> None:
        """BD-T14: 注入控制平面引用。"""
        self._control_plane = control_plane

    # === Factor Management (BD-T06) ===

    _factor_registry = None

    def wire_factor_registry(self, registry) -> None:
        self._factor_registry = registry

    def list_factors(self) -> list[dict]:
        if not self._factor_registry:
            return []
        result = []
        for fid, rec in self._factor_registry._factors.items():
            result.append(
                {
                    "factor_id": fid,
                    "lifecycle": rec.lifecycle.value,
                    "can_transition_to": [t.value for t in rec._valid_transitions()]
                    if hasattr(rec, "_valid_transitions")
                    else [],
                }
            )
        return result

    def promote_factor(
        self,
        factor_id: str,
        target_state: str,
        *,
        performance=None,
        evidence_ids: list[str] | None = None,
        factor_version: str = "",
        commit: str = "",
        dataset_hash: str = "",
        policy_version: str = "",
        falsifier: str = "control-api",
    ) -> dict:
        """按统一 FactorPromotionGate 晋级；控制面不提供证据旁路。

        旧实现直接调用 ``record.transition``，任何本地/测试网调用都能把
        IDEA 因子写成 ACTIVE。现在所有晋级都生成不可变
        ``PromotionDecision``，缺少 dataset/OOS/cost/paper/批准证据时明确
        拒绝。API 的 query-only 入口因此默认是 fail-closed。
        """
        if not self._factor_registry:
            return {"success": False, "error": "factor_registry not wired"}
        rec = self._factor_registry.get(factor_id)
        if rec is None:
            return {"success": False, "error": f"factor {factor_id} not found"}
        try:
            from beidou_research.factors.factor import FactorLifecycle, FactorPromotionGate

            target = FactorLifecycle(target_state)
            decision = FactorPromotionGate(strict=True).promote(
                rec,
                target,
                performance=performance,
                evidence_ids=evidence_ids,
                factor_version=factor_version,
                commit=commit,
                dataset_hash=dataset_hash,
                policy_version=policy_version,
                falsifier=falsifier,
            )
            return {
                "success": decision.approved,
                "factor_id": factor_id,
                "new_state": rec.lifecycle.value,
                "target": target_state,
                "decision_id": decision.decision_id,
                "reason": decision.reason,
            }
        except (ValueError, KeyError) as e:
            return {"success": False, "error": str(e)}

    def promote_all_to_active(self) -> dict:
        """拒绝批量晋级；ACTIVE 必须逐因子绑定证据和具名批准。"""
        if not self._factor_registry:
            return {"success": False, "error": "factor_registry not wired"}
        return {
            "success": False,
            "error": "BULK_FACTOR_PROMOTION_FORBIDDEN",
            "reason": "ACTIVE requires per-factor sealed evidence, cost/capacity, paper/shadow, and approval",
            "factors": {fid: rec.lifecycle.value for fid, rec in self._factor_registry._factors.items()},
        }


def create_app(api: ControlPlaneAPI):
    """创建 FastAPI 应用。"""
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError:
        return None

    app = FastAPI(title="北斗 V2.0 Control Plane", version="2.0.0")

    @app.get("/health")
    async def health_endpoint():
        return api.health().__dict__

    @app.get("/ready")
    async def ready_endpoint():
        r = api.readiness()
        if not r.ready:
            raise HTTPException(status_code=503, detail="Not ready")
        return r.__dict__

    @app.get("/trading-eligibility")
    async def eligibility_endpoint() -> dict:
        return api.trading_eligibility().__dict__

    @app.get("/facts")
    async def facts_endpoint() -> dict:
        return {"status": "NOT_VERIFIABLE", "message": "Use dedicated facts endpoint"}

    @app.get("/factors")
    async def factors_list_endpoint() -> list[dict]:
        return api.list_factors()

    @app.post("/factors/promote-all")
    async def factors_promote_all_endpoint() -> dict:
        return api.promote_all_to_active()

    @app.post("/factors/promote/{factor_id}")
    async def factors_promote_endpoint(factor_id: str, target: str = "ACTIVE") -> dict:
        return api.promote_factor(factor_id, target)

    @app.post("/emergency/{action}")
    async def emergency_endpoint(action: str, request: Request) -> dict:
        ea = api.emergency(action, "api-user", "api-call")
        if not ea.success:
            raise HTTPException(status_code=400, detail=ea.reason)
        return ea.__dict__

    return app
