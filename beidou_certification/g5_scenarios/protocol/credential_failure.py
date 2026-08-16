"""credential_failure: 无效凭据只读请求 — 不崩溃、不伪造 ok。

真实路径:构造独立 BinanceRESTClient(rest_url=demo-fapi, api_key="",
api_secret=""),调 get_account()(只读、signed)。收到交易所错误响应且
classify_auth_error 判为 AUTH(code -2014/-2015)→ PASS;请求因网络不可达
失败(未收到带 code 的交易所错误响应,如连接拒绝/超时)→ NOT_VERIFIABLE
(venue_unreachable)—— 认证分类只在收到交易所错误响应时验证;其余意外
响应(未分类错误码、意外成功)→ FAIL。notional 记账 0。

判定纯函数 classify_auth_error:code -2014/-2015 → AUTH,其余 → OTHER。
dry_run 路径不构造客户端。
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
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.core.error_taxonomy import Result

logger = logging.getLogger(__name__)

DEMO_FAPI_URL = "https://demo-fapi.binance.com"

AUTH_ERROR_CODES = (-2014, -2015)  # -2014 API-key 格式无效; -2015 无效 API-key/IP/权限


def classify_auth_error(payload: Any) -> str:
    """分类交易所错误负载:认证类(code -2014/-2015)→ AUTH,其余 → OTHER。"""
    if isinstance(payload, dict):
        try:
            code = int(payload.get("code") or 0)
        except (TypeError, ValueError):
            return "OTHER"
        if code in AUTH_ERROR_CODES:
            return "AUTH"
    return "OTHER"


def _build_client() -> BinanceRESTClient:
    """构造无效凭据只读客户端(空 api_key/api_secret,仅发只读请求)。"""
    return BinanceRESTClient(rest_url=DEMO_FAPI_URL, api_key="", api_secret="")


def _received_exchange_error(res: Result[dict]) -> bool:
    """是否收到交易所错误响应(负载带 code)—— 认证分类只在此场景下验证。"""
    if res.error is None:
        return False
    return isinstance(res.error.raw, dict) and "code" in res.error.raw


class CredentialFailureScenario(ScenarioBase):
    scenario_id = "credential_failure"

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"dry_run": True, "steps": steps}, time.monotonic() - started
                )
            logger.info("credential_failure: 空凭据只读请求 get_account(不崩溃、不伪造 ok)")
            client = _build_client()
            res = await client.get_account()
            steps.append({"action": "get_account", "is_ok": res.is_ok})
            if res.is_ok:
                return ScenarioResult(
                    self.scenario_id,
                    ScenarioStatus.FAIL,
                    {"steps": steps},
                    time.monotonic() - started,
                    error_type="INVALID_CREDENTIALS_ACCEPTED",
                    error_message="invalid credentials unexpectedly succeeded",
                )
            if not _received_exchange_error(res):
                # 网络不可达/传输层失败:非认证错误 → 不可验证(认证分类只在
                # 收到交易所错误响应时验证),绝不 FAIL 归罪于凭据
                message = str(res.error)[:200] if res.error is not None else "transport failure"
                return ScenarioResult(
                    self.scenario_id,
                    ScenarioStatus.NOT_VERIFIABLE,
                    {"steps": steps, "reason": "venue_unreachable"},
                    time.monotonic() - started,
                    error_type="VENUE_UNREACHABLE",
                    error_message=message,
                )
            assert res.error is not None  # is_ok=False 且收到交易所错误响应,error 必存在
            payload = res.error.raw
            classification = classify_auth_error(payload)
            code = payload.get("code") if isinstance(payload, dict) else None
            steps.append({"action": "classify_auth_error", "code": code, "class": classification})
            if classification == "AUTH":
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"steps": steps}, time.monotonic() - started
                )
            message = str(res.error)[:200] if res.error is not None else "unexpected error"
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.FAIL,
                {"steps": steps},
                time.monotonic() - started,
                error_type="UNEXPECTED_ERROR_CLASS",
                error_message=f"auth class={classification}: {message}",
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[CredentialFailureScenario.scenario_id] = CredentialFailureScenario
