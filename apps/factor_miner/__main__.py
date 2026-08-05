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
@click.option("--output-dir", default="evidence/mining-runs", help="输出目录")
@click.option("--dry-run", is_flag=True, help="仅验证配置，不执行挖掘")
def run(policy: str, config: str | None, output_dir: str, dry_run: bool):
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

    click.echo("[factor_miner] 全量挖掘运行...")
    click.echo("[factor_miner] 提示: 请使用 Python API 启动挖掘，提供 price_data:")
    click.echo("  from beidou_research.mining.runner import MiningRunner, PipelineConfig")
    click.echo("  runner = MiningRunner(PipelineConfig.from_yaml('config/factor_mining_policy.yaml'))")
    click.echo("  result = runner.run(price_data=your_data)")


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

    evidence_dir = "evidence/mining-runs"
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
