"""process_restart: 重启组场景一 — 引擎进程 SIGKILL 后的 launchd 拉起与恢复验证。

真实重启路径仅由 run_g5.py 在认证轮经用户批准后执行;开发与单测期间绝对
禁止真实运维动作 —— 所有运维动作(pgrep/kill/kickstart/HTTP status/PG)都是
可注入依赖,单测注入假驱动:
1. 前置:pgrep 记录当前引擎 PID;读 /status 基线(trading_ready=True 且
   recon=MATCHED,否则 NOT_VERIFIABLE("engine_not_ready_before_restart"));
   PG durable 快照(opening 基线 record hash + UNKNOWN outbox message_id 集)
2. SIGKILL 旧 PID → launchctl kickstart gui/501/com.beidou.autopilot →
   轮询 ≤120s 等新进程(新 PID ≠ 旧 PID 且 /status 可达;超时 FAIL)
3. 等 /status trading_ready=True 且 recon=MATCHED(≤180s;超时 FAIL)
4. durable 恢复验证:opening 基线 hash 未变、无新增 UNKNOWN outbox 行
   (漂移 → FAIL)
5. 证据:旧/新 PID、重启耗时、恢复耗时、恢复后 status 摘要;notional 记账 0。

dry_run 直接 NOT_VERIFIABLE("restart requires real execution"),不触碰任何
真实资源;run() 自捕获异常返回 FAIL。真实执行 SIGKILL 前 logger.info 打印
操作意图(全局约束)。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import subprocess
import time
import urllib.request
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
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

logger = logging.getLogger(__name__)

# 共享 PG 基线 key(与 reconciliation_mismatch 同一定义)
_BASELINE_RECORD_TYPE = "account_opening_projection"
_BASELINE_RECORD_ID = "default:BINANCE"
_BASELINE_READ_SQL = "SELECT payload::text FROM v3_runtime_records WHERE record_type=%s AND record_id=%s"
_UNKNOWN_OUTBOX_SQL = "SELECT message_id FROM v3_transactional_outbox WHERE status='UNKNOWN'"

# launchd autopilot 运维约定(与 deploy/com.beidou.autopilot.plist 一致)
_KICKSTART_TARGET = "gui/501/com.beidou.autopilot"

# /status 轮询约定(引擎 health server 见 beidou_core/health.py;9090 见
# beidou_launcher/manifest.py HEALTH_PORT)
_STATUS_URL = "http://127.0.0.1:9090/status"
_HTTP_TIMEOUT_SECONDS = 5.0

_POLL_INTERVAL_SECONDS = 5.0
_PROCESS_DEADLINE_SECONDS = 120.0  # SIGKILL 后等 autopilot 拉起新进程 + /status 可达
_READY_DEADLINE_SECONDS = 180.0  # 新进程就绪后等 trading_ready + recon MATCHED


def parse_engine_status(payload: dict) -> tuple[bool, str]:
    """按 brief 契约解析 /status 响应 → (trading_ready, reason)。

    - 缺 trading_ready 或 last_reconciliation.status → (False, "MALFORMED")
    - trading_ready=True 且 recon=MATCHED → (True, "READY")
    - trading_ready=True 但 recon 非 MATCHED → (False, "RECON_{status}")
    - trading_ready=False 且 recon=MATCHED → (False, "ENGINE_NOT_READY")
    - trading_ready=False 且 recon 非 MATCHED → (False, "RECON_{status}")
    """
    trading_ready = payload.get("trading_ready")
    recon = payload.get("last_reconciliation")
    if trading_ready is None or not isinstance(recon, dict):
        return False, "MALFORMED"
    recon_status = recon.get("status")
    if not isinstance(recon_status, str):
        return False, "MALFORMED"
    if bool(trading_ready):
        if recon_status == "MATCHED":
            return True, "READY"
        return False, f"RECON_{recon_status}"
    if recon_status == "MATCHED":
        return False, "ENGINE_NOT_READY"
    return False, f"RECON_{recon_status}"


class StatusUnreachableError(RuntimeError):
    """/status 端点不可达(引擎未监听或处于重启窗口)。"""


class ProcessRestartTimeoutError(RuntimeError):
    """SIGKILL 后 120s 内未观测到新进程且 /status 不可达。"""


class EngineRecoveryTimeoutError(RuntimeError):
    """新进程就绪后 180s 内 trading_ready/recon MATCHED 未恢复。"""


class DurableStateDriftError(RuntimeError):
    """重启后 durable 状态漂移(opening 基线损坏或新增 UNKNOWN outbox 行)。"""


def _payload_hash(payload: dict[str, Any]) -> str:
    """确定性 sha256:sort_keys + default=str,与 base.artifact_hash 同风格。"""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _read_baseline_payload(conn: Any) -> dict[str, Any] | None:
    cursor = conn.execute(_BASELINE_READ_SQL, (_BASELINE_RECORD_TYPE, _BASELINE_RECORD_ID))
    row = cursor.fetchone()
    if row is None:
        return None
    payload = json.loads(str(row[0]))
    return payload if isinstance(payload, dict) else None


def _unknown_outbox_ids(conn: Any) -> set[str]:
    cursor = conn.execute(_UNKNOWN_OUTBOX_SQL)
    rows = cursor.fetchall()
    return {str(row[0]) for row in rows}


class ProcessRestartScenario(ScenarioBase):
    scenario_id = "process_restart"

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        fetch_status: Callable[[], dict[str, Any]] | None = None,
        engine_pid: Callable[[], int | None] | None = None,
        sigkill: Callable[[int], None] | None = None,
        kickstart: Callable[[], None] | None = None,
        connect: Callable[[str], Any] | None = None,
        dsn: str | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        process_deadline: float = _PROCESS_DEADLINE_SECONDS,
        ready_deadline: float = _READY_DEADLINE_SECONDS,
    ) -> None:
        """运维动作全部可注入(单测注入假驱动,禁止真实执行)。

        fetch_status/engine_pid/sigkill/kickstart/connect 的默认实现为真实
        运维;场景逻辑只调用注入的依赖。
        """
        self._now = now or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._fetch_status = fetch_status or self._fetch_status_http
        self._engine_pid = engine_pid or self._engine_pid_os
        self._sigkill = sigkill or self._sigkill_os
        self._kickstart = kickstart or self._kickstart_os
        self._connect = connect or psycopg.connect
        self._dsn = dsn or PG_DSN
        self._poll_interval = poll_interval
        self._process_deadline = process_deadline
        self._ready_deadline = ready_deadline

    # ---- 真实运维实现(默认依赖;认证轮经用户批准后由 run_g5.py 执行) ----

    def _fetch_status_http(self) -> dict[str, Any]:
        """真实 /status 查询:http://127.0.0.1:9090/status(引擎 health server)。

        响应结构见 beidou_core/health.py /status 处理器:trading_ready 与
        last_reconciliation.status 均在顶层,与 parse_engine_status 契约一致;
        不可达/非 JSON/非对象均抛 StatusUnreachableError(调用侧按未就绪处理)。
        """
        try:
            with urllib.request.urlopen(_STATUS_URL, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
        except OSError as exc:
            raise StatusUnreachableError(f"status unreachable: {exc}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StatusUnreachableError(f"status payload decode failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise StatusUnreachableError(f"status payload is not an object: {type(payload).__name__}")
        return payload

    @staticmethod
    def _engine_pid_os() -> int | None:
        """真实 PID 探测:pgrep -f "beidou start"(launchd autopilot 进程)。"""
        proc = subprocess.run(["/usr/bin/pgrep", "-f", "beidou start"], check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        pids = [int(line.strip()) for line in proc.stdout.splitlines() if line.strip().isdigit()]
        return pids[0] if pids else None

    @staticmethod
    def _sigkill_os(pid: int) -> None:
        """真实 SIGKILL:os.kill(pid, SIGKILL)(等价 kill -9 <pid>)。"""
        os.kill(pid, signal.SIGKILL)

    @staticmethod
    def _kickstart_os() -> None:
        """真实拉起:launchctl kickstart gui/501/com.beidou.autopilot。"""
        subprocess.run(
            ["/bin/launchctl", "kickstart", "gui/501/com.beidou.autopilot"],
            check=True,
            capture_output=True,
            text=True,
        )

    # ---- 轮询与校验 ----

    async def _wait_for_new_process(self, old_pid: int, *, since: float, steps: list[dict[str, Any]]) -> int:
        """轮询新进程:新 PID ≠ 旧 PID 且 /status 可达;超时抛 ProcessRestartTimeoutError。"""
        deadline_at = since + self._process_deadline
        polls = 0
        while True:
            polls += 1
            new_pid = self._engine_pid()
            if new_pid is not None and new_pid != old_pid:
                try:
                    self._fetch_status()
                except StatusUnreachableError:
                    pass
                else:
                    steps.append({"action": "process_up", "polls": polls, "new_pid": new_pid})
                    return new_pid
            if self._now() >= deadline_at:
                steps.append(
                    {
                        "action": "process_poll_timeout",
                        "polls": polls,
                        "deadline_seconds": self._process_deadline,
                    }
                )
                raise ProcessRestartTimeoutError(
                    f"PROCESS_RESTART_TIMEOUT: {self._process_deadline:.0f}s 内未观测到新 PID"
                    f"且 /status 不可达(旧 pid={old_pid})"
                )
            await self._sleep(self._poll_interval)

    async def _wait_for_ready(self, *, since: float, steps: list[dict[str, Any]]) -> dict[str, Any]:
        """轮询 /status 直到 parse_engine_status 判定 ready;超时抛 EngineRecoveryTimeoutError。"""
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

    def _snapshot_durable(self, conn: Any, steps: list[dict[str, Any]]) -> tuple[str | None, set[str]]:
        """重启前 durable 快照:opening 基线 hash + UNKNOWN outbox message_id 集。"""
        baseline = _read_baseline_payload(conn)
        if baseline is None:
            steps.append({"action": "baseline_snapshot", "record_missing": True})
            return None, set()
        baseline_hash = _payload_hash(baseline)
        unknown_ids = _unknown_outbox_ids(conn)
        steps.append(
            {
                "action": "baseline_snapshot",
                "record_type": _BASELINE_RECORD_TYPE,
                "record_id": _BASELINE_RECORD_ID,
                "hash": baseline_hash,
                "unknown_outbox_ids": sorted(unknown_ids),
            }
        )
        return baseline_hash, unknown_ids

    def _verify_durable(
        self,
        conn: Any,
        *,
        before_hash: str | None,
        before_unknown: set[str],
        steps: list[dict[str, Any]],
    ) -> None:
        """重启后 durable 校验:基线 hash 未变、无新增 UNKNOWN outbox 行;漂移抛错。"""
        baseline = _read_baseline_payload(conn)
        after_hash = _payload_hash(baseline) if baseline is not None else None
        after_unknown = _unknown_outbox_ids(conn)
        new_unknown = sorted(after_unknown - before_unknown)
        baseline_intact = before_hash is not None and after_hash == before_hash
        steps.append(
            {
                "action": "durable_verify",
                "baseline_intact": baseline_intact,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "new_unknown_outbox": new_unknown,
                "unknown_outbox_after": sorted(after_unknown),
            }
        )
        if not baseline_intact or new_unknown:
            raise DurableStateDriftError(
                f"DURABLE_STATE_DRIFT: baseline_intact={baseline_intact} new_unknown_outbox={new_unknown}"
            )

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        conn: Any = None
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "RESTART_REQUIRES_REAL_EXECUTION",
                    "restart requires real execution",
                    {"dry_run": True, "steps": steps},
                )
            old_pid = self._engine_pid()
            if old_pid is None:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "engine_process_not_found",
                    "pgrep 未找到运行中引擎进程(launchd autopilot 未在跑)",
                    {"steps": steps},
                )
            steps.append({"action": "baseline_pid", "pid": old_pid})
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
            steps.append(
                {
                    "action": "baseline_status",
                    "ready": ready_ok,
                    "reason": ready_reason,
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
            conn = self._connect(self._dsn)
            before_hash, before_unknown = self._snapshot_durable(conn, steps)
            if before_hash is None:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "baseline_record_missing",
                    f"{_BASELINE_RECORD_TYPE}:{_BASELINE_RECORD_ID} 基线缺失,无法校验 durable 恢复",
                    {"steps": steps},
                )
            # 破坏性操作前打印意图(全局约束;真实执行时操作者可在日志中看到完整意图)
            logger.info("process_restart: sending SIGKILL to engine pid=%s", old_pid)
            t_kill = self._now()
            self._sigkill(old_pid)
            steps.append({"action": "sigkill", "pid": old_pid})
            logger.info("process_restart: launchctl kickstart %s", _KICKSTART_TARGET)
            self._kickstart()
            steps.append({"action": "kickstart", "target": _KICKSTART_TARGET})
            new_pid = await self._wait_for_new_process(old_pid, since=t_kill, steps=steps)
            restart_elapsed = self._now() - t_kill
            t_up = self._now()
            ready_payload = await self._wait_for_ready(since=t_up, steps=steps)
            recovery_elapsed = self._now() - t_up
            self._verify_durable(conn, before_hash=before_hash, before_unknown=before_unknown, steps=steps)
            recon = ready_payload.get("last_reconciliation", {})
            steps.append(
                {
                    "action": "post_restart_summary",
                    "old_pid": old_pid,
                    "new_pid": new_pid,
                    "restart_elapsed_seconds": round(restart_elapsed, 2),
                    "recovery_elapsed_seconds": round(recovery_elapsed, 2),
                    "trading_ready": ready_payload.get("trading_ready"),
                    "recon_status": recon.get("status"),
                    "uptime_seconds": ready_payload.get("uptime_seconds"),
                }
            )
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS,
                {
                    "steps": steps,
                    "verdict": "process_restart_recovered",
                    "old_pid": old_pid,
                    "new_pid": new_pid,
                    "restart_elapsed_seconds": round(restart_elapsed, 2),
                    "recovery_elapsed_seconds": round(recovery_elapsed, 2),
                    "notional_usdt": 0.0,
                },
                time.monotonic() - started,
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()


SCENARIO_REGISTRY[ProcessRestartScenario.scenario_id] = ProcessRestartScenario
