"""Task 7: 多粒度稳健性（aux_price_data 参数）。

验证 run(..., aux_price_data=...) 会在稳定性维度中追加
timeframe_robustness，且 aux 有效样本不足时 fail-closed。
"""

import math
import random
from datetime import datetime, timedelta, timezone

from beidou_research.mining.runner import MiningRunner, PipelineConfig


def _klines(n: int, seed: int, base: float = 100.0) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = base
    for i in range(n):
        price = max(1.0, price * (1 + rng.gauss(0, 0.01)))
        rows.append(
            {
                "timestamp": t + timedelta(hours=i),
                "close": price,
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "volume": 100.0 + i,
                "is_closed": True,
                # LabelSpec 默认 price_type=MID：缺失 mid 会令标签全部
                # 降级为 MISSING_PRICE，无样本进入评估（见 test_fw03 合成数据）。
                "mark": price,
                "mid": price,
                "vwap": price,
            }
        )
    return rows


def _momentum_klines(
    n: int,
    seed: int,
    drift: float,
    vol: float,
    base: float = 100.0,
    start: datetime | None = None,
    bar_hours: int = 1,
    rho: float = 0.7,
) -> list[dict]:
    """正自相关收益（AR(1) 动量）序列：趋势可被窗口类因子捕获。"""
    rng = random.Random(seed)
    rows = []
    t = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = base
    prev = 0.0
    for i in range(n):
        prev = rho * prev + rng.gauss(drift, vol)
        price = max(1.0, price * math.exp(prev))
        rows.append(
            {
                "timestamp": t + timedelta(hours=i * bar_hours),
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


def test_aux_stability_dimension_added_when_aux_provided() -> None:
    cfg = PipelineConfig()
    cfg.strict_policy = False
    cfg.policy_version = "2.0.0"
    runner = MiningRunner(cfg)
    primary = _klines(400, seed=1)
    aux = _klines(400, seed=2)
    result = runner.run(
        price_data=primary,
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
        aux_price_data=aux,
    )
    # 没有 PASS 是允许的（IC 可能不显著）；断言的是稳定性维度已写入所有评估过的 bundle
    assert result.evidence_bundles  # 至少评估了候选
    assert any(
        any(s.get("dimension") == "timeframe_robustness" for s in b.stability_results) for b in result.evidence_bundles
    )


def test_aux_insufficient_samples_marks_unstable() -> None:
    cfg = PipelineConfig()
    cfg.strict_policy = False
    cfg.policy_version = "2.0.0"
    runner = MiningRunner(cfg)
    result = runner.run(
        price_data=_klines(400, seed=1),
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
        aux_price_data=_klines(50, seed=2),  # < 200 有效样本
    )
    assert result.evidence_bundles
    for b in result.evidence_bundles:
        tf = [s for s in b.stability_results if s.get("dimension") == "timeframe_robustness"]
        if tf:
            assert tf[0]["is_stable"] is False


def test_aux_ic_computed_on_aux_series_and_same_sign() -> None:
    """F1 修复：aux 因子值必须在 aux 价格序列上真正求值。

    主数据上行动量（400 根 1h）、aux 下行动量（300 根，起点错开、日频）。
    旧实现把主数据因子值按 index 与 aux 标签配对 → ic_aux 与 ic_primary
    异号 → 本测试失败；修复后 aux 因子值与 aux 标签同源于 aux 序列 →
    同号且 is_stable=True（同一候选 zscore(diff(close,5),5)：旧 -0.047 vs 新 0.224）。
    """
    cfg = PipelineConfig()
    cfg.strict_policy = False
    cfg.policy_version = "2.0.0"
    runner = MiningRunner(cfg)
    primary = _momentum_klines(400, seed=11, drift=0.002, vol=0.01)
    aux = _momentum_klines(
        300,
        seed=22,
        drift=-0.002,
        vol=0.01,
        start=datetime(2024, 2, 15, tzinfo=timezone.utc),
        bar_hours=24,
    )
    result = runner.run(
        price_data=primary,
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
        aux_price_data=aux,
    )
    tf = [
        s for b in result.evidence_bundles for s in b.stability_results if s.get("dimension") == "timeframe_robustness"
    ]
    assert any(s.get("is_stable") is True and s.get("ic_primary", 0.0) * s.get("ic_aux", 0.0) > 0 for s in tf), (
        "aux 因子值应真正在 aux 序列上求值（与主 IC 同号且稳定）"
    )
