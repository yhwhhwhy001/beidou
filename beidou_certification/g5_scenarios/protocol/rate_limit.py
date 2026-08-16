"""rate_limit: 客户端限频熔断状态模拟 — 不真实触发限频、不打交易所。

真实路径:构造独立 BinanceRESTClient,手动置 _rate_state.circuit_open=True 且
circuit_open_until 指向未来(等价真实限频 5 连败后的熔断态),断言
is_circuit_breaker_open() 为 True;BD-FIX(rate-budget) 语义下高权重 GET
缓存命中在熔断窗口内仍返回最近成功事实(无网络);未命中缓存的请求被
circuit 短路为 ErrorCategory.RATE_LIMIT 失败(无网络);reset_circuit_breaker()
后恢复 False。notional 记账 0。

判定纯函数 classify_rate_limit:交易所限频负载 -1003 → RATE_LIMIT,
业务拒绝 -2010 → BUSINESS,其余 → UNKNOWN。dry_run 路径不构造客户端。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result

logger = logging.getLogger(__name__)

RATE_LIMIT_CODE = -1003  # Too many requests(交易所限频)
BUSINESS_CODE = -2010  # 业务拒绝(余额/保证金等不足)

CIRCUIT_OPEN_SECONDS = 3600.0  # 模拟熔断窗口长度(远大于真实 cooldown,仅状态模拟)


def classify_rate_limit(payload: Any) -> str:
    """分类交易所限频负载:限频拒绝 → RATE_LIMIT,业务拒绝 → BUSINESS,其余 → UNKNOWN。"""
    if isinstance(payload, dict):
        try:
            code = int(payload.get("code") or 0)
        except (TypeError, ValueError):
            return "UNKNOWN"
        if code == RATE_LIMIT_CODE:
            return "RATE_LIMIT"
        if code == BUSINESS_CODE:
            return "BUSINESS"
    return "UNKNOWN"


def _build_client() -> BinanceRESTClient:
    """构造独立限频状态模拟客户端(不发送任何请求)。"""
    return BinanceRESTClient(rest_url="https://demo-fapi.binance.com")


class RateLimitScenario(ScenarioBase):
    scenario_id = "rate_limit"

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"dry_run": True, "steps": steps}, time.monotonic() - started
                )
            logger.info("rate_limit: 模拟客户端熔断状态(不真实触发限频,不打交易所)")
            client = _build_client()
            state = client._rate_state
            state.circuit_open = True
            state.circuit_open_until = time.monotonic() + CIRCUIT_OPEN_SECONDS
            steps.append({"action": "inject_circuit_open", "circuit_open": True})

            is_open = client.is_circuit_breaker_open()
            steps.append({"action": "is_circuit_breaker_open", "is_open": is_open})
            ok = is_open

            # BD-FIX (rate-budget): 熔断窗口内高权重 GET 缓存命中仍返回最近成功事实(无网络)
            cached: Result[list] = Result.ok([], source="cache_seed")
            client._get_cache[(Endpoint.OPEN_ORDERS, ())] = (time.monotonic(), cached)
            cached_res = await client.get_open_orders()
            steps.append({"action": "cached_get_during_circuit_open", "is_ok": cached_res.is_ok, "from_cache": True})
            ok = ok and cached_res.is_ok

            # 未命中缓存的请求在熔断窗口内被 circuit 短路为 RATE_LIMIT(不产生网络)
            open_res = await client.get_open_orders("OTHER")
            rate_category = open_res.error.category if open_res.error is not None else None
            steps.append(
                {
                    "action": "uncached_get_rejected",
                    "is_ok": open_res.is_ok,
                    "category": rate_category.value if rate_category is not None else None,
                }
            )
            ok = ok and (not open_res.is_ok) and rate_category == ErrorCategory.RATE_LIMIT

            rate_class = classify_rate_limit({"code": RATE_LIMIT_CODE, "msg": "Too many requests"})
            steps.append({"action": "classify_rate_limit_payload", "class": rate_class})
            ok = ok and rate_class == "RATE_LIMIT"

            client.reset_circuit_breaker()
            after_reset = client.is_circuit_breaker_open()
            steps.append({"action": "reset_circuit_breaker", "is_open_after": after_reset})
            ok = ok and not after_reset

            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                {"steps": steps},
                time.monotonic() - started,
                error_type="" if ok else "CIRCUIT_SEMANTIC_MISMATCH",
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[RateLimitScenario.scenario_id] = RateLimitScenario
