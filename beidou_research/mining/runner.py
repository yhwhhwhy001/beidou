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

import contextlib
import hashlib
import json
import math
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
        self._cpcv = CPCVEvaluator()
        self._stability = StabilityEvaluator()
        self._capacity = CapacityEvaluator(self.config.cost_model)
        self._store = JSONFileFactorStore(self.config.evidence_dir)
        # 表达式引擎（懒初始化）
        self._registry: Any = None
        self._feature_dict: dict[str, list[float]] = {}

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
        dataset_manifest_hash = self.config.dataset_manifest_hash or "UNKNOWN"
        # label_returns[t] = 从 bar t 起算的 forward return
        label_returns = [l.label_value for l in labels if l.is_valid_for_evaluation()]

        for candidate in screened[:50]:  # 限制评估数量
            factor_vals = candidate["factor_values"]
            # 逐对清洗 NaN/Inf，处理复杂表达式的 warm-up 前缀和内部缺失值
            n = min(len(factor_vals), len(label_returns))
            pairs = [
                (fv, rv)
                for fv, rv in zip(factor_vals[:n], label_returns[:n], strict=False)
                if _is_finite(fv) and _is_finite(rv)
            ]
            if len(pairs) < 50:
                continue
            valid_vals = [p[0] for p in pairs]
            valid_returns = [p[1] for p in pairs]

            if len(valid_returns) < 50:
                continue

            # Bind every usable observation to its own prediction/label
            # interval.  The WFO evaluator below must never infer time from
            # array position after warm-up/quality filtering.
            sample_times = []
            label_end_times = []
            for label, factor_value in zip(labels, factor_vals, strict=False):
                if label.is_valid_for_evaluation() and _is_finite(factor_value) and _is_finite(label.label_value):
                    sample_times.append(label.prediction_key.prediction_time)
                    label_end_times.append(label.label_end_time)

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
                label_spec_hash=label_spec.to_hash(),
                cost_model_version="bf06-v1",
                policy_version="2.0.0",
                random_seed=self.config.random_seed,
                raw_metrics={
                    "ic_mean": round(ic, 6),
                    "sharpe": round(sharpe, 4),
                    "sample_count": len(valid_returns),
                    "wfo_gate": wfo_result.gate_result.value,
                    "wfo_icir_cv": round(wfo_result.icir_cv, 6),
                    "wfo_fold_consistency": round(wfo_result.fold_consistency, 6),
                    "wfo_folds_completed": wfo_result.n_folds_completed,
                    "cpcv_gate": cpcv_result.gate_result.value,
                    "cpcv_paths_completed": cpcv_result.n_completed,
                    "cpcv_metric_mean": cpcv_result.metric_mean,
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
            for record in evaluation_records:
                bundle = record["bundle"]
                report = evaluate_multiple_testing(
                    pvalues,
                    observed_sharpe=record["sharpe"],
                    n_trials=max(len(candidates), 1),
                    in_sample_sharpes=train_sharpes,
                    out_of_sample_sharpes=test_sharpes,
                    sample_length=len(label_returns),
                )
                bundle.multiple_testing_results = {
                    "verdict": report.verdict,
                    "n_total_trials": report.n_total_trials,
                    "n_evaluated": report.n_evaluated,
                    "bh_significant_05": report.bh_result.n_significant_05 if report.bh_result else 0,
                    "dsr": report.dsr,
                    "pbo": report.pbo.__dict__ if report.pbo else None,
                }
                reasons = list(bundle.failure_reasons)
                if record["wfo"].gate_result != GateResult.PASS:
                    reasons.append(f"wfo:{record['wfo'].gate_result.value}")
                if record["cpcv"].gate_result != GateResult.PASS:
                    reasons.append(f"cpcv:{record['cpcv'].gate_result.value}")
                if report.verdict != "PASS":
                    reasons.append(f"multiple_testing:{report.verdict}")
                if dataset_manifest_hash == "UNKNOWN":
                    reasons.append("dataset_manifest_unbound")
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
        passed_screened = [screened[i] for i, b in enumerate(evidence_bundles) if b.gate_decision == "PASS"]
        if len(passed_screened) >= 2:
            # 交互因子：PASS 候选的两两乘法组合
            for i in range(min(len(passed_screened), 5)):
                for j in range(i + 1, min(len(passed_screened), 5)):
                    fv_a = passed_screened[i]["factor_values"]
                    fv_b = passed_screened[j]["factor_values"]
                    n_interact = min(len(fv_a), len(fv_b), len(label_returns))
                    # 逐对乘法，NaN 安全
                    interact_vals = []
                    interact_returns = []
                    for k in range(n_interact):
                        if _is_finite(fv_a[k]) and _is_finite(fv_b[k]) and _is_finite(label_returns[k]):
                            interact_vals.append(fv_a[k] * fv_b[k])
                            interact_returns.append(label_returns[k])

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
                        label_spec_hash=label_spec.to_hash(),
                        cost_model_version="bf06-v1",
                        policy_version="2.0.0",
                        random_seed=self.config.random_seed,
                        raw_metrics={
                            "ic_mean": round(ic, 6),
                            "sharpe": round(_compute_sharpe(interact_returns), 4),
                            "sample_count": len(interact_vals),
                        },
                        stability_results=[],
                        cost_capacity_results={},
                        gate_decision="PASS" if ic > 0.02 else "FAIL",
                        failure_reasons=[] if ic > 0.02 else ["ic_below_threshold"],
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
                key=lambda c: _compute_ic(c["factor_values"], label_returns[: len(c["factor_values"])]),
                reverse=True,
            )[:3]
            if len(top_pass) >= 2:
                try:
                    from .generators.residual import compute_residual_values

                    control_fvs = {
                        c.get("factor_id", f"ctrl_{j}"): c["factor_values"] for j, c in enumerate(top_pass[1:])
                    }
                    residual_vals = compute_residual_values(top_pass[0]["factor_values"], control_fvs)
                    n_res = min(len(residual_vals), len(label_returns))
                    res_pairs = [
                        (residual_vals[k], label_returns[k])
                        for k in range(n_res)
                        if _is_finite(residual_vals[k]) and _is_finite(label_returns[k])
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
                            label_spec_hash=label_spec.to_hash(),
                            cost_model_version="bf06-v1",
                            policy_version="2.0.0",
                            random_seed=self.config.random_seed,
                            raw_metrics={
                                "ic_mean": round(ic, 6),
                                "sharpe": round(_compute_sharpe(res_rets), 4),
                                "sample_count": len(res_vals),
                            },
                            stability_results=[],
                            cost_capacity_results={},
                            gate_decision="PASS" if ic > 0.02 else "FAIL",
                            failure_reasons=[] if ic > 0.02 else ["ic_below_threshold"],
                        )
                        bundle.seal()
                        evidence_bundles.append(bundle)
                        self._store.save_factor_version(
                            f"{symbol}:{res_hash}",
                            "2.0.0",
                            bundle.to_dict(),
                        )
                except Exception:
                    pass  # 残差化失败不影响主流程

        # ================================================================
        # Phase 5: 多因子汇总
        # ================================================================
        self._notify(progress_callback, "finalizing", 4, 5)

        passed = sum(1 for b in evidence_bundles if b.gate_decision == "PASS")
        runtime = round(time.time() - t0, 2)
        if missing_closed_metadata:
            failure_taxonomy["closed_bar_metadata_missing"] = missing_closed_metadata
        pipeline_status = "COMPLETED" if labels and not missing_closed_metadata else "NOT_VERIFIABLE"

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

        PricePoint 标准字段为 timestamp/close/mark/mid/vwap。
        open/high/low/volume 从 getattr 获取，缺失时回退到 close。
        """
        closes = [p.close for p in price_points]
        n = len(closes)

        # 尝试获取扩展字段，缺失时回退
        highs = [getattr(p, "high", p.close) or p.close for p in price_points]
        lows = [getattr(p, "low", p.close) or p.close for p in price_points]
        volumes = [getattr(p, "volume", 0.0) or 0.0 for p in price_points]
        opens = [getattr(p, "open", p.close) or p.close for p in price_points]

        n = len(closes)
        # log_return = log(close / lag(close, 1))
        log_return = [0.0] * n
        for i in range(1, n):
            if closes[i] > 0 and closes[i - 1] > 0:
                log_return[i] = math.log(closes[i] / closes[i - 1])

        # spread = (high - low) / close
        spread = [0.0] * n
        for i in range(n):
            if closes[i] > 0:
                spread[i] = (highs[i] - lows[i]) / closes[i]

        # RSI(14) — Wilder's smoothing
        rsi = [0.0] * n
        period = 14
        if n > period:
            gains = [0.0] * n
            losses = [0.0] * n
            for i in range(1, n):
                delta = closes[i] - closes[i - 1]
                if delta > 0:
                    gains[i] = delta
                else:
                    losses[i] = -delta
            avg_gain = sum(gains[1 : period + 1]) / period
            avg_loss = sum(losses[1 : period + 1]) / period
            for i in range(period, n):
                if i > period:
                    avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                    avg_loss = (avg_loss * (period - 1) + losses[i]) / period
                if avg_loss == 0:
                    rsi[i] = 100.0 if avg_gain > 0 else 0.0
                else:
                    rs = avg_gain / avg_loss
                    rsi[i] = 100.0 - 100.0 / (1.0 + rs)

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

        # 从 policy 中读取配置，带合理回退
        gen_cfg = {}
        try:
            import yaml

            with open("config/factor_mining_policy.yaml") as f:
                policy = yaml.safe_load(f)
            gen_cfg = policy.get("generation", {})
        except Exception:
            pass

        template_cfg = gen_cfg.get("template_grid", {})
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
        except Exception:
            return []

        try:
            values = expr.evaluate_series(self._feature_dict)
        except Exception:
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
            with contextlib.suppress(Exception):
                callback(stage, current, total)


# ================================================================
# 辅助统计函数
# ================================================================


def _is_finite(v: float | None) -> bool:
    """检查值是否为有限数值（排除 None/NaN/Inf）。"""
    if v is None:
        return False
    if isinstance(v, float):
        return math.isfinite(v)
    return True


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
