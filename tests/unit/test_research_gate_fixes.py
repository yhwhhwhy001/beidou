"""Task 13 验收缺口修复测试：负 IC 方向翻转 + feature manifest 注入。

覆盖:
- GAP-2: 反转信号（原始 IC<0）在评估前翻转因子值，bundle ic_mean 不为负，
  payload 表达式绑定 -(expr) 形式且 hash 与表达式一致。
- GAP-1: compute_feature_manifest_hash() 确定性；注入 PipelineConfig 后
  评估 bundle 不再出现 feature_manifest_unbound。
- 纯函数级：-(expr) 包装可被 PrimitiveRegistry 解析求值且符号相反。
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timedelta, timezone

from beidou_research.mining.expression_ast import ExprType
from beidou_research.mining.persistence import JSONFileFactorStore
from beidou_research.mining.primitive_library import FeatureDef, PrimitiveRegistry
from beidou_research.mining.runner import (
    MiningRunner,
    PipelineConfig,
    compute_feature_manifest_hash,
)


def _mean_reverting_klines(n: int, seed: int, rho: float = -0.75, vol: float = 0.02, base: float = 100.0) -> list[dict]:
    """强均值回归（AR(1) rho<0）价格序列。

    过去上涨（pct_change/diff/zscore 为正）之后 4 根 forward return 为负：
    窗口类趋势因子与标签负相关（原始 IC<0），可被 GAP-2 翻转逻辑捕获。
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = base
    prev = 0.0
    for i in range(n):
        prev = rho * prev + rng.gauss(0.0, vol)
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
                # LabelSpec 默认 price_type=MID：缺失 mid 会令标签全部
                # 降级为 MISSING_PRICE，无样本进入评估。
                "mark": price,
                "mid": price,
                "vwap": price,
            }
        )
    return rows


def _run_with(tmp_path, *, inject_feature_manifest: bool) -> MiningRunner:
    cfg = PipelineConfig(run_id="gate-fix-test", policy_version="2.0.0", evidence_dir=str(tmp_path))
    cfg.strict_policy = False
    if inject_feature_manifest:
        cfg.feature_manifest_hash = compute_feature_manifest_hash()
    return MiningRunner(cfg)


def test_negative_ic_flipped_positive(tmp_path) -> None:
    """GAP-2: 反转信号翻转后 ic_mean 不为负，payload 绑定 -(expr) 与一致 hash。"""
    runner = _run_with(tmp_path, inject_feature_manifest=True)
    result = runner.run(
        price_data=_mean_reverting_klines(n=600, seed=3),
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
    )
    assert result.evidence_bundles, "均值回归数据应产出评估 bundle"

    # 翻转逻辑生效：评估产出的 bundle ic_mean 均不为负
    for bundle in result.evidence_bundles:
        assert bundle.raw_metrics["ic_mean"] >= 0.0, (
            f"bundle {bundle.factor_id} ic_mean 应为非负（flipped 逻辑生效），got {bundle.raw_metrics['ic_mean']}"
        )

    # 至少一个候选确实被翻转：payload expression_string 为 -(expr) 形式
    # （f"-({expr})" 包装，以 "-(" 开头）且 factor_expression_hash 与之一致
    store = JSONFileFactorStore(str(tmp_path))
    flipped_payloads = []
    for bundle in result.evidence_bundles:
        payload = store.get_factor_version(f"BTCUSDT:{bundle.candidate_id}", "2.0.0")
        assert payload is not None, f"payload 缺失: {bundle.candidate_id}"
        expr = payload["expression_string"]
        assert expr.startswith("-("), f"翻转 bundle {bundle.factor_id} 表达式应以 '-(' 开头: {expr!r}"
        assert bundle.factor_expression_hash == hashlib.sha256(expr.encode()).hexdigest()[:16], (
            f"factor_expression_hash 与表达式不一致: {bundle.factor_id}"
        )
        assert bundle.raw_metrics["ic_mean"] > 0.0
        flipped_payloads.append(expr)
    assert flipped_payloads, "至少一个候选应被翻转（原始 IC<0）"
    assert any(expr.startswith("-(") for expr in flipped_payloads)


def test_feature_manifest_injected_no_unbound(tmp_path) -> None:
    """GAP-1: 注入 compute_feature_manifest_hash() 后无 feature_manifest_unbound。"""
    h1 = compute_feature_manifest_hash()
    h2 = compute_feature_manifest_hash()
    assert h1 == h2, "compute_feature_manifest_hash() 应确定性"
    assert len(h1) == 64, "manifest hash 必须是 64 位 hex（_validated_manifest_hash 要求）"

    runner = _run_with(tmp_path, inject_feature_manifest=True)
    result = runner.run(
        price_data=_mean_reverting_klines(n=600, seed=3),
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
    )
    assert result.evidence_bundles
    for bundle in result.evidence_bundles:
        assert bundle.feature_manifest_hash == h1
        assert "feature_manifest_unbound" not in bundle.failure_reasons, (
            f"注入 feature manifest 后不应出现 feature_manifest_unbound: {bundle.factor_id}"
        )
        assert "feature_manifest_invalid" not in bundle.failure_reasons


def test_neg_wrapped_expression_parses_and_inverts_sign() -> None:
    """纯函数级：-(expr) 包装可被 PrimitiveRegistry 解析求值且符号相反。"""
    registry = PrimitiveRegistry()
    registry.register(FeatureDef(name="close", type=ExprType.PRICE))

    values = [10.0, 11.0, 12.0, math.nan, 11.5, 10.2, 13.0, 14.1, math.nan, 9.9]
    features: dict[str, list[float]] = {"close": values}

    positive = registry.parse("close").evaluate_series(features)
    negative = registry.parse("-(close)").evaluate_series(features)

    assert len(positive) == len(values) == len(negative)
    for i, (p, n) in enumerate(zip(positive, negative, strict=True)):
        if math.isnan(p) and math.isnan(n):
            continue  # NaN 取负仍 NaN，安全
        assert n == -p, f"index {i}: -(close) 求值应为 close 的相反数: {p} vs {n}"
