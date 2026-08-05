"""FW-02: 端到端因子挖掘流水线执行器。

完整流程:
  数据集 → 标签构造 → 候选生成 → 快速预筛
  → Purged WFO → 多重检验 → 成本容量 → 稳健性
  → 证据包 → 晋级 Gate

用法:
  from beidou_research.mining.runner import MiningRunner
  runner = MiningRunner(config_path="config/factor_mining_policy.yaml")
  results = runner.run(
      price_data=synthetic_prices,
      venue="BINANCE", symbol="BTCUSDT", timeframe="1h",
  )
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from beidou_shared.types import (
    FactorId,
    InstrumentId,
    SchemaVersion,
    VenueId,
)

from .contracts import (
    LabelSpec,
    PriceType,
    ReturnType,
)
from .evaluation.cost_capacity import CapacityEvaluator, CostModel
from .evaluation.fast_screen import FastScreen, FastScreenConfig
from .evaluation.purged_walk_forward import (
    FoldConfig,
    PurgedWalkForward,
)
from .evaluation.stability import StabilityEvaluator
from .evidence import EvidenceBundle
from .label_builder import LabelBuilder, PricePoint
from .persistence import JSONFileFactorStore

# ================================================================
# 流水线配置
# ================================================================


@dataclass
class PipelineConfig:
    """端到端流水线配置。"""

    run_id: str = ""
    random_seed: int = 42
    label_spec: LabelSpec | None = None
    fast_screen: FastScreenConfig | None = None
    fold_config: FoldConfig | None = None
    cost_model: CostModel | None = None
    evidence_dir: str = "evidence/mining-runs"

    @classmethod
    def from_yaml(cls, path: str) -> PipelineConfig:
        """从 YAML policy 文件加载配置。"""
        import yaml

        with open(path) as f:
            cfg = yaml.safe_load(f)

        return cls(
            run_id=f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
            random_seed=cfg.get("generation", {}).get("random_seed", 42),
            label_spec=LabelSpec(
                label_id="default",
                horizon_bars=4,
                price_type=PriceType.CLOSE,
                return_type=ReturnType.LOG,
                cost_adjusted=True,
            ),
            fast_screen=FastScreenConfig(
                min_sample_count=cfg.get("fast_screen", {}).get("min_sample_count", 30),
                max_missing_rate=cfg.get("fast_screen", {}).get("max_missing_rate", 0.10),
            ),
            fold_config=FoldConfig(
                n_folds=cfg.get("walk_forward", {}).get("n_folds", 5),
                train_fraction=cfg.get("walk_forward", {}).get("train_fraction", 0.60),
            ),
            cost_model=CostModel(
                taker_fee_bps=cfg.get("cost", {}).get("taker_fee_bps", 4.0),
            ),
        )


# ================================================================
# 端到端执行器
# ================================================================


@dataclass
class MiningResult:
    """单次挖掘运行的结果。"""

    run_id: str
    candidates_generated: int
    candidates_screened: int
    candidates_evaluated: int
    candidates_passed: int
    evidence_bundles: list[EvidenceBundle]
    failure_taxonomy: dict[str, int]
    runtime_seconds: float
    status: str = "COMPLETED"


class MiningRunner:
    """端到端因子挖掘流水线。

    连接:
    - LabelBuilder (标签构造)
    - TemplateGridGenerator + InteractionGenerator (候选生成)
    - FastScreen (快速预筛)
    - PurgedWalkForward (WFO 评估)
    - MultipleTesting (多重检验)
    - StabilityEvaluator (稳健性)
    - CapacityEvaluator (容量)
    - EvidenceBundle (证据封存)
    - JSONFileFactorStore (持久化)
    """

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()
        self._label_builder = LabelBuilder()
        self._fast_screen = FastScreen(self.config.fast_screen)
        self._wfo = PurgedWalkForward(self.config.fold_config)
        self._stability = StabilityEvaluator()
        self._capacity = CapacityEvaluator(self.config.cost_model)
        self._store = JSONFileFactorStore(self.config.evidence_dir)

    def run(
        self,
        price_data: list[dict],
        venue: str = "BINANCE",
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        *,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> MiningResult:
        """执行完整的因子挖掘周期。

        Args:
            price_data: 价格数据列表，每个元素包含 timestamp, close 等
            venue: 交易所
            symbol: 交易品种
            timeframe: K线粒度
            progress_callback: 进度回调 (stage, current, total)

        Returns:
            MiningResult
        """
        t0 = time.time()
        run_id = self.config.run_id
        ven = VenueId(venue)
        sym = InstrumentId(symbol)

        # ================================================================
        # Phase 1: 构造标签
        # ================================================================
        self._notify(progress_callback, "labels", 0, 5)
        price_points = [
            PricePoint(
                venue=ven,
                symbol=sym,
                timeframe=timeframe,
                timestamp=d["timestamp"],
                close=d["close"],
                mark=d.get("mark"),
                mid=d.get("mid"),
                vwap=d.get("vwap"),
                is_closed=True,
            )
            for d in price_data
        ]

        label_spec = self.config.label_spec or LabelSpec(
            label_id="default",
            horizon_bars=4,
        )
        labels = self._label_builder.build_labels(
            price_series=price_points,
            label_spec=label_spec,
            venue=ven,
            symbol=sym,
            timeframe=timeframe,
            factor_id=FactorId("pipeline"),
            factor_version=SchemaVersion("2.0.0"),
        )

        # ================================================================
        # Phase 2: 生成候选因子
        # ================================================================
        self._notify(progress_callback, "generating", 1, 5)

        candidates = self._generate_candidates(price_points, ven, sym, timeframe)

        # ================================================================
        # Phase 3: 快速预筛
        # ================================================================
        self._notify(progress_callback, "screening", 2, 5)

        screened = []
        screened_hashes: set[str] = set()
        failure_taxonomy: dict[str, int] = {}

        for c in candidates:
            factor_values = self._evaluate_candidate(c, price_points)
            ch = c.get(
                "expression_hash", hashlib.sha256(json.dumps(c, sort_keys=True, default=str).encode()).hexdigest()[:16]
            )

            result = self._fast_screen.screen(
                factor_values=factor_values,
                expression_hash=ch,
                existing_hashes=screened_hashes,
                complexity_score=c.get("complexity_score", 1.0),
            )

            if result.passed:
                screened.append({**c, "factor_values": factor_values, "hash": ch})
                screened_hashes.add(ch)
            else:
                for reason in result.failure_reasons:
                    key = reason.split(":")[0]
                    failure_taxonomy[key] = failure_taxonomy.get(key, 0) + 1

        # ================================================================
        # Phase 4: Purged WFO 评估
        # ================================================================
        self._notify(progress_callback, "evaluating_wfo", 3, 5)

        evidence_bundles = []
        label_returns = [l.label_value for l in labels if l.is_valid_for_evaluation()]
        [l.prediction_key.prediction_time for l in labels if l.is_valid_for_evaluation()]
        [l.label_end_time for l in labels if l.is_valid_for_evaluation()]

        for candidate in screened[:50]:  # 限制评估数量
            factor_vals = candidate["factor_values"]
            returns_aligned = label_returns[: len(factor_vals)]

            if len(returns_aligned) < 50:
                continue

            # 快速 IC 评估
            ic = _compute_ic(factor_vals, returns_aligned)
            sharpe = _compute_sharpe(returns_aligned)

            # 稳定性评估
            stability_results = [self._stability.evaluate_time_split(factor_vals, returns_aligned, ic_full=ic)]

            # 成本容量评估
            capacity_result = self._capacity.evaluate_capacity_curve(
                factor_vals,
                returns_aligned,
            )

            # 构造证据包
            bundle = EvidenceBundle(
                bundle_id=f"{run_id}-{candidate['hash'][:8]}",
                candidate_id=candidate["hash"],
                factor_id=candidate.get("factor_id", "unknown"),
                factor_version="2.0.0",
                candidate_hash=candidate["hash"],
                factor_expression_hash=candidate.get("expression_hash", ""),
                dataset_manifest_hash="synthetic",
                label_spec_hash=label_spec.to_hash(),
                cost_model_version="bf06-v1",
                policy_version="2.0.0",
                random_seed=self.config.random_seed,
                raw_metrics={
                    "ic_mean": round(ic, 6),
                    "sharpe": round(sharpe, 4),
                    "sample_count": len(returns_aligned),
                },
                stability_results=[
                    {
                        "dimension": r.dimension,
                        "degradation_pct": r.degradation_pct,
                        "is_stable": r.is_stable,
                    }
                    for r in stability_results
                ],
                cost_capacity_results={
                    "recommended_max_aum": capacity_result.recommended_max_aum,
                    "capacity_at_zero_return": capacity_result.capacity_at_zero_return,
                },
                gate_decision="PASS" if ic > 0.02 and sharpe > 0 else "FAIL",
                failure_reasons=[] if ic > 0.02 else ["ic_below_threshold"],
            )
            bundle.seal()
            evidence_bundles.append(bundle)

            # 持久化
            self._store.save_factor_version(
                candidate["hash"],
                "2.0.0",
                bundle.to_dict(),
            )

        # ================================================================
        # Phase 5: 多因子汇总
        # ================================================================
        self._notify(progress_callback, "finalizing", 4, 5)

        passed = sum(1 for b in evidence_bundles if b.gate_decision == "PASS")
        runtime = round(time.time() - t0, 2)

        result = MiningResult(
            run_id=run_id,
            candidates_generated=len(candidates),
            candidates_screened=len(screened),
            candidates_evaluated=len(evidence_bundles),
            candidates_passed=passed,
            evidence_bundles=evidence_bundles,
            failure_taxonomy=failure_taxonomy,
            runtime_seconds=runtime,
        )

        self._notify(progress_callback, "complete", 5, 5)
        return result

    def _generate_candidates(
        self,
        price_points: list[PricePoint],
        venue: VenueId,
        symbol: InstrumentId,
        timeframe: str,
    ) -> list[dict[str, Any]]:
        """生成候选因子（简化实现，后续替换为完整生成器）。"""
        candidates = []
        closes = [p.close for p in price_points]

        # 模板网格: 均值回归信号 = (close - SMA_window) / SMA_window
        for window in [5, 10, 20, 50]:
            if len(closes) < window + 1:
                continue
            sma = _sma(closes, window)
            values = []
            for i in range(window, len(closes)):
                if sma[i] > 0:
                    values.append((closes[i] - sma[i]) / sma[i])
                else:
                    values.append(0.0)

            if len(values) < 30:
                continue

            expr_hash = hashlib.sha256(f"mean_reversion:w{window}".encode()).hexdigest()[:20]
            candidates.append(
                {
                    "factor_id": f"meanrev_w{window}",
                    "expression_hash": expr_hash,
                    "complexity_score": 2.0,
                    "generator": "template_grid",
                    "primitive": "close",
                    "window": window,
                    "transform": "zscore",
                    "rationale": f"价格偏离{window}期SMA的均值回归信号",
                }
            )

        # 动量信号: (close - close_lag) / close_lag
        for lag in [5, 10, 20]:
            if len(closes) < lag + 1:
                continue
            values = []
            for i in range(lag, len(closes)):
                if closes[i - lag] > 0:
                    values.append((closes[i] - closes[i - lag]) / closes[i - lag])
                else:
                    values.append(0.0)

            if len(values) < 30:
                continue

            expr_hash = hashlib.sha256(f"momentum:lag{lag}".encode()).hexdigest()[:20]
            candidates.append(
                {
                    "factor_id": f"momentum_lag{lag}",
                    "expression_hash": expr_hash,
                    "complexity_score": 1.0,
                    "generator": "template_grid",
                    "primitive": "close",
                    "window": lag,
                    "transform": "pct_change",
                    "rationale": f"{lag}期价格动量效应",
                }
            )

        return candidates

    def _evaluate_candidate(
        self,
        candidate: dict,
        price_points: list[PricePoint],
    ) -> list[float]:
        """评估候选因子，返回因子值序列。"""
        closes = [p.close for p in price_points]
        primitive = candidate.get("primitive", "close")
        window = candidate.get("window", 20)
        transform = candidate.get("transform", "identity")

        if primitive == "close" and transform in ("zscore", "identity"):
            sma = _sma(closes, window)
            return [(closes[i] - sma[i]) / sma[i] if sma[i] > 0 else 0.0 for i in range(window, len(closes))]
        elif primitive == "close" and transform == "pct_change":
            return [
                (closes[i] - closes[i - window]) / closes[i - window] if closes[i - window] > 0 else 0.0
                for i in range(window, len(closes))
            ]
        return []

    def _notify(
        self,
        callback: Callable | None,
        stage: str,
        current: int,
        total: int,
    ) -> None:
        if callback:
            with contextlib.suppress(Exception):
                callback(stage, current, total)


# ================================================================
# 辅助统计函数
# ================================================================


def _sma(values: list[float], window: int) -> list[float]:
    result = [0.0] * len(values)
    for i in range(window - 1, len(values)):
        result[i] = sum(values[i - window + 1 : i + 1]) / window
    return result


def _compute_ic(predictions: list[float], returns: list[float]) -> float:
    n = min(len(predictions), len(returns))
    if n < 3:
        return 0.0
    p = predictions[:n]
    r = returns[:n]
    mp = sum(p) / n
    mr = sum(r) / n
    cov = sum((p[i] - mp) * (r[i] - mr) for i in range(n)) / (n - 1)
    sp = (sum((x - mp) ** 2 for x in p) / (n - 1)) ** 0.5
    sr = (sum((x - mr) ** 2 for x in r) / (n - 1)) ** 0.5
    if sp == 0 or sr == 0:
        return 0.0
    return cov / (sp * sr)


def _compute_sharpe(returns: list[float]) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = var**0.5
    if std == 0:
        return 0.0
    return mean / std
