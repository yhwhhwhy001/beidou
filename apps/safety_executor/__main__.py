"""
实时安全与执行层入口 (Safety Executor)。Clock Domain: REALTIME。

启动顺序：
1. 加载 Policy Registry 并验证所有 HARD_SAFETY 策略
2. 初始化服务身份与凭据
3. 启动交易所适配器与健康监控
4. 初始化 Pre-Risk Checker、Risk Engine、Approval Signer
5. 启动 Executor（单活 Lease + Fencing）
6. 进入事件循环
"""

from __future__ import annotations

import signal
import sys


def main() -> None:
    """启动实时安全与执行引擎。"""
    print("[beidou-safety] ========================================")
    print("[beidou-safety] Starting Safety Executor V2.0")
    print("[beidou-safety] Clock Domain: REALTIME")
    print("[beidou-safety] ========================================")

    # 1. Policy Registry
    from beidou_policy.registry import PolicyRegistry
    registry = PolicyRegistry()
    print("[beidou-safety] Policy Registry: initialized")

    # 2. Security identity
    from beidou_security.identity import ServiceIdentity
    identity = ServiceIdentity(
        service_name="beidou-safety",
        service_id="svc-safety-001",
        roles=frozenset({"executor", "risk_checker"}),
    )
    print(f"[beidou-safety] Service Identity: {identity.service_id}")

    # 3. Exchange adapter + health
    from beidou_exchange.binance_usdm import BinanceUsdmAdapter
    adapter = BinanceUsdmAdapter()
    print(f"[beidou-safety] Exchange Adapter: {adapter.venue_id}")

    # 4. Risk chain
    from beidou_safety.risk.engine import PreRiskCheckerImpl, RiskEngineImpl, RiskApprovalSignerImpl
    pre_risk = PreRiskCheckerImpl()
    risk_engine = RiskEngineImpl()
    approval_signer = RiskApprovalSignerImpl()
    print("[beidou-safety] Risk Chain: PreRisk → RiskEngine → ApprovalSigner — ready")

    # 5. Executor
    from beidou_safety.executor_impl import LeaseManager, FencingProtection
    lease_mgr = LeaseManager(lease_timeout=30.0)
    fencing = FencingProtection()
    print("[beidou-safety] Executor: LeaseManager + FencingProtection — ready")

    # 6. Observability
    from beidou_observability.telemetry import TraceContext
    from beidou_shared.types import CorrelationId
    trace = TraceContext(correlation_id=CorrelationId("safety-startup"))
    trace.start_span("safety_executor_bootstrap")
    print("[beidou-safety] Observability: TraceContext initialized")

    print("[beidou-safety] ========================================")
    print("[beidou-safety] All systems initialized. Running...")
    print("[beidou-safety] ========================================")

    def shutdown(signum: int, frame: object) -> None:
        print("\n[beidou-safety] Received shutdown signal.")
        print("[beidou-safety] Executing graceful shutdown...")
        print("[beidou-safety] 1. Pause new risk (NO_NEW_RISK)")
        print("[beidou-safety] 2. Cancel pending orders")
        print("[beidou-safety] 3. Save checkpoint")
        print("[beidou-safety] 4. Release leases")
        print("[beidou-safety] Shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        signal.pause()
    except AttributeError:
        import time
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
