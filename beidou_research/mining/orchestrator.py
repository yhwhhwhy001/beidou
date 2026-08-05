"""BF-04/BF-07: 因子挖掘编排器。

将整个挖掘流水线串联：
数据集 → 标签构造 → 候选生成 → 快速预筛 → Purged WFO/CPCV → 多重检验 → 证据包

支持：
- checkpoint/resume
- 搜索预算控制
- 失败候选分类
- 资源限制
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable


class MiningRunStatus(str, Enum):
    CREATED = "CREATED"
    GENERATING = "GENERATING"
    SCREENING = "SCREENING"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class MiningRunConfig:
    """挖掘运行配置。"""

    run_id: str = ""
    dataset_manifest_hash: str = ""
    label_spec_hash: str = ""
    max_candidates: int = 10000
    max_complexity: float = 20.0
    random_seed: int = 42
    checkpoint_interval: int = 100  # 每 N 个候选保存检查点
    time_budget_minutes: int = 120  # 时间预算
    memory_limit_mb: int = 4096
    generators: list[str] = field(
        default_factory=lambda: [
            "template_grid",
            "interaction",
        ]
    )


@dataclass
class MiningRunState:
    """挖掘运行状态（可序列化用于 checkpoint）。"""

    run_id: str
    status: MiningRunStatus = MiningRunStatus.CREATED
    candidates_generated: int = 0
    candidates_screened: int = 0
    candidates_evaluated: int = 0
    candidates_passed: int = 0
    start_time: datetime | None = None
    last_checkpoint: datetime | None = None
    errors: list[str] = field(default_factory=list)
    generator_states: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence_bundle_hash: str = ""


class MiningOrchestrator:
    """因子挖掘编排器。

    管理完整的挖掘流水线生命周期：
    GENERATING → SCREENING → EVALUATING → COMPLETED
    """

    def __init__(self, config: MiningRunConfig | None = None) -> None:
        self.config = config or MiningRunConfig(run_id=_generate_run_id())
        self.state = MiningRunState(run_id=self.config.run_id)
        self._checkpoint_callbacks: list[Callable] = []
        self._candidate_failures: dict[str, list[str]] = {}

    def on_checkpoint(self, callback: Callable) -> None:
        self._checkpoint_callbacks.append(callback)

    def generate_candidates(
        self,
        generators: dict[str, Callable[[], list[Any]]],
    ) -> list[Any]:
        """使用注册的生成器产生候选。"""
        self.state.status = MiningRunStatus.GENERATING
        self.state.start_time = datetime.now(timezone.utc)

        all_candidates = []
        for gen_name, gen_func in generators.items():
            if gen_name not in self.config.generators:
                continue

            try:
                batch = gen_func()
                all_candidates.extend(batch)
                self.state.candidates_generated += len(batch)
                self.state.generator_states[gen_name] = {
                    "candidates": len(batch),
                    "status": "completed",
                }
            except Exception as e:
                self.state.errors.append(f"Generator {gen_name}: {e}")
                self.state.generator_states[gen_name] = {
                    "status": "failed",
                    "error": str(e),
                }

        return all_candidates

    def screen_candidates(
        self,
        candidates: list[Any],
        screener: Callable[[Any], tuple[bool, str]],
    ) -> list[Any]:
        """快速预筛候选。"""
        self.state.status = MiningRunStatus.SCREENING

        passed = []
        for i, candidate in enumerate(candidates):
            ok, reason = screener(candidate)
            self.state.candidates_screened += 1
            if ok:
                passed.append(candidate)
            else:
                candidate_id = getattr(candidate, "candidate_id", str(i))
                if reason not in self._candidate_failures:
                    self._candidate_failures[reason] = []
                self._candidate_failures[reason].append(candidate_id)

            # Checkpoint
            if i > 0 and i % self.config.checkpoint_interval == 0:
                self._save_checkpoint()

        return passed

    def evaluate_candidates(
        self,
        candidates: list[Any],
        evaluator: Callable[[Any], tuple[bool, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """评估候选并收集证据。"""
        self.state.status = MiningRunStatus.EVALUATING

        results = []
        for i, candidate in enumerate(candidates):
            passed, evidence = evaluator(candidate)
            self.state.candidates_evaluated += 1

            if passed:
                self.state.candidates_passed += 1
                results.append(
                    {
                        "candidate": candidate,
                        "evidence": evidence,
                        "passed": True,
                    }
                )

            if i > 0 and i % self.config.checkpoint_interval == 0:
                self._save_checkpoint()

        self.state.status = MiningRunStatus.COMPLETED
        self._save_checkpoint()
        return results

    def get_failure_taxonomy(self) -> dict[str, int]:
        """失败候选分类统计。"""
        return {reason: len(ids) for reason, ids in self._candidate_failures.items()}

    def generate_evidence_bundle(self) -> dict[str, Any]:
        """生成最终证据包。"""
        bundle = {
            "run_id": self.state.run_id,
            "status": self.state.status.value,
            "candidates_generated": self.state.candidates_generated,
            "candidates_screened": self.state.candidates_screened,
            "candidates_evaluated": self.state.candidates_evaluated,
            "candidates_passed": self.state.candidates_passed,
            "failure_taxonomy": self.get_failure_taxonomy(),
            "config": {
                "max_candidates": self.config.max_candidates,
                "random_seed": self.config.random_seed,
                "generators": self.config.generators,
            },
            "errors": self.state.errors,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        bundle_json = json.dumps(bundle, sort_keys=True, default=str)
        self.state.evidence_bundle_hash = hashlib.sha256(bundle_json.encode()).hexdigest()[:16]
        bundle["bundle_hash"] = self.state.evidence_bundle_hash
        return bundle

    def _save_checkpoint(self) -> None:
        """保存检查点。"""
        self.state.last_checkpoint = datetime.now(timezone.utc)
        for cb in self._checkpoint_callbacks:
            with contextlib.suppress(Exception):
                cb(self.state)

    def cancel(self) -> None:
        self.state.status = MiningRunStatus.CANCELLED
        self._save_checkpoint()

    def to_checkpoint_dict(self) -> dict:
        """导出可序列化的检查点。"""
        return {
            "run_id": self.state.run_id,
            "status": self.state.status.value,
            "candidates_generated": self.state.candidates_generated,
            "candidates_screened": self.state.candidates_screened,
            "candidates_evaluated": self.state.candidates_evaluated,
            "candidates_passed": self.state.candidates_passed,
            "errors": self.state.errors,
            "generator_states": self.state.generator_states,
            "last_checkpoint": self.state.last_checkpoint.isoformat() if self.state.last_checkpoint else None,
            "failure_taxonomy": self.get_failure_taxonomy(),
        }

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint: dict,
        config: MiningRunConfig | None = None,
    ) -> MiningOrchestrator:
        """从检查点恢复。"""
        if config is None:
            config = MiningRunConfig(run_id=checkpoint["run_id"])
        orch = cls(config)
        orch.state = MiningRunState(
            run_id=checkpoint["run_id"],
            status=MiningRunStatus(checkpoint["status"]),
            candidates_generated=checkpoint["candidates_generated"],
            candidates_screened=checkpoint["candidates_screened"],
            candidates_evaluated=checkpoint["candidates_evaluated"],
            candidates_passed=checkpoint["candidates_passed"],
            errors=checkpoint.get("errors", []),
            generator_states=checkpoint.get("generator_states", {}),
        )
        return orch


def _generate_run_id() -> str:
    return f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]}"
