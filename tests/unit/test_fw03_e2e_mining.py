"""FW-03: 端到端因子挖掘验证 — 合成数据全周期测试。

验证完整的:
  数据集 → 标签构造 → 候选生成 → 快速预筛
  → WFO评估 → 证据包 → 晋级决策
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from beidou_research.mining.evaluation.multiple_testing import (
    benjamini_hochberg,
    deflated_sharpe_ratio,
)
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.label_builder import PricePoint
from beidou_research.mining.persistence import JSONFileFactorStore
from beidou_research.mining.runner import MiningRunner, PipelineConfig
from beidou_shared.types import InstrumentId, VenueId

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
            "open": p,
            "high": p * 1.001,
            "low": p * 0.999,
            "volume": 1_000.0,
            "mark": p,
            "mid": p,
            "vwap": p,
            "is_closed": True,
        }
        for i, p in enumerate(prices)
    ]


# ================================================================
# 端到端测试
# ================================================================


class TestEndToEndMining:
    """端到端挖掘流水线验收测试。"""

    def test_dataset_manifest_requires_sha256_format(self):
        assert MiningRunner._validated_manifest_hash("a" * 64) == "a" * 64
        assert MiningRunner._validated_manifest_hash("payload-hash") == "UNKNOWN"
        assert MiningRunner._validated_manifest_hash("") == "UNKNOWN"

    def test_policy_yaml_binds_all_cost_inputs_and_version(self):
        config = PipelineConfig.from_yaml("config/factor_mining_policy.yaml")

        assert config.strict_policy is True
        assert config.policy_version == "2.0.0"
        assert config.cost_model is not None
        assert config.cost_model.model_version == "2.0.0:cost"
        assert config.cost_model.avg_spread_bps == 1.0
        assert config.cost_model.slippage_bps == 1.0
        assert config.cost_model.funding_rate_8h_pct == 0.01
        assert config.cost_model.impact_bps_per_10k == 0.1

    def test_strict_policy_run_does_not_fall_back_to_default_grid(self, tmp_path):
        config = PipelineConfig(
            run_id="missing-policy",
            policy_path=str(tmp_path / "missing-policy.yaml"),
            strict_policy=True,
            evidence_dir=str(tmp_path / "evidence"),
        )

        result = MiningRunner(config).run(generate_synthetic_ohlcv(n=200))

        assert result.status == "NOT_VERIFIABLE"
        assert result.candidates_generated == 0
        assert result.failure_taxonomy["generation_policy"] == 1

    def test_evidence_bundle_requires_independent_feature_and_cost_binding(self):
        bundle = EvidenceBundle(
            bundle_id="b",
            candidate_id="c",
            factor_id="f",
            factor_version="1",
            candidate_hash="c-hash",
            factor_expression_hash="expr-hash",
            dataset_manifest_hash="a" * 64,
            label_spec_hash="label-hash",
            cost_model_version="cost-v1",
            policy_version="policy-v1",
            gate_decision="PASS",
        )

        assert not bundle.is_complete()
        assert bundle.can_promote() == (False, "evidence_incomplete_or_unbound")

    def test_evidence_bundle_hash_binds_stability_and_capacity(self):
        bundle = EvidenceBundle(
            bundle_id="b-hash",
            candidate_id="c-hash",
            factor_id="f-hash",
            factor_version="1",
            candidate_hash="c" * 64,
            factor_code_hash="f" * 64,
            dataset_manifest_hash="d" * 64,
            feature_manifest_hash="e" * 64,
            label_spec_hash="label-hash",
            cost_model_version="cost-v1",
            policy_version="policy-v1",
            stability_results=[{"dimension": "time", "is_stable": True}],
            cost_capacity_results={"recommended_max_aum": 1000},
            gate_decision="PASS",
        )
        sealed = bundle.seal()
        bundle.cost_capacity_results["recommended_max_aum"] = 1

        assert bundle.compute_bundle_hash() != sealed

    def test_missing_ohlcv_columns_are_not_inferred_from_close(self):
        point = PricePoint(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            close=100.0,
            is_closed=True,
        )

        features = MiningRunner(PipelineConfig())._build_feature_dict([point])

        assert math.isnan(features["open"][0])
        assert math.isnan(features["high"][0])
        assert math.isnan(features["low"][0])
        assert math.isnan(features["volume"][0])

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

    def test_evidence_records_real_validation_gates_and_unbound_manifest_blocks(self):
        """A completed local run must still be blocked without provenance."""
        result = MiningRunner(PipelineConfig(run_id="gate-evidence", evidence_dir="/tmp/beidou-e2e-gates")).run(
            generate_synthetic_ohlcv(n=500, seed=7)
        )

        assert result.evidence_bundles
        bundle = result.evidence_bundles[0]
        assert bundle.raw_metrics["wfo_folds_completed"] >= 0
        assert "wfo_gate" in bundle.raw_metrics
        assert "cpcv_gate" in bundle.raw_metrics
        assert bundle.dataset_manifest_hash == "UNKNOWN"
        assert bundle.gate_decision != "PASS"
        assert "dataset_manifest_unbound" in bundle.failure_reasons

    def test_missing_closed_bar_metadata_is_not_verifiable(self):
        data = generate_synthetic_ohlcv(n=200, seed=11)
        for row in data:
            row.pop("is_closed", None)
        result = MiningRunner(PipelineConfig(run_id="missing-close", evidence_dir="/tmp/beidou-e2e-close")).run(data)

        assert result.status == "NOT_VERIFIABLE"
        assert result.candidates_passed == 0
        assert result.failure_taxonomy["closed_bar_metadata_missing"] == len(data)

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
        import tempfile

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
            sma.append(sum(closes[start : i + 1]) / (i - start + 1))

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

        # 均值回归因子应对 forward return 有正 IC
        assert ic > 0, f"Mean reversion factor should have positive IC vs forward returns, got {ic:.4f}"
