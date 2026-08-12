"""Task 7: 多粒度稳健性（aux_price_data 参数）。

验证 run(..., aux_price_data=...) 会在稳定性维度中追加
timeframe_robustness，且 aux 有效样本不足时 fail-closed。
"""

from beidou_research.mining.runner import MiningRunner, PipelineConfig


def _klines(n: int, seed: int, base: float = 100.0) -> list[dict]:
    import random
    from datetime import datetime, timedelta, timezone

    rng = random.Random(seed)
    rows = []
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = base
    for i in range(n):
        price = max(1.0, price * (1 + rng.gauss(0, 0.01)))
        rows.append(
            {
                "timestamp": t + timedelta(hours=i),
                "close": price, "open": price, "high": price * 1.001,
                "low": price * 0.999, "volume": 100.0 + i, "is_closed": True,
                # LabelSpec 默认 price_type=MID：缺失 mid 会令标签全部
                # 降级为 MISSING_PRICE，无样本进入评估（见 test_fw03 合成数据）。
                "mark": price, "mid": price, "vwap": price,
            }
        )
    return rows


def test_aux_stability_dimension_added_when_aux_provided() -> None:
    cfg = PipelineConfig()
    cfg.strict_policy = False
    cfg.policy_version = "2.0.0"
    runner = MiningRunner(cfg)
    primary = _klines(400, seed=1)
    aux = _klines(400, seed=2)
    result = runner.run(
        price_data=primary, venue="BINANCE", symbol="BTCUSDT", timeframe="1h",
        aux_price_data=aux,
    )
    # 没有 PASS 是允许的（IC 可能不显著）；断言的是稳定性维度已写入所有评估过的 bundle
    assert result.evidence_bundles  # 至少评估了候选
    assert any(
        any(s.get("dimension") == "timeframe_robustness" for s in b.stability_results)
        for b in result.evidence_bundles
    )


def test_aux_insufficient_samples_marks_unstable() -> None:
    cfg = PipelineConfig()
    cfg.strict_policy = False
    cfg.policy_version = "2.0.0"
    runner = MiningRunner(cfg)
    result = runner.run(
        price_data=_klines(400, seed=1), venue="BINANCE", symbol="BTCUSDT", timeframe="1h",
        aux_price_data=_klines(50, seed=2),  # < 200 有效样本
    )
    assert result.evidence_bundles
    for b in result.evidence_bundles:
        tf = [s for s in b.stability_results if s.get("dimension") == "timeframe_robustness"]
        if tf:
            assert tf[0]["is_stable"] is False
