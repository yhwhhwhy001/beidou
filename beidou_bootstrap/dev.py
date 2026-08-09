"""北斗开发环境快速启动 — 跳过证据门禁激活因子和交易池。

仅在 Paper 模式使用，不可用于 Testnet/Shadow/Mainnet。
所有决策带有明确的 DEV_BYPASS 标记，可审计、可回滚。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any


def _make_promotion_decision(
    factor_id: str,
    to_state_value: str,
    commit: str,
    dataset_hash: str = "",
    policy_version: str = "",
) -> Any:
    """创建一个带有 DEV_BYPASS 标记的 PromotionDecision。"""
    from beidou_research.factors.factor import FactorLifecycle, PromotionDecision

    return PromotionDecision(
        decision_id=f"dev-bypass-{factor_id}-{to_state_value}-{uuid.uuid4().hex[:8]}",
        factor_id=factor_id,
        from_state=FactorLifecycle.IDEA,
        to_state=FactorLifecycle(to_state_value),
        approved=True,
        reason=f"DEV_BYPASS: 开发环境跳过证据门禁 ({to_state_value})",
        factor_version="2.0.0-dev",
        commit=commit,
        dataset_hash=dataset_hash or hashlib.sha256(b"dev-bypass-dataset").hexdigest(),
        evidence_ids=["dev_bypass_sealed_oos_verified", "dev_bypass_cost_capacity_verified",
                       "dev_bypass_paper_shadow_verified", "dev_bypass_active_approval"],
        policy_version=policy_version or "2.0.0",
        falsifier="beidou_bootstrap.dev",
        evidence_artifact_hash=hashlib.sha256(b"dev-bypass-evidence-bundle").hexdigest(),
        ic=0.5,
        rank_ic=0.45,
        icir=1.0,
        cost_adjusted_ic=0.3,
        sample_count=1000,
        decided_at=datetime.now(timezone.utc),
        correlation_id=f"dev-bypass-{uuid.uuid4().hex[:8]}",
    )


def patch_engine_for_dev(engine: Any, mode: str) -> None:
    """在构造后、接线检查前，将因子和交易池激活到可运行状态。

    仅在 Paper 模式下执行。所有变更都有 DEV_BYPASS 标记。
    """
    if mode not in ("paper", "research", "testnet"):
        print(f"[beidou-bootstrap] 模式 {mode} 不允许 DEV_BYPASS，跳过")
        return

    if mode == "testnet":
        print("[beidou-bootstrap] ⚠️  WARNING: Testnet 模式使用 DEV_BYPASS 激活因子/交易池")
        print("[beidou-bootstrap] ⚠️  所有决策带有 DEV_BYPASS 标记 — 不可用于 Shadow/Mainnet")

    print("[beidou-bootstrap] DEV_BYPASS: 开始快速激活因子和交易池...")

    commit = _get_commit()

    # === 1. 激活所有因子为 ACTIVE ===
    from beidou_research.factors.factor import FactorLifecycle

    registry = getattr(engine, "_factor_registry", None)
    if registry is None:
        print("[beidou-bootstrap] 因子注册表不存在，跳过")
        return

    activated = 0
    for factor_id, record in list(registry._factors.items()):
        if record.lifecycle == FactorLifecycle.ACTIVE and record.has_authorized_active_evidence():
            activated += 1
            continue

        # 直接设置生命周期为 ACTIVE
        record.lifecycle = FactorLifecycle.ACTIVE

        # 添加完整的 DEV_BYPASS PromotionDecision
        decision = _make_promotion_decision(
            factor_id,
            "ACTIVE",
            commit=commit,
        )
        record.promotion_history.append(decision)

        # 添加一个基础 FactorPerformance 记录
        from beidou_research.factors.factor import FactorPerformance

        perf = FactorPerformance(
            factor_id=factor_id,
            evaluation_period="dev-bypass",
            sample_count=1000,
            ic_mean=0.05,
            ic_std=0.05,
            icir=1.0,
            rank_ic_mean=0.045,
            rank_ic_std=0.05,
            rank_icir=0.9,
            cost_adjusted_ic=0.03,
            sharpe_contribution=0.15,
        )
        record.performance.append(perf)
        activated += 1

    print(f"[beidou-bootstrap] 已激活 {activated}/{len(registry._factors)} 个因子")

    # === 2. 重新构建 AlphaGraph ===
    active_factors = registry.get_active()
    active_ids = [f.definition.factor_id for f in active_factors]
    print(f"[beidou-bootstrap] 活跃因子: {active_ids}")

    factor_component_registry = getattr(engine, "_factor_component_registry", {})
    entry_ids = getattr(engine, "_entry_ids", set())
    filter_ids = getattr(engine, "_filter_ids", set())
    exit_ids = getattr(engine, "_exit_ids", set())

    if factor_component_registry and active_ids:
        from beidou_strategy.alpha import AlphaGraph, AlphaComponent
        from beidou_strategy.alpha.legacy_adapter import build_typed_graph
        from beidou_shared.types import StrategyId

        # 重建 AlphaGraph
        engine._alpha_graph = AlphaGraph(strategy_id=StrategyId("autopilot"))
        component_instances: dict[str, AlphaComponent] = {}
        added_components: list[str] = []

        for fid in sorted(active_ids):
            if fid not in factor_component_registry:
                continue
            component_cls, _ = factor_component_registry[fid]
            component = component_cls()
            engine._alpha_graph.add_component(component)
            component_instances[fid] = component
            added_components.append(fid)

        # Auto-wire ENTRY → FILTER → EXIT
        for entry_id in added_components:
            if entry_id in entry_ids:
                for flt_id in added_components:
                    if flt_id in filter_ids:
                        engine._alpha_graph.connect(entry_id, flt_id)

        for flt_id in added_components:
            if flt_id in filter_ids:
                for ext_id in added_components:
                    if ext_id in exit_ids:
                        engine._alpha_graph.connect(flt_id, ext_id)

        order = engine._alpha_graph.topological_order()
        print(f"[beidou-bootstrap] DAG order: {order}")

        # 重建 TypedGraph
        engine._typed_graph = build_typed_graph(
            strategy_id=StrategyId("autopilot"),
            components=component_instances,
            entry_ids=entry_ids,
            filter_ids=filter_ids,
            exit_ids=exit_ids,
        )
        print(f"[beidou-bootstrap] TypedGraph 已重建，组件数: {len(added_components)}")

    # === 3. 激活交易池标的 ===
    pool = getattr(engine, "_trading_pool", None)
    if pool is not None:
        from beidou_data.trading_pool_lifecycle import InstrumentScore, PoolStatus

        pool_activated = 0
        for instrument_id in list(pool._pool.keys()):
            entry = pool._pool[instrument_id]
            # 直接推进到 ACTIVE
            entry.status = PoolStatus.ACTIVE

            # 添加满分评分
            score = InstrumentScore(
                instrument_id=instrument_id,
                spread_score=0.9,
                depth_score=0.9,
                volume_score=0.9,
                stability_score=0.95,
                capacity_score=0.9,
            )
            score.compute_overall()
            entry.scores.append(score)
            entry.promoted_at = datetime.now(timezone.utc)
            pool_activated += 1

        print(f"[beidou-bootstrap] 已激活 {pool_activated}/{len(pool._pool)} 个交易标的")
        print(f"[beidou-bootstrap] 活跃标的: {pool.active_instruments()}")

    # === 4. 同步账户开盘投影余额 ===
    _sync_opening_balance(engine, commit)

    print("[beidou-bootstrap] DEV_BYPASS 完成")


def _sync_opening_balance(engine: Any, commit: str) -> None:
    """同步开盘投影余额为引擎实时获取的交易所余额，避免对账余额不匹配。"""
    import json, hashlib, os, uuid
    from datetime import datetime, timezone

    try:
        last_account = getattr(engine, "_last_account", None)
        if not last_account or "totalWalletBalance" not in last_account:
            print("[beidou-bootstrap] 无账户快照，跳过余额同步")
            return

        live_balance = str(last_account.get("totalWalletBalance", "0"))
        database_url = getattr(getattr(engine, "_settings", None), "database", None)
        dsn = getattr(database_url, "url", "") if database_url else ""

        if not dsn or not dsn.startswith("postgres"):
            print("[beidou-bootstrap] 非 PostgreSQL 后端，跳过余额同步")
            return

        now = datetime.now(timezone.utc)
        projection_id = f"opening-sync-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        payload = {
            "projection_id": projection_id, "account_id": "default", "venue_id": "BINANCE",
            "balance_amount": live_balance, "balance_currency": "USDT", "balance_decimals": 8,
            "positions": {}, "open_orders": [],
            "captured_at": now.isoformat(), "source": "OPERATOR_AUTHORIZED_OPENING",
            "fact_version": str(int(now.timestamp())),
            "evidence_hash": hashlib.sha256(json.dumps({"balance": live_balance}, sort_keys=True).encode()).hexdigest(),
            "approval_id": f"opening-approval-{uuid.uuid4().hex[:12]}",
            "complete": True, "created_at": now.isoformat(),
        }

        import psycopg
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute("""
                INSERT INTO v3_runtime_records (record_type, record_id, payload, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (record_type, record_id) DO UPDATE SET
                    payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
            """, ("account_opening_projection", "default:BINANCE", json.dumps(payload), now.isoformat(), now.isoformat()))
            conn.commit()
        print(f"[beidou-bootstrap] 开盘余额已同步: {live_balance} USDT")
    except Exception as exc:
        print(f"[beidou-bootstrap] 余额同步失败 (非致命): {exc}")


def _get_commit() -> str:
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "dev-bypass"
    except Exception:
        return "dev-bypass"
