"""北斗统一命令行入口：beidou / 北斗 / bd。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import click

from .checks import find_project_root
from .manifest import DEFAULT_MODE, DEFAULT_SYMBOLS, HEALTH_PORT, SUPPORTED_MODES
from .preflight import run_preflight
from .state import force_stop_existing, inspect_runtime_status, stop_running_instance
from .supervisor import BeidouSupervisor


def _parse_symbols(value: str) -> list[str]:
    values = [item.strip().upper() for item in value.split(",") if item.strip()]
    if values in (["ALL"], ["DEFAULT"]):
        from beidou_core.engine import DEFAULT_UNIVERSE

        return list(DEFAULT_UNIVERSE)
    return values or ["BTCUSDT", "ETHUSDT"]


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("action", required=False, default="start", type=click.Choice(["start", "doctor", "status", "stop"]))
@click.option("--mode", type=click.Choice(SUPPORTED_MODES), default=DEFAULT_MODE, show_default=True)
@click.option("--symbols", default=",".join(DEFAULT_SYMBOLS), show_default=True)
@click.option("--port", type=click.IntRange(1024, 65535), default=HEALTH_PORT, show_default=True)
@click.option("--startup-timeout", type=click.FloatRange(30.0, 900.0), default=300.0, show_default=True)
@click.option(
    "--monitor-interval",
    "--poll-interval",
    type=click.FloatRange(1.0, 60.0),
    default=5.0,
    show_default=True,
)
@click.option("--self-heal/--no-self-heal", default=True, show_default=True)
@click.option(
    "--max-restarts",
    type=click.IntRange(0, 10),
    default=2,
    show_default=True,
    help="兼容参数：允许的证据驱动恢复次数；不执行未验证的 Testnet 进程硬重启。",
)
def main(
    action: str,
    mode: str,
    symbols: str,
    port: int,
    startup_timeout: float,
    monitor_interval: float,
    self_heal: bool,
    max_restarts: int,
) -> None:
    """北斗一键启动、深度自检、状态查询和安全停止。

    直接执行 `beidou`、`北斗` 或 `bd` 等价于 `start`。
    """
    root: Path = find_project_root()
    os.chdir(root)
    os.environ["BEIDOU_ENV"] = mode

    if action == "doctor":
        checks, _ = run_preflight(root, mode, port)
        for item in checks:
            click.echo(json.dumps(item.to_dict(), ensure_ascii=False))
        raise SystemExit(2 if any(item.is_blocking for item in checks) else 0)

    if action == "status":
        status = inspect_runtime_status(root)
        if status is None:
            click.echo("未发现监督器状态证据。")
            raise SystemExit(1)
        click.echo(json.dumps(status, ensure_ascii=False, indent=2))
        return

    if action == "stop":
        ok, message = stop_running_instance(root)
        click.echo(message)
        raise SystemExit(0 if ok else 1)

    # 启动前强制清理旧实例，以当前启动为准
    cleaned, clean_msg = force_stop_existing(root)
    click.echo(f"{'🧹' if cleaned else '❌'} {clean_msg}")
    if not cleaned:
        raise SystemExit(1)

    parsed_symbols = _parse_symbols(symbols)
    supervisor = BeidouSupervisor(
        project_root=root,
        mode=mode,
        symbols=parsed_symbols,
        port=port,
        startup_timeout=startup_timeout,
        monitor_interval=monitor_interval,
        self_heal=self_heal,
        max_restarts=max_restarts,
    )
    try:
        exit_code = asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        exit_code = 130
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
