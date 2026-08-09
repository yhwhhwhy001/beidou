"""FW-02: 端到端因子挖掘流水线执行器。

完整流程:
  数据集 → 标签构造 → 候选生成 → 快速预筛
  → Purged WFO → 多重检验 → 成本容量 → 稳健性
  → 证据包 → 晋级 Gate

BD-P1-12:
  - dataset_manifest_hash 必须从真实数据计算
  - 禁止 synthetic/test-fixture 证据用于生产晋级
  - 必须执行 Purged WFO（不可跳过 Purge/Embargo）
  - 仅有效 Gate Certificate 可驱动生命周期迁移

用法:
  from beidou_research.mining.runner import MiningRunner
  runner = MiningRunner(config_path="config/factor_mining_policy.yaml")
  results = runner.run(
      price_data=real_ohlcv_data,  # 必须真实数据，禁止 synthetic
      venue="BINANCE", symbol="BTCUSDT", timeframe="1h",
  )
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from beidou_shared.types import (
    FactorId,
    GateResult,
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
from .evaluation.cpcv import CPCVEvaluator
from .evaluation.fast_screen import FastScreen, FastScreenConfig
from .evaluation.multiple_testing import evaluate_multiple_testing
from .evaluation.purged_walk_forward import (
    FoldConfig,
    FoldResult,
    PurgedWalkForward,
)
from .evaluation.stability import StabilityEvaluator
from .evidence import EvidenceBundle
from .label_builder import LabelBuilder, PricePoint
from .persistence import JSONFileFactorStore

logger = logging.getLogger(__name__)

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
    # Production runs must bind evidence to an externally supplied dataset
    # manifest.  A hash inferred only from payload bytes is not provenance.
    dataset_manifest_hash: str = ""
    # Feature definitions/availability must be bound independently of the
    # dataset payload; otherwise a candidate can be replayed under a changed
    # feature schema while retaining the old evidence hash.
    feature_manifest_hash: str = ""
    evidence_dir: str = "evidence/mining-runs"
    # A policy-file run is a production-style research request.  It must not
    # silently fall back to an implicit candidate grid when the file changes
    # or disappears.  Bare PipelineConfig remains available for local tests.
    policy_path: str = ""
    strict_policy: bool = False
    # The policy version is part of the evidence identity.  A bare
    # PipelineConfig intentionally leaves it empty so diagnostic runs cannot
    # manufacture a production promotion certificate.
    policy_version: str = ""

    @classmethod
    def from_yaml(cls, path: str) -> PipelineConfig:
        """从 YAML policy 文件加载配置。"""
        import yaml

        with open(path) as f:
            cfg = yaml.safe_load(f)

        if not isinstance(cfg, dict):
            raise ValueError("MINING_POLICY_MUST_BE_MAPPING")

        policy_version = cfg.get("policy_version")
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise ValueError("MINING_POLICY_VERSION_REQUIRED")

        generation = cfg.get("generation")
        fast_screen = cfg.get("fast_screen")
        walk_forward = cfg.get("walk_forward")
        cost = cfg.get("cost")
        if not all(isinstance(section, dict) for section in (generation, fast_screen, walk_forward, cost)):
            raise ValueError("MINING_POLICY_REQUIRED_SECTIONS_MISSING")

        template_grid = generation.get("template_grid")
        if not isinstance(template_grid, dict):
            raise ValueError("MINING_POLICY_TEMPLATE_GRID_MISSING")
        required_template_keys = {"primitives", "windows", "transforms", "normalizations"}
        if not required_template_keys.issubset(template_grid):
            raise ValueError("MINING_POLICY_TEMPLATE_GRID_INCOMPLETE")

        required_cost_keys = {
            "taker_fee_bps",
            "maker_fee_bps",
            "avg_spread_bps",
            "slippage_bps",
            "funding_rate_8h_pct",
            "impact_bps_per_10k",
        }
        if not required_cost_keys.issubset(cost):
            raise ValueError("MINING_POLICY_COST_MODEL_INCOMPLETE")

        return cls(
            run_id=f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
            random_seed=int(generation["random_seed"]),
            label_spec=LabelSpec(
                label_id="default",
                horizon_bars=4,
                price_type=PriceType.CLOSE,
                return_type=ReturnType.LOG,
                cost_adjusted=True,
            ),
            fast_screen=FastScreenConfig(
                min_sample_count=int(fast_screen["min_sample_count"]),
                max_missing_rate=float(fast_screen["max_missing_rate"]),
                min_variance=float(fast_screen["min_variance"]),
                max_extreme_ratio=float(fast_screen["max_extreme_ratio"]),
                extreme_sigma=float(fast_screen["extreme_sigma"]),
                max_complexity_score=float(fast_screen["max_complexity_score"]),
                min_effective_samples=int(fast_screen["min_effective_samples"]),
                max_turnover_pct=float(fast_screen["max_turnover_pct"]),
            ),
            fold_config=FoldConfig(
                n_folds=int(walk_forward["n_folds"]),
                train_fraction=float(walk_forward["train_fraction"]),
                purge_fraction=float(walk_forward["purge_fraction"]),
                embargo_fraction=float(walk_forward["embargo_fraction"]),
                min_train_samples=int(walk_forward["min_train_samples"]),
                min_test_samples=int(walk_forward["min_test_samples"]),
                min_folds_for_verdict=int(walk_forward["min_folds_for_verdict"]),
            ),
            cost_model=CostModel(
                taker_fee_bps=float(cost["taker_fee_bps"]),
                maker_fee_bps=float(cost["maker_fee_bps"]),
                avg_spread_bps=float(cost["avg_spread_bps"]),
                slippage_bps=float(cost["slippage_bps"]),
                funding_rate_8h_pct=float(cost["funding_rate_8h_pct"]),
                impact_bps_per_10k=float(cost["impact_bps_per_10k"]),
                model_version=f"{policy_version}:cost",
            ),
            policy_path=path,
            strict_policy=True,
            policy_version=policy_version.strip(),
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
        self._label_builder = LabelBuilder(cost_model=self.config.cost_model)
        self._fast_screen = FastScreen(self.config.fast_screen)
        self._wfo = PurgedWalkForward(self.config.fold_config)
        self._cpcv = CPCVEvaluator()
        self._stability = StabilityEvaluator()
        self._capacity = CapacityEvaluator(self.config.cost_model)
        self._store = JSONFileFactorStore(self.config.evidence_dir)
        # 表达式引擎（懒初始化）
        self._registry: Any = None
        self._feature_dict: dict[str, list[float]] = {}
        self._generation_policy_error: str = ""

    @staticmethod
    def _compute_dataset_hash(price_data: list[dict]) -> str:
        """BD-P1-12: 从真实数据计算 dataset manifest hash。

        synthetic/test-fixture 数据返回 "UNKNOWN"。
        真实数据使用 SHA256 计算确定性哈希。
        """
        if not price_data or len(price_data) < 100:
            return "UNKNOWN"
        # A payload hash is useful for reproducibility but is not a data
        # provenance manifest.  Require an explicit manifest at the run
        # boundary; otherwise certification must remain UNKNOWN.
        return "UNKNOWN"

    @staticmethod
    def _validated_manifest_hash(value: Any) -> str:
        """Accept only a canonical externally-bound SHA-256 manifest hash.

        A non-empty arbitrary string is not provenance.  Treat malformed
        values as UNKNOWN so a caller cannot turn a label/payload run into a
        promotable evidence bundle by supplying a placeholder token.
        """

        candidate = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", candidate):
            return candidate
        return "UNKNOWN"

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
        self._generation_policy_error = ""
        run_id = self.config.run_id
        ven = VenueId(venue)
        sym = InstrumentId(symbol)

        # ================================================================
        # Phase 1: 构造标签
        # ================================================================
        self._notify(progress_callback, "labels", 0, 5)
        missing_closed_metadata = sum(1 for d in price_data if "is_closed" not in d)
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
                open=d.get("open"),
                high=d.get("high"),
                low=d.get("low"),
                volume=d.get("volume"),
                is_closed=bool(d.get("is_closed", False)),
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

        # 预构建特征字典，供 Phase 3/4 中表达式求值使用
        self._build_feature_dict(price_points)

        # ================================================================
        # Phase 3: 快速预筛
        # ================================================================
        self._notify(progress_callback, "screening", 2, 5)

        screened = []
        screened_hashes: set[str] = set()
        failure_taxonomy: dict[str, int] = {}
        if self._generation_policy_error:
            failure_taxonomy["generation_policy"] = 1

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
        evaluation_records: list[dict[str, Any]] = []
        raw_manifest_hash = str(self.config.dataset_manifest_hash or "").strip()
        dataset_manifest_hash = self._validated_manifest_hash(raw_manifest_hash)
        manifest_invalid = bool(raw_manifest_hash) and dataset_manifest_hash == "UNKNOWN"
        raw_feature_manifest_hash = str(self.config.feature_manifest_hash or "").strip()
        feature_manifest_hash = self._validated_manifest_hash(raw_feature_manifest_hash)
        feature_manifest_invalid = bool(raw_feature_manifest_hash) and feature_manifest_hash == "UNKNOWN"
        # label_returns[t] = 从 bar t 起算的 forward return
        label_returns = [l.label_value for l in labels if l.is_valid_for_evaluation()]

        def _aligned_samples(factor_values: list[float]) -> list[tuple[int, float, float, datetime, datetime]]:
            """Align predictions and labels by their original bar index.

            Factor expressions retain their full price-series index while label
            construction may drop invalid rows.  Positionally zipping filtered
            arrays silently moves a prediction onto another bar.  Keeping the
            source index makes every downstream statistic use the same causal
            observation and its actual label interval.
            """

            samples: list[tuple[int, float, float, datetime, datetime]] = []
            for index, label in enumerate(labels):
                if index >= len(factor_values) or not label.is_valid_for_evaluation():
                    continue
                value = factor_values[index]
                if not _is_finite(value) or not _is_finite(label.label_value):
                    continue
                samples.append(
                    (
                        index,
                        float(value),
                        float(label.label_value),
                        label.prediction_key.prediction_time,
                        label.label_end_time,
                    )
                )
            return samples

        for candidate in screened[:50]:  # 限制评估数量
            factor_vals = candidate["factor_values"]
            samples = _aligned_samples(factor_vals)
            if len(samples) < 50:
                continue
            candidate["_aligned_samples"] = samples
            valid_vals = [sample[1] for sample in samples]
            valid_returns = [sample[2] for sample in samples]

            if len(valid_returns) < 50:
                continue

            # Bind every usable observation to its own prediction/label
            # interval.  The WFO evaluator below must never infer time from
            # array position after warm-up/quality filtering.
            sample_times = [sample[3] for sample in samples]
            label_end_times = [sample[4] for sample in samples]

            wfo_fold_counter = [0]

            def _evaluate_wfo_fold(
                train_indices: list[int],
                test_indices: list[int],
                *,
                _valid_vals: list[float] = valid_vals,
                _valid_returns: list[float] = valid_returns,
                _fold_counter: list[int] = wfo_fold_counter,
            ) -> FoldResult:
                fold_id = _fold_counter[0]
                _fold_counter[0] += 1
                train_preds = [_valid_vals[i] for i in train_indices if i < len(_valid_vals)]
                train_rets = [_valid_returns[i] for i in train_indices if i < len(_valid_returns)]
                test_preds = [_valid_vals[i] for i in test_indices if i < len(_valid_vals)]
                test_rets = [_valid_returns[i] for i in test_indices if i < len(_valid_returns)]
                if len(test_preds) < 3 or len(train_preds) < 3:
                    return FoldResult(
                        fold_id=fold_id,
                        train_samples=len(train_preds),
                        test_samples=len(test_preds),
                        failure_reason="insufficient_fold_samples",
                    )
                test_ic = _compute_ic(test_preds, test_rets)
                train_ic = _compute_ic(train_preds, train_rets)
                return FoldResult(
                    fold_id=fold_id,
                    train_samples=len(train_preds),
                    test_samples=len(test_preds),
                    ic_mean=test_ic,
                    ic_std=abs(train_ic - test_ic),
                    icir=test_ic,
                    sharpe=_compute_sharpe(test_rets),
                    cost_adjusted_return=sum(test_rets) / len(test_rets),
                    metrics={
                        "train_ic": train_ic,
                        "test_ic": test_ic,
                        "train_sharpe": _compute_sharpe(train_rets),
                    },
                )

            total_duration_days = (
                (max(sample_times) - min(sample_times)).total_seconds() / 86400.0 if sample_times else 0.0
            )
            label_horizon_days = label_spec.horizon_bars * _timeframe_to_hours(timeframe) / 24.0
            wfo_result = self._wfo.run(
                sample_times,
                label_end_times,
                total_duration_days,
                evaluator=_evaluate_wfo_fold,
                label_horizon_days=label_horizon_days,
            )
            cpcv_result = self._cpcv.evaluate(valid_vals, valid_returns)

            # 快速 IC 评估
            ic = _compute_ic(valid_vals, valid_returns)
            sharpe = _compute_sharpe(valid_returns)

            # 稳定性评估
            stability_results = [self._stability.evaluate_time_split(valid_vals, valid_returns, ic_full=ic)]

            # 成本容量评估
            capacity_result = self._capacity.evaluate_capacity_curve(
                valid_vals,
                valid_returns,
            )

            # BD-P1-12: only an externally bound manifest can support a
            # promotion decision.  A local payload hash is not provenance.
            # Fold-level train Sharpe is recorded in metrics when available;
            # keep the explicit arrays for the later multi-test report.
            wfo_train_sharpes = [
                float(r.metrics.get("train_sharpe", 0.0)) for r in wfo_result.fold_results if not r.failure_reason
            ]
            wfo_test_sharpes = [r.sharpe for r in wfo_result.fold_results if not r.failure_reason]
            p_value = _correlation_p_value(ic, len(valid_returns))

            # 构造证据包
            bundle = EvidenceBundle(
                bundle_id=f"{run_id}-{candidate['hash'][:8]}",
                candidate_id=candidate["hash"],
                factor_id=candidate.get("factor_id", "unknown"),
                factor_version="2.0.0",
                candidate_hash=candidate["hash"],
                factor_expression_hash=candidate.get("expression_hash", ""),
                dataset_manifest_hash=dataset_manifest_hash,
                feature_manifest_hash=feature_manifest_hash,
                label_spec_hash=label_spec.to_hash(),
                cost_model_version=self._label_builder.cost_model_version,
                policy_version=self.config.policy_version,
                random_seed=self.config.random_seed,
                raw_metrics={
                    "ic_mean": round(ic, 6),
                    "sharpe": round(sharpe, 4),
                    "sample_count": len(valid_returns),
                    "wfo_gate": wfo_result.gate_result.value,
                    "wfo_icir_cv": round(wfo_result.icir_cv, 6),
                    "wfo_fold_consistency": round(wfo_result.fold_consistency, 6),
                    "wfo_folds_completed": wfo_result.n_folds_completed,
                    "wfo_failure_reasons": wfo_result.failure_reasons,
                    "cpcv_gate": cpcv_result.gate_result.value,
                    "cpcv_paths_completed": cpcv_result.n_completed,
                    "cpcv_paths_total": cpcv_result.n_paths,
                    "cpcv_metric_mean": cpcv_result.metric_mean,
                    "cpcv_metric_q05": cpcv_result.metric_q05,
                    "cpcv_failure_reasons": cpcv_result.failure_reasons,
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
                gate_decision="NOT_VERIFIABLE",
                failure_reasons=[] if ic > 0.02 else ["ic_below_threshold"],
            )
            evaluation_records.append(
                {
                    "bundle": bundle,
                    "p_value": p_value,
                    "sharpe": sharpe,
                    "train_sharpes": wfo_train_sharpes,
                    "test_sharpes": wfo_test_sharpes,
                    "wfo": wfo_result,
                    "cpcv": cpcv_result,
                    "candidate": candidate,
                }
            )
            evidence_bundles.append(bundle)

        # Multi-test correction is a single run-level operation: every
        # candidate attempted by the generator belongs in the denominator.
        if evaluation_records:
            pvalues = [r["p_value"] for r in evaluation_records]
            train_sharpes = [
                sum(r["train_sharpes"]) / len(r["train_sharpes"]) if r["train_sharpes"] else 0.0
                for r in evaluation_records
            ]
            test_sharpes = [
                sum(r["test_sharpes"]) / len(r["test_sharpes"]) if r["test_sharpes"] else 0.0
                for r in evaluation_records
            ]
            for candidate_index, record in enumerate(evaluation_records):
                bundle = record["bundle"]
                report = evaluate_multiple_testing(
                    pvalues,
                    observed_sharpe=record["sharpe"],
                    n_trials=max(len(candidates), 1),
                    in_sample_sharpes=train_sharpes,
                    out_of_sample_sharpes=test_sharpes,
                    sample_length=len(label_returns),
                    candidate_index=candidate_index,
                )
                bundle.multiple_testing_results = {
                    "verdict": report.verdict,
                    "n_total_trials": report.n_total_trials,
                    "n_evaluated": report.n_evaluated,
                    "bh_significant_05": report.bh_result.n_significant_05 if report.bh_result else 0,
                    "dsr": report.dsr,
                    "pbo": report.pbo.__dict__ if report.pbo else None,
                    "failure_reasons": report.failure_reasons,
                }
                reasons = list(bundle.failure_reasons)
                if record["wfo"].gate_result != GateResult.PASS:
                    reasons.append(f"wfo:{record['wfo'].gate_result.value}")
                if record["cpcv"].gate_result != GateResult.PASS:
                    reasons.append(f"cpcv:{record['cpcv'].gate_result.value}")
                if report.verdict != "PASS":
                    reasons.append(f"multiple_testing:{report.verdict}")
                if dataset_manifest_hash == "UNKNOWN":
                    reasons.append("dataset_manifest_invalid" if manifest_invalid else "dataset_manifest_unbound")
                if feature_manifest_hash == "UNKNOWN":
                    reasons.append(
                        "feature_manifest_invalid" if feature_manifest_invalid else "feature_manifest_unbound"
                    )
                if self.config.cost_model is None:
                    reasons.append("cost_model_unbound")
                if not self.config.policy_version.strip() or self.config.policy_version.strip().upper() == "UNKNOWN":
                    reasons.append("policy_version_unbound")
                if missing_closed_metadata:
                    reasons.append("closed_bar_metadata_missing")
                bundle.failure_reasons = list(dict.fromkeys(reasons))
                bundle.gate_decision = "PASS" if not bundle.failure_reasons else "FAIL"
                bundle.seal()
                self._store.save_factor_version(
                    f"{symbol}:{bundle.candidate_id}",
                    "2.0.0",
                    bundle.to_dict(),
                )

        # ================================================================
        # Phase 4.5: 交互因子 & 残差因子（基于 PASS 候选的因子值）
        # ================================================================
        passed_screened = [
            record["candidate"] for record in evaluation_records if record["bundle"].gate_decision == "PASS"
        ]
        if len(passed_screened) >= 2:
            # 交互因子：PASS 候选的两两乘法组合
            for i in range(min(len(passed_screened), 5)):
                for j in range(i + 1, min(len(passed_screened), 5)):
                    samples_a = {sample[0]: sample for sample in passed_screened[i].get("_aligned_samples", [])}
                    samples_b = {sample[0]: sample for sample in passed_screened[j].get("_aligned_samples", [])}
                    common_indices = sorted(set(samples_a) & set(samples_b))
                    # 逐对乘法，NaN 安全
                    interact_vals = []
                    interact_returns = []
                    for index in common_indices:
                        sample_a = samples_a[index]
                        sample_b = samples_b[index]
                        interact_vals.append(sample_a[1] * sample_b[1])
                        interact_returns.append(sample_a[2])

                    if len(interact_vals) < 50:
                        continue

                    ic = _compute_ic(interact_vals, interact_returns)
                    inter_id = f"interact_{passed_screened[i].get('factor_id', 'a')}_{passed_screened[j].get('factor_id', 'b')}"
                    inter_hash = hashlib.sha256(inter_id.encode()).hexdigest()[:20]
                    bundle = EvidenceBundle(
                        bundle_id=f"{run_id}-inter-{inter_hash[:8]}",
                        candidate_id=inter_hash,
                        factor_id=inter_id,
                        factor_version="2.0.0",
                        candidate_hash=inter_hash,
                        factor_expression_hash=inter_hash,
                        dataset_manifest_hash=dataset_manifest_hash,
                        feature_manifest_hash=feature_manifest_hash,
                        label_spec_hash=label_spec.to_hash(),
                        cost_model_version=self._label_builder.cost_model_version,
                        policy_version=self.config.policy_version,
                        random_seed=self.config.random_seed,
                        raw_metrics={
                            "ic_mean": round(ic, 6),
                            "sharpe": round(_compute_sharpe(interact_returns), 4),
                            "sample_count": len(interact_vals),
                        },
                        stability_results=[],
                        cost_capacity_results={},
                        # Derived interaction candidates have not gone through
                        # the same purged WFO/CPCV, multiple-testing, cost and
                        # capacity gates as primary candidates.  A positive
                        # in-sample IC is therefore diagnostic only and must
                        # never create a promotable PASS bundle.
                        gate_decision="NOT_VERIFIABLE",
                        failure_reasons=["derived_candidate_requires_full_evaluation"],
                    )
                    bundle.seal()
                    evidence_bundles.append(bundle)
                    self._store.save_factor_version(
                        f"{symbol}:{inter_hash}",
                        "2.0.0",
                        bundle.to_dict(),
                    )

            # 残差因子：对 top-3 PASS 候选做彼此残差化
            top_pass = sorted(
                passed_screened,
                key=lambda c: _compute_ic(
                    [sample[1] for sample in c.get("_aligned_samples", [])],
                    [sample[2] for sample in c.get("_aligned_samples", [])],
                ),
                reverse=True,
            )[:3]
            if len(top_pass) >= 2:
                try:
                    from .generators.residual import compute_residual_values

                    aligned_maps = [
                        {sample[0]: sample for sample in candidate.get("_aligned_samples", [])}
                        for candidate in top_pass
                    ]
                    common_indices = sorted(set.intersection(*(set(item) for item in aligned_maps)))
                    if len(common_indices) < 50:
                        raise ValueError("insufficient common residual observations")
                    base_values = [aligned_maps[0][index][1] for index in common_indices]
                    control_fvs = {
                        candidate.get("factor_id", f"ctrl_{j}"): [
                            aligned_maps[j + 1][index][1] for index in common_indices
                        ]
                        for j, candidate in enumerate(top_pass[1:])
                    }
                    residual_vals = compute_residual_values(base_values, control_fvs)
                    residual_returns = [aligned_maps[0][index][2] for index in common_indices]
                    n_res = min(len(residual_vals), len(residual_returns))
                    res_pairs = [
                        (residual_vals[k], residual_returns[k])
                        for k in range(n_res)
                        if _is_finite(residual_vals[k]) and _is_finite(residual_returns[k])
                    ]
                    if len(res_pairs) >= 50:
                        res_vals = [p[0] for p in res_pairs]
                        res_rets = [p[1] for p in res_pairs]
                        ic = _compute_ic(res_vals, res_rets)
                        res_id = f"residual_{top_pass[0].get('factor_id', 'a')}"
                        res_hash = hashlib.sha256(res_id.encode()).hexdigest()[:20]
                        bundle = EvidenceBundle(
                            bundle_id=f"{run_id}-res-{res_hash[:8]}",
                            candidate_id=res_hash,
                            factor_id=res_id,
                            factor_version="2.0.0",
                            candidate_hash=res_hash,
                            factor_expression_hash=res_hash,
                            dataset_manifest_hash=dataset_manifest_hash,
                            feature_manifest_hash=feature_manifest_hash,
                            label_spec_hash=label_spec.to_hash(),
                            cost_model_version=self._label_builder.cost_model_version,
                            policy_version=self.config.policy_version,
                            random_seed=self.config.random_seed,
                            raw_metrics={
                                "ic_mean": round(ic, 6),
                                "sharpe": round(_compute_sharpe(res_rets), 4),
                                "sample_count": len(res_vals),
                            },
                            stability_results=[],
                            cost_capacity_results={},
                            # Residual candidates have the same evidence gap
                            # as interactions; do not let a raw IC bypass the
                            # primary candidate promotion gates.
                            gate_decision="NOT_VERIFIABLE",
                            failure_reasons=["derived_candidate_requires_full_evaluation"],
                        )
                        bundle.seal()
                        evidence_bundles.append(bundle)
                        self._store.save_factor_version(
                            f"{symbol}:{res_hash}",
                            "2.0.0",
                            bundle.to_dict(),
                        )
                except Exception as exc:
                    logger.warning("residual factor evaluation failed; candidate omitted: %s", type(exc).__name__)

        # ================================================================
        # Phase 5: 多因子汇总
        # ================================================================
        self._notify(progress_callback, "finalizing", 4, 5)

        passed = sum(1 for b in evidence_bundles if b.gate_decision == "PASS")
        runtime = round(time.time() - t0, 2)
        if missing_closed_metadata:
            failure_taxonomy["closed_bar_metadata_missing"] = missing_closed_metadata
        pipeline_status = (
            "COMPLETED"
            if labels and not missing_closed_metadata and not self._generation_policy_error
            else "NOT_VERIFIABLE"
        )

        result = MiningResult(
            run_id=run_id,
            candidates_generated=len(candidates),
            candidates_screened=len(screened),
            candidates_evaluated=len(evidence_bundles),
            candidates_passed=passed,
            evidence_bundles=evidence_bundles,
            failure_taxonomy=failure_taxonomy,
            runtime_seconds=runtime,
            status=pipeline_status,
        )

        self._notify(progress_callback, "complete", 5, 5)
        return result

    # ================================================================
    # 表达式引擎 & 特征字典（懒初始化）
    # ================================================================

    def _ensure_registry(self) -> None:
        """懒初始化 PrimitiveRegistry，注册 OHLCV 特征列。"""
        if self._registry is not None:
            return
        from .expression_ast import ExprType
        from .primitive_library import FeatureDef, PrimitiveRegistry

        self._registry = PrimitiveRegistry()
        for name, etype in [
            ("close", ExprType.PRICE),
            ("open", ExprType.PRICE),
            ("high", ExprType.PRICE),
            ("low", ExprType.PRICE),
            ("volume", ExprType.VOLUME),
            ("log_return", ExprType.RETURN),
            ("spread", ExprType.RATE),
            ("rsi", ExprType.RATE),
        ]:
            self._registry.register(FeatureDef(name=name, type=etype))

    def _build_feature_dict(self, price_points: list[PricePoint]) -> dict[str, list[float]]:
        """从价格序列预计算所有表达式引擎需要的特征列。

        Missing OHLCV columns become NaN and are removed by the quality gates;
        they are never inferred from close or zero.
        """

        def _finite_or_nan(value: float | None) -> float:
            try:
                parsed = float(value) if value is not None else math.nan
            except (TypeError, ValueError, OverflowError):
                return math.nan
            return parsed if math.isfinite(parsed) else math.nan

        closes = [_finite_or_nan(p.close) for p in price_points]
        n = len(closes)
        highs = [_finite_or_nan(p.high) for p in price_points]
        lows = [_finite_or_nan(p.low) for p in price_points]
        volumes = [_finite_or_nan(p.volume) for p in price_points]
        opens = [_finite_or_nan(p.open) for p in price_points]

        # log_return = log(close / lag(close, 1)); the first/invalid row is
        # unavailable, not a zero return.
        log_return = [math.nan] * n
        for i in range(1, n):
            if math.isfinite(closes[i]) and math.isfinite(closes[i - 1]) and closes[i] > 0 and closes[i - 1] > 0:
                log_return[i] = math.log(closes[i] / closes[i - 1])

        # spread = (high - low) / close
        spread = [math.nan] * n
        for i in range(n):
            if (
                math.isfinite(closes[i])
                and closes[i] > 0
                and math.isfinite(highs[i])
                and math.isfinite(lows[i])
                and highs[i] >= lows[i]
            ):
                spread[i] = (highs[i] - lows[i]) / closes[i]

        # RSI(14) — Wilder's smoothing
        rsi = [math.nan] * n
        period = 14
        if n > period:
            gains = [math.nan] * n
            losses = [math.nan] * n
            for i in range(1, n):
                if not (math.isfinite(closes[i]) and math.isfinite(closes[i - 1])):
                    continue
                delta = closes[i] - closes[i - 1]
                if delta > 0:
                    gains[i] = delta
                else:
                    losses[i] = -delta
            if any(not math.isfinite(item) for item in gains[1 : period + 1]) or any(
                not math.isfinite(item) for item in losses[1 : period + 1]
            ):
                gains = []
                losses = []
            if not gains or not losses:
                return self._feature_dict_from_columns(closes, opens, highs, lows, volumes, log_return, spread, rsi)
            avg_gain = sum(gains[1 : period + 1]) / period
            avg_loss = sum(losses[1 : period + 1]) / period
            for i in range(period, n):
                if i > period:
                    avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                    avg_loss = (avg_loss * (period - 1) + losses[i]) / period
                if avg_loss == 0:
                    rsi[i] = 100.0 if avg_gain > 0 else math.nan
                else:
                    rs = avg_gain / avg_loss
                    rsi[i] = 100.0 - 100.0 / (1.0 + rs)

        return self._feature_dict_from_columns(closes, opens, highs, lows, volumes, log_return, spread, rsi)

    def _feature_dict_from_columns(
        self,
        closes: list[float],
        opens: list[float],
        highs: list[float],
        lows: list[float],
        volumes: list[float],
        log_return: list[float],
        spread: list[float],
        rsi: list[float],
    ) -> dict[str, list[float]]:
        self._feature_dict = {
            "close": closes,
            "open": opens,
            "high": highs,
            "low": lows,
            "volume": volumes,
            "log_return": log_return,
            "spread": spread,
            "rsi": rsi,
        }
        return self._feature_dict

    @staticmethod
    def _spec_to_expression(primitive: str, window: int, transform: str, normalization: str) -> str:
        """将 TemplateSpec 的 (primitive, window, transform, normalization) 映射为表达式字符串。"""
        # Step 1: transform
        if transform == "identity":
            expr = primitive
        elif transform == "pct_change":
            expr = f"pct_change({primitive}, {window})"
        elif transform == "zscore":
            expr = f"zscore({primitive}, {window})"
        elif transform == "diff":
            expr = f"diff({primitive}, {window})"
        else:
            expr = primitive

        # Step 2: normalization (叠加在 transform 之上)
        if normalization == "none" or normalization == "":
            return expr
        elif normalization == "zscore":
            return f"zscore({expr}, {window})"
        elif normalization == "robust_zscore":
            return f"robust_zscore({expr}, {window})"
        elif normalization == "rank":
            return f"ts_rank({expr}, {window})"
        return expr

    # ================================================================
    # 候选生成
    # ================================================================

    def _generate_candidates(
        self,
        price_points: list[PricePoint],
        venue: VenueId,
        symbol: InstrumentId,
        timeframe: str,
    ) -> list[dict[str, Any]]:
        """使用 TemplateGridGenerator 生成候选因子网格。

        根据 factor_mining_policy.yaml 中定义的 generation 段配置，
        生成 primitives × windows × transforms × normalizations 的笛卡尔积。
        """
        from .generators.template_grid import TemplateConfig, TemplateGridGenerator

        # 从 policy 中读取配置。仅裸 PipelineConfig 的离线/单测运行允许
        # 显式默认网格；由 policy 文件启动的运行缺事实即不可验证。
        gen_cfg = {}
        policy_path = self.config.policy_path or "config/factor_mining_policy.yaml"
        try:
            import yaml

            with open(policy_path) as f:
                policy = yaml.safe_load(f)
            if not isinstance(policy, dict) or not isinstance(policy.get("generation"), dict):
                raise ValueError("generation_policy_missing")
            gen_cfg = policy["generation"]
        except Exception as exc:
            self._generation_policy_error = f"GENERATION_POLICY_UNAVAILABLE:{type(exc).__name__}"
            if self.config.strict_policy:
                logger.error("factor mining policy unavailable; strict run blocked: %s", type(exc).__name__)
                return []
            logger.warning("factor mining policy unavailable; using local diagnostic defaults: %s", type(exc).__name__)

        template_cfg = gen_cfg.get("template_grid", {})
        if self.config.strict_policy:
            required_keys = {"max_candidates", "template_grid"}
            required_template_keys = {"primitives", "windows", "transforms", "normalizations"}
            if not required_keys.issubset(gen_cfg) or not isinstance(template_cfg, dict):
                self._generation_policy_error = "GENERATION_POLICY_INCOMPLETE"
                return []
            if not required_template_keys.issubset(template_cfg):
                self._generation_policy_error = "GENERATION_TEMPLATE_POLICY_INCOMPLETE"
                return []
        primitives = template_cfg.get("primitives", ["close", "log_return", "volume", "rsi", "spread"])
        windows = template_cfg.get("windows", [5, 10, 20, 50, 100])
        transforms_list = template_cfg.get("transforms", ["identity", "pct_change", "zscore", "diff"])
        normalizations_list = template_cfg.get("normalizations", ["none", "zscore", "robust_zscore", "rank"])
        max_candidates = gen_cfg.get("max_candidates", 10000)

        config = TemplateConfig(
            primitives=primitives,
            windows=windows,
            transforms=transforms_list,
            normalizations=normalizations_list,
            max_combinations=min(max_candidates, 2000),  # 覆盖全窗口网格
        )
        generator = TemplateGridGenerator(config)
        specs = generator.generate_templates()

        # 筛选与标签 horizon 一致的模板，避免 4x 冗余消耗候选上限
        label_horizon = self.config.label_spec.horizon_bars if self.config.label_spec else 4
        specs = [s for s in specs if s.horizon == label_horizon]

        candidates = []
        for spec in specs:
            expr_str = self._spec_to_expression(spec.primitive, spec.window, spec.transform, spec.normalization)
            candidates.append(
                {
                    "factor_id": spec.template_id,
                    "expression_hash": spec.canonical_hash(),
                    "expression_string": expr_str,
                    "complexity_score": round(2.0 + spec.window / 50.0, 2),
                    "generator": "template_grid",
                    "primitive": spec.primitive,
                    "window": spec.window,
                    "transform": spec.transform,
                    "normalization": spec.normalization,
                    "rationale": spec.economic_rationale,
                }
            )

        return candidates

    def _evaluate_candidate(
        self,
        candidate: dict,
        price_points: list[PricePoint],
    ) -> list[float]:
        """使用表达式引擎计算候选因子值。

        通过 PrimitiveRegistry.parse() → Expression.evaluate_series() 求值，
        返回全长度序列（含 NaN warm-up 期），NaN 由 FastScreen 和 Phase 4 对齐处理。
        """
        self._ensure_registry()

        expr_str = candidate.get("expression_string", "")
        if not expr_str:
            return []

        try:
            expr = self._registry.parse(expr_str)
        except Exception as exc:
            logger.warning("factor expression parse failed; candidate rejected: %s", type(exc).__name__)
            return []

        try:
            values = expr.evaluate_series(self._feature_dict)
        except Exception as exc:
            logger.warning("factor expression evaluation failed; candidate rejected: %s", type(exc).__name__)
            return []

        return values

    def _notify(
        self,
        callback: Callable | None,
        stage: str,
        current: int,
        total: int,
    ) -> None:
        if callback:
            try:
                callback(stage, current, total)
            except Exception as exc:
                logger.warning("mining progress callback failed: %s", type(exc).__name__)


# ================================================================
# 辅助统计函数
# ================================================================


def _is_finite(v: float | None) -> bool:
    """检查值是否为有限数值（排除 None/NaN/Inf）。"""
    if v is None or isinstance(v, bool):
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError, OverflowError):
        return False


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


def _timeframe_to_hours(timeframe: str) -> float:
    """Convert a supported bar timeframe to hours for purge/embargo math."""

    value = timeframe.strip().lower()
    if not value:
        return 0.0
    units = {"m": 1 / 60, "h": 1.0, "d": 24.0, "w": 24.0 * 7}
    try:
        return float(value[:-1]) * units[value[-1]]
    except (KeyError, ValueError):
        return 0.0


def _correlation_p_value(correlation: float, sample_length: int) -> float:
    """Conservative normal approximation for a correlation test statistic."""

    if sample_length < 4 or not math.isfinite(correlation) or abs(correlation) >= 1.0:
        return 1.0 if abs(correlation) < 1.0 else 0.0
    z = abs(correlation) * math.sqrt(max(sample_length - 2, 1) / max(1.0 - correlation**2, 1e-12))
    return max(0.0, min(1.0, 2.0 * (1.0 - _normal_cdf(z))))


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
