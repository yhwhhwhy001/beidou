"""BF-11: 因子挖掘 CLI 入口。

用法:
    python -m apps.factor_miner run --policy config/factor_mining_policy.yaml
    python -m apps.factor_miner resume --run-id <id>
    python -m apps.factor_miner report --run-id <id>
    python -m apps.factor_miner validate-factor --factor-version <id>
    python -m apps.factor_miner compare --candidate <id> --champion <id>

连接实际的 MiningRunner 流水线: 候选生成 → 预筛 → WFO → 证据包。
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import click

from beidou_research.mining.persistence import JSONFileFactorStore


def _echo_budget_hint(result) -> None:
    """时间预算耗尽提示:优雅停止是预期路径,已落盘证据保留。"""
    if getattr(result, "stopped_by_time_budget", False):
        click.echo("  WARNING: 时间预算耗尽 — 已优雅停止,已保存的证据保留", err=True)


def _resolve_jobs(requested: int, n_symbols: int) -> int:
    """并行进程数收敛:至少 1,不超过品种数。"""
    return max(1, min(requested, n_symbols))


def _run_symbol_worker(payload: dict) -> dict:
    """单品种完整挖掘;串行路径与进程池路径共用(模块级,spawn 可 pickle)。"""
    sym = payload["symbol"]

    from beidou_core.feed import MarketDataFeed
    from beidou_research.data.dataset_manifest import DatasetManifest
    from beidou_research.data.kline_store import KlineStore, frame_to_price_data
    from beidou_research.mining.runner import (
        MiningRunner,
        PipelineConfig,
        compute_feature_manifest_hash,
    )

    store = KlineStore(root=payload["data_root"])
    manifest_hash = ""
    if payload["from_store"] or store.has_data(sym, payload["interval"], min_rows=100):
        if not store.has_data(sym, payload["interval"], min_rows=100):
            raise RuntimeError(f"SYMBOL_LOCAL_DATA_MISSING:{sym}/{payload['interval']}")
        frame = store.load(sym, payload["interval"])
        price_data = frame_to_price_data(frame)
        manifest = DatasetManifest.compute(frame, sym, payload["interval"])
        manifest_hash = DatasetManifest.hash_of(manifest)
        source = "local"
    else:
        feed = MarketDataFeed()
        klines = feed.fetch_klines(sym, interval=payload["interval"], limit=payload["limit"])
        if len(klines) < 100:
            return {"symbol": sym, "skipped": True, "reason": "insufficient_klines"}
        price_data = [
            {
                "timestamp": k["open_time"],
                "close": k["close"],
                "open": k["open"],
                "high": k["high"],
                "low": k["low"],
                "volume": k["volume"],
                "is_closed": k.get("is_closed") is True,
            }
            for k in klines
        ]
        source = "api"

    pipeline_config = PipelineConfig.from_yaml(payload["policy"])
    pipeline_config.evidence_dir = payload["output_dir"]
    pipeline_config.dataset_manifest_hash = manifest_hash
    # GAP-1: 特征 schema manifest 必须独立于 dataset payload 绑定。
    # 本地数据与 API 拉取两条路径共用此处配置构造，均注入。
    pipeline_config.feature_manifest_hash = compute_feature_manifest_hash()
    runner = MiningRunner(pipeline_config)

    result = runner.run(
        price_data=price_data,
        venue="BINANCE",
        symbol=sym,
        timeframe=payload["interval"],
    )
    return {
        "symbol": sym,
        "result": result,
        "manifest_hash": manifest_hash,
        "source": source,
        "rows": len(price_data),
        "pid": os.getpid(),
    }


def _run_symbols(payloads: list[dict], jobs: int) -> list[dict]:
    """执行所有品种,按提交顺序返回结果;jobs > 1 且多品种时用进程池并行。"""
    if jobs <= 1 or len(payloads) <= 1:
        return [_run_symbol_worker(p) for p in payloads]
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(_run_symbol_worker, p): p["symbol"] for p in payloads}
        results: list[dict] = []
        for future in futures:
            symbol = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # 单品种失败不阻断其他品种
                results.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
        return results


def _echo_symbol_result(symbol: str, result, manifest_hash: str) -> None:
    """打印单品种结果;无数据集清单与预算耗尽需显式警告。"""
    if not manifest_hash:
        click.echo("  WARNING: 无数据集清单 — 证据将 FAIL（dataset_manifest_unbound）", err=True)
    _echo_budget_hint(result)
    click.echo(
        f"  候选: {result.candidates_generated} | 预筛: {result.candidates_screened} | 评估: {result.candidates_evaluated} | PASS: {result.candidates_passed} | {result.runtime_seconds:.1f}s"
    )


@click.group()
def cli() -> None:
    """北斗因子挖掘 CLI — 离线研究工具。"""


@cli.command()
@click.option("--policy", required=True, help="因子挖掘 policy YAML 路径")
@click.option("--config", default=None, help="研究数据集配置 YAML 路径")
@click.option("--output-dir", default="evidence/factors", help="证据输出目录")
@click.option("--symbol", default=None, help="单个交易品种（优先于 --symbols）")
@click.option("--symbols", default=None, help="逗号分隔多品种；必须显式提供")
@click.option(
    "--interval",
    default="1h",
    show_default=True,
    type=click.Choice(["1m", "5m", "15m", "1h", "4h", "1d"]),
    help="K 线粒度",
)
@click.option(
    "--limit",
    default=500,
    show_default=True,
    type=click.IntRange(100, 1500),
    help="K 线条数",
)
@click.option("--dry-run", is_flag=True, help="仅验证配置，不执行挖掘")
@click.option("--from-store", is_flag=True, help="强制从本地 parquet 读取（缺失即报错）")
@click.option("--data-root", default=".beidou/data/klines", show_default=True, help="本地 K 线数据根目录")
@click.option(
    "--jobs",
    default=4,
    show_default=True,
    type=click.IntRange(1, 16),
    help="并行品种进程数（1 = 串行）",
)
def run(
    policy: str,
    config: str | None,
    output_dir: str,
    symbol: str | None,
    symbols: str | None,
    interval: str,
    limit: int,
    dry_run: bool,
    from_store: bool,
    data_root: str,
    jobs: int,
) -> None:
    """执行因子挖掘运行。"""
    click.echo(f"[factor_miner] 启动挖掘运行 (policy={policy})")

    # 验证 policy 文件存在
    if not os.path.exists(policy):
        click.echo(f"[factor_miner] ERROR: policy 文件不存在: {policy}", err=True)
        sys.exit(1)

    if dry_run:
        click.echo("[factor_miner] DRY RUN — 仅验证配置")
        import yaml

        with open(policy) as f:
            cfg = yaml.safe_load(f)
        click.echo(f"  生成器: {cfg.get('generation', {}).get('generators', [])}")
        click.echo(f"  最大候选: {cfg.get('generation', {}).get('max_candidates', 'N/A')}")
        click.echo(f"  WFO Folds: {cfg.get('walk_forward', {}).get('n_folds', 'N/A')}")
        click.echo("[factor_miner] 配置验证通过")
        return

    # 解析品种列表
    if symbol:
        symbols_list = [symbol.strip().upper()]
    elif symbols:
        symbols_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        raise click.ClickException("必须通过 --symbol 或 --symbols 显式指定研究品种")
    if not symbols_list or any(item in {"ALL", "DEFAULT"} for item in symbols_list):
        raise click.ClickException("固定 DEFAULT/ALL 交易池已禁用，请指定实际品种")

    # 研究入口只能默认进入零写的 research 环境。
    os.environ.setdefault("BEIDOU_ENV", "research")
    os.makedirs(output_dir, exist_ok=True)

    resolved_jobs = _resolve_jobs(jobs, len(symbols_list))
    click.echo(
        f"[factor_miner] 全量挖掘运行 (品种={symbols_list}, interval={interval}, limit={limit}, jobs={resolved_jobs})..."
    )

    payloads = [
        {
            "symbol": sym,
            "interval": interval,
            "limit": limit,
            "policy": policy,
            "output_dir": output_dir,
            "from_store": from_store,
            "data_root": data_root,
        }
        for sym in symbols_list
    ]

    all_results: list[dict] = []
    had_errors = False
    for item in _run_symbols(payloads, resolved_jobs):
        sym = item["symbol"]
        click.echo(f"\n--- {sym} ---")
        if item.get("error"):
            click.echo(f"  ERROR: {item['error']}", err=True)
            had_errors = True
            continue
        if item.get("skipped"):
            click.echo(f"  跳过 ({item['reason']})")
            continue

        result = item["result"]
        if item.get("source") == "local":
            click.echo(f"  本地数据: {item['rows']} 条 manifest={item['manifest_hash'][:12]}")
        else:
            click.echo(f"  API K线: {item['rows']} 条")
        _echo_symbol_result(sym, result, item.get("manifest_hash", ""))
        all_results.append({"symbol": sym, "result": result})

    # 汇总
    click.echo(f"\n{'=' * 50}")
    click.echo("汇总:")
    for r in all_results:
        sym = r["symbol"]
        res = r["result"]
        click.echo(
            f"  {sym}: 候选{res.candidates_generated} 预筛{res.candidates_screened} PASS {res.candidates_passed} ({res.runtime_seconds:.1f}s)"
        )

    if had_errors:
        sys.exit(1)


@cli.command()
@click.option("--symbols", required=True, help="逗号分隔品种列表")
@click.option("--intervals", default="1h,1d", show_default=True, help="逗号分隔 K 线粒度")
@click.option("--start", required=True, help="起始日期 YYYY-MM-DD（UTC）")
@click.option("--end", default=None, help="结束日期 YYYY-MM-DD（UTC），默认今天")
@click.option("--max-pages", default=400, show_default=True, type=click.IntRange(1, 2000))
@click.option("--data-root", default=".beidou/data/klines", show_default=True)
@click.option("--dry-run", is_flag=True, help="仅打印计划，不拉取")
def backfill(
    symbols: str, intervals: str, start: str, end: str | None, max_pages: int, data_root: str, dry_run: bool
) -> None:
    """批量回填历史 K 线到本地 parquet 存储。"""
    from datetime import datetime, timezone

    from beidou_core.feed import MarketDataFeed
    from beidou_research.data.backfill import backfill_all
    from beidou_research.data.kline_store import KlineStore

    def _to_ms(date_str: str) -> int:
        return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

    symbols_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    intervals_list = [i.strip() for i in intervals.split(",") if i.strip()]
    if not symbols_list or not intervals_list:
        raise click.ClickException("--symbols 与 --intervals 不能为空")
    start_ms = _to_ms(start)
    end_ms = _to_ms(end) if end else int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_ms >= end_ms:
        raise click.ClickException("--start 必须早于 --end")

    if dry_run:
        bar_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
        for symbol in symbols_list:
            for interval in intervals_list:
                span_ms = end_ms - start_ms
                per_bar = bar_ms.get(interval, 3_600_000)
                click.echo(f"  {symbol} {interval}: ~{span_ms // per_bar} 根 ≈ {span_ms // per_bar // 1000 + 1} 页")
        return

    os.environ.setdefault("BEIDOU_ENV", "research")
    feed = MarketDataFeed()
    store = KlineStore(root=data_root)
    reports = backfill_all(feed, store, symbols_list, intervals_list, start_ms, end_ms, max_pages=max_pages)
    failed = [r for r in reports if r["errors"]]
    for r in reports:
        state = "ERROR" if r["errors"] else "OK"
        click.echo(
            f"  [{state}] {r['symbol']} {r['interval']}: pages={r['pages']} rows={r['rows']} manifest={r['manifest_hash'][:12]}"
        )
    if failed:
        sys.exit(1)


@cli.command()
@click.option("--run-id", required=True, help="运行 ID")
def resume(run_id: str) -> None:
    """从检查点恢复挖掘运行。"""
    # M00-F06: 占位实现退役 —— 恢复逻辑从未实现，不得假装成功。
    click.echo(f"[factor_miner] resume 尚未实现 (NOT_IMPLEMENTED): {run_id}")
    sys.exit(2)


@cli.command()
@click.option("--run-id", required=True, help="运行 ID")
@click.option("--format", default="html", type=click.Choice(["html", "json", "parquet"]))
def report(run_id: str, format: str) -> None:
    """生成挖掘运行报告。"""
    click.echo(f"[factor_miner] 生成报告: {run_id} (format={format})")

    # 尝试加载已有证据
    import glob

    evidence_dir = "evidence/factors"
    patterns = [
        f"{evidence_dir}/**/{run_id}*.json",
        f"{evidence_dir}/*.json",
    ]
    found = []
    for pattern in patterns:
        found.extend(glob.glob(pattern, recursive=True))

    if found:
        click.echo(f"[factor_miner] 找到 {len(found)} 个证据文件")
        if format == "json":
            for fpath in found[:5]:
                with open(fpath) as f:
                    data = json.load(f)
                click.echo(json.dumps(data, indent=2, default=str)[:500])
    else:
        click.echo(f"[factor_miner] 未找到运行 {run_id} 的证据文件")


@cli.command()
@click.option("--factor-version", required=True, help="因子版本 ID")
def validate_factor(factor_version: str) -> None:
    """验证因子版本并输出 Gate 状态。"""
    click.echo(f"[factor_miner] 验证因子: {factor_version}")
    store = JSONFileFactorStore("evidence/factors")
    # 解析 factor_id:version
    if ":" in factor_version:
        fid, ver = factor_version.split(":", 1)
        data = store.get_factor_version(fid, ver)
        if data:
            click.echo(f"[factor_miner] 因子 {fid} v{ver}: 已找到")
            click.echo(f"  Gate: {data.get('gate_decision', 'N/A')}")
            click.echo(f"  Artifact: {data.get('artifact_hash', 'N/A')[:16]}")
        else:
            click.echo(f"[factor_miner] 因子 {fid} v{ver}: 未找到")
    else:
        click.echo("[factor_miner] 格式: --factor-version <factor_id>:<version>")


@cli.command()
@click.option("--candidate", required=True, help="候选因子 ID")
@click.option("--champion", required=True, help="冠军因子 ID")
def compare(candidate: str, champion: str) -> None:
    """比较候选因子与冠军因子的增量贡献。"""
    # M00-F06: 占位实现退役 —— 比较逻辑从未实现，不得假装输出分析。
    click.echo(f"[factor_miner] compare 尚未实现 (NOT_IMPLEMENTED): {candidate} vs {champion}")
    sys.exit(2)


if __name__ == "__main__":
    cli()
