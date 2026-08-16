"""时间预算强制:policy 的 resources.time_budget_minutes 必须在运行时生效。

动机:factor_miner 曾连续满核运行 6.7 小时,而 policy 声明的
time_budget_minutes: 120 从未被任何代码执行。预算耗尽必须优雅停止
(非异常),已落盘的证据保留,结果对象带 stopped_by_time_budget 标记。
"""

from __future__ import annotations

import yaml

from beidou_research.mining.runner import MiningRunner, PipelineConfig

from tests.unit.test_fw03_e2e_mining import generate_synthetic_ohlcv


class _FakeBudgetClock:
    """可编程单调时钟:第一次调用返回 0,之后返回给定的超时值。"""

    def __init__(self, after_first: float) -> None:
        self._after_first = after_first
        self._calls = 0

    def __call__(self) -> float:
        self._calls += 1
        return self._after_first if self._calls > 1 else 0.0


def _make_runner(evidence_dir: str, time_budget_minutes: float = 0.0) -> MiningRunner:
    return MiningRunner(
        PipelineConfig(
            run_id="budget-test",
            random_seed=42,
            evidence_dir=evidence_dir,
            time_budget_minutes=time_budget_minutes,
        )
    )


def test_budget_zero_means_unlimited(tmp_path):
    """默认(0)预算表示不限制,完整跑完且标记为 False。"""
    runner = _make_runner(str(tmp_path))
    result = runner.run(
        price_data=generate_synthetic_ohlcv(n=500, seed=42),
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
    )
    assert result.stopped_by_time_budget is False
    assert result.candidates_evaluated > 0
    assert result.status == "COMPLETED"


def test_budget_exceeded_stops_gracefully_with_flag(tmp_path):
    """预算耗尽时优雅停止:预筛与评估不再执行,结果带标记而非抛异常。"""
    runner = _make_runner(str(tmp_path), time_budget_minutes=120.0)
    # 时钟在 run() 启动后立刻跳到 1e9 秒之后,首次预算检查即超时。
    runner._budget_clock = _FakeBudgetClock(after_first=1e9)

    result = runner.run(
        price_data=generate_synthetic_ohlcv(n=500, seed=42),
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
    )
    assert result.stopped_by_time_budget is True
    assert result.candidates_screened == 0
    assert result.candidates_evaluated == 0


def test_budget_not_yet_exceeded_runs_normally(tmp_path):
    """时钟未越过预算时,标记保持 False 且流水线正常产出。"""
    runner = _make_runner(str(tmp_path), time_budget_minutes=120.0)
    # 时钟单调不变(始终 0),预算永远未耗尽。
    runner._budget_clock = lambda: 0.0

    result = runner.run(
        price_data=generate_synthetic_ohlcv(n=500, seed=42),
        venue="BINANCE",
        symbol="BTCUSDT",
        timeframe="1h",
    )
    assert result.stopped_by_time_budget is False
    assert result.candidates_evaluated > 0


def test_policy_yaml_parses_time_budget_minutes(tmp_path):
    """from_yaml 必须解析 resources.time_budget_minutes。"""
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "policy_version": "2.0.0",
                "generation": {
                    "random_seed": 42,
                    "template_grid": {
                        "primitives": ["close"],
                        "windows": [5],
                        "transforms": ["identity"],
                        "normalizations": ["none"],
                    },
                },
                "fast_screen": {
                    "min_sample_count": 30,
                    "max_missing_rate": 0.1,
                    "min_variance": 1e-12,
                    "max_extreme_ratio": 0.05,
                    "extreme_sigma": 5.0,
                    "max_complexity_score": 20.0,
                    "min_effective_samples": 20,
                    "max_turnover_pct": 50.0,
                },
                "walk_forward": {
                    "n_folds": 5,
                    "train_fraction": 0.6,
                    "purge_fraction": 0.05,
                    "embargo_fraction": 0.02,
                    "min_train_samples": 100,
                    "min_test_samples": 20,
                    "min_folds_for_verdict": 5,
                },
                "cost": {
                    "taker_fee_bps": 4.0,
                    "maker_fee_bps": 2.0,
                    "avg_spread_bps": 1.0,
                    "slippage_bps": 1.0,
                    "funding_rate_8h_pct": 0.01,
                    "impact_bps_per_10k": 0.1,
                },
                "resources": {"time_budget_minutes": 7},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    config = PipelineConfig.from_yaml(str(policy))
    assert config.time_budget_minutes == 7.0


def test_budget_hint_printed_only_when_stopped(capsys):
    """CLI 提示:预算耗尽时输出警告,正常完成时静默。"""
    from apps.factor_miner.__main__ import _echo_budget_hint
    from beidou_research.mining.runner import MiningResult

    result = MiningResult(
        run_id="x",
        candidates_generated=1,
        candidates_screened=1,
        candidates_evaluated=1,
        candidates_passed=0,
        evidence_bundles=[],
        failure_taxonomy={},
        runtime_seconds=1.0,
        stopped_by_time_budget=True,
    )
    _echo_budget_hint(result)
    captured = capsys.readouterr()
    assert "时间预算耗尽" in captured.err

    result.stopped_by_time_budget = False
    _echo_budget_hint(result)
    captured = capsys.readouterr()
    assert "时间预算耗尽" not in captured.err


def test_policy_yaml_without_resources_defaults_to_unlimited(tmp_path):
    """缺少 resources 段的 policy 回退为 0(不限时),兼容旧配置。"""
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "policy_version": "2.0.0",
                "generation": {
                    "random_seed": 42,
                    "template_grid": {
                        "primitives": ["close"],
                        "windows": [5],
                        "transforms": ["identity"],
                        "normalizations": ["none"],
                    },
                },
                "fast_screen": {
                    "min_sample_count": 30,
                    "max_missing_rate": 0.1,
                    "min_variance": 1e-12,
                    "max_extreme_ratio": 0.05,
                    "extreme_sigma": 5.0,
                    "max_complexity_score": 20.0,
                    "min_effective_samples": 20,
                    "max_turnover_pct": 50.0,
                },
                "walk_forward": {
                    "n_folds": 5,
                    "train_fraction": 0.6,
                    "purge_fraction": 0.05,
                    "embargo_fraction": 0.02,
                    "min_train_samples": 100,
                    "min_test_samples": 20,
                    "min_folds_for_verdict": 5,
                },
                "cost": {
                    "taker_fee_bps": 4.0,
                    "maker_fee_bps": 2.0,
                    "avg_spread_bps": 1.0,
                    "slippage_bps": 1.0,
                    "funding_rate_8h_pct": 0.01,
                    "impact_bps_per_10k": 0.1,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    config = PipelineConfig.from_yaml(str(policy))
    assert config.time_budget_minutes == 0.0
