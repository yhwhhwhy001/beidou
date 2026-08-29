"""Rebuild config/write-capability-registry.json against the current tree.

M00-E01 governance tool. The registry binds every executable or
exchange-sensitive path to a typed governance record. Mechanical facts
(source digests, terminal write call sites, declared entrypoints, network
imports) are recomputed from the live tree; governance decisions (capability,
status, owner, call_graph) are preserved from the existing registry, with
explicit fail-closed defaults for newly discovered paths.

New paths MUST NOT silently inherit permissive defaults. Unknown paths are
emitted as HARD_HOLD with WRITE_CAPABILITY_REGISTRY_INCOMPLETE and listed in
the printed review set; an operator reviews and re-runs validation.

Usage:
    python -m scripts.rebuild_write_registry
    .venv/bin/python scripts/rebuild_write_registry.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beidou_launcher.write_registry import (
    compute_governance_digest,
    discover_declared_entrypoints,
    discover_governed_source_digests,
    discover_network_imports,
    discover_sensitive_entry_paths,
    discover_terminal_write_calls,
    load_registry,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "write-capability-registry.json"

# Governance decisions for paths introduced after the initial registry was
# authored. Each decision is deliberate: capability + status + owner +
# call_graph, following the semantics of the closest existing family.
_NEW_ENTRY_DECISIONS: dict[str, dict[str, str]] = {
    "apps/testnet_verify/__main__.py": {
        "kind": "script",
        "capability": "TERMINAL_WRITE_INTERLOCK",
        "status": "HARD_HOLD",
        "owner": "Runtime Owner",
        "call_graph": "operator -> canonical Testnet verifier -> bounded runtime guard",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
    },
    "apps/testnet_verify/runtime.py": {
        "kind": "module",
        "capability": "TERMINAL_WRITE_INTERLOCK",
        "status": "HARD_HOLD",
        "owner": "Runtime Owner",
        "call_graph": "canonical verifier -> adaptive execution episode -> bounded Binance adapter",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
    },
    # M00-F07: launchd 实装由 wrapper 托管执行，委托 canonical launcher。
    "deploy/beidou_launchd_wrapper.sh": {
        "kind": "shell",
        "capability": "DELEGATE_TO_LAUNCHER",
        "status": "DELEGATE_ONLY",
        "owner": "Runtime Owner",
        "call_graph": "launchd -> governed wrapper -> canonical launcher (beidou start)",
        "expected_rejection": "CANONICAL_LAUNCHER_REQUIRED",
    },
    # M00-F07-R2: watchdog 只负责受控 kickstart，不能绕过 canonical launcher。
    "deploy/beidou_watchdog.sh": {
        "kind": "shell",
        "capability": "RUNTIME_ACTIVATION",
        "status": "HARD_HOLD",
        "owner": "Runtime Owner",
        "call_graph": "launchd -> governed watchdog -> autopilot kickstart -> canonical launcher",
        "expected_rejection": "NONCANONICAL_ENTRYPOINT_HELD",
    },
    "deploy/com.beidou.watchdog.plist": {
        "kind": "script",
        "capability": "RUNTIME_ACTIVATION",
        "status": "HARD_HOLD",
        "owner": "Runtime Owner",
        "call_graph": "launchd -> governed watchdog script -> autopilot kickstart -> canonical launcher",
        "expected_rejection": "NONCANONICAL_ENTRYPOINT_HELD",
    },
    # G5 certification uses a separate, non-KeepAlive producer that can only
    # observe Testnet user-stream/reconciliation state; all engine terminal
    # writes remain hard-held and the G5 runner owns bounded scenario writes.
    "deploy/com.beidou.g5-producer.plist": {
        "kind": "script",
        "capability": "RUNTIME_ACTIVATION",
        "status": "HARD_HOLD",
        "owner": "Test Quality Owner",
        "call_graph": "G5 certification runner -> dedicated launchd producer -> producer-only canonical launcher",
        "expected_rejection": "NONCANONICAL_ENTRYPOINT_HELD",
    },
    # M22: PITR 启用脚本写宿主机 PG 配置（本地运维迁移族）。
    "scripts/enable_pitr.sh": {
        "kind": "shell",
        "capability": "DATABASE_MIGRATION",
        "status": "OFFLINE_ONLY",
        "owner": "Storage Owner",
        "call_graph": "operator -> PostgreSQL WAL archiving configuration",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    },
    # R-M03-1: 阈值重标定研究脚本（离线回测扫描族）。
    "scripts/recalibrate_r_m03_1.py": {
        "kind": "script",
        "capability": "OFFLINE_REPLAY",
        "status": "OFFLINE_ONLY",
        "owner": "Research Owner",
        "call_graph": "operator -> research recalibration evidence (M08 kernel)",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    },
    # 本工具自身（CI/operator 治理重建，纯源码读取）。
    "scripts/rebuild_write_registry.py": {
        "kind": "script",
        "capability": "SOURCE_READ_ONLY",
        "status": "OFFLINE_ONLY",
        "owner": "Security Owner",
        "call_graph": "CI/operator -> registry rebuild -> source scan only",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    },
    # G5 认证场景协议文件（终端写由认证运行器显式驱动，仅 testnet venue）。
    "beidou_certification/g5_scenarios/protocol/credential_failure.py": {
        "kind": "script",
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "owner": "Test Quality Owner",
        "call_graph": "G5 certification runner -> credential failure protocol scenario -> governed testnet write",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
    },
    "beidou_certification/g5_scenarios/protocol/rate_limit.py": {
        "kind": "script",
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "owner": "Test Quality Owner",
        "call_graph": "G5 certification runner -> rate limit protocol scenario -> governed testnet write",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
    },
    # testnet 订单流核验脚本（人工驱动的 testnet 只读探测/受控写）。
    "scripts/testnet/verify_order_flow.py": {
        "kind": "script",
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "owner": "Test Quality Owner",
        "call_graph": "operator -> testnet order-flow verification script -> governed testnet write",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
    },
}

# 新发现网络导入的治理决策(否则重建写入 UNREVIEWED_NETWORK_IMPORT 并阻断验证)。
_NEW_NETWORK_DECISIONS: dict[str, dict[str, str]] = {
    "beidou_certification/g5_scenarios/restart/process_restart.py::urllib.request": {
        "purpose": "LOCAL_HEALTH_READ",
        "status": "READ_ONLY",
        "owner": "Test Quality Owner",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    },
    "scripts/testnet/run_g5.py::urllib.request": {
        "id": "net-scripts-testnet-run_g5-py--urllib-request",
        "purpose": "LOCAL_HEALTH_READ",
        "status": "READ_ONLY",
        "owner": "Test Quality Owner",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    },
}

# New terminal write call sites introduced by M19-F01 (authorized resume
# relocation) and audited dynamic boundaries.
_NEW_TERMINAL_DECISIONS: dict[str, dict[str, str]] = {
    # The canonical Testnet verifier is held in the registry until the
    # bounded runtime guard is explicitly confirmed for a local campaign.
    "apps/testnet_verify/runtime.py::VerificationRuntime._create_order::create_order": {
        "capability": "TERMINAL_CREATE_SCOPE_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Execution Owner",
        "call_graph": "run_once -> durable PREPARED -> bound Testnet guard -> sole adapter create_order",
    },
    "apps/testnet_verify/runtime.py::VerificationRuntime._settle_order_lifecycle::cancel_order": {
        "capability": "TERMINAL_CANCEL_SCOPE_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Execution Owner",
        "call_graph": "verifier lifecycle -> bound order identity -> Testnet guard -> cancel owned remainder",
    },
    "apps/testnet_verify/runtime.py::_jsonable::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Runtime Owner",
        "call_graph": "verifier evidence serialization -> dynamic field read -> local manifest",
    },
    "beidou_exchange/binance_usdm/adapter.py::BinanceUsdmAdapter.set_leverage::request[POST]": {
        "capability": "ACCOUNT_RISK_SETTING_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Risk Owner",
        "call_graph": "adaptive sizing -> leverage set/readback -> guarded Binance REST POST",
    },
    "beidou_shared/decision_trace.py::DecisionTrace.__post_init__::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Storage Owner",
        "call_graph": "DecisionTrace normalization -> dynamic field read -> append-only evidence",
    },
    "beidou_control/plane.py::ControlPlane.execute_authorized_resume::execute_action[RESUME]": {
        "capability": "CONTROL_RESUME_AUTHORITY_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "CONTROL_AUTHORITY_REQUIRED",
        "owner": "Control Owner",
        "call_graph": "control plane authorized resume -> ControlPlane RESUME -> supervisor terminal interlock",
    },
    "beidou_core/engine.py::AutonomousEngine._policy_float_audited::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_WRITE_BOUNDARY_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Execution Owner",
        "call_graph": "engine audited policy read -> dynamic write boundary",
    },
    "beidou_research/factors/factor.py::FactorPromotionGate.validate_evidence::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_WRITE_BOUNDARY_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Research Owner",
        "call_graph": "factor promotion evidence validation -> dynamic write boundary",
    },
    # V3 contract canonicalization uses dynamic field reads for dataclass
    # validation only.  These are not terminal mutations and must remain
    # explicitly governed as read-only boundaries.
    "beidou_reporting/pnl_attribution.py::AttributionRecord.__post_init__::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Research Owner",
        "call_graph": "attribution contract validation -> dynamic field read -> canonical record",
    },
    "beidou_reporting/pnl_attribution.py::AttributionRecord._known_contributions::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Research Owner",
        "call_graph": "attribution completeness check -> dynamic contribution read -> read-only report",
    },
    "beidou_reporting/pnl_attribution.py::AttributionRecord.field_values::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Research Owner",
        "call_graph": "attribution serialization -> dynamic field read -> canonical values",
    },
    "beidou_reporting/pnl_attribution.py::AttributionRecord.is_complete::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Research Owner",
        "call_graph": "attribution completeness predicate -> dynamic field read -> gate decision",
    },
    "beidou_strategy/alpha/contracts.py::AlphaForecast.__post_init__::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Research Owner",
        "call_graph": "alpha forecast validation -> dynamic field read -> immutable contract",
    },
    "beidou_strategy/alpha/contracts.py::EnsembleComponent.__post_init__::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Research Owner",
        "call_graph": "ensemble component validation -> dynamic field read -> immutable contract",
    },
    "beidou_strategy/alpha/contracts.py::EnsembleForecast.__post_init__::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Research Owner",
        "call_graph": "ensemble forecast validation -> dynamic field read -> immutable contract",
    },
    "beidou_strategy/alpha/contracts.py::_forecast_value::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Research Owner",
        "call_graph": "ensemble value extraction -> dynamic forecast read -> fusion input",
    },
    "beidou_strategy/alpha/trend.py::TrendAlphaPolicy.__post_init__::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Research Owner",
        "call_graph": "trend policy validation -> dynamic field read -> immutable policy",
    },
    "beidou_strategy/portfolio/contracts.py::ExposureTarget.__post_init__::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Risk Owner",
        "call_graph": "exposure target validation -> dynamic field read -> immutable target",
    },
    "beidou_strategy/portfolio/exposure_governor.py::ExposureGovernor._value::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Risk Owner",
        "call_graph": "exposure governor policy read -> dynamic field read -> risk gate",
    },
    "beidou_strategy/portfolio/exposure_governor.py::ExposureGovernor._value::returned_callable[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Risk Owner",
        "call_graph": "exposure governor policy read -> callable result read -> risk gate",
    },
    "beidou_strategy/state/market_state.py::MarketStatePolicy.__post_init__::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Market Data Owner",
        "call_graph": "market state policy validation -> dynamic field read -> immutable policy",
    },
    # G5 认证场景终端写:认证运行器显式驱动,仅 testnet venue。
    "beidou_certification/g5_scenarios/engine/partial_fill.py::PartialFillScenario._attempt_place::create_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 partial-fill scenario -> governed testnet order placement -> adapter create_order",
    },
    "beidou_certification/g5_scenarios/engine/partial_fill.py::PartialFillScenario._close_position::create_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 partial-fill scenario -> governed testnet close-position order -> adapter create_order",
    },
    "beidou_certification/g5_scenarios/engine/partial_fill.py::PartialFillScenario.run::cancel_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 partial-fill scenario -> governed testnet cancellation -> adapter cancel_order",
    },
    "beidou_certification/g5_scenarios/protection/native_protection.py::NativeProtectionScenario._cancel_algo_order_impl::cancel_algo_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 native-protection scenario -> governed testnet algo cancellation -> adapter cancel_algo_order",
    },
    "beidou_certification/g5_scenarios/protection/native_protection.py::NativeProtectionScenario._create_algo_order_impl::create_algo_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 native-protection scenario -> governed testnet algo creation -> adapter create_algo_order",
    },
    "beidou_certification/g5_scenarios/protection/native_protection.py::NativeProtectionScenario.run::_cancel_algo_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 native-protection scenario -> governed testnet algo cancellation wrapper",
    },
    "beidou_certification/g5_scenarios/protection/native_protection.py::NativeProtectionScenario.run::_create_algo_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 native-protection scenario -> governed testnet algo creation wrapper",
    },
    "beidou_certification/g5_scenarios/protocol/clock_skew.py::ClockSkewScenario.run::cancel_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 clock-skew scenario -> governed testnet cancellation -> adapter cancel_order",
    },
    "beidou_certification/g5_scenarios/protocol/clock_skew.py::ClockSkewScenario.run::create_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 clock-skew scenario -> governed testnet order placement -> adapter create_order",
    },
    "beidou_certification/g5_scenarios/protocol/create_query_cancel.py::CreateQueryCancelScenario.run::cancel_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 create-query-cancel scenario -> governed testnet cancellation -> adapter cancel_order",
    },
    "beidou_certification/g5_scenarios/protocol/create_query_cancel.py::CreateQueryCancelScenario.run::create_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 create-query-cancel scenario -> governed testnet order placement -> adapter create_order",
    },
    "beidou_certification/g5_scenarios/protocol/stable_client_order_id.py::StableClientOrderIdScenario.run::cancel_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 stable-client-order-id scenario -> governed testnet cancellation -> adapter cancel_order",
    },
    "beidou_certification/g5_scenarios/protocol/stable_client_order_id.py::StableClientOrderIdScenario.run::create_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 stable-client-order-id scenario -> governed testnet order placement -> adapter create_order",
    },
    "beidou_certification/g5_scenarios/restart/process_restart.py::fetch_status_http::urllib_urlopen": {
        "capability": "DYNAMIC_READ_BOUNDARY",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Test Quality Owner",
        "call_graph": "G5 restart scenario -> local health HTTP read -> status endpoint",
    },
    "scripts/testnet/run_g5.py::_producer_status_http::urllib_urlopen": {
        "capability": "LOCAL_HEALTH_READ",
        "status": "READ_ONLY",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
        "owner": "Test Quality Owner",
        "call_graph": "G5 certification runner -> local producer health HTTP read -> status endpoint",
    },
    "beidou_certification/g5_scenarios/restart/user_stream_reconnect.py::UserStreamReconnectScenario._close_listen_key_impl::request[DELETE]": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "G5 user-stream reconnect scenario -> governed listen-key DELETE -> testnet transport mutation",
    },
    # 引擎终端写调用点(源码重组后相对登记表迁移;治理语义沿用最近族)。
    "beidou_core/engine.py::AutonomousEngine._ensure_entry_protection::_cancel_algo_orders": {
        "capability": "CANCEL_OWNED_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Execution Owner",
        "call_graph": "entry protection replacement -> owned stale protection cancellation batch",
    },
    "beidou_core/engine.py::AutonomousEngine._ensure_entry_protection::_create_algo_order": {
        "capability": "CREATE_PROTECTION_SCOPE_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Execution Owner",
        "call_graph": "entry protection establishment -> create SL/TP Algo order",
    },
    "beidou_core/engine.py::AutonomousEngine._reconciliation_segment::execute_action[RESUME]": {
        "capability": "CONTROL_RESUME_AUTHORITY_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "CONTROL_AUTHORITY_REQUIRED",
        "owner": "Control Owner",
        "call_graph": "supervisor reconciliation segment -> RESUME terminal interlock -> control plane authority",
    },
    "beidou_core/engine.py::AutonomousEngine._sync_venue_leverage::_api_async[POST]": {
        "capability": "ACCOUNT_RISK_SETTING_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Execution Owner",
        "call_graph": "adaptive leverage reconciliation -> venue leverage POST (testnet gated, EXEMPT-21)",
    },
    "beidou_core/engine.py::AutonomousEngine.run::_cancel_algo_order": {
        "capability": "CANCEL_OWNED_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Execution Owner",
        "call_graph": "engine bootstrap recovery -> owned algo cancellation -> adapter cancel_algo_order",
    },
    "beidou_core/engine.py::AutonomousEngine._cancel_stale_protection_algos::_cancel_algo_order": {
        "capability": "CANCEL_OWNED_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Execution Owner",
        "call_graph": "startup stale-protection recovery -> owned stale algo cancellation -> adapter cancel_algo_order",
    },
    # testnet 订单流核验脚本终端写(人工驱动)。
    "scripts/testnet/verify_order_flow.py::main::cancel_algo_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "testnet order-flow verification -> governed algo cancellation -> adapter cancel_algo_order",
    },
    "scripts/testnet/verify_order_flow.py::main::cancel_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "testnet order-flow verification -> governed cancellation -> adapter cancel_order",
    },
    "scripts/testnet/verify_order_flow.py::main::create_algo_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "testnet order-flow verification -> governed algo creation -> adapter create_algo_order",
    },
    "scripts/testnet/verify_order_flow.py::main::create_order": {
        "capability": "TESTNET_CERTIFICATION_WRITE_HELD",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Test Quality Owner",
        "call_graph": "testnet order-flow verification -> governed order placement -> adapter create_order",
    },
}

# Stale terminal sources superseded by relocated call sites in main.
_STALE_TERMINAL_SOURCES = {
    "beidou_control/api.py::ControlPlaneAPI.resume_trading::execute_action[RESUME]",
    "beidou_core/engine.py::AutonomousEngine.run::_cancel_algo_order",
}


def _entry_id(path: str) -> str:
    stem = Path(path).stem.upper().replace("-", "_").replace(".", "_")
    if stem == "__MAIN__":
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8].upper()
        return f"ENTRY-__MAIN__-{digest}"
    return f"ENTRY-{stem[:40]}"


def _terminal_id(source: str) -> str:
    owner = source.split("::", 1)[0].split("/", 1)[0].upper()
    tail = source.split("::")[-1].split("[", 1)[0]
    tail = "".join(ch if ch.isalnum() else "-" for ch in tail).strip("-").upper()
    # 同 owner+tail 的来源(如多个 G5 场景都调用 create_order)需要
    # 唯一后缀:取 source 的短哈希,保证登记表 id 唯一(validate 拒绝重复)。
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8].upper()
    return f"WRITE-{owner}-{tail[:24]}-{digest}"


def _rebuild_oracle_findings(registry: dict[str, Any]) -> list[str]:
    """Rebind independent-oracle findings to governed records.

    Uses the independent oracle's own scanner (scripts/verify_write_registry.py)
    so the registry stays honest to an implementation the primary scanner does
    not share. Previous bindings are kept when still allowed; new findings are
    bound to the first allowed governance record. Findings with no allowed
    record are reported for manual governance work.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_write_registry import expected_governance_ids, scan_repository

    governed = {
        record.get("id"): record
        for section in ("entries", "terminal_write_paths", "network_imports")
        for record in registry.get(section, [])
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    old = {declaration["identity"]: declaration for declaration in registry.get("independent_oracle_findings", [])}
    declarations: list[dict[str, str]] = []
    unbindable: list[str] = []
    for finding in scan_repository(ROOT):
        identity = f"{finding.path}:{finding.line}:{finding.kind}:{finding.detail}"
        allowed = expected_governance_ids(finding, root=ROOT, registry=registry)
        previous = old.get(identity)
        if previous and previous.get("governance_id") in allowed:
            # 复用既有声明时同步治理记录的当前字段(owner/status 等
            # 决策更新后旧声明不得残留过期值)。
            _record = governed.get(str(previous.get("governance_id")))
            if isinstance(_record, dict):
                for _field in ("owner", "status", "negative_test"):
                    if _field in _record:
                        previous[_field] = _record[_field]
            declarations.append(previous)
            continue
        if not allowed:
            unbindable.append(identity)
            print(f"[review] oracle finding has no allowed governance: {identity}")
            continue
        governance_id = sorted(allowed)[0]
        record = governed[governance_id]
        declarations.append(
            {
                "identity": identity,
                "governance_id": governance_id,
                "owner": record.get("owner", ""),
                "status": record.get("status", ""),
                "negative_test": record.get("negative_test", ""),
            }
        )
    registry["independent_oracle_findings"] = sorted(declarations, key=lambda declaration: declaration["identity"])
    return unbindable


def main() -> int:
    registry = load_registry(REGISTRY_PATH)

    # --- mechanical facts, recomputed from the live tree ---
    registry["governed_source_digests"] = discover_governed_source_digests(ROOT)
    registry["declared_entrypoints"] = discover_declared_entrypoints(ROOT)

    # --- network imports: keep governance records, refresh occurrences ---
    discovered_network = discover_network_imports(ROOT)
    raw_network = registry.get("network_imports", [])
    if isinstance(raw_network, dict):
        # 之前一次损坏运行把 dict 写进了 JSON；从 codex 基线恢复治理记录。
        import subprocess

        baseline = subprocess.run(
            ["git", "show", "9e5279d:config/write-capability-registry.json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        raw_network = json.loads(baseline).get("network_imports", [])
    old_network = {item["source"]: item for item in raw_network}
    new_network: list[dict[str, Any]] = []
    for source, occurrences in sorted(discovered_network.items()):
        # 决策优先于旧记录:审查通过的新决策必须覆盖此前写入的
        # UNREVIEWED_NETWORK_IMPORT 占位行(否则验证恒失败)。
        decision = _NEW_NETWORK_DECISIONS.get(source)
        if decision is None and source in old_network:
            record = dict(old_network[source])
            record["occurrences"] = occurrences
            new_network.append(record)
            continue
        if decision is None:
            print(f"[review] new network import without governance decision: {source}")
            decision = {
                "purpose": "UNREVIEWED_NETWORK_IMPORT",
                "status": "HARD_HOLD",
                "owner": "Runtime Owner",
                "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
            }
        network_id = str(decision.get("id") or f"NETWORK-{source.split('::')[-1].upper().replace('.', '-')[:44]}")
        new_network.append(
            {
                "expected_rejection": decision["expected_rejection"],
                "id": network_id,
                "negative_test": (
                    "tests/architecture/test_registry_record_contracts.py"
                    f"::test_network_record_is_behaviorally_bound[{network_id}]"
                ),
                "occurrences": occurrences,
                "owner": decision["owner"],
                "purpose": decision["purpose"],
                "source": source,
                "status": decision["status"],
            }
        )
    registry["network_imports"] = new_network

    # --- entries ---
    old_entries = {entry["path"]: entry for entry in registry.get("entries", [])}
    discovered_paths = discover_sensitive_entry_paths(ROOT)
    new_entries: list[dict[str, Any]] = []
    for path in sorted(discovered_paths):
        decision = _NEW_ENTRY_DECISIONS.get(path)
        if decision is None and path in old_entries:
            new_entries.append(old_entries[path])
            continue
        if decision is None:
            decision = {
                "kind": "shell" if path.endswith(".sh") else "script",
                "capability": "TERMINAL_WRITE_BOUNDARY",
                "status": "HARD_HOLD",
                "owner": "Runtime Owner",
                "call_graph": "UNREVIEWED: newly discovered sensitive path",
                "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
            }
            print(f"[review] new sensitive path without governance decision: {path}")
        entry_id = _entry_id(path)
        new_entries.append(
            {
                "call_graph": decision["call_graph"],
                "capability": decision["capability"],
                "expected_rejection": decision["expected_rejection"],
                "id": entry_id,
                "kind": decision["kind"],
                "negative_test": (
                    "tests/architecture/test_registry_record_contracts.py"
                    f"::test_entry_record_is_behaviorally_bound[{entry_id}]"
                ),
                "owner": decision["owner"],
                "path": path,
                "status": decision["status"],
            }
        )
    registry["entries"] = new_entries

    # --- terminal write paths ---
    discovered_terminal = discover_terminal_write_calls(ROOT)
    old_terminal = {item["source"]: item for item in registry.get("terminal_write_paths", [])}
    new_terminal: list[dict[str, Any]] = []
    for source, occurrences in sorted(discovered_terminal.items()):
        if source in _STALE_TERMINAL_SOURCES:
            print(f"[review] dropping stale terminal source: {source}")
            continue
        decision = _NEW_TERMINAL_DECISIONS.get(source)
        if decision is None and source in old_terminal:
            record = dict(old_terminal[source])
            record["occurrences"] = occurrences
            new_terminal.append(record)
            continue
        if decision is None:
            decision = {
                "capability": "TERMINAL_WRITE_BOUNDARY",
                "status": "HARD_HOLD",
                "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
                "owner": "Runtime Owner",
                "call_graph": "UNREVIEWED: newly discovered terminal write path",
            }
            print(f"[review] new terminal write path without governance decision: {source}")
        terminal_id = _terminal_id(source)
        new_terminal.append(
            {
                "call_graph": decision["call_graph"],
                "capability": decision["capability"],
                "expected_rejection": decision["expected_rejection"],
                "id": terminal_id,
                "negative_test": (
                    "tests/architecture/test_registry_record_contracts.py"
                    f"::test_terminal_record_is_behaviorally_bound[{terminal_id}]"
                ),
                "occurrences": occurrences,
                "owner": decision["owner"],
                "source": source,
                "status": decision["status"],
            }
        )
    registry["terminal_write_paths"] = new_terminal

    # --- declared entrypoint records: sync command to the live tree ---
    declared = registry["declared_entrypoints"]
    records = registry.get("declared_entrypoint_records", {})
    for declaration, command in declared.items():
        record = records.get(declaration)
        if record is None:
            print(f"[review] entrypoint {declaration} has no governance record")
            continue
        if record.get("command") != command:
            print(f"[sync] {declaration}: {record.get('command')} -> {command}")
            record["command"] = command
    registry["declared_entrypoint_records"] = records

    # --- independent oracle findings: rebind against the live tree ---
    unbindable = _rebuild_oracle_findings(registry)
    if unbindable:
        print(f"[review] {len(unbindable)} oracle findings lack an allowed governance record")

    # --- governance digest over the updated payload ---
    registry.pop("governance_digest", None)
    registry["governance_digest"] = compute_governance_digest(registry)

    REGISTRY_PATH.write_text(
        json.dumps(registry, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {REGISTRY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
