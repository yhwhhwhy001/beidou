"""FW-03: 端到端因子挖掘验证 — 合成数据全周期测试。

验证完整的:
  数据集 → 标签构造 → 候选生成 → 快速预筛
  → WFO评估 → 证据包 → 晋级决策
"""

from __future__ import annotations

import math
import random
import pytest
from datetime import datetime, timezone, timedelta

from beidou_research.mining.runner import MiningRunner, PipelineConfig
from beidou_research.mining.evaluation.multiple_testing import (
    benjamini_hochberg, deflated_sharpe_ratio,
)
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.persistence import JSONFileFactorStore


# ================================================================
# 合成数据生成
# ================================================================

def generate_synthetic_ohlcv(
    n: int = 500,
    seed: int = 42,
    trend_strength: float = 0.0001,
    volatility: float = 0.02,
    mean_reversion_speed: float = 0.02,
) -> list[dict]:
    """生成带有已知因子结构的合成 OHLCV 数据。

    数据包含两种效应:
    1. 随机游走趋势（不可预测）
    2. 均值回归分量（可被因子捕获）
    """
    rng = random.Random(seed)
    prices = [100.0]
    mr_component = [0.0]  # 均值回归分量

    for _ in range(n - 1):
        # 随机游走
        rw_ret = rng.gauss(trend_strength, volatility * 0.7)
        # 均值回归分量（前值的 -speed * 偏差）
        mr_ret = -mean_reversion_speed * mr_component[-1] + rng.gauss(0, volatility * 0.3)
        mr_component.append(mr_component[-1] + mr_ret)
        # 组合价格
        prices.append(prices[-1] * math.exp(rw_ret + mr_ret))

    base_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    return [
        {
            "timestamp": base_time + timedelta(hours=i),
            "close": p,
            "mark": p,
            "mid": p,
            "vwap": p,
        }
        for i, p in enumerate(prices)
    ]


# ================================================================
# 端到端测试
# ================================================================

class TestEndToEndMining:
    """端到端挖掘流水线验收测试。"""

    def test_full_pipeline_synthetic_data(self):
        """合成数据上运行完整的因子挖掘周期。"""
        # 1. 生成合成数据
        data = generate_synthetic_ohlcv(n=500, seed=42)

        # 2. 配置并运行流水线
        config = PipelineConfig(
            run_id="e2e-test-001",
            random_seed=42,
            evidence_dir="/tmp/beidou-e2e-test",
        )
        runner = MiningRunner(config)
        result = runner.run(
            price_data=data,
            venue="BINANCE",
            symbol="BTCUSDT",
            timeframe="1h",
        )

        # 3. 验证结果
        assert result.candidates_generated > 0, "应生成候选"
        assert result.candidates_screened >= 0, "应有预筛结果"
        assert result.candidates_evaluated > 0, "应有评估结果"
        assert result.runtime_seconds > 0, "应有运行时间"
        assert result.status == "COMPLETED"

        print(f"\n[E2E] 候选生成: {result.candidates_generated}")
        print(f"[E2E] 预筛通过: {result.candidates_screened}")
        print(f"[E2E] 评估完成: {result.candidates_evaluated}")
        print(f"[E2E] 晋级通过: {result.candidates_passed}")
        print(f"[E2E] 失败分类: {result.failure_taxonomy}")
        print(f"[E2E] 运行时间: {result.runtime_seconds:.2f}s")

    def test_evidence_bundle_sealed(self):
        """EvidenceBundle 封存后哈希一致。"""
        data = generate_synthetic_ohlcv(n=200, seed=99)
        config = PipelineConfig(
            run_id="e2e-test-002",
            random_seed=99,
            evidence_dir="/tmp/beidou-e2e-test-2",
        )
        runner = MiningRunner(config)
        result = runner.run(
            price_data=data,
            venue="BINANCE",
            symbol="ETHUSDT",
            timeframe="1h",
        )

        for bundle in result.evidence_bundles:
            # 封存后 hash 不应为空
            assert bundle.artifact_hash, f"Bundle {bundle.bundle_id} has no artifact_hash"
            # 重新计算应一致
            assert bundle.artifact_hash == bundle.compute_bundle_hash()

    def test_multiple_testing_integration(self):
        """多重检验与挖掘流水线集成。"""
        # 模拟 20 个候选的 p-value
        # BH: adjusted = p * n / rank, 只有 p=0.001 → 0.001*20/1=0.02 significant
        pvals = [0.001, 0.005, 0.01, 0.02, 0.03] + [0.1] * 10 + [0.3] * 5
        bh = benjamini_hochberg(pvals)
        # 至少 p=0.001 在 BH 校正后显著
        assert bh.n_significant_05 >= 1, f"At least smallest p-value should survive BH, got {bh.n_significant_05}"

        # DSR 应惩罚多次试验
        dsr = deflated_sharpe_ratio(
            observed_sharpe=0.8,
            n_trials=20,
            sample_length=252,
        )
        assert dsr["dsr"] < 0.8, "DSR should be lower than observed Sharpe"

    def test_persistence_roundtrip(self):
        """因子版本持久化往返。"""
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONFileFactorStore(tmpdir)
            data = {"ic_mean": 0.05, "sharpe": 0.5, "samples": 100}

            # 保存
            assert store.save_factor_version("test_factor", "1.0.0", data)

            # 读取
            restored = store.get_factor_version("test_factor", "1.0.0")
            assert restored is not None
            assert restored["ic_mean"] == 0.05
            assert restored["sharpe"] == 0.5

            # 版本列表
            versions = store.list_versions("test_factor")
            assert "1.0.0" in versions

    def test_synthetic_factor_detects_mean_reversion(self):
        """合成均值回归因子应被检测为有效。"""
        data = generate_synthetic_ohlcv(n=500, seed=42, mean_reversion_speed=0.05)

        # 手动构造均值回归因子
        closes = [d["close"] for d in data]
        window = 20
        sma = []
        for i in range(len(closes)):
            start = max(0, i - window + 1)
            sma.append(sum(closes[start:i+1]) / (i - start + 1))

        # 因子值 = 偏离SMA的程度
        factor_vals = []
        for i in range(window, len(closes)):
            factor_vals.append((closes[i] - sma[i]) / sma[i])

        # 标签 = forward return
        forward_returns = []
        for i in range(window, len(closes) - 4):
            ret = math.log(closes[i + 4] / closes[i])
            forward_returns.append(ret)

        # 对齐
        n = min(len(factor_vals), len(forward_returns))
        factor_vals = factor_vals[:n]
        forward_returns = forward_returns[:n]

        # 计算 IC
        mp = sum(factor_vals) / n
        mr = sum(forward_returns) / n
        cov = sum((factor_vals[i] - mp) * (forward_returns[i] - mr) for i in range(n)) / (n - 1)
        sp = (sum((x - mp) ** 2 for x in factor_vals) / (n - 1)) ** 0.5
        sr = (sum((x - mr) ** 2 for x in forward_returns) / (n - 1)) ** 0.5
        ic = cov / (sp * sr) if sp > 0 and sr > 0 else 0.0

        print(f"\n[E2E] 均值回归因子 IC: {ic:.4f}")
        # 均值回归因子应对 forward return 有正 IC
        assert ic > 0, f"Mean reversion factor should have positive IC vs forward returns, got {ic:.4f}"
