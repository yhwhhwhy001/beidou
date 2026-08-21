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
from typing import Any, Callable, cast

import numpy as np

from beidou_shared.types import (
    FactorId,
    GateResult,
    InstrumentId,
    SchemaVersion,
    VenueId,
)

from ..backtest.replay import PaperReplayResult, simulate_paper_window
from ..factors.factor import PROMOTION_EVIDENCE_REQUIREMENTS, FactorLifecycle
from .contracts import (
    LabelRecord,
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
# 特征 schema manifest（GAP-1）
# ================================================================

# 与 _build_feature_dict() 的实际输出键完全一致（排序后）。
# feature_manifest_hash 必须独立于 dataset payload 绑定特征 schema：
# 候选在特征定义变更后重放时，旧证据 hash 立即失效。
FEATURE_SCHEMA_COLUMNS: tuple[str, ...] = (
    "close",
    "high",
    "log_return",
    "low",
    "open",
    "rsi",
    "spread",
    "volume",
)


def compute_feature_manifest_hash() -> str:
    """特征 schema 的确定性 manifest hash（64 hex）。

    只依赖 FEATURE_SCHEMA_COLUMNS 常量，与 _build_feature_dict 输出键
    严格一致（排序后序列化），保证调用方注入的 feature_manifest_hash
    可被 _validated_manifest_hash 接受（64 位 hex）。
    """
    return hashlib.sha256(json.dumps(sorted(FEATURE_SCHEMA_COLUMNS)).encode()).hexdigest()


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
    # resources.time_budget_minutes 运行时强制。0 表示不限时(裸配置/旧 policy)。
    time_budget_minutes: float = 0.0

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
        assert isinstance(generation, dict) and isinstance(fast_screen, dict)
        assert isinstance(walk_forward, dict) and isinstance(cost, dict)

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

        resources = cfg.get("resources")
        time_budget_minutes = 0.0
        if isinstance(resources, dict) and resources.get("time_budget_minutes") is not None:
            time_budget_minutes = float(resources["time_budget_minutes"])

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
            time_budget_minutes=time_budget_minutes,
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
    stopped_by_time_budget: bool = False


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
        # 时间预算:时钟可注入以便测试确定性控制超时。
        self._budget_clock = time.monotonic
        self._budget_deadline: float | None = None
        self._stopped_by_time_budget = False
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

    def _within_time_budget(self) -> bool:
        """时间预算未耗尽返回 True;预算为 0/未设置表示不限时。"""
        return self._budget_deadline is None or self._budget_clock() <= self._budget_deadline

    def _compute_residual_values(
        self,
        factor_values: list[float],
        control_values: dict[str, list[float]],
    ) -> list[float]:
        """残差化(top 候选互残差):懒加载可选生成器并调用其方法。

        compute_residual_values 是 ResidualGenerator 的实例方法而非
        模块级函数;此前按模块级函数导入导致每次运行都吞 ImportError。
        """
        from .generators.residual import ResidualGenerator

        return ResidualGenerator().compute_residual_values(factor_values, control_values)

    def run(
        self,
        price_data: list[dict],
        venue: str = "BINANCE",
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        *,
        aux_price_data: list[dict] | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> MiningResult:
        """执行完整的因子挖掘周期。

        Args:
            price_data: 价格数据列表，每个元素包含 timestamp, close 等
            venue: 交易所
            symbol: 交易品种
            timeframe: K线粒度
            aux_price_data: 辅助粒度价格数据（如 1d），用于多粒度稳健性评估
            progress_callback: 进度回调 (stage, current, total)

        Returns:
            MiningResult
        """
        t0 = time.time()
        self._generation_policy_error = ""
        self._stopped_by_time_budget = False
        self._budget_deadline = None
        if self.config.time_budget_minutes and self.config.time_budget_minutes > 0:
            self._budget_deadline = self._budget_clock() + self.config.time_budget_minutes * 60.0
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

        # 多粒度稳健性：aux 数据（如 1d）的 PricePoint 与标签。
        # 与主粒度同用 label_spec，按 index 对齐后评估 aux IC。
        self._aux_price_points: list[PricePoint] = []
        if aux_price_data:
            self._aux_price_points = [
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
                for d in aux_price_data
            ]

        self._aux_labels = []
        if self._aux_price_points:
            self._aux_labels = self._label_builder.build_labels(
                price_series=self._aux_price_points,
                label_spec=label_spec,
                venue=ven,
                symbol=sym,
                timeframe=timeframe,
                factor_id=FactorId("pipeline"),
                factor_version=SchemaVersion("2.0.0"),
            )
        # aux 特征列：与主数据同构但独立构建（纯函数），
        # 保证表达式真正在 aux 价格序列上求值。
        self._aux_feature_dict: dict[str, list[float]] = {}
        if self._aux_price_points:
            self._aux_feature_dict = self._build_feature_dict(self._aux_price_points)

        # ================================================================
        # Phase 2: 生成候选因子
        # ================================================================
        self._notify(progress_callback, "generating", 1, 5)

        candidates = self._generate_candidates(price_points, ven, sym, timeframe)

        # 预构建特征字典，供 Phase 3/4 中表达式求值使用
        self._feature_dict = self._build_feature_dict(price_points)

        # label_returns[t] = 从 bar t 起算的 forward return。
        # GAP-5: 预筛（Phase 3）之前即构造，预筛循环为每个候选（含被淘汰者）
        # 计算全样本 IC p 值，作为多重检验分母的覆盖。
        label_returns = [l.label_value for l in labels if l.is_valid_for_evaluation()]

        # ================================================================
        # Phase 3: 快速预筛
        # ================================================================
        self._notify(progress_callback, "screening", 2, 5)

        screened = []
        screened_hashes: set[str] = set()
        failure_taxonomy: dict[str, int] = {}
        if self._generation_policy_error:
            failure_taxonomy["generation_policy"] = 1

        for candidate_index, c in enumerate(candidates):
            if not self._within_time_budget():
                self._stopped_by_time_budget = True
                break
            factor_values = self._evaluate_candidate(c, price_points)
            ch = c.get(
                "expression_hash", hashlib.sha256(json.dumps(c, sort_keys=True, default=str).encode()).hexdigest()[:16]
            )

            # GAP-5: 每个被尝试的候选都贡献多重检验分母的 p 值（全样本 IC，
            # 未过 WFO——统计口径诚实；样本不足或无信号时保守取 1.0）。
            # _candidate_index 是该候选在全量 candidates 列表中的位置。
            c["_p_value"] = _candidate_full_sample_p_value(factor_values, labels)
            c["_candidate_index"] = candidate_index

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

        def _aligned_aux_samples(factor_values: list[float]) -> list[tuple[int, float, float]]:
            out: list[tuple[int, float, float]] = []
            for index, label in enumerate(self._aux_labels):
                if index >= len(factor_values) or not label.is_valid_for_evaluation():
                    continue
                value = factor_values[index]
                if not _is_finite(value) or not _is_finite(label.label_value):
                    continue
                out.append((index, float(value), float(label.label_value)))
            return out

        flip_count = 0  # M05-F01 (P0): 翻转数计入多重检验试验预算

        for candidate in screened[:50]:  # 限制评估数量
            if not self._within_time_budget():
                self._stopped_by_time_budget = True
                break
            factor_vals = candidate["factor_values"]
            samples = _aligned_samples(factor_vals)
            if len(samples) < 50:
                continue
            candidate["_aligned_samples"] = samples
            valid_vals = [sample[1] for sample in samples]
            valid_returns = [sample[2] for sample in samples]

            if len(valid_returns) < 50:
                continue

            # ============================================================
            # GAP-2: 短期反转信号方向翻转
            # 真实数据上反转类因子（如 tmpl_close_w50_pct_change_none_h4）
            # 的原始 IC 为负；门禁只接受 ic > 0.02，负方向永不晋级。
            # 因子值取负后重算 IC，后续 WFO/CPCV/稳定性/容量评估与
            # replay 一律使用翻转后的信号；bundle 表达式与 hash 同步
            # 绑定为 -(expr) 形式（PrimitiveRegistry 可解析 Neg 节点）。
            # M05-F01 (P0-08): 方向选择（全样本 IC 符号）本身是一次
            # 额外假设 —— 翻转必须计入多重检验分母,否则 p 值系统性
            # 低估（数据窥探）。
            # ============================================================
            ic = _compute_ic(valid_vals, valid_returns)
            flipped = ic < 0
            if flipped:
                flip_count += 1
                # NaN 取负仍为 NaN，安全；此处 valid_vals 已无 NaN（对齐时过滤）
                valid_vals = [-v for v in valid_vals]
                ic = _compute_ic(valid_vals, valid_returns)
            expr_string = f"-({candidate['expression_string']})" if flipped else candidate["expression_string"]
            expr_hash = (
                hashlib.sha256(expr_string.encode()).hexdigest()[:16] if flipped else candidate["expression_hash"]
            )

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
                train_preds = np.asarray(
                    [_valid_vals[i] for i in train_indices if i < len(_valid_vals)], dtype=np.float64
                )
                train_rets = np.asarray(
                    [_valid_returns[i] for i in train_indices if i < len(_valid_returns)], dtype=np.float64
                )
                test_preds = np.asarray(
                    [_valid_vals[i] for i in test_indices if i < len(_valid_vals)], dtype=np.float64
                )
                test_rets = np.asarray(
                    [_valid_returns[i] for i in test_indices if i < len(_valid_returns)], dtype=np.float64
                )
                if len(test_preds) < 3 or len(train_preds) < 3:
                    return FoldResult(
                        fold_id=fold_id,
                        train_samples=len(train_preds),
                        test_samples=len(test_preds),
                        failure_reason="insufficient_fold_samples",
                    )
                test_ic = _compute_ic_np(test_preds, test_rets)
                train_ic = _compute_ic_np(train_preds, train_rets)
                # GAP-3: 门禁 Sharpe 用策略化收益（多空组合，方向由因子决定），
                # 而非原始 test fold forward-return 的 Sharpe（与因子无关）。
                return FoldResult(
                    fold_id=fold_id,
                    train_samples=len(train_preds),
                    test_samples=len(test_preds),
                    ic_mean=test_ic,
                    ic_std=abs(train_ic - test_ic),
                    icir=test_ic,
                    sharpe=_compute_sharpe_np(test_rets),
                    strategy_sharpe=_compute_sharpe_np(np.sign(test_preds) * test_rets),
                    cost_adjusted_return=float(test_rets.mean()),
                    metrics={
                        "train_ic": train_ic,
                        "test_ic": test_ic,
                        # GAP-6: PBO 的 IS 序列也用策略化收益（方向由因子决定），
                        # 而非与因子无关的原始收益；train_sharpe 保留信息用。
                        "train_sharpe": _compute_sharpe_np(train_rets),
                        "train_strategy_sharpe": _compute_sharpe_np(np.sign(train_preds) * train_rets),
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

            # 快速 IC 评估（ic 已在候选循环开头翻转后重算，此处不再重复）。
            # GAP-6: observed_sharpe（DSR 输入）必须用策略化收益，否则原始收益
            # 均值 < 0 时 DSR 恒不显著。
            sharpe = _compute_sharpe([float(np.sign(v) * r) for v, r in zip(valid_vals, valid_returns, strict=False)])

            # 稳定性评估
            stability_results = [self._stability.evaluate_time_split(valid_vals, valid_returns, ic_full=ic)]

            # 多粒度稳健性：aux 数据（如 1d）上重算 IC，与主粒度同向且
            # 衰减 ≤ 50% 才稳定。aux 样本不足一律不稳（fail-closed）。
            if aux_price_data:
                # 在 aux 序列构建的特征列上求值（与主数据同构、独立构建），
                # 保证 aux 因子值与 aux 标签同源于 aux 价格序列。
                aux_values = self._evaluate_candidate(
                    candidate, self._aux_price_points, feature_dict=self._aux_feature_dict
                )
                # 翻转的候选：aux 信号同样取负，与主粒度同向可比
                # （timeframe_robustness 的 ic*ic_aux>0 判据基于翻转后信号）。
                if flipped:
                    aux_values = [-v for v in aux_values]
                aux_samples = _aligned_aux_samples(aux_values)
                if len(aux_samples) < 200:
                    stability_results.append(
                        cast(
                            Any,
                            {
                                "dimension": "timeframe_robustness",
                                "ic_primary": ic,
                                "ic_aux": 0.0,
                                "degradation_pct": 1.0,
                                "is_stable": False,
                                "aux_samples": len(aux_samples),
                            },
                        )
                    )
                    failure_reasons_aux = "aux_insufficient_samples"
                else:
                    aux_vals = [s[1] for s in aux_samples]
                    aux_rets = [s[2] for s in aux_samples]
                    ic_aux = _compute_ic(aux_vals, aux_rets)
                    degradation = abs(ic - ic_aux) / max(abs(ic), 1e-9)
                    stable_aux = (ic * ic_aux > 0) and degradation <= 0.5
                    stability_results.append(
                        cast(
                            Any,
                            {
                                "dimension": "timeframe_robustness",
                                "ic_primary": ic,
                                "ic_aux": ic_aux,
                                "degradation_pct": degradation,
                                "is_stable": stable_aux,
                                "aux_samples": len(aux_samples),
                            },
                        )
                    )
                    failure_reasons_aux = "" if stable_aux else "timeframe_unstable"
            else:
                failure_reasons_aux = ""

            # 成本容量评估（PKG07: 信号感知）
            # 从 price_data 估算 ADV
            _adv = _estimate_adv_from_price_data(price_data, symbol)
            _vol = _compute_volatility_from_returns(valid_returns)
            if _adv > 0 and self.config.cost_model is not None:
                self.config.cost_model.adv_30d = _adv
                self.config.cost_model.volatility = _vol
                self._capacity = CapacityEvaluator(self.config.cost_model)
            # GAP-4: 容量门禁评估策略化收益（多空组合，方向由翻转后因子决定），
            # 而非与因子无关的原始 forward returns；predictions 保持 valid_vals。
            strategy_returns = [float(np.sign(v) * r) for v, r in zip(valid_vals, valid_returns, strict=False)]
            # GAP-7: excessive_turnover 阈值按 timeframe 传入（高频 K 线
            # 天然换手更高，固定 100 会把 1h 因子恒拒）。
            capacity_result, capacity_gate_ok, capacity_gate_reason = self._capacity.evaluate_with_gate(
                valid_vals,
                strategy_returns,
                avg_daily_volume=_adv if _adv > 0 else None,
                max_annual_turnover=_max_turnover_for_timeframe(timeframe),
            )
            # BD-P1-12: only an externally bound manifest can support a
            # promotion decision.  A local payload hash is not provenance.
            # Fold-level train Sharpe is recorded in metrics when available;
            # keep the explicit arrays for the later multi-test report.
            # GAP-6: PBO 的 IS/OOS 序列取策略化收益 Sharpe（与 observed_sharpe
            # 同口径），否则原始收益口径下 PBO=1.00 恒拒。train_sharpe 保留为
            # 回退（旧 FoldResult 兼容）。
            wfo_train_sharpes = [
                float(r.metrics.get("train_strategy_sharpe", r.metrics.get("train_sharpe", 0.0)))
                for r in wfo_result.fold_results
                if not r.failure_reason
            ]
            wfo_test_sharpes = [
                getattr(r, "strategy_sharpe", r.sharpe) for r in wfo_result.fold_results if not r.failure_reason
            ]
            p_value = _correlation_p_value(ic, len(valid_returns))

            _initial_failures = [] if ic > 0.02 else ["ic_below_threshold"]
            if failure_reasons_aux:
                _initial_failures.append(failure_reasons_aux)

            # replay 只用主粒度全序列（factor_values 含 NaN 由 simulate 内部对齐跳过）。
            # 翻转候选必须重放翻转后的全序列（NaN 取负仍 NaN，simulate 内部跳过）。
            # 返回 None 时 promotion_chain 不产出（fail-closed）。
            all_closes = [float(p.close or 0.0) for p in price_points]
            replay_vals = [-v for v in candidate["factor_values"]] if flipped else candidate["factor_values"]
            # M08-F04: 成本与年化频率从配置绑定 —— 旧实现硬编码
            # cost_bps=8.0 与 √24(仅 1h 正确),证据与成本模型脱节。
            replay_result = simulate_paper_window(
                replay_vals,
                all_closes,
                cost_bps=float(self.config.cost_model.avg_spread_bps) if self.config.cost_model else 8.0,
                bars_per_year=_bars_per_year_for_timeframe(timeframe),
            )

            # 构造证据包
            bundle = EvidenceBundle(
                bundle_id=f"{run_id}-{candidate['hash'][:8]}",
                candidate_id=candidate["hash"],
                factor_id=candidate.get("factor_id", "unknown"),
                factor_version="2.0.0",
                candidate_hash=candidate["hash"],
                factor_expression_hash=expr_hash,
                # M05-R2: 方向翻转审计进 seal hash(篡改检测覆盖方向选择)
                direction_flipped=bool(flipped),
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
                    r
                    if isinstance(r, dict)  # timeframe_robustness：透传完整 aux 评估 dict
                    else {
                        "dimension": r.dimension,
                        "degradation_pct": r.degradation_pct,
                        "is_stable": r.is_stable,
                    }
                    for r in stability_results
                ],
                cost_capacity_results={
                    "recommended_max_aum": capacity_result.recommended_max_aum,
                    "capacity_at_zero_return": capacity_result.capacity_at_zero_return,
                    "capacity_at_half_return": capacity_result.capacity_at_half_return,
                    "signal_decay_ratio": capacity_result.signal_decay_ratio,
                    "avg_turnover": capacity_result.avg_turnover,
                    "adv_used": capacity_result.adv_used,
                    "participation_at_capacity": capacity_result.participation_at_capacity,
                    "gate_passed": capacity_gate_ok,
                    "gate_reason": capacity_gate_reason,
                    "warnings": capacity_result.warnings,
                },
                gate_decision="NOT_VERIFIABLE",
                failure_reasons=_initial_failures,
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
                    # 链构建所需逐候选值：多检验循环中闭包变量已是最后候选的值，
                    # 必须从 record 读取以保证每个 bundle 绑定自己的证据。
                    "ic": ic,
                    "icir_cv": wfo_result.icir_cv,
                    "sample_count": len(valid_returns),
                    "replay_result": replay_result,
                    # 翻转候选绑定 -(expr) 形式的表达式（hash 与之一致）
                    "expression_string": expr_string,
                    # M05-F01: 方向翻转审计（方向选择计入试验预算的证据）
                    "direction_flipped": flipped,
                }
            )
            evidence_bundles.append(bundle)

        # Multi-test correction is a single run-level operation: every
        # candidate attempted by the generator belongs in the denominator.
        if evaluation_records:
            # GAP-5: 全量 pvalues 按 candidates 原始顺序构建，长度 = len(candidates)。
            # 预筛时每个候选已写入 _p_value（未评估者 = 全样本 IC p 值）；
            # 评估候选覆盖为其评估阶段 p 值（post-flip IC，与评估口径一致）。
            evaluated_pvalues: dict[int, float] = {
                record["candidate"]["_candidate_index"]: record["p_value"]
                for record in evaluation_records
                if record["candidate"].get("_candidate_index") is not None
            }
            pvalues = [evaluated_pvalues.get(index, c.get("_p_value", 1.0)) for index, c in enumerate(candidates)]
            # M05-F01: 每个方向翻转 = 一次额外假设 —— 以保守 p=1.0 计入
            # p 值数组（被舍弃的未翻转方向没有独立检验,保守处理),使
            # BH/Holm 分母与 n_trials 一致(翻转不得从分母漏掉)。
            if flip_count > 0:
                pvalues = [*pvalues, *([1.0] * flip_count)]
            train_sharpes = [
                sum(r["train_sharpes"]) / len(r["train_sharpes"]) if r["train_sharpes"] else 0.0
                for r in evaluation_records
            ]
            test_sharpes = [
                sum(r["test_sharpes"]) / len(r["test_sharpes"]) if r["test_sharpes"] else 0.0
                for r in evaluation_records
            ]
            # GAP-9: DSR 是 Harvey-Liu 年化 Sharpe 框架（sharpe_std=0.15 与
            # expected_max=0.15*sqrt(2*ln(n_trials)) 均为年化口径）。observed_sharpe
            # 必须年化（×sqrt(bars_per_year)）、sample_length 必须是年数；否则
            # per-bar 量级（~0.001-0.05）相对 0.513 的 expected_max 恒负 → DSR
            # 恒不显著。PBO 的 IS/OOS 序列保持 per-bar 不动（相对比较，
            # 口径组内一致即可；当前 PBO=0.0 已正常）。
            bars_per_year = _bars_per_year_for_timeframe(timeframe)
            # M05-F01 (P0-08): 试验预算 = 生成候选数 + 方向翻转次数 ——
            # 方向选择是全样本假设,必须计入 BH/Holm/DSR/PBO 的分母。
            n_trials_effective = len(candidates) + flip_count
            for record in evaluation_records:
                bundle = record["bundle"]
                # candidate_index 用该候选在全量 candidates 列表中的位置
                # （BH adjusted p 的索引须与全量 pvalues 对齐）。
                report = evaluate_multiple_testing(
                    pvalues,
                    observed_sharpe=record["sharpe"] * math.sqrt(bars_per_year),
                    n_trials=max(n_trials_effective, 1),
                    in_sample_sharpes=train_sharpes,
                    out_of_sample_sharpes=test_sharpes,
                    sample_length=max(1, round(len(label_returns) / bars_per_year)),
                    candidate_index=record["candidate"].get("_candidate_index"),
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
                elif not capacity_gate_ok:
                    reasons.append(f"capacity_gate:{capacity_gate_reason}")
                if not self.config.policy_version.strip() or self.config.policy_version.strip().upper() == "UNKNOWN":
                    reasons.append("policy_version_unbound")
                if missing_closed_metadata:
                    reasons.append("closed_bar_metadata_missing")
                bundle.failure_reasons = list(dict.fromkeys(reasons))
                bundle.gate_decision = "PASS" if not bundle.failure_reasons else "FAIL"
                bundle.seal()
                payload = bundle.to_dict()
                # 所有 bundle 的 payload 都绑定（可能翻转后的）表达式，
                # 供消费方按表达式对拍，而不仅限于 PASS 晋级路径。
                payload["expression_string"] = record["expression_string"]
                if bundle.gate_decision == "PASS":
                    chain = build_promotion_chain(
                        bundle,
                        ic=record["ic"],
                        icir=record["icir_cv"],
                        sample_count=record["sample_count"],
                        replay=record["replay_result"],
                        git_commit=_current_git_commit(),
                        expression_string=record["expression_string"],
                        role=self._pipeline_role(),
                    )
                    if chain:
                        payload["promotion_chain"] = chain
                        # 链完整性对拍：promotion_chain 不在 bundle artifact_hash 覆盖内，
                        # 单独哈希绑定（与 bundle hash 同模型：防意外损坏/格式漂移，
                        # 非密钥化签名）。桥接侧（EvidenceBridge）逐字节重算对拍。
                        payload["promotion_chain_hash"] = hashlib.sha256(
                            json.dumps(chain, sort_keys=True, default=str).encode()
                        ).hexdigest()
                        payload["evidence_source"] = "historical_replay"
                        payload["role"] = self._pipeline_role()
                self._store.save_factor_version(
                    f"{symbol}:{bundle.candidate_id}",
                    "2.0.0",
                    payload,
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
                    residual_vals = self._compute_residual_values(base_values, control_fvs)
                    residual_returns = [aligned_maps[0][index][2] for index in common_indices]
                    n_res = min(len(residual_vals), len(residual_returns))
                    res_pairs = [
                        (residual_vals[k], residual_returns[k])
                        for k in range(n_res)
                        if _is_finite(residual_vals[k]) and _is_finite(residual_returns[k])
                    ]
                    if len(res_pairs) >= 50:
                        res_vals = [p[0] for p in res_pairs]
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
                            # M05-F06 (P1): 残差因子用全样本 OLS 系数 ——
                            # 系数含未来信息(look-ahead),raw IC 基于泄漏
                            # 数据,不再产出 ic_mean/sharpe,只保留样本计数。
                            # 滚动/扩展窗口系数(PIT 安全)登记 M05-R2。
                            raw_metrics={
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

        mining_result = MiningResult(
            run_id=run_id,
            candidates_generated=len(candidates),
            candidates_screened=len(screened),
            candidates_evaluated=len(evidence_bundles),
            candidates_passed=passed,
            evidence_bundles=evidence_bundles,
            failure_taxonomy=failure_taxonomy,
            runtime_seconds=runtime,
            status=pipeline_status,
            stopped_by_time_budget=self._stopped_by_time_budget,
        )

        self._notify(progress_callback, "complete", 5, 5)
        return mining_result

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

        纯函数：不修改实例状态，返回独立特征字典。主数据特征列由
        Phase 2 显式赋值 self._feature_dict；aux 序列在 run() 内以同样
        方式独立构建（保证表达式真正在 aux 价格序列上求值）。
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
            gains = [0.0] * n
            losses = [0.0] * n
            for i in range(1, n):
                if not (math.isfinite(closes[i]) and math.isfinite(closes[i - 1])):
                    gains[i] = math.nan
                    losses[i] = math.nan
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
        return {
            "close": closes,
            "open": opens,
            "high": highs,
            "low": lows,
            "volume": volumes,
            "log_return": log_return,
            "spread": spread,
            "rsi": rsi,
        }

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
        feature_dict: dict[str, list[float]] | None = None,
    ) -> list[float]:
        """使用表达式引擎计算候选因子值。

        通过 PrimitiveRegistry.parse() → Expression.evaluate_series() 求值，
        返回全长度序列（含 NaN warm-up 期），NaN 由 FastScreen 和 Phase 4 对齐处理。

        feature_dict 缺省使用 self._feature_dict（主数据特征列）；传入 aux
        特征列即可让同一表达式真正在 aux 价格序列上求值。
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

        eval_features = feature_dict if feature_dict is not None else self._feature_dict
        try:
            values = expr.evaluate_series(eval_features)
        except Exception as exc:
            logger.warning("factor expression evaluation failed; candidate rejected: %s", type(exc).__name__)
            return []

        return cast(list[float], values)

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

    def _pipeline_role(self) -> str:
        """因子在 DAG 中的角色；从 policy generation.role 读取，默认 entry。"""
        try:
            import yaml
        except ImportError as exc:
            logger.warning("factor policy role unavailable; defaulting to entry: %s", type(exc).__name__)
            return "entry"

        try:
            policy_path = self.config.policy_path or "config/factor_mining_policy.yaml"
            with open(policy_path) as f:
                policy = yaml.safe_load(f)
            role = str((policy.get("generation", {}) or {}).get("role", "entry")).strip().lower()
            if role in {"entry", "filter", "exit"}:
                return role
        except (OSError, TypeError, ValueError, AttributeError, yaml.YAMLError) as exc:
            logger.warning("factor policy role read failed; defaulting to entry: %s", type(exc).__name__)
        return "entry"


# ================================================================
# 辅助统计函数
# ================================================================


def _estimate_adv_from_price_data(price_data: list[dict], symbol: str) -> float:
    """PKG07: 从 price_data 估算日均交易量（USD）。

    使用 close × volume 估算 notional volume。
    如果 price_data 不足，返回 0.0（ADV 未知）。
    """
    if not price_data:
        return 0.0

    # 筛选该 symbol 的数据点
    relevant = [d for d in price_data if d.get("symbol", symbol) == symbol or symbol in str(d.get("symbol", ""))]
    if not relevant:
        relevant = price_data  # fallback: 使用全部数据

    total_notional = 0.0
    count = 0
    for d in relevant:
        close = float(d.get("close", 0))
        volume = float(d.get("volume", 0))
        if close > 0 and volume > 0 and math.isfinite(close) and math.isfinite(volume):
            total_notional += close * volume
            count += 1

    if count == 0:
        return 0.0
    return total_notional / count


def _compute_volatility_from_returns(returns: list[float]) -> float:
    """PKG07: 从收益序列计算年化波动率。"""
    finite = [r for r in returns if math.isfinite(r)]
    if len(finite) < 2:
        return 0.0
    mean_r = sum(finite) / len(finite)
    var = sum((r - mean_r) ** 2 for r in finite) / (len(finite) - 1)
    daily_vol = math.sqrt(var)
    return daily_vol * math.sqrt(365)


def _is_finite(v: float | None) -> bool:
    """检查值是否为有限数值（排除 None/NaN/Inf）。"""
    if v is None or isinstance(v, bool):
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError, OverflowError):
        return False


def _candidate_full_sample_p_value(factor_values: list[float], labels: list[LabelRecord]) -> float:
    """候选全样本 IC 的 p 值（未过 WFO）。

    GAP-5 多重检验分母覆盖：所有被生成器尝试过的候选都必须计入分母。
    预筛淘汰候选没有评估期 IC，其全样本 IC 是统计口径上唯一诚实的信号估计。

    对齐约定与 Phase 4 的 _aligned_samples 完全一致：labels 按 price-series
    索引枚举，factor_values[index] 对应 labels[index]，仅取两侧有限样本，
    样本量用对齐后的有效对数（len(rets)）。（不能直接对 factor_values 与
    label_returns 做位置 zip：factor_values 含 NaN warm-up 期，NaN 会让 IC
    为 NaN，而 _correlation_p_value 会把 NaN 归零——作为分母 p 值会灾难性
    地低估。）
    样本不足时保守返回 1.0（无证据，不降低显著性门槛）。
    """
    vals: list[float] = []
    rets: list[float] = []
    for index, label in enumerate(labels):
        if index >= len(factor_values) or not label.is_valid_for_evaluation():
            continue
        value = factor_values[index]
        if _is_finite(value) and _is_finite(label.label_value):
            vals.append(float(value))
            rets.append(float(label.label_value))
    if len(vals) < 4:
        return 1.0
    return _correlation_p_value(_compute_ic(vals, rets), len(vals))


# ================================================================
# 逐级证据链（promotion_chain）
# ================================================================

# IDEA→ACTIVE 的 8 级晋级路径；每级转换都须在 FACTOR_LIFECYCLE_TRANSITIONS 中合法。
_PROMOTION_PATH: list[tuple[str, str]] = [
    ("IDEA", "GENERATED"),
    ("GENERATED", "SANITY_PASSED"),
    ("SANITY_PASSED", "RESEARCH_VALIDATED"),
    ("RESEARCH_VALIDATED", "OOS_VERIFIED"),
    ("OOS_VERIFIED", "COST_CAPACITY_VERIFIED"),
    ("COST_CAPACITY_VERIFIED", "PAPER_TRADING"),
    ("PAPER_TRADING", "CHALLENGER"),
    ("CHALLENGER", "ACTIVE"),
]


def build_promotion_chain(
    bundle: EvidenceBundle,
    *,
    ic: float,
    icir: float,
    sample_count: int,
    replay: PaperReplayResult | None,
    git_commit: str,
    expression_string: str,
    role: str,
) -> list[dict] | None:
    """为 PASS bundle 构建 IDEA→ACTIVE 的完整逐级证据链。

    PAPER_TRADING/CHALLENGER 两级依赖 replay 观察；replay 缺失时返回 None
    （不产出可晋级证据链，fail-closed）。
    """
    if replay is None:
        return None
    # M05-F04 (P1): 晋级链逐级经 FactorPromotionGate 验证 —— 旧实现全部
    # approved=True 自证,证据链沦为展示产物(消费方以 approved 记录作为
    # 晋级历史,等于绕过门禁)。现在每级用真实 performance 跑门禁,失败
    # 步 approved=False 且链终止(后续晋级不可能合法)。
    from beidou_research.factors.factor import FactorPerformance, FactorPromotionGate

    gate = FactorPromotionGate()
    performance = FactorPerformance(
        factor_id=bundle.factor_id,
        evaluation_period="historical_replay",
        sample_count=int(sample_count),
        ic_mean=float(ic),
        ic_std=0.0,
        icir=float(icir),
        rank_ic_mean=float(ic),
        rank_ic_std=0.0,
        rank_icir=float(icir),
    )
    chain: list[dict] = []
    for from_state, to_state in _PROMOTION_PATH:
        target = FactorLifecycle(to_state)
        requirements = PROMOTION_EVIDENCE_REQUIREMENTS[target]
        evidence_ids = list(requirements["required_evidence"])
        # M05-R2（对抗审查）: replay 相关步骤必须校验 replay 的真实值
        # （旧实现仅非 None 检查,paper_sharpe/drawdown/paper_ir
        # 从不验证,链可凭声明证据自证到 CHALLENGER）。
        replay_failures: list[str] = []
        if to_state == "PAPER_TRADING":
            if float(getattr(replay, "paper_sharpe", -1.0) or -1.0) <= 0:
                replay_failures.append(f"paper_sharpe={getattr(replay, 'paper_sharpe', None)} <= 0")
            if float(getattr(replay, "paper_drawdown_pct", 0.0) or 0.0) < -50.0:
                replay_failures.append(f"paper_drawdown_pct={getattr(replay, 'paper_drawdown_pct', None)} < -50%")
        # M14-R2: paper_ir(paper PnL 的 per-bar 信息比率)门槛 0.1 ——
        # 仅约束"paper 盈利有信息量",不冒充 IC 选股能力;真实 IC 拦截
        # 由 engine 侧 FactorEvaluator ICIR 门槛(0.3)承担。门槛签名
        # 策略化属研究链演进,登记。
        if to_state == "CHALLENGER" and float(getattr(replay, "paper_ir", -1.0) or -1.0) < 0.1:
            replay_failures.append(f"paper_ir={getattr(replay, 'paper_ir', None)} < 0.1")
        decision = gate.validate_evidence(
            factor_id=bundle.factor_id,
            current_state=FactorLifecycle(from_state),
            target_state=target,
            performance=performance,
            evidence_ids=evidence_ids,
            factor_version=bundle.factor_version,
            commit=git_commit,
            dataset_hash=bundle.dataset_manifest_hash,
            policy_version=bundle.policy_version,
            falsifier="factor-miner",
        )
        step: dict = {
            "from": from_state,
            "to": to_state,
            "approved": decision.approved and not replay_failures,
            "reason": "; ".join([decision.reason, *replay_failures]) if replay_failures else decision.reason,
            "commit": git_commit,
            "dataset_hash": bundle.dataset_manifest_hash,
            "policy_version": bundle.policy_version,
            "falsifier": "factor-miner",
            "evidence_ids": evidence_ids,
            "factor_version": bundle.factor_version,
            "icir": round(icir, 6),
            "ic": round(ic, 6),
            "sample_count": int(sample_count),
            "evidence_source": "historical_replay",
        }
        chain.append(step)
        if not decision.approved or replay_failures:
            break
    return chain


def _current_git_commit() -> str:
    """当前工作区 HEAD commit sha；无法获取时返回空串（链将因绑定缺失被门禁拒绝）。"""
    import subprocess  # nosec B404 - fixed local git inspection command

    try:
        out = subprocess.run(  # nosec B603, B607 - fixed git command, shell disabled
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


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
    return float(cov / (sp * sr))


def _compute_sharpe(returns: list[float]) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = var**0.5
    if std == 0:
        return 0.0
    return float(mean / std)


def _compute_ic_np(preds: np.ndarray, rets: np.ndarray) -> float:
    """numpy 版 Pearson IC；与 _compute_ic 数值等价（对拍测试固化）。"""
    n = min(len(preds), len(rets))
    if n < 3:
        return 0.0
    p = preds[:n].astype(np.float64, copy=False)
    r = rets[:n].astype(np.float64, copy=False)
    mean_p = p.mean()
    mean_r = r.mean()
    cov = float(((p - mean_p) * (r - mean_r)).sum() / (n - 1))
    std_p = float(np.sqrt(((p - mean_p) ** 2).sum() / (n - 1)))
    std_r = float(np.sqrt(((r - mean_r) ** 2).sum() / (n - 1)))
    if std_p == 0 or std_r == 0:
        return 0.0
    return cov / (std_p * std_r)


def _compute_sharpe_np(rets: np.ndarray) -> float:
    """numpy 版 Sharpe；与 _compute_sharpe 数值等价。"""
    n = len(rets)
    if n < 2:
        return 0.0
    r = rets[:n].astype(np.float64, copy=False)
    std = float(np.std(r, ddof=1))
    if std == 0:
        return 0.0
    return float(np.mean(r) / std)


def _max_turnover_for_timeframe(timeframe: str) -> float:
    """GAP-7: 按 K 线粒度返回年化换手率上限。

    依据：250 交易日 × 24/bar_hours × 0.5 次/日（约每两 bar 交易一次），
    bar_hours 从 timeframe 映射；未知粒度保守取 3000（1h 档）。
    高频 K 线天然换手更高，固定阈值会把 1h 因子恒拒。
    """
    mapping: dict[str, float] = {
        "1m": 180_000.0,
        "5m": 36_000.0,
        "15m": 12_000.0,
        "1h": 3_000.0,
        "4h": 1_000.0,
        "1d": 200.0,
    }
    return mapping.get(timeframe.strip().lower(), 3_000.0)


def _bars_per_year_for_timeframe(timeframe: str) -> int:
    """GAP-9: 按 K 线粒度返回每年的 bar 数（年化框架用）。

    DSR（Harvey-Liu）的 sharpe_std=0.15 与 expected_max 均为年化口径，
    observed_sharpe 必须年化（×sqrt(bars_per_year)）、sample_length 必须是
    年数。1h→8760、4h→2190、1d→365 等；未知粒度保守取 1h 档。
    """
    mapping: dict[str, int] = {
        "1m": 525_600,
        "5m": 105_120,
        "15m": 35_040,
        "1h": 8_760,
        "4h": 2_190,
        "1d": 365,
    }
    return mapping.get(timeframe.strip().lower(), 8_760)


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

    if not math.isfinite(correlation):
        return 1.0
    if sample_length < 4 or abs(correlation) >= 1.0:
        return 1.0 if abs(correlation) < 1.0 else 0.0
    z = abs(correlation) * math.sqrt(max(sample_length - 2, 1) / max(1.0 - correlation**2, 1e-12))
    return max(0.0, min(1.0, 2.0 * (1.0 - _normal_cdf(z))))


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
