"""Task 13 延伸修复：研究评估门禁语义缺陷测试（GAP-3 / GAP-4 / GAP-5）。

覆盖:
- GAP-3: WFO 门禁 Sharpe 使用策略化收益 sign(pred) * ret（多空组合，方向由
  因子决定），而非与因子无关的原始 test fold forward-return Sharpe。原始收益
  均值 < 0 但因子与收益正相关时，旧逻辑 negative_sharpe_cv 恒拒；
  新逻辑 strategy_sharpe > 0 不触发该失败。
- GAP-4: capacity 门禁在账户规模点（aum_range 首点，默认 10k）判定
  is_cost_viable 与 severe_decay，不再被 50M 最大冲击点恒拒；
  zero_capacity 与空曲线 fail-closed 分支保留。
- GAP-5: 多重检验分母 = 全部被尝试候选（len(candidates)），
  trials_not_fully_evaluated 不再出现。
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from beidou_research.mining.evaluation.cost_capacity import CapacityEvaluator, CostModel
from beidou_research.mining.evaluation.purged_walk_forward import (
    FoldConfig,
    FoldResult,
    PurgedWalkForward,
    PurgedWFOResult,
)
from beidou_research.mining.runner import MiningRunner, PipelineConfig, _compute_sharpe
from tests.unit.test_research_gate_fixes import _mean_reverting_klines

# ================================================================
# 数据辅助
# ================================================================


def _momentum_negative_drift_klines(
    n: int,
    seed: int,
    drift: float = -0.0015,
    vol: float = 0.02,
    rho: float = 0.7,
    base: float = 100.0,
) -> list[dict]:
    """负漂移 + 正自相关（动量）价格序列。

    因子（过去窗口收益）与 4-bar forward return 正相关（动量可被窗口类
    模板捕获），但原始 forward returns 均值 < 0 → 原始收益 Sharpe < 0
    （GAP-3 旧逻辑恒拒的输入）。
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = base
    prev = 0.0
    for i in range(n):
        prev = rho * prev + drift + rng.gauss(0.0, vol)
        price = max(1.0, price * math.exp(prev))
        rows.append(
            {
                "timestamp": t + timedelta(hours=i),
                "close": price,
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "volume": 1000.0 + i,
                "is_closed": True,
                "mark": price,
                "mid": price,
                "vwap": price,
            }
        )
    return rows


def _raw_forward_returns(rows: list[dict], horizon: int = 4) -> list[float]:
    """与 LabelSpec(horizon_bars=4) 同口径的原始 forward returns（对数）。"""
    closes = [float(r["close"]) for r in rows]
    out = []
    for i in range(len(closes) - horizon):
        out.append(math.log(closes[i + horizon] / closes[i]))
    return out


# ================================================================
# GAP-3: WFO 策略化 Sharpe
# ================================================================


def _fold_result(fold_id: int, ic_mean: float, strategy_sharpe: float) -> FoldResult:
    """构造折结果：原始收益 Sharpe 恒定 -0.5（与因子无关），
    strategy_sharpe 为待判定的策略化 Sharpe。"""
    return FoldResult(
        fold_id=fold_id,
        train_samples=100,
        test_samples=80,
        ic_mean=ic_mean,
        sharpe=-0.5,
        strategy_sharpe=strategy_sharpe,
    )


def _run_wfo_aggregation(fold_results: list[FoldResult]) -> PurgedWFOResult:
    """以自定义 evaluator 驱动 PurgedWalkForward 的聚合路径（与 runner
    的 _evaluate_wfo_fold 填字段方式解耦的纯汇总测试）。"""
    config = FoldConfig(
        n_folds=5,
        min_train_samples=20,
        min_test_samples=10,
        min_folds_for_verdict=5,
    )
    pfo = PurgedWalkForward(config)
    times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(600)]
    end_times = [t + timedelta(hours=4) for t in times]
    counter = [0]

    def evaluator(train_indices: list[int], test_indices: list[int]) -> FoldResult:
        del train_indices, test_indices
        idx = counter[0]
        counter[0] += 1
        return fold_results[idx % len(fold_results)]

    return pfo.run(
        times,
        end_times,
        total_duration_days=25.0,
        evaluator=evaluator,
        label_horizon_days=4 / 24,
    )


def test_wfo_gate_uses_strategy_sharpe_not_raw_sharpe() -> None:
    """GAP-3 聚合层：原始收益 Sharpe < 0 但策略化 Sharpe > 0 → 不触发
    negative_sharpe_cv（旧逻辑用 r.sharpe 汇总必然触发）。"""
    folds = [_fold_result(i, 0.1 + 0.1 * i, 0.6) for i in range(5)]
    result = _run_wfo_aggregation(folds)
    assert result.n_folds_completed == 5
    assert result.sharpe_cv == 0.6
    assert "negative_sharpe_cv" not in result.failure_reasons
    assert result.gate_result.value == "PASS"


def test_wfo_gate_rejects_negative_strategy_sharpe() -> None:
    """GAP-3 控制：策略化 Sharpe 为负仍必须 FAIL。"""
    folds = [_fold_result(i, 0.1 + 0.1 * i, -0.6) for i in range(5)]
    result = _run_wfo_aggregation(folds)
    assert result.sharpe_cv == -0.6
    assert "negative_sharpe_cv" in result.failure_reasons
    assert result.gate_result.value == "FAIL"


def test_wfo_legacy_fold_result_defaults_to_neutral_sharpe() -> None:
    """GAP-3 兼容：旧构造（未填 strategy_sharpe）默认 0.0，不误触发失败。"""
    folds = [
        FoldResult(fold_id=i, train_samples=100, test_samples=80, ic_mean=0.1 + 0.1 * i, sharpe=-0.5) for i in range(5)
    ]
    result = _run_wfo_aggregation(folds)
    assert result.sharpe_cv == 0.0
    assert "negative_sharpe_cv" not in result.failure_reasons


def test_wfo_strategy_sharpe_in_pipeline(tmp_path: Path) -> None:
    """GAP-3 端到端：原始 forward-return Sharpe < 0 的数据上，绝大多数候选
    通过 WFO 门禁（旧逻辑对所有候选恒拒 negative_sharpe_cv）。剩余被拒的
    候选是其策略化收益（sign(pred)*ret）真正为负——门禁在正确的口径上工作：
    例如 ts_rank 归一化输出全正信号 → sign(pred)*ret ≈ 原始收益，负漂移下
    策略确实亏损，拒绝是语义正确的。"""
    rows = _momentum_negative_drift_klines(n=1100, seed=7)
    raw = _raw_forward_returns(rows)
    assert _compute_sharpe(raw) < 0, "前置条件：原始收益 Sharpe 必须为负（旧逻辑恒拒的输入）"

    cfg = PipelineConfig(run_id="gap3-wfo", policy_version="2.0.0", evidence_dir=str(tmp_path))
    cfg.strict_policy = False
    result = MiningRunner(cfg).run(
        price_data=rows,
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
    )
    assert result.evidence_bundles, "应产出评估 bundle"
    for bundle in result.evidence_bundles:
        assert bundle.raw_metrics["wfo_folds_completed"] == 5, (
            f"WFO 必须完成 5 折（聚合路径真实执行）: {bundle.factor_id}"
        )
    # 新逻辑：多数候选（固定 seed 下 38/50）不再被 negative_sharpe_cv 拒绝
    passing = sum(
        1 for b in result.evidence_bundles if "negative_sharpe_cv" not in b.raw_metrics["wfo_failure_reasons"]
    )
    assert passing >= len(result.evidence_bundles) // 2, (
        f"策略化 Sharpe 门禁应放行多数候选（旧逻辑全部恒拒），实际 {passing}/{len(result.evidence_bundles)}"
    )


# ================================================================
# GAP-4: capacity 账户规模点
# ================================================================


def _capacity_returns_pattern() -> list[float]:
    """收益序列：gross>0 且 10k 点净收益 > 0，但 50M 最大冲击点净收益 < 0
    （旧逻辑在 50M 点判定 → 恒拒；新逻辑在 10k 账户规模点判定 → 通过）。"""
    return ([0.004] * 4 + [-0.002] * 4) * 12 + [0.004] * 4


def test_capacity_gate_evaluates_at_account_size_point() -> None:
    """GAP-4: is_cost_viable 与 severe_decay 在 aum_range 首点（默认 10k）判定。"""
    model = CostModel(adv_30d=2_000_000, volatility=0.1)
    evaluator = CapacityEvaluator(model)
    predictions = [0.5] * 100
    returns = _capacity_returns_pattern()

    report, gate_ok, reason = evaluator.evaluate_with_gate(predictions, returns)
    assert report.curve[0].aum_level == 10_000, "默认 aum_range 首点应为账户规模 10k"
    assert report.curve[0].net_return > 0
    assert report.curve[-1].net_return < 0, "50M 最大冲击点净收益应为负（旧逻辑恒拒的根源）"
    assert gate_ok, f"账户规模点判定应通过 gate: {reason}"

    # 旧逻辑控制：在 50M 点判定 is_cost_viable 与衰减必然拒绝
    old_viable, old_reason = evaluator.is_cost_viable(report.curve[-1].net_return, report.curve[-1].gross_return)
    assert not old_viable, old_reason
    old_decay = report.curve[-1].net_return / report.curve[-1].gross_return
    assert old_decay < 0.3, "50M 点衰减必然 < 30%（旧 severe_decay 恒拒）"


def test_capacity_gate_zero_capacity_preserved() -> None:
    """GAP-4: 全曲线无正净收益（gross<=0）仍判 zero_capacity 拒绝。"""
    evaluator = CapacityEvaluator(CostModel(adv_30d=2_000_000, volatility=0.1))
    report, gate_ok, reason = evaluator.evaluate_with_gate([0.5] * 100, [-0.01] * 100)
    assert report.capacity_at_zero_return == 0.0
    assert not gate_ok
    assert "zero_capacity" in reason


def test_capacity_gate_empty_curve_fail_closed() -> None:
    """GAP-4 边界：无曲线点 → fail-closed（False + 明确 reason），不崩溃。"""
    evaluator = CapacityEvaluator(CostModel(adv_30d=2_000_000, volatility=0.1))
    report, gate_ok, reason = evaluator.evaluate_with_gate([0.5] * 100, [0.001] * 100, aum_range=[])
    assert report.curve == []
    assert not gate_ok
    assert "capacity_curve_empty" in reason


def test_capacity_gate_net_units_are_annualized() -> None:
    """GAP-4 单位一致性：gross 与成本项同为年化口径
    （net = gross_annual - cost_decimal * trades_per_period * annual_turnover）。"""
    model = CostModel(adv_30d=2_000_000, volatility=0.1)
    evaluator = CapacityEvaluator(model)
    returns = _capacity_returns_pattern()
    report = evaluator.evaluate_capacity_curve([0.5] * 100, returns)

    expected_gross = round(sum(returns) / len(returns) * 365.0, 8)
    assert report.curve[0].gross_return == expected_gross, (
        f"gross 应为 per-period 均值的年化（×365 模块约定）: {report.curve[0].gross_return} vs {expected_gross}"
    )
    # 单位一致后衰减比不受尺度影响，且 net 随 AUM 单调递减
    for i in range(1, len(report.curve)):
        assert report.curve[i].net_return <= report.curve[i - 1].net_return + 1e-10


# ================================================================
# GAP-5: 多重检验全量分母
# ================================================================


def test_multiple_testing_denominator_covers_all_candidates(tmp_path: Path) -> None:
    """GAP-5: 多重检验 pvalues 长度 == len(candidates)（全部被尝试候选），
    trials_not_fully_evaluated 不再出现。"""
    cfg = PipelineConfig(run_id="gap5-mt", policy_version="2.0.0", evidence_dir=str(tmp_path))
    cfg.strict_policy = False
    result = MiningRunner(cfg).run(
        price_data=_mean_reverting_klines(n=600, seed=3),
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
    )
    assert result.evidence_bundles, "应产出评估 bundle"
    n_candidates = result.candidates_generated
    assert n_candidates > len(result.evidence_bundles), "预筛必须淘汰部分候选（分母 > 评估数）"

    for bundle in result.evidence_bundles:
        mt = bundle.multiple_testing_results
        assert mt["n_total_trials"] == n_candidates
        assert mt["n_evaluated"] == n_candidates, "pvalues 长度必须 == len(candidates)"
        assert "trials_not_fully_evaluated" not in mt["failure_reasons"], (
            f"分母覆盖后不应出现 trials_not_fully_evaluated: {bundle.factor_id}"
        )
