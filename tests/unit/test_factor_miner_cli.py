"""factor_miner CLI 并行化:worker 提取、--jobs 解析、结果打印编排。

串行路径与进程池路径共用同一 worker(_run_symbol_worker),worker 是
纯函数式模块级函数,保证 macOS spawn 下可 pickle。
"""

from __future__ import annotations

import pytest
import yaml
from click.testing import CliRunner

from apps.factor_miner.__main__ import (
    _echo_symbol_result,
    _resolve_jobs,
    _run_symbol_worker,
    cli,
)
from beidou_research.mining.runner import MiningResult
from tests.unit.test_fw03_e2e_mining import generate_synthetic_ohlcv


def _write_minimal_policy(tmp_path) -> str:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "policy_version": "2.0.0",
                "generation": {
                    "random_seed": 42,
                    "max_candidates": 1000,
                    "generators": ["template_grid"],
                    "template_grid": {
                        "primitives": ["close"],
                        "windows": [5],
                        "transforms": ["pct_change"],
                        "normalizations": ["none"],
                        "horizons": [4],
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
    return str(policy)


def _payload(policy: str, tmp_path, **overrides) -> dict:
    base = {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "limit": 500,
        "policy": policy,
        "output_dir": str(tmp_path / "evidence"),
        "from_store": False,
        "data_root": str(tmp_path / "data"),
    }
    base.update(overrides)
    return base


class _FakeFeed:
    """进程内可替换的 K 线数据源(worker 内部动态 import,可被 monkeypatch)。"""

    def __init__(self, n_bars: int = 500) -> None:
        self._n_bars = n_bars

    def fetch_klines(self, symbol: str, interval: str, limit: int):
        data = generate_synthetic_ohlcv(n=self._n_bars, seed=7)
        return [
            {
                "open_time": d["timestamp"],
                "close": d["close"],
                "open": d["open"],
                "high": d["high"],
                "low": d["low"],
                "volume": d["volume"],
                "is_closed": True,
            }
            for d in data
        ]


def _fake_result(**overrides) -> MiningResult:
    fields = {
        "run_id": "x",
        "candidates_generated": 10,
        "candidates_screened": 5,
        "candidates_evaluated": 3,
        "candidates_passed": 1,
        "evidence_bundles": [],
        "failure_taxonomy": {},
        "runtime_seconds": 1.5,
    }
    fields.update(overrides)
    return MiningResult(**fields)


# ================================================================
# --jobs 解析
# ================================================================


def test_resolve_jobs_clamps_to_symbol_count():
    assert _resolve_jobs(4, 4) == 4
    assert _resolve_jobs(16, 3) == 3
    assert _resolve_jobs(4, 1) == 1
    assert _resolve_jobs(1, 5) == 1


# ================================================================
# worker:单品种完整挖掘
# ================================================================


def test_run_symbol_worker_fetch_path_runs_full_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr("beidou_core.feed.MarketDataFeed", _FakeFeed)
    item = _run_symbol_worker(_payload(_write_minimal_policy(tmp_path), tmp_path))

    assert item["symbol"] == "BTCUSDT"
    assert "skipped" not in item
    result = item["result"]
    assert result.candidates_generated > 0
    assert result.candidates_evaluated > 0
    assert result.stopped_by_time_budget is False
    assert item["manifest_hash"] == ""  # API 路径无数据集清单


def test_run_symbol_worker_skips_when_klines_insufficient(tmp_path, monkeypatch):
    monkeypatch.setattr("beidou_core.feed.MarketDataFeed", lambda: _FakeFeed(n_bars=50))
    item = _run_symbol_worker(_payload(_write_minimal_policy(tmp_path), tmp_path))

    assert item["skipped"] is True
    assert "result" not in item


def test_run_symbol_worker_from_store_missing_data_raises(tmp_path):
    with pytest.raises(RuntimeError, match="SYMBOL_LOCAL_DATA_MISSING"):
        _run_symbol_worker(
            _payload(
                _write_minimal_policy(tmp_path),
                tmp_path,
                from_store=True,
            )
        )


# ================================================================
# 结果打印编排
# ================================================================


def test_echo_symbol_result_prints_summary_and_manifest_warning(capsys):
    _echo_symbol_result("BTCUSDT", _fake_result(), "")
    captured = capsys.readouterr()
    assert "WARNING: 无数据集清单" in captured.err
    assert "候选: 10 | 预筛: 5 | 评估: 3 | PASS: 1" in captured.out


def test_echo_symbol_result_budget_hint(capsys):
    _echo_symbol_result("BTCUSDT", _fake_result(stopped_by_time_budget=True), "a" * 64)
    captured = capsys.readouterr()
    assert "时间预算耗尽" in captured.err
    assert "候选: 10" in captured.out


# ================================================================
# 进程池端到端:spawn 子进程 + 本地 store 真实数据
# ================================================================


def _write_synthetic_store(data_root, symbols: tuple[str, ...]) -> None:
    from beidou_research.data.kline_store import KlineStore

    for sym in symbols:
        data = generate_synthetic_ohlcv(n=300, seed=7)
        klines = [
            {
                "open_time": int(d["timestamp"].timestamp() * 1000),
                "close": d["close"],
                "open": d["open"],
                "high": d["high"],
                "low": d["low"],
                "volume": d["volume"],
                "is_closed": True,
            }
            for d in data
        ]
        KlineStore(root=str(data_root)).append(sym, "1h", klines)


def test_run_symbols_pool_spawns_worker_processes(tmp_path):
    """jobs=2 时每个品种在独立子进程中执行(spawn),结果按提交顺序返回。"""
    import os as _os

    from apps.factor_miner.__main__ import _run_symbols

    data_root = tmp_path / "klines"
    policy = _write_minimal_policy(tmp_path)
    _write_synthetic_store(data_root, ("BTCUSDT", "ETHUSDT"))

    payloads = [_payload(policy, tmp_path, symbol=sym, data_root=str(data_root)) for sym in ("BTCUSDT", "ETHUSDT")]
    items = _run_symbols(payloads, jobs=2)

    assert [i["symbol"] for i in items] == ["BTCUSDT", "ETHUSDT"]
    pids = {i["pid"] for i in items}
    assert _os.getpid() not in pids  # 真正 spawn 了子进程
    assert len(pids) == 2  # 两个品种在不同进程中并行
    for item in items:
        assert "skipped" not in item
        assert item["result"].candidates_generated > 0


def test_pool_worker_is_defined_in_importable_module():
    """CLI 以 ``python -m`` 启动时，spawn 必须能导入 worker。"""
    assert _run_symbol_worker.__module__ == "apps.factor_miner.worker"


def test_cli_run_pool_path_two_symbols_ordered_output(tmp_path):
    data_root = tmp_path / "klines"
    policy = _write_minimal_policy(tmp_path)
    _write_synthetic_store(data_root, ("BTCUSDT", "ETHUSDT"))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "--policy",
            policy,
            "--symbols",
            "BTCUSDT,ETHUSDT",
            "--jobs",
            "2",
            "--data-root",
            str(data_root),
            "--output-dir",
            str(tmp_path / "evidence"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "--- BTCUSDT ---" in result.output
    assert "--- ETHUSDT ---" in result.output
    assert "本地数据: 300 条" in result.output
    assert "候选: " in result.output
    assert "PASS" in result.output


# ================================================================
# CLI 串行编排
# ================================================================


def test_cli_run_serial_uses_worker_per_symbol_in_order(tmp_path, monkeypatch):
    calls: list[str] = []

    def _fake_worker(payload):
        calls.append(payload["symbol"])
        return {
            "symbol": payload["symbol"],
            "result": _fake_result(),
            "manifest_hash": "b" * 64,
            "source": "api",
            "rows": 500,
        }

    monkeypatch.setattr("apps.factor_miner.__main__._run_symbol_worker", _fake_worker)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "--policy",
            _write_minimal_policy(tmp_path),
            "--symbols",
            "BTCUSDT,ETHUSDT",
            "--jobs",
            "1",
            "--data-root",
            str(tmp_path / "data"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == ["BTCUSDT", "ETHUSDT"]
    assert "BTCUSDT" in result.output
    assert "ETHUSDT" in result.output
    assert "候选: 10 | 预筛: 5 | 评估: 3 | PASS: 1" in result.output
