"""user_stream_reconnect: 重启组场景三 — 探针诱发引擎 user stream 断开 → 引擎自动重连恢复。

语义(2026-08-17 认证轮修正):探针重建 listen key 会使引擎同账户的 listen
key 失效 —— 这是 Binance 用户数据流语义(Binance 账户级 listen key 唯一,
新建即失效旧 key),属预期副作用。场景验证引擎对意外断开的自愈能力:
1. 前置:ctx.client 可用;引擎 /status user_stream_runtime.status 基线
   (缺失/不可达 → NOT_VERIFIABLE)
2. 自建 listen key(client.create_listen_key)→ 建 ws 连接收 3s → 再
   create_listen_key(引擎同账户 key 失效,即"探针诱发断开")→ 观察引擎
   kicked 状态(时间线证据,非致命)→ 探针第二次连接,断言连接成功且收到
   数据帧或 ACK
3. 轮询 /status(≤120s,间隔 5s,用注入时钟/睡眠 seam)等引擎
   user_stream_runtime.status 自动重连回 CONNECTED/HEALTHY;时间线证据:
   before → kicked → recovered
4. 最终判定:引擎恢复(事件年龄 ≤ 300s 停流豁免窗口)+ 探针重连成功 →
   PASS("engine_reconnected");窗口内未恢复 → FAIL
5. 清理:Binance USDⓈ-M 官方提供 DELETE /fapi/v1/listenKey 关闭用户数据流
   (客户端未暴露专用方法,经通用 request 通道调用真实端点);探针 listen key
   从不续期(keep-alive),删除失败也会在 60 分钟 TTL 后自然过期 —— 清理
   非致命,证据记录原因。

ws_reconnect_verdict 纯函数保留,判定输入为"引擎恢复后状态 + 探针结果"
(健康态 + 事件年龄 → ok;恢复但停流 → stale)。notional 记账 0;dry_run →
NOT_VERIFIABLE("restart requires real execution");run() 自捕获异常返回 FAIL。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.restart.process_restart import StatusUnreachableError, fetch_status_http
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.ws_client import (
    CONNECT_TIMEOUT,
    FSTREAM_TESTNET_URL,
    OP_BINARY,
    OP_TEXT,
    WebSocketConnection,
    WebSocketError,
)

logger = logging.getLogger(__name__)

# 探针 ws 基址(testnet;与引擎 ws_client.FSTREAM_TESTNET_URL 同源,运行时可用
# BEIDOU_G5_WS_BASE_URL 覆盖)
_WS_BASE_URL = os.environ.get("BEIDOU_G5_WS_BASE_URL", FSTREAM_TESTNET_URL)

_PROBE_WINDOW_SECONDS = 3.0  # 每次连接的数据接收窗口(brief 契约:收 3s)
# 事件新鲜度豁免窗口:与引擎 testnet 判定一致(engine.py effective_max_age=300)
_MAX_EVENT_AGE_SECONDS = 300.0
# 引擎 user stream 自动重连恢复的轮询窗口:探针诱发断开后引擎应自动重连
_RECOVERY_POLL_INTERVAL_S = 5.0
_RECOVERY_DEADLINE_S = 120.0
# 恢复判定:status 回到 CONNECTED/HEALTHY 即认为引擎已自动重连(引擎把
# 事件停流超过豁免窗口的状态标记为 STALE,不在恢复集内)
_RECOVERED_STATUSES = frozenset({"HEALTHY", "CONNECTED"})


def _mask_key(key: str) -> str:
    """listen key 脱敏:证据落盘只保留前后片段,不泄露完整凭据。

    内存中仍持有完整 key 供实际 ws/清理调用;仅写入 steps 前脱敏。
    短 key(测试/畸形输入)缩窄片段避免前后缀重叠。
    """
    if len(key) < 12:
        return f"{key[:4]}...{key[-2:]}"
    return f"{key[:6]}...{key[-4:]}"


def ws_reconnect_verdict(before: str, after: str, last_event_age_s: float) -> tuple[bool, str]:
    """引擎 user stream 恢复判定纯函数:(引擎已恢复?, 判定原因)。

    输入为"引擎恢复后状态 + 探针结果":after 是探针诱发断开后引擎恢复到的
    status,before 是探针前基线,last_event_age_s 是恢复观察的事件年龄。
    - after 为 HEALTHY/CONNECTED 且事件年龄 ≤ 300s(引擎 testnet 豁免窗口)
      → 引擎自愈:
        * before 处于过渡态(RECONNECTING/CONNECTING/STARTING)→ "reconnected"
        * 否则 → "healthy_after_reconnect"
    - after 非健康态,或健康但事件年龄超窗(停流保护)→ (False,
      "stale_after_reconnect")
    """
    before_upper = str(before or "").upper()
    after_upper = str(after or "").upper()
    if after_upper in ("HEALTHY", "CONNECTED"):
        if last_event_age_s > _MAX_EVENT_AGE_SECONDS:
            return False, "stale_after_reconnect"
        if before_upper in ("RECONNECTING", "CONNECTING", "STARTING"):
            return True, "reconnected"
        return True, "healthy_after_reconnect"
    return False, "stale_after_reconnect"


def _user_stream_status(payload: dict[str, Any]) -> str | None:
    """提取 /status 的 user_stream_runtime.status(缺失/非字符串 → None)。"""
    runtime = payload.get("user_stream_runtime")
    if not isinstance(runtime, dict):
        return None
    status = runtime.get("status")
    if not isinstance(status, str) or not status.strip():
        return None
    return status.strip().upper()


def _user_stream_event_age(payload: dict[str, Any], now: float) -> tuple[float, bool]:
    """按 user_stream_runtime.last_event_mono 计算事件年龄;
    时间戳缺失/非法时返回 (0.0, False)(无停流证据,不判 stale)。"""
    runtime = payload.get("user_stream_runtime")
    mono = runtime.get("last_event_mono") if isinstance(runtime, dict) else None
    if mono is None:
        return 0.0, False
    try:
        return max(0.0, now - float(mono)), True
    except (TypeError, ValueError, OverflowError):
        return 0.0, False


class UserStreamReconnectScenario(ScenarioBase):
    scenario_id = "user_stream_reconnect"

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        fetch_status: Callable[[], dict[str, Any]] | None = None,
        create_listen_key: Callable[[], Awaitable[str]] | None = None,
        close_listen_key: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        ws_probe: Callable[[str, float], Awaitable[dict[str, Any]]] | None = None,
        probe_window: float = _PROBE_WINDOW_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        poll_interval: float = _RECOVERY_POLL_INTERVAL_S,
        recovery_deadline: float = _RECOVERY_DEADLINE_S,
    ) -> None:
        """探针动作全部可注入(单测注入假驱动,禁止真实交易所调用)。

        fetch_status/create_listen_key/close_listen_key/ws_probe 的默认实现
        为真实调用;场景逻辑只调用注入的依赖。默认依赖在 run() 时绑定到
        ctx.client 的客户端。now/sleep 为引擎恢复轮询的时钟/睡眠 seam
        (单测注入假时钟);poll_interval/recovery_deadline 为恢复轮询参数。
        """
        self._now = now or time.monotonic
        self._fetch_status = fetch_status or fetch_status_http
        self._create_listen_key = create_listen_key or self._create_listen_key_impl
        self._close_listen_key = close_listen_key or self._close_listen_key_impl
        self._ws_probe = ws_probe or self._ws_probe_impl
        self._probe_window = probe_window
        self._sleep = sleep or asyncio.sleep
        self._poll_interval = poll_interval
        self._recovery_deadline = recovery_deadline
        self._client: Any = None

    # ---- 真实探针实现(默认依赖;认证轮经用户批准后由 run_g5.py 执行) ----

    async def _create_listen_key_impl(self) -> str:
        """真实建独立 listen key:client.create_listen_key()(POST /fapi/v1/listenKey)。"""
        result = await self._client.create_listen_key()
        if not result.is_ok:
            raise RuntimeError(f"create_listen_key failed: {result.error}")
        data = result.data if isinstance(result.data, dict) else {}
        key = data.get("listenKey")
        if not isinstance(key, str) or not key:
            raise RuntimeError("create_listen_key 未返回 listenKey")
        return key

    async def _close_listen_key_impl(self, key: str) -> dict[str, Any]:
        """真实清理:Binance USDⓈ-M 官方提供 DELETE /fapi/v1/listenKey 关闭
        用户数据流;客户端未暴露专用方法,经通用 request 通道调用真实端点。
        失败非致命(探针 key 从不续期,60 分钟 TTL 自然过期)。"""
        result = await self._client.request("DELETE", Endpoint.LISTEN_KEY, signed=True, params={"listenKey": key})
        return {"ok": result.is_ok, "error": str(result.error) if not result.is_ok else None}

    async def _ws_probe_impl(self, listen_key: str, window_s: float) -> dict[str, Any]:
        """真实 ws 探针:连 {base}/ws/{listenKey},窗口内收帧。

        connected=握手成功;ack=整个窗口连接保持健康(无异常关闭);
        frames=窗口内收到的 text/binary 帧数(与 ws_client 的 RFC 6455
        帧处理同源)。
        """
        ws = WebSocketConnection()
        url = f"{_WS_BASE_URL}/ws/{listen_key}"
        try:
            await ws.connect(url, timeout=CONNECT_TIMEOUT)
        except (WebSocketError, OSError) as exc:
            ws.abort()
            return {
                "connected": False,
                "frames": 0,
                "ack": False,
                "window_seconds": window_s,
                "error": str(exc)[:200],
            }
        frames = 0
        try:
            deadline_at = time.monotonic() + window_s
            while True:
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    ack = True
                    break
                try:
                    opcode, _payload = await asyncio.wait_for(ws.recv_message(), timeout=remaining)
                except asyncio.TimeoutError:
                    ack = True
                    break
                except WebSocketError:
                    ack = False
                    break
                if opcode in (OP_TEXT, OP_BINARY):
                    frames += 1
            return {"connected": True, "frames": frames, "ack": ack, "window_seconds": window_s}
        finally:
            ws.abort()

    # ---- 引擎观察与探针执行 ----

    async def _observe_engine(self, action: str, steps: list[dict[str, Any]]) -> tuple[str | None, bool]:
        """只读观察引擎 /status 的 user_stream_runtime.status。

        返回 (status, reachable):reachable=False 表示 /status 不可达(与
        "可达但缺 user_stream_runtime" 区分,调用侧据此选择错误类型)。
        """
        try:
            payload = self._fetch_status()
        except StatusUnreachableError as exc:
            steps.append({"action": action, "reachable": False, "error": str(exc)[:200]})
            return None, False
        status = _user_stream_status(payload)
        steps.append({"action": action, "status": status, "reachable": True})
        return status, True

    async def _poll_engine_recovery(self, steps: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
        """轮询 /status 直至 user_stream_runtime.status 回到 CONNECTED/HEALTHY。

        窗口 ≤ recovery_deadline、间隔 poll_interval(用注入时钟/睡眠 seam);
        返回 (payload, last_unreachable):payload=None 表示窗口内未恢复,
        last_unreachable 为最近一次 /status 不可达原因(窗口内瞬时不可达不
        中断轮询,留给判定区分"引擎未恢复"与"引擎不可达")。
        """
        poll_started = self._now()
        deadline_at = poll_started + self._recovery_deadline
        polls = 0
        last_unreachable: str | None = None
        while True:
            polls += 1
            try:
                payload = self._fetch_status()
            except StatusUnreachableError as exc:
                last_unreachable = str(exc)[:200]
                payload = None
            status = _user_stream_status(payload) if payload is not None else None
            if status in _RECOVERED_STATUSES:
                steps.append(
                    {
                        "action": "engine_user_stream_recovered",
                        "status": status,
                        "reachable": True,
                        "polls": polls,
                        "elapsed_seconds": round(self._now() - poll_started, 2),
                    }
                )
                return payload, None
            remaining = deadline_at - self._now()
            if remaining <= 0:
                steps.append(
                    {
                        "action": "engine_recovery_timeout",
                        "deadline_seconds": self._recovery_deadline,
                        "polls": polls,
                        "last_status": status,
                        "last_unreachable": last_unreachable,
                    }
                )
                return None, last_unreachable
            await self._sleep(min(self._poll_interval, remaining))

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
            if ctx.client is None:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "CLIENT_UNAVAILABLE",
                    "需要交易所只读客户端创建探针 listen key",
                    {"steps": steps},
                )
            self._client = ctx.client
            before_status, before_reachable = await self._observe_engine("engine_user_stream_before", steps)
            if not before_reachable:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "engine_status_unreachable_before_probe",
                    "探针前引擎 /status 不可达(无法观察 user_stream 基线)",
                    {"steps": steps},
                )
            if before_status is None:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "engine_user_stream_status_missing",
                    "引擎 /status 缺 user_stream_runtime.status",
                    {"steps": steps},
                )
            logger.info(
                "user_stream_reconnect: listen key 探针开始(重建 listen key 诱发引擎断开,验证引擎自动重连)"
            )
            key1 = await self._create_listen_key()
            steps.append({"action": "listen_key_created", "key": _mask_key(key1), "probe": "first"})
            probe1 = await self._ws_probe(key1, self._probe_window)
            steps.append({"action": "ws_probe_first", **probe1})
            if not probe1.get("connected"):
                raise RuntimeError("probe 首次 ws 连接失败")
            key2 = await self._create_listen_key()
            steps.append(
                {
                    "action": "listen_key_recreated",
                    "key": _mask_key(key2),
                    "note": "Binance 语义:新 listenKey 使引擎同账户 key 失效,即探针诱发引擎断开",
                }
            )
            # 时间线证据:探针诱发后引擎 user_stream 状态(可能已 STOPPED/
            # RECONNECTING,也可能尚未感知 —— 只读观察,非致命)
            kicked_status, _kicked_reachable = await self._observe_engine("engine_user_stream_kicked", steps)
            probe2 = await self._ws_probe(key2, self._probe_window)
            steps.append({"action": "ws_probe_second", **probe2})
            if not probe2.get("connected"):
                raise RuntimeError("probe 重连 ws 连接失败")
            frames = int(probe2.get("frames") or 0)
            ack = bool(probe2.get("ack"))
            data_or_ack = frames > 0 or ack
            steps.append({"action": "reconnect_assert", "frames": frames, "ack": ack, "data_or_ack": data_or_ack})
            if not data_or_ack:
                raise RuntimeError("重连后未收到数据帧且无连接 ACK")
            # 清理(非致命):DELETE /fapi/v1/listenKey 关闭探针 key;从不续期,
            # 删除失败时 60 分钟 TTL 自然过期
            steps.append(
                {
                    "action": "cleanup_keepalive_policy",
                    "keepalive_issued": False,
                    "note": "探针 listen key 从不续期;DELETE 失败时 60 分钟自然过期",
                }
            )
            close1 = await self._close_listen_key(key1)
            close2 = await self._close_listen_key(key2)
            steps.append({"action": "cleanup_close", "first": close1, "second": close2})
            # 轮询引擎自动重连恢复:探针诱发断开是预期副作用,引擎应自动重连
            recovered_payload, last_unreachable = await self._poll_engine_recovery(steps)
            if recovered_payload is None:
                if last_unreachable is not None:
                    return self._fail(
                        ScenarioStatus.FAIL,
                        "engine_status_unreachable_after_probe",
                        f"探针后引擎 /status 不可达(恢复窗口内): {last_unreachable}",
                        {"steps": steps},
                    )
                return self._fail(
                    ScenarioStatus.FAIL,
                    "ENGINE_NOT_RECOVERED",
                    f"引擎 user stream 在 {self._recovery_deadline:g}s 内未自动重连恢复",
                    {"steps": steps},
                )
            recovered_status = _user_stream_status(recovered_payload)
            if recovered_status is None:
                return self._fail(
                    ScenarioStatus.FAIL,
                    "engine_user_stream_status_missing_after",
                    "引擎恢复后 /status 缺 user_stream_runtime.status",
                    {"steps": steps},
                )
            age, age_known = _user_stream_event_age(recovered_payload, self._now())
            steps.append({"action": "engine_event_age", "last_event_age_s": round(age, 2), "age_known": age_known})
            ok, reason = ws_reconnect_verdict(before_status, recovered_status, age)
            if ok:
                reason = "engine_reconnected"
            steps.append(
                {
                    "action": "verdict",
                    "before": before_status,
                    "kicked": kicked_status,
                    "after": recovered_status,
                    "last_event_age_s": round(age, 2),
                    "ok": ok,
                    "reason": reason,
                }
            )
            if not ok:
                return self._fail(ScenarioStatus.FAIL, reason.upper(), reason, {"steps": steps})
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS,
                {
                    "steps": steps,
                    "verdict": reason,
                    "engine_user_stream_before": before_status,
                    "engine_user_stream_after": recovered_status,
                    "notional_usdt": 0.0,
                },
                time.monotonic() - started,
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[UserStreamReconnectScenario.scenario_id] = UserStreamReconnectScenario
