"""北斗统一命令行入口：beidou / 北斗 / bd。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click

from .checks import find_project_root
from .manifest import (
    DEFAULT_MODE,
    DEFAULT_SYMBOLS,
    HEALTH_PORT,
    MAX_RESTARTS,
    MONITOR_INTERVAL,
    STARTUP_TIMEOUT,
    SUPPORTED_MODES,
)
from .preflight import run_preflight
from .state import inspect_runtime_status, stop_running_instance
from .supervisor import BeidouSupervisor


def _enable_unbuffered_stdout() -> None:
    """BD-FIX: 引擎日志实时可见。

    stdout 重定向到文件时为块缓冲（数 KB），观测窗口内 nearline/realtime
    诊断输出不可见——2026-08-14 排查"长时间无订单"时被缓冲假象误导
    （fd offset 不增长被误判为 tick 停摆）。行缓冲让日志逐行落盘；
    tty 场景保持默认不干预。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if not stream.isatty():
                reconfigure = getattr(stream, "reconfigure", None)
                if callable(reconfigure):
                    reconfigure(line_buffering=True)
        except (AttributeError, OSError, TypeError, ValueError):
            continue  # 不可 reconfigure 的流（如某些测试捕获器）保持默认


def _parse_symbols(value: str) -> list[str]:
    values = [item.strip().upper() for item in value.split(",") if item.strip()]
    if any(item in {"ALL", "DEFAULT"} for item in values):
        return []
    return list(dict.fromkeys(values))


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("action", required=False, default="start", type=click.Choice(["start", "doctor", "status", "stop"]))
@click.option("--mode", type=click.Choice(SUPPORTED_MODES), default=DEFAULT_MODE, show_default=True)
@click.option(
    "--symbols",
    default=",".join(DEFAULT_SYMBOLS),
    show_default=False,
    help="显式指定交易品种，逗号分隔；不允许 DEFAULT/ALL 固定交易池回退。",
)
@click.option("--port", type=click.IntRange(1024, 65535), default=HEALTH_PORT, show_default=True)
@click.option("--startup-timeout", type=click.FloatRange(30.0, 900.0), default=STARTUP_TIMEOUT, show_default=True)
@click.option(
    "--monitor-interval",
    "--poll-interval",
    type=click.FloatRange(1.0, 60.0),
    default=MONITOR_INTERVAL,
    show_default=True,
)
@click.option("--self-heal/--no-self-heal", default=True, show_default=True)
@click.option(
    "--max-restarts",
    type=click.IntRange(0, 20),
    default=MAX_RESTARTS,
    show_default=True,
    help="10 分钟滑动窗口内允许的最大恢复次数；超限后需人工介入。",
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
    _enable_unbuffered_stdout()
    root: Path = find_project_root()

    if action == "doctor":
        os.chdir(root)
        os.environ["BEIDOU_ENV"] = mode
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

    os.chdir(root)
    os.environ["BEIDOU_ENV"] = mode

    # 引擎文件日志装配点。beidou_core.engine 不再在模块导入时创建
    # FileHandler —— 否则任何导入它的旁路进程 (pytest/运维脚本/REPL) 都会
    # 写入 evidence/beidou_engine.log, 使其无法反映引擎进程的真实生命周期。
    # 必须在 chdir 之后调用, 相对路径 evidence/ 才解析到项目根。
    from beidou_core.engine import attach_engine_file_log

    attach_engine_file_log()

    parsed_symbols = _parse_symbols(symbols)
    if not parsed_symbols:
        raise click.ClickException("必须显式提供 --symbols；固定 DEFAULT/ALL 交易池已禁用")

    # BD-CV53: 启动就绪门禁 — 进程启动默认 NO_NEW_RISK
    from beidou_launcher.readiness_gate import ReadinessGate, StartupPhase

    gate = ReadinessGate()
    gate.start()
    gate.complete_phase(StartupPhase.CONFIG, True, "CLI config loaded")
    gate.complete_phase(StartupPhase.DEPENDENCY, True, "Dependencies verified")
    click.echo(f"[beidou] Readiness gate: {gate.current_phase().value}")

    supervisor = BeidouSupervisor(
        project_root=root,
        mode=mode,
        symbols=parsed_symbols,
        port=port,
        startup_timeout=startup_timeout,
        monitor_interval=monitor_interval,
        self_heal=self_heal,
        max_restarts=max_restarts,
        # M22-F05: 已登记 dev 便利豁免(.env 配置/wrapper 注入)——仅在
        # 此层读取 env 并显式传参;preflight/supervisor 源码不含该
        # 标签(架构测试硬约束),G5 检查永不缺席、status 恒真实。
        g5_dev_exemption=bool(os.environ.get("BEIDOU_DEV_FAST_START")),
    )
    try:
        exit_code = asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        exit_code = 130
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
