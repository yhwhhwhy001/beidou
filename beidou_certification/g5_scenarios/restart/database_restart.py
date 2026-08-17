"""database_restart: 重启组场景二 — Homebrew PG 重启后引擎的恢复验证。

真实重启路径仅由 run_g5.py 在认证轮经用户批准后执行;开发与单测期间绝对
禁止真实运维动作 —— 所有运维动作(brew/psycopg 探测/HTTP status/pgrep)都是
可注入依赖,单测注入假驱动:
1. 前置:/status 健康基线(parse_engine_status 就绪且 state_backend_error 为
   空,否则 NOT_VERIFIABLE);记录引擎 PID
2. brew services restart postgresql@16 → 轮询 ≤120s:PG 可连
   (psycopg.connect(dsn, connect_timeout=3))且 /status 的 state_backend_error
   为 null、引擎进程仍存活;超时 FAIL
3. 恢复后断言 recon 回到 MATCHED(≤180s,parse_engine_status);超时 FAIL
4. 证据:PG 重启耗时、引擎自愈耗时、恢复后 status 摘要;notional 记账 0。

dry_run 直接 NOT_VERIFIABLE("restart requires real execution"),不触碰任何
真实资源;run() 自捕获异常返回 FAIL。真实执行 brew restart 前 logger.info
打印操作意图(全局约束)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
from typing import Any, Awaitable, Callable

import psycopg

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.engine.ack_loss import PG_DSN
from beidou_certification.g5_scenarios.restart.process_restart import (
    EngineRecoveryTimeoutError,
    StatusUnreachableError,
    engine_pid_os,
    fetch_status_http,
    parse_engine_status,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

logger = logging.getLogger(__name__)

# Homebrew PG 运维约定(北斗引擎连宿主机 PG 而非 docker;brew 路径硬编码为
# arm64 默认前缀,与 deploy 配置一致)
_BREW_SERVICE = "postgresql@16"

_POLL_INTERVAL_SECONDS = 5.0
_PG_RESTORE_DEADLINE_SECONDS = 120.0  # brew restart 后 PG 可连 + state_backend_error 清空窗口
_READY_DEADLINE_SECONDS = 180.0  # PG 恢复后 recon 回 MATCHED 窗口
_PG_CONNECT_TIMEOUT_SECONDS = 3


class PgRestartTimeoutError(RuntimeError):
    """brew restart 后 120s 内 PG 未恢复可连 / state_backend_error 未清空 /
    引擎进程退出。"""


class DatabaseRestartScenario(ScenarioBase):
    scenario_id = "database_restart"

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        fetch_status: Callable[[], dict[str, Any]] | None = None,
        pg_connect: Callable[[str], Any] | None = None,
        brew_restart: Callable[[], None] | None = None,
        engine_pid: Callable[[], int | None] | None = None,
        dsn: str | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        pg_restore_deadline: float = _PG_RESTORE_DEADLINE_SECONDS,
        ready_deadline: float = _READY_DEADLINE_SECONDS,
    ) -> None:
        """运维动作全部可注入(单测注入假驱动,禁止真实执行)。

        fetch_status/pg_connect/brew_restart/engine_pid 的默认实现为真实
        运维;场景逻辑只调用注入的依赖。
        """
        self._now = now or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._fetch_status = fetch_status or fetch_status_http
        self._pg_connect = pg_connect or self._pg_connect_real
        self._brew_restart = brew_restart or self._brew_restart_real
        self._engine_pid = engine_pid or engine_pid_os
        self._dsn = dsn or PG_DSN
        self._poll_interval = poll_interval
        self._pg_restore_deadline = pg_restore_deadline
        self._ready_deadline = ready_deadline

    # ---- 真实运维实现(默认依赖;认证轮经用户批准后由 run_g5.py 执行) ----

    @staticmethod
    def _pg_connect_real(dsn: str) -> Any:
        """真实 PG 探测:psycopg.connect(dsn, connect_timeout=3)。

        PG 未起时抛 OperationalError,调用侧按未恢复处理。
        """
        return psycopg.connect(dsn, connect_timeout=_PG_CONNECT_TIMEOUT_SECONDS)

    @staticmethod
    def _brew_restart_real() -> None:
        """真实重启:brew services restart postgresql@16。"""
        subprocess.run(
            ["/opt/homebrew/bin/brew", "services", "restart", "postgresql@16"],
            check=True,
            capture_output=True,
            text=True,
        )

    # ---- 轮询与校验 ----

    async def _wait_for_pg_restored(self, *, since: float, steps: list[dict[str, Any]]) -> None:
        """轮询 PG 恢复三条件:PG 可连 且 /status state_backend_error 为空 且
        引擎进程仍存活;超时抛 PgRestartTimeoutError。"""
        deadline_at = since + self._pg_restore_deadline
        polls = 0
        last_error = ""
        while True:
            polls += 1
            conn: Any = None
            pg_ok = False
            try:
                conn = self._pg_connect(self._dsn)
                pg_ok = True
            except Exception as exc:
                last_error = f"pg connect: {exc}"
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
            status_error: Any = "UNKNOWN"
            try:
                payload = self._fetch_status()
                status_error = payload.get("state_backend_error")
            except StatusUnreachableError as exc:
                last_error = f"status unreachable: {exc}"
            pid = self._engine_pid()
            if pg_ok and not status_error and pid is not None:
                steps.append({"action": "pg_restored", "polls": polls, "engine_pid": pid})
                return
            if self._now() >= deadline_at:
                steps.append(
                    {
                        "action": "pg_poll_timeout",
                        "polls": polls,
                        "deadline_seconds": self._pg_restore_deadline,
                        "last_error": last_error[:200],
                    }
                )
                raise PgRestartTimeoutError(
                    f"PG_RESTART_TIMEOUT: {self._pg_restore_deadline:.0f}s 内 PG 未恢复"
                    f"(pg_ok={pg_ok} state_backend_error={status_error!r} engine_alive={pid is not None})"
                )
            await self._sleep(self._poll_interval)

    async def _wait_for_ready(self, *, since: float, steps: list[dict[str, Any]]) -> dict[str, Any]:
        """轮询 /status 直到 parse_engine_status 判定 ready;超时抛
        EngineRecoveryTimeoutError。"""
        deadline_at = since + self._ready_deadline
        polls = 0
        while True:
            polls += 1
            try:
                payload = self._fetch_status()
            except StatusUnreachableError:
                ok, reason = False, "status_unreachable"
            else:
                ok, reason = parse_engine_status(payload)
            if ok:
                steps.append({"action": "ready_observed", "polls": polls, "status": payload})
                return payload
            if self._now() >= deadline_at:
                steps.append(
                    {
                        "action": "ready_poll_timeout",
                        "polls": polls,
                        "deadline_seconds": self._ready_deadline,
                        "reason": reason,
                    }
                )
                raise EngineRecoveryTimeoutError(
                    f"ENGINE_RECOVERY_TIMEOUT: {self._ready_deadline:.0f}s 内"
                    f"trading_ready/recon MATCHED 未恢复(last_reason={reason})"
                )
            await self._sleep(self._poll_interval)

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "RESTART_REQUIRES_REAL_EXECUTION",
                    "restart requires real execution",
                    {"dry_run": True, "steps": steps},
                )
            pid = self._engine_pid()
            if pid is None:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "engine_process_not_found",
                    "pgrep 未找到运行中引擎进程(launchd autopilot 未在跑)",
                    {"steps": steps},
                )
            steps.append({"action": "baseline_pid", "pid": pid})
            try:
                baseline_payload = self._fetch_status()
            except StatusUnreachableError as exc:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "engine_not_ready_before_restart",
                    f"/status 基线不可达: {exc}",
                    {"steps": steps},
                )
            ready_ok, ready_reason = parse_engine_status(baseline_payload)
            state_error = baseline_payload.get("state_backend_error")
            steps.append(
                {
                    "action": "baseline_status",
                    "ready": ready_ok,
                    "reason": ready_reason,
                    "state_backend_error": state_error,
                    "trading_ready": baseline_payload.get("trading_ready"),
                    "recon_status": baseline_payload.get("last_reconciliation", {}).get("status"),
                }
            )
            if not ready_ok:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "engine_not_ready_before_restart",
                    f"重启前引擎未就绪: {ready_reason}",
                    {"steps": steps},
                )
            if state_error:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "state_backend_error_before_restart",
                    f"重启前 PG 后端已有错误: {str(state_error)[:200]}",
                    {"steps": steps},
                )
            # 破坏性操作前打印意图(全局约束;真实执行时操作者可在日志中看到完整意图)
            logger.info("database_restart: brew services restart %s (破坏性操作,打印意图)", _BREW_SERVICE)
            t_brew = self._now()
            self._brew_restart()
            steps.append({"action": "brew_restart", "service": _BREW_SERVICE})
            await self._wait_for_pg_restored(since=t_brew, steps=steps)
            pg_elapsed = self._now() - t_brew
            t_up = self._now()
            ready_payload = await self._wait_for_ready(since=t_up, steps=steps)
            heal_elapsed = self._now() - t_up
            recon = ready_payload.get("last_reconciliation", {})
            steps.append(
                {
                    "action": "post_restart_summary",
                    "pg_restart_elapsed_seconds": round(pg_elapsed, 2),
                    "self_heal_elapsed_seconds": round(heal_elapsed, 2),
                    "trading_ready": ready_payload.get("trading_ready"),
                    "recon_status": recon.get("status"),
                    "state_backend_error": ready_payload.get("state_backend_error"),
                    "engine_pid": self._engine_pid(),
                }
            )
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS,
                {
                    "steps": steps,
                    "verdict": "database_restart_recovered",
                    "pg_restart_elapsed_seconds": round(pg_elapsed, 2),
                    "self_heal_elapsed_seconds": round(heal_elapsed, 2),
                    "notional_usdt": 0.0,
                },
                time.monotonic() - started,
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[DatabaseRestartScenario.scenario_id] = DatabaseRestartScenario
