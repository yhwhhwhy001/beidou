"""模型注册、Champion/Challenger 基础设施、原子切换、漂移检测。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import ModelId, SchemaVersion, StrategyId


class ModelStatus(str, Enum):
    CHALLENGER = "CHALLENGER"
    CHAMPION = "CHAMPION"
    ARCHIVED = "ARCHIVED"
    RETIRED = "RETIRED"


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
        for m in models:
            if m.model_id == model_id:
                old_champ = self._champions.get(strategy_id)
                m.status = ModelStatus.CHAMPION
                m.champion_since = datetime.now(timezone.utc)
                self._champions[strategy_id] = model_id
                if old_champ:
                    for om in models:
                        if om.model_id == old_champ:
                            om.status = ModelStatus.ARCHIVED
                return True
        return False

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
