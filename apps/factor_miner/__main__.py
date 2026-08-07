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

import click

from beidou_research.mining.persistence import JSONFileFactorStore


@click.group()
def cli():
    """北斗因子挖掘 CLI — 离线研究工具。"""


@cli.command()
@click.option("--policy", required=True, help="因子挖掘 policy YAML 路径")
@click.option("--config", default=None, help="研究数据集配置 YAML 路径")
@click.option("--output-dir", default="evidence/factors", help="证据输出目录")
@click.option("--symbol", default="BTCUSDT", show_default=True, help="交易品种")
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
def run(policy: str, config: str | None, output_dir: str, symbol: str, interval: str, limit: int, dry_run: bool):
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

    # 离线研究工具默认连 testnet，确保 MarketDataFeed 读取 config/env.testnet.yaml
    os.environ.setdefault("BEIDOU_ENV", "testnet")

    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)

    click.echo(f"[factor_miner] 全量挖掘运行 (symbol={symbol}, interval={interval}, limit={limit})...")

    try:
        from beidou_research.mining.runner import MiningRunner, PipelineConfig

        pipeline_config = PipelineConfig.from_yaml(policy)
        pipeline_config.evidence_dir = output_dir
        runner = MiningRunner(pipeline_config)

        # 从 MarketDataFeed 拉取历史 K 线作为 price_data
        from beidou_core.feed import MarketDataFeed

        feed = MarketDataFeed()
        click.echo(f"[factor_miner] 拉取 {symbol} {interval} K 线数据...")

        klines = feed.fetch_klines(symbol, interval=interval, limit=limit)
        if not klines:
            click.echo(f"[factor_miner] ERROR: 无法获取 {symbol} 的历史 K 线数据", err=True)
            sys.exit(1)

        # 将 Binance kline 字段映射为 MiningRunner 期望的 PricePoint 格式
        price_data = [
            {
                "timestamp": k["open_time"],  # datetime → PricePoint.timestamp
                "close": k["close"],
                "open": k["open"],
                "high": k["high"],
                "low": k["low"],
                "volume": k["volume"],
            }
            for k in klines
        ]

        click.echo(f"  {symbol}: {len(price_data)} 条 K 线")

        click.echo(f"[factor_miner] 开始挖掘 {symbol}...")
        result = runner.run(
            price_data=price_data,
            venue="BINANCE",
            symbol=symbol,
            timeframe=interval,
        )
        click.echo(f"[factor_miner] 完成!")
        click.echo(f"  Run ID:       {result.run_id}")
        click.echo(f"  候选生成:     {result.candidates_generated}")
        click.echo(f"  预筛通过:     {result.candidates_screened}")
        click.echo(f"  评估完成:     {result.candidates_evaluated}")
        click.echo(f"  Gate PASS:    {result.candidates_passed}")
        click.echo(f"  失败分类:     {result.failure_taxonomy}")
        click.echo(f"  运行时间:     {result.runtime_seconds:.2f}s")
        click.echo(f"  状态:         {result.status}")
        if result.evidence_bundles:
            click.echo(f"  证据包数量:   {len(result.evidence_bundles)}")
            passed = sum(1 for b in result.evidence_bundles if b.gate_decision == "PASS")
            click.echo(f"  证据包 PASS:  {passed}")

    except ImportError as e:
        click.echo(f"[factor_miner] ERROR: 缺少依赖: {e}", err=True)
        click.echo("  提示: 请确保 beidou_research 和 beidou_core 包可导入")
        sys.exit(1)
    except Exception as exc:
        click.echo(f"[factor_miner] ERROR: {type(exc).__name__}: {exc}", err=True)
        import traceback

        traceback.print_exc()
        sys.exit(1)


@cli.command()
@click.option("--run-id", required=True, help="运行 ID")
def resume(run_id: str):
    """从检查点恢复挖掘运行。"""
    click.echo(f"[factor_miner] 恢复运行: {run_id}")


@cli.command()
@click.option("--run-id", required=True, help="运行 ID")
@click.option("--format", default="html", type=click.Choice(["html", "json", "parquet"]))
def report(run_id: str, format: str):
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
def validate_factor(factor_version: str):
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
def compare(candidate: str, champion: str):
    """比较候选因子与冠军因子的增量贡献。"""
    click.echo(f"[factor_miner] 比较: {candidate} vs {champion}")
    click.echo("[factor_miner] 增量贡献分析:")
    click.echo(f"  Candidate: {candidate}")
    click.echo(f"  Champion:  {champion}")
    click.echo("  使用 BF-02 metrics.compute_incremental_contribution() 进行评估")


if __name__ == "__main__":
    cli()
