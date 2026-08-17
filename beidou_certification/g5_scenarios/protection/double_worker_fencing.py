"""double_worker_fencing: 原生保护组场景二 — 引擎单实例锁 fencing 验证。

不真实启动第二实例:用 InstanceLock(beidou_launcher.state:22)直接验证 ——
以第二实例身份对引擎 lock 路径 acquire,命中则被 fencing(acquired=False
且消息含「已运行」),随后在临时路径 acquire 验证锁可用并 release 清理。
引擎 lock 文件读取为只读探针(仅 PID,无敏感信息)。

InstanceLock 语义核对(与真实实现一致):acquire 失败(锁被存活 PID 持有)
直接返回 (False, "北斗实例已运行，PID=…") 且不触碰锁文件 —— 不破坏引擎
持有的锁;若引擎未运行(acquire 成功),场景立即 release 归还锁,记
NOT_VERIFIABLE(引擎未运行,fencing 无法验证)。

notional 记账 0;dry_run 仅执行纯函数判定部分,记 NOT_VERIFIABLE
("REAL_EXECUTION_REQUIRED"),不执行任何真实 lock 操作;run() 自捕获
异常返回 FAIL。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_launcher.state import InstanceLock

logger = logging.getLogger(__name__)

# 引擎真实实例锁路径(launchd autopilot 托管;见 beidou_launcher/supervisor.py
# self.lock = InstanceLock(project_root / ".beidou" / "beidou.pid"));只在
# 认证轮真实路径中使用,单测注入 tmp_path
_ENGINE_LOCK_PATH = Path("/Users/maguannan/beidou/.beidou/beidou.pid")

# InstanceLock 拒绝消息约定(beidou_launcher/state.py acquire):存活 PID 持有
# 锁时返回 f"北斗实例已运行，PID={existing}"
_FENCED_MARKER = "已运行"


def fencing_verdict(acquired: bool, message: str) -> tuple[bool, str]:
    """第二实例 lock.acquire() 判定:(fenced?, reason)。

    - acquired=False(锁被引擎持有)→ (True, "second_instance_fenced")
    - acquired=True(第二实例拿到锁,引擎未运行)→ (False, "second_instance_acquired_lock")
    """
    if acquired:
        return False, "second_instance_acquired_lock"
    return True, "second_instance_fenced"


class DoubleWorkerFencingScenario(ScenarioBase):
    scenario_id = "double_worker_fencing"

    def __init__(
        self,
        *,
        engine_lock_path: Path | None = None,
        temp_lock_dir: Path | None = None,
        make_lock: Callable[[Path], InstanceLock] | None = None,
        read_lock_file: Callable[[Path], str | None] | None = None,
    ) -> None:
        """锁动作全部可注入(单测注入假驱动 + tmp_path,禁止真实 lock 操作)。

        make_lock/read_lock_file 的默认实现为真实 InstanceLock/文件读取;
        真实引擎 lock 路径仅认证轮使用(默认 engine_lock_path)。
        """
        self._engine_lock_path = engine_lock_path or _ENGINE_LOCK_PATH
        self._temp_lock_dir = temp_lock_dir
        self._make_lock = make_lock or InstanceLock
        self._read_lock_file = read_lock_file or self._read_lock_file_impl

    @staticmethod
    def _read_lock_file_impl(path: Path) -> str | None:
        """只读探针:读取引擎 lock 文件内容(仅 PID,无敏感信息);缺失/读取失败 → None。"""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        temp_dir: Path | None = None
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                # 跳过真实锁操作,仅执行纯函数判定部分
                fenced, reason = fencing_verdict(False, "dry_run probe")
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "REAL_EXECUTION_REQUIRED",
                    "double_worker_fencing requires real execution (真实实例锁探测只在认证轮执行)",
                    {
                        "dry_run": True,
                        "steps": steps,
                        "pure_verdict_probe": {"fenced": fenced, "reason": reason},
                    },
                )

            # 1. 只读探针:读引擎 lock 文件内容(仅 PID,无敏感信息)
            lock_content = self._read_lock_file(self._engine_lock_path)
            steps.append(
                {
                    "action": "engine_lock_read",
                    "path": str(self._engine_lock_path),
                    "present": lock_content is not None,
                    "pid": lock_content,
                }
            )

            # 2. 第二实例身份 acquire 引擎 lock 路径 → 必须被 fencing
            #    (写操作前打印意图:引擎未运行时将误取锁并立即归还)
            logger.info(
                "double_worker_fencing: 以第二实例身份 acquire 引擎实例锁 %s(引擎未运行将立即归还)",
                self._engine_lock_path,
            )
            engine_lock = self._make_lock(self._engine_lock_path)
            acquired, message = engine_lock.acquire()
            steps.append({"action": "engine_lock_acquire_attempt", "acquired": acquired, "message": message})
            if acquired:
                # 引擎未运行,锁被我们拿到:立即释放,避免占住引擎锁
                logger.info("double_worker_fencing: 引擎未运行,release 归还误取的实例锁")
                engine_lock.release()
                steps.append({"action": "engine_lock_released", "note": "引擎未运行,释放误取的实例锁"})
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "engine_lock_not_held",
                    f"引擎实例锁未被持有(第二实例 acquire 成功),fencing 无法验证: {message[:200]}",
                    {"steps": steps},
                )
            if _FENCED_MARKER not in message:
                return self._fail(
                    ScenarioStatus.FAIL,
                    "UNEXPECTED_LOCK_MESSAGE",
                    f"锁拒绝消息不符合约定(缺「{_FENCED_MARKER}」): {message[:200]}",
                    {"steps": steps},
                )

            # 3. 临时路径 acquire → 应成功;随后 release 清理(证明锁本身可用,
            #    拒绝只可能来自引擎持有而非锁实现缺陷)
            temp_dir = self._temp_lock_dir or Path(tempfile.mkdtemp(prefix="g5-fencing-"))
            probe_lock = self._make_lock(temp_dir / "probe.pid")
            # 探针锁 acquire 为文件创建型写操作(仅临时路径,不触碰引擎锁):
            # 写操作前打印意图,措辞与 engine-path acquire 一致
            logger.info(
                "double_worker_fencing: 以第二实例身份 acquire 探针锁 %s(证明锁实现可用,拒绝只可能来自引擎持有)",
                temp_dir / "probe.pid",
            )
            probe_acquired, probe_message = probe_lock.acquire()
            steps.append({"action": "probe_lock_acquire", "acquired": probe_acquired, "message": probe_message})
            if probe_acquired:
                probe_lock.release()
                steps.append({"action": "probe_lock_release", "released": True})
            else:
                steps.append({"action": "probe_lock_release", "released": False, "note": "acquire 失败,无锁可释放"})
            if not probe_acquired:
                return self._fail(
                    ScenarioStatus.FAIL,
                    "PROBE_LOCK_ACQUIRE_FAILED",
                    f"临时路径锁获取失败: {probe_message[:200]}",
                    {"steps": steps},
                )

            # 4. 纯函数判定
            fenced, reason = fencing_verdict(acquired, message)
            steps.append({"action": "verdict", "fenced": fenced, "reason": reason})
            if not fenced or reason != "second_instance_fenced":
                return self._fail(ScenarioStatus.FAIL, reason.upper(), reason, {"steps": steps})

            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS,
                {
                    "steps": steps,
                    "verdict": reason,
                    "engine_pid": lock_content,
                    "fencing_message": message,
                    "probe_lock_acquired": probe_acquired,
                    "notional_usdt": 0.0,
                },
                time.monotonic() - started,
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})
        finally:
            if temp_dir is not None and self._temp_lock_dir is None:
                shutil.rmtree(temp_dir, ignore_errors=True)


SCENARIO_REGISTRY[DoubleWorkerFencingScenario.scenario_id] = DoubleWorkerFencingScenario
