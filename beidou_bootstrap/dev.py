"""北斗开发环境快速启动 — 跳过证据门禁激活因子和交易池。

仅在 Paper 模式使用，不可用于 Testnet/Shadow/Mainnet。
所有决策带有明确的 DEV_BYPASS 标记，可审计、可回滚。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
        evidence_ids=[
            "dev_bypass_sealed_oos_verified",
            "dev_bypass_cost_capacity_verified",
            "dev_bypass_paper_shadow_verified",
            "dev_bypass_active_approval",
        ],
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
    """在构造后、接线检查前，将因子激活到可运行状态。

    仅激活因子（信号生成所需）。交易池标的由 TradingPool 真实生命周期算法管理：
    OBSERVING → PROMOTED → ACTIVE，需要评分达标 + 观察期满。
    设置 BEIDOU_SKIP_FACTOR_BYPASS=1 可同时跳过因子强制激活。
    """
    import os as _os

    if mode not in ("paper", "research"):
        logger.warning("Mode %s is not eligible for DEV_BYPASS", mode)
        return

    skip_pool = _os.environ.get("BEIDOU_SKIP_POOL_BYPASS", "1") == "1"  # 默认跳过交易池 bypass

    logger.info("DEV_BYPASS factor activation started")

    commit = _get_commit()

    # === 1. 激活所有因子为 ACTIVE ===
    from beidou_research.factors.factor import FactorLifecycle

    registry = getattr(engine, "_factor_registry", None)
    if registry is None:
        logger.info("DEV_BYPASS skipped because factor registry is unavailable")
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

    logger.info("DEV_BYPASS activated %d/%d factors", activated, len(registry._factors))

    # === 2. 重新构建 AlphaGraph ===
    active_factors = registry.get_active()
    active_ids = [f.definition.factor_id for f in active_factors]
    logger.info("DEV_BYPASS active factor count: %d", len(active_ids))

    factor_component_registry = getattr(engine, "_factor_component_registry", {})
    entry_ids: set[str] = getattr(engine, "_entry_ids", set())
    filter_ids: set[str] = getattr(engine, "_filter_ids", set())
    exit_ids: set[str] = getattr(engine, "_exit_ids", set())

    if factor_component_registry and active_ids:
        from beidou_shared.types import StrategyId
        from beidou_strategy.alpha import AlphaComponent, AlphaGraph
        from beidou_strategy.alpha.legacy_adapter import build_typed_graph

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
        logger.debug("DEV_BYPASS DAG order: %s", order)

        # 重建 TypedGraph 并同步到内核
        engine._typed_graph = build_typed_graph(
            strategy_id=StrategyId("autopilot"),
            components=component_instances,
            entry_ids=entry_ids,
            filter_ids=filter_ids,
            exit_ids=exit_ids,
        )
        if hasattr(engine, "_strategy_kernel"):
            engine._strategy_kernel.set_typed_graph(engine._typed_graph)
        logger.info("DEV_BYPASS rebuilt TypedGraph with %d components", len(added_components))

    # === 3. 交易池：由真实 TradingPool 生命周期算法管理 ===
    pool = getattr(engine, "_trading_pool", None)
    if pool is not None:
        if skip_pool:
            # 缩短观察期（本地开发快速验证），首次评估将使用已有行情数据
            for instrument_id in list(pool._pool.keys()):
                entry = pool._pool[instrument_id]
                entry.min_observation_hours = 5.0 / 60.0  # 5 分钟观察期
            logger.info(
                "DEV_BYPASS pool has %d OBSERVING instruments (window=%dmin, threshold=%s)",
                len(pool._pool),
                5,
                pool.PROMOTE_THRESHOLD,
            )
        else:
            from beidou_data.trading_pool_lifecycle import InstrumentScore, PoolStatus

            pool_activated = 0
            for instrument_id in list(pool._pool.keys()):
                entry = pool._pool[instrument_id]
                entry.status = PoolStatus.ACTIVE
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
            logger.info("DEV_BYPASS activated %d/%d pool instruments", pool_activated, len(pool._pool))
            logger.debug("DEV_BYPASS active instruments: %s", pool.active_instruments())

    logger.info("DEV_BYPASS completed")


async def bootstrap_universe(engine: Any, mode: str) -> None:
    """启动时立即运行首次宇宙评估。

    使用 REST API 的 kline 数据做评分（不依赖 WebSocket ticker）。
    对 OBSERVING 标的评分后跳过观察期直接晋级达标标的。
    """
    import asyncio as _asyncio
    import math as _math

    from beidou_data.trading_pool_lifecycle import InstrumentScore
    from beidou_data.trading_pool_lifecycle import PoolStatus as _PoolStatus

    if mode not in ("paper", "research"):
        logger.warning("Mode %s is not eligible for universe DEV_BYPASS", mode)
        return

    pool = getattr(engine, "_trading_pool", None)
    feed = getattr(engine, "_feed", None)
    if pool is None or feed is None:
        logger.info("Universe DEV_BYPASS skipped because pool or feed is unavailable")
        return

    candidates = [iid for iid, e in pool._pool.items() if e.status == _PoolStatus.OBSERVING]
    if not candidates:
        active = pool.active_instruments()
        logger.info("Universe DEV_BYPASS has no candidates; active=%d", len(active))
        return

    logger.info("Universe DEV_BYPASS evaluating %d candidates", len(candidates))
    scored = 0
    promoted = 0
    for instrument_id in candidates:
        try:
            # 用一个 REST 调用同时获取 ticker（价格/spread）和 kline（波动率/成交量）
            await feed.async_update_features(instrument_id)
            ticker = feed.get_last_ticker(instrument_id)
            features = await feed.async_get_kline_features(instrument_id, "1h", 50)

            close = float((features or {}).get("close", 0) or 0)
            if close <= 0:
                continue

            # 点差: 从 ticker bid/ask 获取，不可用时用 kline high-low 估算
            bid = ask = close
            if ticker:
                bid = float(ticker.get("bidPrice", 0) or ticker.get("bid", 0) or close)
                ask = float(ticker.get("askPrice", 0) or ticker.get("ask", 0) or close)
            if 0 < bid <= ask:
                spread_bps = (ask - bid) / ask * 10000
            else:
                # 用 high-low 作为 spread 估计
                high = float((features or {}).get("high", close) or close)
                low = float((features or {}).get("low", close) or close)
                spread_bps = max(0.1, (high - low) / close * 10000)
            spread_score = max(0.0, min(1.0, 1.0 - (spread_bps - 1) / 49)) if spread_bps > 1 else 1.0

            # 深度: 从 orderbook 获取，不可用时默认中位
            depth_score = 0.5
            try:
                ob = feed.get_last_orderbook(instrument_id)
                if ob:
                    bids_vol = sum(float(b[1]) for b in (ob.get("bids", []) or [])[:5])
                    asks_vol = sum(float(a[1]) for a in (ob.get("asks", []) or [])[:5])
                    depth_usdt = (bids_vol + asks_vol) * close
                    depth_score = max(0.0, min(1.0, _math.log10(max(1, depth_usdt)) / 5))
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("DEV_BYPASS depth estimate unavailable for %s: %s", instrument_id, type(exc).__name__)

            # 成交量: 24h 量 × 价格
            vol_24h = float((features or {}).get("volume_24h", 0) or 0)
            vol_usdt = vol_24h * close if vol_24h > 0 else 0
            volume_score = max(0.0, min(1.0, _math.log10(max(1, vol_usdt)) / 8)) if vol_usdt > 0 else 0.1

            # 稳定性: 有 OHLC 数据 = 稳定
            stability_score = 0.8 if (features and features.get("high") and features.get("low")) else 0.4

            # 容量: 低波动 = 高容量
            ann_vol = float((features or {}).get("ann_volatility", 0.5) or 0.5)
            capacity_score = max(0.0, min(1.0, 1.0 - ann_vol))

            overall_raw = (
                spread_score * 0.25
                + depth_score * 0.25
                + volume_score * 0.20
                + stability_score * 0.15
                + capacity_score * 0.15
            )

            score = InstrumentScore(
                instrument_id=instrument_id,
                spread_score=round(spread_score, 4),
                depth_score=round(depth_score, 4),
                volume_score=round(volume_score, 4),
                stability_score=round(stability_score, 4),
                capacity_score=round(capacity_score, 4),
            )
            pool.score(instrument_id, score)
            scored += 1

            # 跳过观察期，直接尝试晋级
            entry = pool._pool.get(instrument_id)
            if entry is not None and entry.status == _PoolStatus.OBSERVING:
                entry.observing_since = datetime(2020, 1, 1, tzinfo=timezone.utc)
                if pool.try_promote(instrument_id):
                    pool.activate(instrument_id)
                    promoted += 1
                    logger.info(
                        "Universe DEV_BYPASS promoted %s: overall=%.3f spread=%.1fbps volume=%.1fM",
                        instrument_id,
                        overall_raw,
                        spread_bps,
                        vol_usdt / 1e6,
                    )

            await _asyncio.sleep(0.05)  # 减少 API 压力

        except Exception as exc:
            logger.warning("DEV_BYPASS universe evaluation failed for %s: %s", instrument_id, type(exc).__name__)
            continue

    active = pool.active_instruments()
    logger.info(
        "Universe DEV_BYPASS completed: scored=%d promoted=%d active=%d",
        scored,
        promoted,
        len(active),
    )


def _get_commit() -> str:
    from beidou_launcher.preflight import current_commit

    commit = current_commit(Path(__file__).resolve().parents[1])
    return commit if commit != "UNKNOWN" else "dev-bypass"
