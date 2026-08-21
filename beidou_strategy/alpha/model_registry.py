"""模型注册、Champion/Challenger 基础设施、原子切换、漂移检测。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import ModelId, SchemaVersion, StrategyId

from .forecast import CalibrationArtifact


class ModelStatus(str, Enum):
    CHALLENGER = "CHALLENGER"
    CHAMPION = "CHAMPION"
    ARCHIVED = "ARCHIVED"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class CalibrationRegistration:
    """Published calibration artifact plus the evidence that authorized it."""

    artifact: CalibrationArtifact
    evidence_ids: tuple[str, ...]
    published_by: str
    published_at: datetime
    alpha_id: str = ""


class CalibrationRegistry:
    """Small in-process registry for deterministic production artifacts.

    Research code may create artifacts, but runtime callers can only consume
    checksum-valid artifacts that have been explicitly published with evidence.
    """

    def __init__(self) -> None:
        self._registrations: dict[str, CalibrationRegistration] = {}

    def publish(
        self,
        artifact: CalibrationArtifact,
        *,
        evidence_ids: list[str] | tuple[str, ...],
        published_by: str,
        alpha_id: str | None = None,
    ) -> bool:
        evidence = tuple(sorted(str(item).strip() for item in evidence_ids if str(item).strip()))
        publisher = published_by.strip()
        binding = (alpha_id or artifact.artifact_id).strip()
        if not artifact.verify_checksum() or not evidence or not publisher or not binding:
            return False
        existing = self._registrations.get(artifact.artifact_id)
        if existing is not None and existing.artifact.checksum != artifact.checksum:
            return False
        self._registrations[artifact.artifact_id] = CalibrationRegistration(
            artifact=artifact,
            evidence_ids=evidence,
            published_by=publisher,
            published_at=datetime.now(timezone.utc),
            alpha_id=binding,
        )
        return True

    def get(self, artifact_id: str) -> CalibrationArtifact | None:
        registration = self._registrations.get(artifact_id)
        return registration.artifact if registration is not None else None

    def registration(self, artifact_id: str) -> CalibrationRegistration | None:
        return self._registrations.get(artifact_id)

    def get_for_alpha(self, alpha_id: str) -> CalibrationArtifact | None:
        """Return the published artifact explicitly bound to an Alpha id."""
        binding = alpha_id.strip()
        for registration in self._registrations.values():
            if registration.alpha_id == binding:
                return registration.artifact
        return None

    def is_published(self, artifact: CalibrationArtifact) -> bool:
        registration = self._registrations.get(artifact.artifact_id)
        return registration is not None and registration.artifact.checksum == artifact.checksum


@dataclass
class ModelRecord:
    model_id: ModelId
    strategy_id: StrategyId
    status: ModelStatus
    version: SchemaVersion
    deployed_at: datetime | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    training_dataset_version: str = ""
    champion_since: datetime | None = None


class ModelRegistry:
    """模型注册中心。Champion/Challenger 管理与原子切换。"""

    def __init__(self) -> None:
        self._models: dict[StrategyId, list[ModelRecord]] = {}
        self._champions: dict[StrategyId, ModelId] = {}
        # M14-F01: Champion 更替历史审计（内存;持久化属 M15）
        self.champion_history: list[dict[str, Any]] = []

    def register(self, model: ModelRecord) -> None:
        sid = model.strategy_id
        if sid not in self._models:
            self._models[sid] = []
        self._models[sid].append(model)

    def register_model(self, model: ModelRecord) -> None:
        """注册模型（register 的语义别名，供调用方统一命名）。"""
        self.register(model)

    def list_models(self, strategy_id: StrategyId) -> list[ModelRecord]:
        """列出策略下所有已注册模型（含 Champion/Challenger/Archived）。"""
        return list(self._models.get(strategy_id, []))

    def promote_to_champion(self, strategy_id: StrategyId, model_id: ModelId) -> bool:
        models = self._models.get(strategy_id, [])
        old_champ = self._champions.get(strategy_id)
        # M14-R2: 幂等短路 —— 对当前 Champion 重复晋级会命中归档循环
        # (old_champ == model_id 时把自身 ARCHIVED),公开 API 踩中即
        # 破坏账本(对抗审查运行时复现)。
        if old_champ == model_id:
            return True
        for m in models:
            if m.model_id == model_id:
                m.status = ModelStatus.CHAMPION
                m.champion_since = datetime.now(timezone.utc)
                self._champions[strategy_id] = model_id
                if old_champ:
                    for om in models:
                        if om.model_id == old_champ:
                            om.status = ModelStatus.ARCHIVED
                # M14-F01: 更替历史审计（旧 champion/新 champion/指标快照）
                self.champion_history.append(
                    {
                        "strategy_id": str(strategy_id),
                        "previous_champion": str(old_champ) if old_champ else None,
                        "new_champion": str(model_id),
                        "promoted_at": datetime.now(timezone.utc).isoformat(),
                        "new_metrics": dict(m.metrics),
                    }
                )
                return True
        return False

    def demote_champion(self, strategy_id: StrategyId, reason: str = "") -> bool:
        """M14-R2: Champion 降级 —— 归档当前 Champion 并清空席位。

        降级事件计入 champion_history(含 reason)。账本与部署解耦:
        部署由因子生命周期驱动,此处只维护审计台账。
        """
        champ_id = self._champions.get(strategy_id)
        if champ_id is None:
            return False
        for m in self._models.get(strategy_id, []):
            if m.model_id == champ_id:
                m.status = ModelStatus.ARCHIVED
        self.champion_history.append(
            {
                "strategy_id": str(strategy_id),
                "event": "DEMOTED",
                "previous_champion": str(champ_id),
                "new_champion": None,
                "demoted_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
            }
        )
        self._champions.pop(strategy_id, None)
        return True

    def get_champion(self, strategy_id: StrategyId) -> ModelRecord | None:
        champ_id = self._champions.get(strategy_id)
        if champ_id is None:
            return None
        for m in self._models.get(strategy_id, []):
            if m.model_id == champ_id:
                return m
        return None

    def retire_strategy(self, strategy_id: StrategyId, reason: str = "") -> list[ModelRecord]:
        models = self._models.pop(strategy_id, [])
        self._champions.pop(strategy_id, None)
        for m in models:
            m.status = ModelStatus.RETIRED
        return models


class DriftDetector:
    """漂移检测器。监控特征分布、预测分布、性能指标漂移。"""

    def __init__(self, threshold: float = 0.1):
        self.threshold = threshold
        self._baseline: dict[str, float] | None = None  # None = 未校准

    def set_baseline(self, metrics: dict[str, float]) -> None:
        self._baseline = dict(metrics)

    def is_calibrated(self) -> bool:
        """基线是否已校准。"""
        return self._baseline is not None and len(self._baseline) > 0

    def detect(self, current_metrics: dict[str, float]) -> dict[str, float]:
        drift: dict[str, float] = {}
        if self._baseline is None:
            return drift
        for k, v in current_metrics.items():
            baseline = self._baseline.get(k)
            if baseline and baseline != 0:
                change = abs(v - baseline) / abs(baseline)
                if change > self.threshold:
                    drift[k] = change
        return drift

    def should_retire(self, drift: dict[str, float]) -> bool:
        """至少一个指标显著漂移就触发退役警告。"""
        return len(drift) >= 1
