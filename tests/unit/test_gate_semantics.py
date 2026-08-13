"""Task 13 延伸修复：研究评估门禁语义缺陷测试（GAP-3 ~ GAP-8）。

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
- GAP-6: 策略化收益补全——DSR 的 observed_sharpe 与 PBO 的 IS/OOS 序列
  全部取策略化收益（原始收益均值 < 0 时 observed_sharpe 恒负、PBO=1.00）。
- GAP-7: excessive_turnover 阈值按 K 线粒度（timeframe-aware），1h 因子
  自然换手水平（~365x/年）不再被固定 100 阈值恒拒。
- GAP-8: CPCV q05 改为 95% 置信下界判定（q05 + 1.645*se <= 0 才 FAIL），
  个别路径略负不再判 non_positive_oos_q05。
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from beidou_research.mining.evaluation.cost_capacity import CapacityEvaluator, CostModel
from beidou_research.mining.evaluation.cpcv import CPCVEvaluator
from beidou_research.mining.evaluation.purged_walk_forward import (
    FoldConfig,
    FoldResult,
    PurgedWalkForward,
    PurgedWFOResult,
)
from beidou_research.mining.runner import (
    MiningRunner,
    PipelineConfig,
    _compute_sharpe,
    _max_turnover_for_timeframe,
)
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


# ================================================================
# GAP-6: 策略化收益补全（DSR observed_sharpe / PBO IS-OOS 序列）
# ================================================================


def test_observed_sharpe_and_pbo_use_strategy_returns(tmp_path: Path) -> None:
    """GAP-6 端到端：原始 forward-return Sharpe < 0 的数据上——
    1) raw_metrics["sharpe"]（DSR 的 observed_sharpe 输入）多数候选为正
       （旧逻辑恒负 → DSR 恒不显著）；
    2) PBO 的 IS/OOS 序列来自策略化收益，不再是退化值 1.00（旧逻辑恒拒）。"""
    rows = _momentum_negative_drift_klines(n=1100, seed=7)
    raw = _raw_forward_returns(rows)
    assert _compute_sharpe(raw) < 0, "前置条件：原始收益 Sharpe 必须为负（旧逻辑恒拒的输入）"

    cfg = PipelineConfig(run_id="gap6-strategy", policy_version="2.0.0", evidence_dir=str(tmp_path))
    cfg.strict_policy = False
    result = MiningRunner(cfg).run(
        price_data=rows,
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
    )
    assert result.evidence_bundles, "应产出评估 bundle"

    # observed_sharpe 来自策略化收益：多数候选为正（旧逻辑全部为负）
    assert any(b.raw_metrics["sharpe"] > 0 for b in result.evidence_bundles), (
        "策略化收益下至少一个候选 observed_sharpe 应为正"
    )
    # PBO 输入为策略化收益（跨 bundle 相同序列）：不再是 1.00 退化值。
    # 固定 seed 下实测 PBO=0.0（16 个组合全部一致），断言 < 0.5 留足余量。
    for bundle in result.evidence_bundles:
        mt = bundle.multiple_testing_results
        assert mt["pbo"] is not None, f"PBO 必须存在: {bundle.factor_id}"
        assert mt["pbo"]["pbo"] < 0.5, f"PBO 不应为退化值（策略化收益 IS/OOS）: {bundle.factor_id}"


# ================================================================
# GAP-7: excessive_turnover 阈值按粒度
# ================================================================


def test_turnover_threshold_is_timeframe_aware() -> None:
    """GAP-7: 每 bar 换向的收益序列年化换手 ≈ 365（1h 因子的自然水平）——
    默认阈值 100 拒绝（旧逻辑恒拒），按 1h 上限 3000 传入后不再
    触发 excessive_turnover（其余门禁在账户规模点仍通过）。"""
    model = CostModel(adv_30d=2_000_000, volatility=0.1)
    evaluator = CapacityEvaluator(model)
    predictions = [0.5] * 102
    returns = [0.004, -0.002] * 51  # 相邻符号全翻转 → 年化换手 ≈ 365

    # 默认阈值（向后兼容）：> 100 → excessive_turnover 拒绝
    report_default, ok_default, reason_default = evaluator.evaluate_with_gate(predictions, returns)
    assert report_default.avg_turnover > 100.0
    assert not ok_default
    assert "excessive_turnover" in reason_default

    # 1h 粒度上限 3000：365 ≤ 3000 → 通过
    report_1h, ok_1h, reason_1h = evaluator.evaluate_with_gate(predictions, returns, max_annual_turnover=3000.0)
    assert report_1h.avg_turnover <= 3000.0
    assert ok_1h, f"1h 上限下应通过 gate: {reason_1h}"
    assert "excessive_turnover" not in reason_1h

    # 粒度映射：250 交易日 × 24/bar_hours × 0.5 次/日
    assert _max_turnover_for_timeframe("1m") == 180_000.0
    assert _max_turnover_for_timeframe("5m") == 36_000.0
    assert _max_turnover_for_timeframe("15m") == 12_000.0
    assert _max_turnover_for_timeframe("1h") == 3_000.0
    assert _max_turnover_for_timeframe("4h") == 1_000.0
    assert _max_turnover_for_timeframe("1d") == 200.0
    assert _max_turnover_for_timeframe("unknown-tf") == 3_000.0  # 未知粒度保守取 1h 档


# ================================================================
# GAP-8: CPCV q05 置信下界判定
# ================================================================


def test_cpcv_q05_uses_confidence_lower_bound() -> None:
    """GAP-8: metric 均值 > 0 但个别路径 q05 略负（36 值中 2 个负、其余正）时——
    旧逻辑（q05 <= 0 即 FAIL）拒绝，新逻辑（q05 + 1.645*se <= 0 才 FAIL）通过。"""
    evaluator = CPCVEvaluator()
    n_groups, group_size = 8, 60
    n = n_groups * group_size
    rng_p = random.Random(10)  # 该种子下 q05 ≈ -0.011（略负），置信下界 > 0
    rng_r = random.Random(1010)
    predictions: list[float] = []
    returns: list[float] = []
    for g in range(n_groups):
        if g < 6:
            # 好组：预测 = 收益（完美相关 IC=1.0），方差极小
            for _ in range(group_size):
                r = rng_r.gauss(0.0, 0.0001)
                predictions.append(r)
                returns.append(r)
        else:
            # 坏组：预测与收益独立（IC ≈ 0，采样噪声），方差大
            for _ in range(group_size):
                predictions.append(rng_p.gauss(0.0, 1.0))
                returns.append(rng_r.gauss(0.0, 1.0))
    group_labels = [i // group_size for i in range(n)]

    result = evaluator.evaluate(predictions, returns, group_labels=group_labels)
    assert result.n_completed == 28, "C(8,2) 路径应全部完成"
    assert result.metric_mean > 0, "构造数据均值必须 > 0（mean 检查不变）"
    assert result.metric_q05 < 0, "构造数据应使 q05 < 0（旧逻辑必 FAIL）"
    assert "non_positive_oos_q05" not in result.failure_reasons, (
        f"置信下界应 > 0（q05={result.metric_q05}）: {result.failure_reasons}"
    )
    assert result.gate_result.value == "PASS", f"置信下界判定应 PASS: {result.failure_reasons}"
