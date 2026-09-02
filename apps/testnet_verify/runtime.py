"""Closed-bar Testnet verification runtime.

This module is the only composition root for the V4 Testnet path.  It uses
the existing Binance adapter as its sole writer and keeps every order-linked
fact in :class:`DecisionTraceStore`.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import itertools
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from beidou_data.trading_pool_lifecycle import (
    PoolStatus,
    TradingPool,
    TradingPoolSnapshot,
    discover_startup_candidates,
)
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.core.protocol import OrderRequest
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
from beidou_exchange.core.write_authority import TerminalWriteContext
from beidou_exchange.testnet_guard import TestnetEnvironmentGuard
from beidou_shared.decision_trace import DecisionTrace, DecisionTraceStore, TraceStatus
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    InstrumentId,
    OrderSide,
    OrderStatus,
    OrderType,
    Quantity,
    SchemaVersion,
    StrategyId,
    VenueId,
    VenueInstrument,
)
from beidou_strategy.alpha.contracts import EntryProposal, FilterDecision, FilterResult
from beidou_strategy.alpha.typed_graph import EntryNode, FeatureNode, FilterNode, FusionNode, TypedAlphaGraph
from beidou_strategy.kernel_parity import StrategyKernel, StrategyKernelContract
from beidou_strategy.risk.adaptive_sizing_engine import SizingInput, compute_adaptive_sizing

from .config import VerifierConfig


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, dict):
        return _jsonable(value.value)
    if is_dataclass(value):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _jsonable(value.model_dump())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _stable_hash(value: Any) -> str:
    payload = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _top_of_book_price(levels: list[Any], side: str) -> float | None:
    """Return the best price from an orderbook side without trusting shape.

    The Binance demo orderbook depth response is a list of ``[price, qty]``
    rows; anything malformed yields ``None`` so the caller can fail closed
    with an explicit reason instead of treating garbage as a spread.
    """

    if not isinstance(levels, list):
        return None
    for row in levels:
        if not isinstance(row, list) or len(row) < 2:
            continue
        price = _finite_float(row[0])
        quantity = _finite_float(row[1])
        if price is not None and price > 0 and quantity is not None and quantity >= 0:
            return price
    logger = logging.getLogger("beidou.testnet_verify")
    logger.warning("orderbook side has no valid %s level", side)
    return None


def _economic_truth_assessment() -> dict[str, Any]:
    """PKG-09-M03: E0–E6 economic truth is independent of Testnet readiness.

    The verifier owns execution facts only; it never supplies research
    evidence, so the assessment stays fail-closed NOT_EVALUATED.  Research
    pipelines may call ``assess_economic_truth`` directly with real evidence.
    """

    from beidou_research.economic_truth import assess_economic_truth

    return assess_economic_truth().to_dict()


def _git_revision() -> str:
    """Read the current repository revision without invoking a shell."""

    configured = os.getenv("BEIDOU_GIT_COMMIT", "").strip()
    if configured:
        return configured
    root = Path(__file__).resolve().parents[2]
    git_path = root / ".git"
    try:
        if git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                git_path = (root / pointer.split(":", 1)[1].strip()).resolve()
        head = (git_path / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_path = git_path / ref
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()
            packed_refs = git_path / "packed-refs"
            if packed_refs.exists():
                for line in packed_refs.read_text(encoding="utf-8").splitlines():
                    if line and not line.startswith("#") and not line.startswith("^"):
                        revision, separator, packed_ref = line.partition(" ")
                        if separator and packed_ref == ref:
                            return revision
        elif head:
            return head
    except (OSError, UnicodeError):
        pass
    return "UNKNOWN"


def _result_error(result: Any, fallback: str) -> dict[str, Any]:
    error = getattr(result, "error", None)
    raw = getattr(error, "raw", None)
    raw_mapping = raw if isinstance(raw, dict) else {}
    return {
        "message": str(getattr(error, "message", fallback))[:300],
        "category": str(getattr(getattr(error, "category", None), "value", "UNKNOWN")),
        "retryable": bool(getattr(error, "retryable", False)),
        "code": raw_mapping.get("code", -1),
    }


class _PoolStateFile:
    """Persist the latest lifecycle event without introducing a new service."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.diff_path = path.with_name(f"{path.stem}-membership-diff.jsonl")
        self._states: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(payload, list):
            for state in payload:
                if isinstance(state, dict) and state.get("instrument_id"):
                    self._states[str(state["instrument_id"])] = dict(state)

    def __call__(self, event: dict[str, Any]) -> None:
        instrument_id = str(event.get("instrument_id") or "").strip().upper()
        if not instrument_id:
            return
        self._states[instrument_id] = copy.deepcopy(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        encoded = json.dumps(list(self._states.values()), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    @property
    def initial_state(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(value) for value in self._states.values()]

    def record_snapshot(self, snapshot: TradingPoolSnapshot) -> None:
        """Append a durable membership diff artifact for every changed snapshot."""

        if not any(snapshot.membership_diff.values()):
            return
        self.diff_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "pool_id": snapshot.pool_id,
            "pool_version": snapshot.version,
            "evaluated_at": snapshot.evaluated_at.isoformat(),
            "snapshot_hash": snapshot.snapshot_hash,
            "membership_diff": snapshot.membership_diff,
        }
        with self.diff_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


@dataclass(slots=True)
class MarketObservation:
    symbol: str
    ticker: dict[str, Any]
    depth: dict[str, Any]
    bars: list[dict[str, Any]]
    funding: list[dict[str, Any]]
    features: dict[str, float]
    source_hashes: tuple[str, ...]


@dataclass(slots=True)
class VerificationSummary:
    run_id: str
    status: str
    startup: dict[str, Any]
    episodes: list[dict[str, Any]]
    trace_ids: list[str]
    manifest_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "startup": _jsonable(self.startup),
            "episodes": _jsonable(self.episodes),
            "trace_ids": list(self.trace_ids),
            "manifest_path": self.manifest_path,
        }


_FEATURE_NAMES = [
    "close",
    "fast_return",
    "slow_return",
    "realized_volatility",
    "spread_bps",
    "depth_notional",
    "quote_volume",
    "liquidity_score",
    "capacity_score",
    "funding_rate",
    "signal_score",
    "signal_confidence",
    "regime_confidence",
    "stop_distance_pct",
]


async def _entry_signal(context: dict[str, Any]) -> EntryProposal:
    features = cast(dict[str, Any], context["features"])
    score = float(features["signal_score"])
    confidence = max(0.0, min(1.0, float(features["signal_confidence"])))
    # This non-zero band is part of the strategy contract: no signal remains
    # NO_ACTION and is never turned into a hand-made order.
    signal_band = 0.0005
    side: OrderSide | None
    if score > signal_band:
        side = OrderSide.BUY
    elif score < -signal_band:
        side = OrderSide.SELL
    else:
        side = None
    strength = min(1.0, abs(score) / max(float(features["realized_volatility"]), 1e-12)) if side else 0.0
    return EntryProposal(
        strategy_id=StrategyId("testnet-verification-strategy-v1"),
        instrument_id=InstrumentId(str(context["instrument_id"])),
        venue_id=VenueId("BINANCE"),
        side=side,
        strength=max(0.0, strength),
        confidence=confidence,
        model_version=SchemaVersion("testnet-momentum-v1"),  # model identity, not a credential
        metadata={"signal_band": signal_band, "factor_hash": str(context.get("factor_hash", ""))},
    )


async def _pool_filter(context: dict[str, Any], _entry: EntryProposal | None) -> FilterResult:
    if str(context.get("pool_status", "")).upper() != PoolStatus.ACTIVE.value:
        return FilterResult(
            decision=FilterDecision.VETO,
            reason_codes=["POOL_SYMBOL_NOT_ACTIVE"],
            component_id="pool-admissibility-v1",
        )
    return FilterResult(decision=FilterDecision.ACCEPT, component_id="pool-admissibility-v1")


async def _quality_filter(context: dict[str, Any], _entry: EntryProposal | None) -> FilterResult:
    features = cast(dict[str, Any], context["features"])
    if features["liquidity_score"] <= 0 or features["capacity_score"] <= 0:
        return FilterResult(
            decision=FilterDecision.VETO,
            reason_codes=["MARKET_LIQUIDITY_UNKNOWN"],
            component_id="market-quality-v1",
        )
    if features["signal_confidence"] < 0.15:
        return FilterResult(
            decision=FilterDecision.DEGRADE,
            confidence_multiplier=0.5,
            size_multiplier=0.5,
            reason_codes=["LOW_SIGNAL_CONFIDENCE"],
            component_id="market-quality-v1",
        )
    return FilterResult(decision=FilterDecision.ACCEPT, component_id="market-quality-v1")


def _build_kernel() -> StrategyKernel:
    graph = TypedAlphaGraph(StrategyId("testnet-verification-strategy-v1"))
    features = FeatureNode("market-features-v1", _FEATURE_NAMES)
    entry = EntryNode(
        "momentum-entry-v1", _entry_signal, factor_version="market-factors-v1", model_version="momentum-v1"
    )
    pool_filter = FilterNode("pool-admissibility-v1", _pool_filter, is_mandatory=True)
    quality_filter = FilterNode("market-quality-v1", _quality_filter, is_mandatory=True)
    fusion = FusionNode("strategy-fusion-v1")
    for node in (features, entry, pool_filter, quality_filter, fusion):
        graph.add_node(node)
    graph.connect(features.node_id, entry.node_id)
    graph.connect(features.node_id, fusion.node_id)
    graph.connect(entry.node_id, pool_filter.node_id)
    graph.connect(entry.node_id, quality_filter.node_id)
    graph.connect(entry.node_id, fusion.node_id)
    graph.connect(pool_filter.node_id, fusion.node_id)
    graph.connect(quality_filter.node_id, fusion.node_id)
    kernel = StrategyKernel(mode="TESTNET")
    kernel.set_typed_graph(graph)
    return kernel


class VerificationRuntime:
    """Bounded closed-bar verifier with dependency injection for offline tests."""

    def __init__(
        self,
        config: VerifierConfig,
        *,
        adapter: Any | None = None,
        pool: TradingPool | None = None,
        trace_store: DecisionTraceStore | None = None,
        kernel: StrategyKernel | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.guard = TestnetEnvironmentGuard(
            config.rest_url,
            max_notional=config.max_notional,
            max_leverage=config.max_leverage,
            max_account_exposure=config.max_account_exposure,
            account_id=config.account_id,
            task_id=config.task_id,
            entrypoint=config.entrypoint,
            writes_enabled=config.confirm_testnet,
            kill_switch_path=config.kill_switch_path,
        )
        self._pool_state = _PoolStateFile(config.pool_state_path)
        self.pool = pool or TradingPool(
            max_instruments=config.max_instruments,
            event_sink=self._pool_state,
            initial_state=self._pool_state.initial_state,
            policy_version="testnet-pool-policy-v1",
            pool_id="testnet-adaptive-pool",
        )
        self.trace_store = trace_store or DecisionTraceStore(config.trace_path)
        self.kernel = kernel or _build_kernel()
        self._adapter_is_injected = adapter is not None
        self._real_order_write_attempted = False
        self._real_order_acknowledged = False
        self._real_order_outcome_unknown = False
        if adapter is None:
            transport = BinanceRESTClient(
                self.guard.rest_base_url,
                api_key=config.api_key,
                api_secret=config.api_secret,
                account_id=config.account_id,
                testnet_guard=self.guard,
            )
            adapter = BinanceUsdmAdapter(
                venue_id=VenueId("BINANCE"),
                account_id=AccountId(config.account_id),
                rest_client=transport,
                testnet_guard=self.guard,
            )
        self.adapter = adapter
        self._observations: dict[str, MarketObservation] = {}
        self._startup_errors: list[dict[str, Any]] = []
        self._startup_facts: dict[str, Any] = {}
        self._episode_identity_namespace = ""

    async def _create_order(
        self,
        request: OrderRequest,
        context: TerminalWriteContext,
    ) -> Any:
        """Submit through the sole adapter and record current-run write facts."""

        if not self._adapter_is_injected:
            self._real_order_write_attempted = True
        response = await self.adapter.create_order(request, write_context=context)
        if not self._adapter_is_injected:
            if response.status is OrderStatus.UNKNOWN or not response.order_id:
                self._real_order_outcome_unknown = True
            else:
                self._real_order_acknowledged = True
        return response

    def _reconcile_flat_filled_traces(self, account_result: Any, mode_result: Any) -> dict[str, Any]:
        """Link durable FILLED traces to later owned closes when venue is flat.

        A restart can leave the opening trace at FILLED even though a later
        reduce-only trace flattened the net position.  Only a successful
        signed account snapshot, one-way position mode, and a quantity-matched
        CLOSED reduce-only trace may close that durable gap.
        """

        report: dict[str, Any] = {"closed_trace_ids": [], "unresolved_trace_ids": []}
        if (
            not account_result.is_success()
            or not isinstance(account_result.data, dict)
            or not mode_result.is_success()
            or not isinstance(mode_result.data, dict)
            or mode_result.data.get("dualSidePosition") is not False
            or not isinstance(account_result.data.get("positions"), list)
        ):
            report["unresolved_trace_ids"] = [
                trace.trace_id for trace in self.trace_store.recovery_candidates() if trace.status is TraceStatus.FILLED
            ]
            return report

        position_totals: dict[str, Decimal] = {}
        for row in account_result.data["positions"]:
            if not isinstance(row, dict) or not str(row.get("symbol", "")).strip():
                report["unresolved_trace_ids"] = [
                    trace.trace_id
                    for trace in self.trace_store.recovery_candidates()
                    if trace.status is TraceStatus.FILLED
                ]
                return report
            try:
                amount = Decimal(str(row.get("positionAmt", "")))
            except InvalidOperation:
                report["unresolved_trace_ids"] = [
                    trace.trace_id
                    for trace in self.trace_store.recovery_candidates()
                    if trace.status is TraceStatus.FILLED
                ]
                return report
            if not amount.is_finite():
                report["unresolved_trace_ids"] = [
                    trace.trace_id
                    for trace in self.trace_store.recovery_candidates()
                    if trace.status is TraceStatus.FILLED
                ]
                return report
            symbol = str(row["symbol"]).strip().upper()
            position_totals[symbol] = position_totals.get(symbol, Decimal("0")) + amount

        filled_traces = sorted(
            (trace for trace in self.trace_store.recovery_candidates() if trace.status is TraceStatus.FILLED),
            key=lambda trace: trace.timestamp,
        )
        closed_traces = sorted(
            (trace for trace in self.trace_store.all() if trace.status is TraceStatus.CLOSED),
            key=lambda trace: trace.timestamp,
        )
        used_close_ids: set[str] = set()
        for filled in filled_traces:
            symbol = str(filled.order_request.get("symbol", "")).strip().upper()
            side = str(filled.order_request.get("side", "")).strip().upper()
            try:
                filled_quantity = Decimal(str(filled.executed_qty))
            except InvalidOperation:
                report["unresolved_trace_ids"].append(filled.trace_id)
                continue
            if (
                not symbol
                or side not in {"BUY", "SELL"}
                or not filled_quantity.is_finite()
                or filled_quantity <= 0
                or position_totals.get(symbol, Decimal("0")) != 0
            ):
                report["unresolved_trace_ids"].append(filled.trace_id)
                continue

            expected_close_side = "SELL" if side == "BUY" else "BUY"
            matching_close: DecisionTrace | None = None
            for close in closed_traces:
                try:
                    close_quantity = Decimal(str(close.executed_qty))
                    close_position_quantity = Decimal(str(close.position_after.get("quantity", "")))
                except InvalidOperation:
                    continue
                if (
                    close.trace_id not in used_close_ids
                    and close.timestamp >= filled.timestamp
                    and str(close.order_request.get("symbol", "")).strip().upper() == symbol
                    and str(close.order_request.get("side", "")).strip().upper() == expected_close_side
                    and close.order_request.get("reduceOnly") is True
                    and close_quantity == filled_quantity
                    and close_position_quantity == 0
                    and close.reconciliation.get("status") == "MATCHED"
                    and close.reconciliation.get("unresolved") == []
                    and close.exchange_order_id
                ):
                    matching_close = close
                    break
            if matching_close is None:
                report["unresolved_trace_ids"].append(filled.trace_id)
                continue

            used_close_ids.add(matching_close.trace_id)
            recovery_metadata = {
                **filled.metadata,
                "recovery": "STARTUP_MATCHED_LATER_REDUCE_ONLY_CLOSE",
                "close_trace_id": matching_close.trace_id,
                "close_order_id": matching_close.exchange_order_id,
            }
            self.trace_store.update(
                filled.trace_id,
                TraceStatus.CLOSED,
                position_after={"symbol": symbol, "quantity": 0.0, "source": "SIGNED_ACCOUNT_SNAPSHOT"},
                reconciliation={
                    "status": "MATCHED",
                    "actual_quantity": "0",
                    "unresolved": [],
                    "source": "SIGNED_ACCOUNT_SNAPSHOT",
                    "close_trace_id": matching_close.trace_id,
                    "close_order_id": matching_close.exchange_order_id,
                },
                metadata=recovery_metadata,
            )
            report["closed_trace_ids"].append(filled.trace_id)
        return report

    def _venue_eligible_candidates(self, exchange_raw: object) -> tuple[list[str], dict[str, Any]]:
        """Select symbols whose venue minimum fits the existing risk cap.

        Eligibility can only remove symbols; it never raises ``max_notional``
        or rounds an approved amount upward. Unknown or stale rules are
        rejected before market-data work and before any write-capable path.
        """

        raw_symbols = exchange_raw.get("symbols") if isinstance(exchange_raw, dict) else None
        discovery_limit = max(len(raw_symbols), 1) if isinstance(raw_symbols, list) else 1
        discovered = discover_startup_candidates(
            exchange_raw,
            max_instruments=discovery_limit,
            preserve_exchange_order=True,
        )
        allowlist = set(self.config.allowed_symbols)
        candidates = [symbol for symbol in discovered if not allowlist or symbol in allowlist]
        rejected: list[dict[str, str]] = []
        if allowlist:
            for symbol in sorted(allowlist - set(discovered)):
                rejected.append({"symbol": symbol, "reason": "ALLOWED_SYMBOL_NOT_TRADING"})

        cap = Decimal(str(self.config.max_notional))
        eligible: list[str] = []
        for symbol in candidates:
            rule = self.adapter.get_rule_snapshot(symbol)
            if not rule.is_known or rule.is_stale:
                rejected.append({"symbol": symbol, "reason": "EXCHANGE_RULE_UNKNOWN_OR_STALE"})
                continue
            try:
                venue_minimum = Decimal(rule.min_notional)
            except (InvalidOperation, TypeError, ValueError):
                rejected.append({"symbol": symbol, "reason": "EXCHANGE_MIN_NOTIONAL_UNKNOWN"})
                continue
            if not venue_minimum.is_finite() or venue_minimum <= 0:
                rejected.append({"symbol": symbol, "reason": "EXCHANGE_MIN_NOTIONAL_UNKNOWN"})
                continue
            if venue_minimum > cap:
                rejected.append(
                    {
                        "symbol": symbol,
                        "reason": "MIN_NOTIONAL_EXCEEDS_MAX_NOTIONAL",
                        "venue_min_notional": rule.min_notional,
                    }
                )
                continue
            eligible.append(symbol)
            if len(eligible) >= self.config.max_instruments:
                break

        if eligible:
            status = "ELIGIBLE"
        elif candidates:
            status = "NO_ELIGIBLE_SYMBOL_UNDER_CAP"
        elif discovered and allowlist:
            status = "NO_ALLOWED_SYMBOL_AVAILABLE"
        elif isinstance(raw_symbols, list):
            status = "NO_TRADING_CANDIDATES"
        else:
            status = "EXCHANGE_INFO_UNKNOWN"
        return eligible, {
            "status": status,
            "max_notional": str(self.config.max_notional),
            "allowed_symbols": list(self.config.allowed_symbols),
            "eligible_symbols": list(eligible),
            "rejected": rejected,
        }

    async def startup(self) -> dict[str, Any]:
        """Read minimum venue facts and construct a dynamic pool."""

        exchange_result = await self.adapter.fetch_exchange_info()
        server_result = await self.adapter.get_server_time()
        mode_result = await self.adapter.get_position_mode()
        account_result = await self.adapter.get_account_snapshot()
        recovery_report = self._reconcile_flat_filled_traces(account_result, mode_result)
        exchange_raw = exchange_result.data if exchange_result.is_success() else None
        if exchange_raw is not None:
            self.pool.record_source_hash(_stable_hash(exchange_raw))
        candidates, venue_eligibility = self._venue_eligible_candidates(exchange_raw)
        bounded_universe = {symbol.upper() for symbol in candidates}
        rejection_reasons = {
            str(item.get("symbol", "")).upper(): str(item.get("reason", ""))
            for item in venue_eligibility["rejected"]
            if isinstance(item, dict)
        }
        for symbol in self.pool.active_instruments():
            if symbol.upper() not in bounded_universe:
                self.pool.quarantine(
                    symbol,
                    rejection_reasons.get(symbol.upper(), "OUTSIDE_BOUNDED_STARTUP_UNIVERSE"),
                )
        for symbol in candidates:
            self.pool.add(symbol)

        for symbol in candidates:
            await self._observe_symbol(symbol)

        snapshot = self.pool.snapshot()
        self._pool_state.record_snapshot(snapshot)
        self._startup_facts = {
            "exchange_info": {
                "status": "SUCCESS" if exchange_result.is_success() else "UNKNOWN",
                "source_hash": _stable_hash(exchange_raw) if exchange_raw is not None else "",
                "candidate_count": len(candidates),
            },
            "venue_eligibility": venue_eligibility,
            "server_time": {
                "status": "SUCCESS" if server_result.is_success() else "UNKNOWN",
                "source_hash": _stable_hash(server_result.data) if server_result.is_success() else "",
            },
            "position_mode": {
                "status": "SUCCESS" if mode_result.is_success() else "UNKNOWN",
                "value": _jsonable(mode_result.data) if mode_result.is_success() else {},
            },
            "account": {
                "status": "SUCCESS" if account_result.is_success() else "UNKNOWN",
                "can_trade": bool(
                    account_result.is_success()
                    and isinstance(account_result.data, dict)
                    and account_result.data.get("canTrade") is True
                ),
                "can_withdraw": (
                    account_result.data.get("canWithdraw")
                    if account_result.is_success() and isinstance(account_result.data, dict)
                    else None
                ),
                "source_hash": _stable_hash(account_result.data) if account_result.is_success() else "",
            },
            "pool": _jsonable(snapshot),
            "recovery": recovery_report,
            "errors": list(self._startup_errors),
        }
        return copy.deepcopy(self._startup_facts)

    async def _observe_symbol(self, symbol: str) -> None:
        ticker_result = await self.adapter.get_ticker(symbol)
        depth_result = await self.adapter.get_depth(symbol, limit=20)
        bars_result = await self.adapter.get_closed_klines(symbol, self.config.interval, self.config.kline_limit)
        funding_result = await self.adapter.get_funding_rate(symbol, limit=1)
        if not all(result.is_success() for result in (ticker_result, depth_result, bars_result, funding_result)):
            self._startup_errors.append(
                {
                    "symbol": symbol,
                    "reason": "MARKET_FACT_UNKNOWN",
                    "ticker": _result_error(ticker_result, "ticker unavailable"),
                    "depth": _result_error(depth_result, "depth unavailable"),
                    "bars": _result_error(bars_result, "closed bars unavailable"),
                    "funding": _result_error(funding_result, "funding unavailable"),
                }
            )
            return
        ticker = cast(dict[str, Any], ticker_result.data)
        depth = cast(dict[str, Any], depth_result.data)
        bars = cast(list[dict[str, Any]], bars_result.data)
        funding = cast(list[dict[str, Any]], funding_result.data)
        try:
            features = self._features_from_observation(symbol, ticker, depth, bars, funding)
        except ValueError as exc:
            self._startup_errors.append({"symbol": symbol, "reason": str(exc)})
            return
        source_hashes = (
            _stable_hash(ticker),
            _stable_hash(depth),
            _stable_hash(bars),
            _stable_hash(funding),
        )
        self.pool.record_source_hashes(source_hashes)
        self._observations[symbol] = MarketObservation(
            symbol=symbol,
            ticker=ticker,
            depth=depth,
            bars=bars,
            funding=funding,
            features=features,
            source_hashes=source_hashes,
        )
        rule = self.adapter.get_rule_snapshot(symbol)
        stability_score = 1.0 if rule.is_known and not rule.is_stale else 0.0
        score = self.pool.update_market_score(
            symbol,
            spread_bps=features["spread_bps"],
            depth_score=min(1.0, features["depth_notional"] / max(self.config.max_notional * 10.0, 1.0)),
            volume_score=min(1.0, features["quote_volume"] / max(self.config.max_notional * 1000.0, 1.0)),
            stability_score=stability_score,
            capacity_score=features["capacity_score"],
            source_hashes=source_hashes,
        )
        interval_hours = self._interval_hours(self.config.interval)
        self.pool.seed_historical_observation(
            symbol,
            score.overall,
            evidence={
                "days": len(bars) * interval_hours / 24.0,
                "bar_count": len(bars),
                "source_hashes": list(source_hashes),
            },
        )
        if self.pool.try_promote(symbol):
            self.pool.activate(symbol)

    @staticmethod
    def _interval_hours(interval: str) -> float:
        if interval.endswith("m"):
            return float(int(interval[:-1])) / 60.0
        if interval.endswith("h"):
            return float(int(interval[:-1]))
        if interval.endswith("d"):
            return float(int(interval[:-1])) * 24.0
        raise ValueError(f"unsupported interval: {interval}")

    @staticmethod
    def _features_from_observation(
        symbol: str,
        ticker: dict[str, Any],
        depth: dict[str, Any],
        bars: list[dict[str, Any]],
        funding: list[dict[str, Any]],
    ) -> dict[str, float]:
        if any(
            not isinstance(row, dict)
            or row.get("is_closed") is not True
            or _finite_float(row.get("close_time")) is None
            for row in bars
        ):
            raise ValueError("CLOSED_BAR_FACT_UNKNOWN")
        close_times = [int(row["close_time"]) for row in bars]
        if any(current <= previous for previous, current in itertools.pairwise(close_times)):
            raise ValueError("CLOSED_BAR_ORDER_INVALID")
        closes = [_finite_float(row.get("close")) for row in bars]
        if len(closes) < 21 or any(value is None or value <= 0 for value in closes):
            raise ValueError("NOT_ENOUGH_CLOSED_BARS")
        close_values = cast(list[float], closes)
        returns = [close_values[index] / close_values[index - 1] - 1.0 for index in range(1, len(close_values))]
        volatility = math.sqrt(sum((value - sum(returns) / len(returns)) ** 2 for value in returns) / len(returns))
        fast_return = close_values[-1] / close_values[-6] - 1.0
        slow_return = close_values[-1] / close_values[-21] - 1.0
        signal_score = 0.7 * fast_return + 0.3 * slow_return
        signal_confidence = min(1.0, abs(signal_score) / max(volatility * math.sqrt(20.0), 1e-12))
        regime_confidence = max(0.25, min(1.0, 1.0 - volatility * 10.0))

        # BD-FIX (V4 B1): demo-fapi `/fapi/v1/ticker/24hr` responses omit
        # bidPrice/askPrice entirely, so the 24h ticker alone cannot evidence
        # a spread.  Top-of-book from the depth snapshot is the canonical
        # bid/ask source; ticker fields remain an optional fallback for
        # transports that carry them.
        bids = depth.get("bids")
        asks = depth.get("asks")
        if not isinstance(bids, list) or not isinstance(asks, list):
            raise ValueError("INVALID_DEPTH")
        bid = _top_of_book_price(bids, "bid")
        ask = _top_of_book_price(asks, "ask")
        if bid is None:
            bid = _finite_float(ticker.get("bidPrice"))
        if ask is None:
            ask = _finite_float(ticker.get("askPrice"))
        last_price = _finite_float(ticker.get("lastPrice"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError(f"INVALID_TICKER:{symbol}")
        price = last_price if last_price is not None and last_price > 0 else (bid + ask) / 2.0
        spread_bps = (ask - bid) / ask * 10000.0
        depth_notional = 0.0
        for row in [*bids[:5], *asks[:5]]:
            if not isinstance(row, list) or len(row) < 2:
                raise ValueError("INVALID_DEPTH_ROW")
            row_price = _finite_float(row[0])
            row_quantity = _finite_float(row[1])
            if row_price is None or row_quantity is None or row_price <= 0 or row_quantity < 0:
                raise ValueError("INVALID_DEPTH_VALUE")
            depth_notional += row_price * row_quantity
        quote_volume = _finite_float(ticker.get("quoteVolume"))
        if quote_volume is None or quote_volume < 0:
            base_volume = _finite_float(ticker.get("volume"))
            quote_volume = base_volume * price if base_volume is not None and base_volume >= 0 else None
        if quote_volume is None:
            raise ValueError("INVALID_24H_VOLUME")
        funding_rate = (
            _finite_float(funding[-1].get("fundingRate")) if funding and isinstance(funding[-1], dict) else None
        )
        if funding_rate is None:
            raise ValueError("INVALID_FUNDING_RATE")
        spread_quality = max(0.0, min(1.0, 1.0 - spread_bps / 100.0))
        depth_quality = max(0.0, min(1.0, depth_notional / max(price, 1.0) / 10.0))
        liquidity_score = max(0.0, min(1.0, 0.5 * spread_quality + 0.5 * depth_quality))
        capacity_score = max(0.0, min(1.0, depth_notional / max(price, 1.0) / 20.0))
        return {
            "close": price,
            "fast_return": fast_return,
            "slow_return": slow_return,
            "realized_volatility": max(volatility, 1e-12),
            "spread_bps": spread_bps,
            "depth_notional": depth_notional,
            "quote_volume": quote_volume,
            "liquidity_score": liquidity_score,
            "capacity_score": capacity_score,
            "funding_rate": funding_rate,
            "signal_score": signal_score,
            "signal_confidence": signal_confidence,
            "regime_confidence": regime_confidence,
            "stop_distance_pct": max(0.005, min(0.50, volatility * 2.0)),
        }

    def _episode_ids(self, symbol: str, snapshot: TradingPoolSnapshot) -> tuple[str, str, str]:
        observation = self._observations.get(symbol)
        bar_key = observation.bars[-1].get("close_time") if observation and observation.bars else "unknown"
        # The intent identity belongs to one closed-bar decision episode.  Do
        # not include the mutable pool version/hash here: scoring persists a
        # new lifecycle version on every observation, and including it would
        # turn a process restart into a fresh clientOrderId for the same bar.
        # The exact pool version/hash is still persisted in DecisionTrace and
        # remains part of the authorization/evidence record.
        del snapshot
        seed = _stable_hash({"symbol": symbol, "bar": bar_key, "episode_namespace": self._episode_identity_namespace})[
            :20
        ]
        return f"tn-{seed}", f"intent-{seed}", f"bd-{seed}"

    def _base_trace(
        self,
        *,
        trace_id: str,
        intent_id: str,
        client_order_id: str,
        snapshot: TradingPoolSnapshot,
        symbol: str,
        strategy: dict[str, Any] | None = None,
        portfolio: dict[str, Any] | None = None,
        sizing: dict[str, Any] | None = None,
        rule: InstrumentRuleSnapshot | None = None,
        factor_outputs: list[dict[str, Any]] | None = None,
    ) -> DecisionTrace:
        observation = self._observations.get(symbol)
        feature_hash = _stable_hash(observation.features) if observation else ""
        return DecisionTrace(
            trace_id=trace_id,
            intent_id=intent_id,
            client_order_id=client_order_id,
            pool={
                "id": snapshot.pool_id,
                "version": snapshot.version,
                "hash": snapshot.snapshot_hash,
                "symbol": symbol,
                "score": snapshot.score_by_symbol.get(symbol, 0.0),
                "active_symbols": list(snapshot.active_symbols),
                "source_hashes": list(snapshot.source_hashes),
            },
            market_data_hash=feature_hash,
            factor_outputs=factor_outputs or [],
            strategy=strategy or {},
            portfolio=portfolio or {},
            sizing=sizing or {},
            exchange_rules_hash=rule.compute_hash() if rule is not None else "",
            metadata={
                "runtime": self.config.entrypoint,
                "task_id": self.config.task_id,
                "config_hash": self.config.config_hash(),
                "episode_identity_namespace": self._episode_identity_namespace,
                **(
                    {"evidence_class": "EXECUTION_PROBE", "alpha_evidence": False}
                    if self.config.entrypoint == "apps.testnet_soak"
                    else {}
                ),
            },
        )

    def _record_no_action(
        self,
        trace: DecisionTrace,
        reason: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.trace_store.get(trace.trace_id)
        if existing is None:
            existing = self.trace_store.prepare(trace)
        if existing.status is TraceStatus.PREPARED:
            existing = self.trace_store.update(
                trace.trace_id,
                TraceStatus.FAILED,
                error={"reason": reason, **(details or {})},
                metadata={**existing.metadata, **trace.metadata, "decision": "NO_ACTION"},
            )
        return {"trace_id": existing.trace_id, "status": existing.status.value, "reason": reason}

    def _durable_episode_result(self, trace: DecisionTrace, reason: str) -> dict[str, Any]:
        """Return all durable trace identities belonging to one episode.

        A close order is a separate trace, but it is part of the same
        closed-bar episode.  Replaying a terminal primary trace must expose
        that already-persisted child as well, otherwise a restart would
        produce a different summary even though no venue write occurred.
        """

        result: dict[str, Any] = {
            "trace_id": trace.trace_id,
            "status": trace.status.value,
            "reason": reason,
        }
        close_trace = self.trace_store.get(f"{trace.trace_id}-c")
        if close_trace is not None:
            result["trace_ids"] = [trace.trace_id, close_trace.trace_id]
        return result

    async def _account_facts(self) -> tuple[float, float, dict[str, Any]] | None:
        result = await self.adapter.get_account_snapshot()
        if not result.is_success() or not isinstance(result.data, dict):
            return None
        account = result.data
        equity = _finite_float(account.get("totalWalletBalance"))
        available = _finite_float(account.get("availableBalance"))
        if equity is None or available is None:
            assets = account.get("assets")
            if isinstance(assets, list):
                usdt = [row for row in assets if isinstance(row, dict) and str(row.get("asset", "")).upper() == "USDT"]
                if len(usdt) == 1:
                    equity = equity if equity is not None else _finite_float(usdt[0].get("walletBalance"))
                    available = available if available is not None else _finite_float(usdt[0].get("availableBalance"))
        if equity is None or available is None or equity <= 0 or available < 0:
            return None
        # BD-FIX (V4 campaign): Binance demo-fapi accounts report
        # ``canWithdraw=true`` by default and it cannot be disabled, so a
        # ``canWithdraw is False`` requirement would block every bounded
        # Testnet campaign.  The V4 minimum safety boundary (02-plan §2) is
        # EnvironmentGuard + caps + idempotency + UNKNOWN recovery + kill
        # switch; withdrawal is additionally out of reach because the write
        # guard never authorizes withdrawal endpoints.  Require trading
        # capability only; ``canWithdraw`` stays an audited account fact in
        # the trace/manifest.
        if account.get("canTrade") is not True:
            return None
        return equity, available, account

    @staticmethod
    def _gross_account_exposure(account: dict[str, Any]) -> float | None:
        """Return fail-closed gross position exposure from the signed account snapshot."""

        rows = account.get("positions")
        if not isinstance(rows, list):
            return None
        total = 0.0
        for row in rows:
            if not isinstance(row, dict):
                return None
            amount = _finite_float(row.get("positionAmt"))
            if amount is None:
                return None
            if amount == 0:
                continue
            notional = _finite_float(row.get("notional"))
            if notional is None:
                mark_price = _finite_float(row.get("markPrice"))
                if mark_price is None or mark_price <= 0:
                    return None
                notional = amount * mark_price
            total += abs(notional)
        return total

    async def _position_facts(self, symbol: str) -> dict[str, Any] | None:
        result = await self.adapter.get_position_risk(symbol)
        if not result.is_success() or not isinstance(result.data, list):
            return None
        rows = [row for row in result.data if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol]
        if not rows:
            return None
        quantity = 0.0
        for row in rows:
            amount = _finite_float(row.get("positionAmt"))
            if amount is None:
                return None
            quantity += amount
        return {
            "symbol": symbol,
            "quantity": quantity,
            "entry_price": _finite_float(rows[0].get("entryPrice")),
            "unrealized_pnl": _finite_float(rows[0].get("unRealizedProfit")),
            "leverage": int(rows[0]["leverage"]) if str(rows[0].get("leverage", "")).isdigit() else None,
            "rows": _jsonable(rows),
        }

    @staticmethod
    def _is_ambiguous(result: Any) -> bool:
        category = getattr(getattr(result, "error", None), "category", "UNKNOWN")
        category_value = getattr(category, "value", category)
        return str(category_value).upper() in {"UNKNOWN", "NETWORK", "TIMEOUT", "EXCHANGE_UNAVAILABLE"}

    async def _query_recover_order(
        self,
        trace: DecisionTrace,
        request: OrderRequest,
        context: TerminalWriteContext,
        *,
        allow_same_id_resubmit: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        query = await self.adapter.query_order_by_client_id(
            str(request.venue_instrument.instrument_id),
            str(request.client_order_id or ""),
        )
        if query.is_success() and isinstance(query.data, dict):
            raw = cast(dict[str, Any], query.data)
            valid, reason = self.adapter.validate_order_ack(request, raw)
            if not valid:
                return None, {"reason": f"RECOVERY_ACK_{reason}"}
            return raw, None
        error = _result_error(query, "order query is UNKNOWN")
        code = error.get("code")
        if allow_same_id_resubmit and str(code) in {"-2013", "-2011"}:
            # Deterministic absence is the only condition permitting a retry,
            # and the exact same client id is reused.  A transport failure
            # never reaches this branch.
            self.trace_store.update(
                trace.trace_id,
                TraceStatus.SUBMITTED,
                metadata={**trace.metadata, "recovery": "QUERY_CONFIRMED_ABSENT_SAME_ID_RETRY"},
            )
            response = await self._create_order(request, context)
            if response.status is OrderStatus.REJECTED and not response.order_id:
                self.trace_store.update(
                    trace.trace_id,
                    TraceStatus.FAILED,
                    error={"reason": "RECOVERY_SECOND_ATTEMPT_REJECTED", "response": _jsonable(response.raw_response)},
                )
                return None, {"reason": "RECOVERY_SECOND_ATTEMPT_REJECTED", "query": error}
            if response.status is not OrderStatus.UNKNOWN and response.order_id:
                raw = response.raw_response if isinstance(response.raw_response, dict) else {}
                valid, reason = self.adapter.validate_order_ack(request, raw)
                if valid:
                    return raw, None
                self.trace_store.update(
                    trace.trace_id,
                    TraceStatus.UNKNOWN,
                    error={"reason": f"RECOVERY_ACK_{reason}"},
                )
                return None, {"reason": f"RECOVERY_ACK_{reason}"}
            self.trace_store.update(
                trace.trace_id,
                TraceStatus.UNKNOWN,
                error={"reason": "RECOVERY_SECOND_ATTEMPT_UNKNOWN", "response": _jsonable(response.raw_response)},
            )
            return None, {"reason": "RECOVERY_SECOND_ATTEMPT_UNKNOWN", "query": error}
        return None, {"reason": "RECOVERY_QUERY_UNKNOWN", "query": error}

    async def _submit_order(
        self,
        trace: DecisionTrace,
        request: OrderRequest,
        context: TerminalWriteContext,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        response = await self._create_order(request, context)
        if response.status is OrderStatus.REJECTED and not response.order_id:
            self.trace_store.update(
                trace.trace_id,
                TraceStatus.FAILED,
                order_status=response.status.value,
                error={"reason": "ORDER_SUBMIT_REJECTED", "response": _jsonable(response.raw_response)},
            )
            return None, {"reason": "ORDER_REJECTED"}
        if response.status is OrderStatus.UNKNOWN or not response.order_id:
            self.trace_store.update(
                trace.trace_id,
                TraceStatus.UNKNOWN,
                order_status=response.status.value,
                error={"reason": "ORDER_SUBMIT_UNKNOWN", "response": _jsonable(response.raw_response)},
            )
            return await self._query_recover_order(
                trace,
                request,
                context,
                allow_same_id_resubmit=True,
            )
        raw = response.raw_response if isinstance(response.raw_response, dict) else {}
        valid, reason = self.adapter.validate_order_ack(request, raw)
        if not valid:
            self.trace_store.update(
                trace.trace_id,
                TraceStatus.UNKNOWN,
                error={"reason": f"ACK_{reason}"},
            )
            return None, {"reason": f"ACK_{reason}"}
        return raw, None

    async def _settle_order_lifecycle(
        self,
        trace: DecisionTrace,
        request: OrderRequest,
        context: TerminalWriteContext,
        raw_ack: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Poll an ACK to terminal state and cancel any unfilled remainder."""

        terminal = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        current = dict(raw_ack)
        events: list[dict[str, Any]] = []
        for attempt in range(self.config.order_poll_attempts):
            status = str(current.get("status", "UNKNOWN")).upper()
            events.append(
                {
                    "attempt": attempt,
                    "status": status,
                    "executed_qty": str(current.get("executedQty", "0")),
                }
            )
            if status in terminal:
                self.trace_store.update(
                    trace.trace_id,
                    self.trace_store.get(trace.trace_id).status,  # type: ignore[union-attr]
                    metadata={**trace.metadata, "order_lifecycle": {"events": events, "remainder_action": "NONE"}},
                )
                return current, None
            if self.config.order_poll_interval_seconds:
                await asyncio.sleep(self.config.order_poll_interval_seconds)
            query = await self.adapter.query_order_by_client_id(
                str(request.venue_instrument.instrument_id),
                str(request.client_order_id or ""),
            )
            if not query.is_success() or not isinstance(query.data, dict):
                self.trace_store.update(
                    trace.trace_id,
                    TraceStatus.UNKNOWN,
                    metadata={**trace.metadata, "order_lifecycle": {"events": events, "remainder_action": "UNKNOWN"}},
                    error={"reason": "ORDER_POLL_UNKNOWN", "query": _result_error(query, "order poll unknown")},
                )
                return None, {"reason": "ORDER_POLL_UNKNOWN"}
            valid, reason = self.adapter.validate_order_ack(request, query.data)
            if not valid:
                self.trace_store.update(
                    trace.trace_id,
                    TraceStatus.UNKNOWN,
                    error={"reason": f"ORDER_POLL_ACK_{reason}"},
                )
                return None, {"reason": f"ORDER_POLL_ACK_{reason}"}
            current = dict(query.data)

        status = str(current.get("status", "UNKNOWN")).upper()
        if status in terminal:
            return current, None
        order_id = str(current.get("orderId", ""))
        if not order_id:
            self.trace_store.update(trace.trace_id, TraceStatus.UNKNOWN, error={"reason": "ORDER_CANCEL_ID_UNKNOWN"})
            return None, {"reason": "ORDER_CANCEL_ID_UNKNOWN"}
        cancel_params = {
            "symbol": str(request.venue_instrument.instrument_id),
            "orderId": int(order_id),
        }
        cancel_context = self.guard.build_write_context(
            intent_id=context.intent_id,
            trace_id=trace.trace_id,
            symbol=str(request.venue_instrument.instrument_id),
            quantity=str(request.quantity.amount),
            notional=context.notional,
            leverage=context.leverage,
            position_id=context.position_id,
            pool_id=context.pool_id,
            pool_version=context.pool_version,
            pool_hash=context.pool_hash,
            pool_symbols=context.pool_symbols,
            command_hash=_stable_hash(cancel_params),
            method="DELETE",
            path=Endpoint.ORDER,
            request_params=cancel_params,
            order_id=order_id,
        )
        canceled = await self.adapter.cancel_order(
            order_id,
            request.venue_instrument,
            write_context=cancel_context,
        )
        canceled_raw = canceled.raw_response if isinstance(canceled.raw_response, dict) else {}
        canceled_status = str(canceled_raw.get("status", canceled.status.value)).upper()
        if canceled.status is OrderStatus.UNKNOWN or canceled_status not in terminal:
            self.trace_store.update(
                trace.trace_id,
                TraceStatus.UNKNOWN,
                metadata={**trace.metadata, "order_lifecycle": {"events": events, "remainder_action": "UNKNOWN"}},
                error={"reason": "ORDER_CANCEL_UNKNOWN", "response": _jsonable(canceled_raw)},
            )
            return None, {"reason": "ORDER_CANCEL_UNKNOWN"}
        events.append(
            {
                "attempt": self.config.order_poll_attempts,
                "status": canceled_status,
                "executed_qty": str(canceled_raw.get("executedQty", "0")),
            }
        )
        current_trace = self.trace_store.get(trace.trace_id)
        assert current_trace is not None
        self.trace_store.update(
            trace.trace_id,
            current_trace.status,
            metadata={**current_trace.metadata, "order_lifecycle": {"events": events, "remainder_action": "CANCELED"}},
        )
        return dict(canceled_raw), None

    async def _execution_attribution(
        self,
        symbol: str,
        order_ids: list[str],
        *,
        start_time_ms: int,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        """Build fee/funding/PnL only from signed venue account facts."""

        trade_rows: list[dict[str, Any]] = []
        for order_id in order_ids:
            trades = await self.adapter.get_account_trades(symbol, order_id)
            if not trades.is_success() or not isinstance(trades.data, list):
                return None
            trade_rows.extend(dict(row) for row in trades.data if isinstance(row, dict))
        income = await self.adapter.get_income_history(
            symbol,
            income_type="FUNDING_FEE",
            start_time=start_time_ms,
        )
        if not income.is_success() or not isinstance(income.data, list):
            return None

        def decimal_sum(rows: list[dict[str, Any]], field: str) -> Decimal | None:
            total = Decimal("0")
            try:
                for row in rows:
                    value = Decimal(str(row[field]))
                    if not value.is_finite():
                        return None
                    total += value
            except (InvalidOperation, KeyError, TypeError, ValueError):
                return None
            return total

        commission = decimal_sum(trade_rows, "commission")
        realized = decimal_sum(trade_rows, "realizedPnl")
        income_rows = [dict(row) for row in income.data if isinstance(row, dict)]
        funding = decimal_sum(income_rows, "income")
        if commission is None or realized is None or funding is None:
            return None
        return (
            {"status": "VENUE_TRADES", "commission": str(commission), "rows": _jsonable(trade_rows)},
            {"status": "VENUE_INCOME", "amount": str(funding), "rows": _jsonable(income_rows)},
            {
                "status": "VENUE_TRADES",
                "realized": str(realized),
                "net_after_fee_funding": str(realized - commission + funding),
            },
        )

    async def _run_symbol(self, symbol: str, snapshot: TradingPoolSnapshot) -> dict[str, Any]:
        observation = self._observations.get(symbol)
        trace_id, intent_id, client_order_id = self._episode_ids(symbol, snapshot)
        if observation is None:
            trace = self._base_trace(
                trace_id=trace_id,
                intent_id=intent_id,
                client_order_id=client_order_id,
                snapshot=snapshot,
                symbol=symbol,
            )
            return self._record_no_action(trace, "MARKET_OBSERVATION_UNKNOWN")

        component_manifest = self.kernel.active_component_manifest()
        component_ids = [item["component_id"] for item in component_manifest]
        context = {
            "instrument_id": InstrumentId(symbol),
            "venue_id": VenueId("BINANCE"),
            "features": observation.features,
            "pool_status": PoolStatus.ACTIVE.value,
            "pool_hash": snapshot.snapshot_hash,
            "factor_hash": _stable_hash(component_manifest),
            "active_components": component_ids,
        }
        evaluation_started = time.perf_counter()
        evaluation = await self.kernel.evaluate(context)
        evaluation_latency_ms = max(0.0, (time.perf_counter() - evaluation_started) * 1000.0)
        proposal = evaluation.get("proposal") if isinstance(evaluation, dict) else None
        proposal_hash = self.kernel.proposal_hash(proposal)
        raw_component_outputs = evaluation.get("component_outputs", {}) if isinstance(evaluation, dict) else {}
        raw_component_outputs = raw_component_outputs if isinstance(raw_component_outputs, dict) else {}
        factor_outputs: list[dict[str, Any]] = []
        for component in component_manifest:
            component_id = component["component_id"]
            output = raw_component_outputs.get(component_id)
            decision = "UNKNOWN"
            confidence: float | None = None
            if isinstance(output, FilterResult):
                decision = output.decision.value
                confidence = float(output.confidence_multiplier)
            elif output is not None:
                output_side = getattr(output, "side", None)
                decision = "ACTION" if output_side is not None else "OUTPUT"
                raw_confidence = _finite_float(getattr(output, "confidence", None))
                confidence = raw_confidence
            elif proposal is None:
                decision = "VETO_OR_NOT_REACHED" if evaluation else "UNKNOWN"
            factor_outputs.append(
                {
                    **component,
                    "input_hash": _stable_hash(observation.features),
                    "output": _jsonable(output),
                    "output_hash": _stable_hash({component_id: _jsonable(output)}),
                    "confidence": confidence,
                    "decision": decision,
                    "latency_ms": evaluation_latency_ms,
                    "latency_scope": "kernel_total",
                }
            )
        # A live Testnet cycle has no authority to manufacture Backtest/Paper
        # evidence by comparing the Testnet proposal with itself.  Keep the
        # parity gate explicitly NOT_RUN until frozen cross-mode inputs are
        # supplied by an independent validation run.
        parity = StrategyKernelContract.verify_parity(None, None, proposal)
        strategy_data = {
            "kernel": evaluation.get("kernel", "") if isinstance(evaluation, dict) else "",
            "mode": evaluation.get("mode", "") if isinstance(evaluation, dict) else "",
            "graph_hash": evaluation.get("graph_hash", "") if isinstance(evaluation, dict) else "",
            "context_hash": evaluation.get("context_hash", "") if isinstance(evaluation, dict) else "",
            "proposal_hash": proposal_hash,
            "proposal": _jsonable(proposal),
            "component_outputs": _jsonable(raw_component_outputs),
            "active_components": component_ids,
            "active_component_count": len(component_ids),
            "component_registry_hash": _stable_hash(component_manifest),
            "parity": {
                "status": parity.status.value,
                "backtest_hash": parity.backtest_hash,
                "paper_hash": parity.paper_hash,
                "testnet_hash": parity.testnet_hash,
                "input_hash": _stable_hash(context),
                "discrepancies": list(parity.discrepancies),
            },
        }
        if proposal is None or getattr(proposal, "side", None) is None:
            trace = self._base_trace(
                trace_id=trace_id,
                intent_id=intent_id,
                client_order_id=client_order_id,
                snapshot=snapshot,
                symbol=symbol,
                strategy={**strategy_data, "decision": "NO_ACTION"},
                factor_outputs=factor_outputs,
            )
            return self._record_no_action(trace, "STRATEGY_NO_ACTION")
        if not self.pool.is_tradable(symbol):
            trace = self._base_trace(
                trace_id=trace_id,
                intent_id=intent_id,
                client_order_id=client_order_id,
                snapshot=snapshot,
                symbol=symbol,
                strategy=strategy_data,
                factor_outputs=factor_outputs,
            )
            return self._record_no_action(trace, "POOL_SYMBOL_NOT_TRADABLE")

        rule = self.adapter.get_rule_snapshot(symbol)
        if not rule.is_known or rule.is_stale:
            trace = self._base_trace(
                trace_id=trace_id,
                intent_id=intent_id,
                client_order_id=client_order_id,
                snapshot=snapshot,
                symbol=symbol,
                strategy=strategy_data,
                rule=rule,
                factor_outputs=factor_outputs,
            )
            return self._record_no_action(trace, "EXCHANGE_RULE_UNKNOWN")
        account_facts = await self._account_facts()
        if account_facts is None:
            trace = self._base_trace(
                trace_id=trace_id,
                intent_id=intent_id,
                client_order_id=client_order_id,
                snapshot=snapshot,
                symbol=symbol,
                strategy=strategy_data,
                rule=rule,
                factor_outputs=factor_outputs,
            )
            return self._record_no_action(trace, "ACCOUNT_FACTS_UNKNOWN")
        equity, available_margin, account = account_facts
        account_exposure = self._gross_account_exposure(account)
        if account_exposure is None:
            trace = self._base_trace(
                trace_id=trace_id,
                intent_id=intent_id,
                client_order_id=client_order_id,
                snapshot=snapshot,
                symbol=symbol,
                strategy=strategy_data,
                rule=rule,
                factor_outputs=factor_outputs,
            )
            return self._record_no_action(trace, "ACCOUNT_EXPOSURE_UNKNOWN")
        entry = self.pool.get_entry(symbol)
        capacity_utilization = min(1.0, max(0.0, 1.0 - entry.capacity_used_pct / 100.0)) if entry else 1.0
        sizing_input = SizingInput(
            equity=equity,
            available_margin=available_margin,
            symbol_price=observation.features["close"],
            step_size=rule.step_size,
            min_qty=rule.min_qty,
            min_notional=rule.min_notional,
            max_notional=self.config.max_notional,
            max_leverage=self.config.max_leverage,
            volatility=observation.features["realized_volatility"],
            capacity_utilization=capacity_utilization,
            liquidity_score=observation.features["liquidity_score"],
            regime_confidence=observation.features["regime_confidence"],
            signal_confidence=float(getattr(proposal, "confidence", observation.features["signal_confidence"])),
            funding_rate=observation.features["funding_rate"],
            drawdown_pct=0.0,
            liquidation_distance_pct=0.50,
            stop_distance_pct=observation.features["stop_distance_pct"],
        )
        sizing = compute_adaptive_sizing(sizing_input)
        sizing_data = {
            "input": _jsonable(asdict(sizing_input)),
            "input_hash": sizing.input_hash,
            "output_hash": sizing.output_hash,
            "leverage": sizing.leverage,
            "raw_quantity": sizing.raw_quantity,
            "final_quantity": sizing.final_quantity,
            "notional": sizing.notional,
            "reason_vector": list(sizing.reason_vector),
        }
        if not sizing.is_safe or not sizing.final_quantity or sizing.final_quantity == "0" or sizing.leverage < 1:
            trace = self._base_trace(
                trace_id=trace_id,
                intent_id=intent_id,
                client_order_id=client_order_id,
                snapshot=snapshot,
                symbol=symbol,
                strategy=strategy_data,
                portfolio={"target_weight": float(proposal.strength) * float(proposal.confidence)},
                sizing=sizing_data,
                rule=rule,
                factor_outputs=factor_outputs,
            )
            return self._record_no_action(trace, "SIZING_BLOCKED")

        side = cast(OrderSide, proposal.side)
        order_request = OrderRequest(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId(symbol)),
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId(self.config.account_id)),
            side=side,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount=sizing.final_quantity),
            client_order_id=client_order_id,
            correlation_id=CorrelationId(trace_id),
        )
        portfolio_data = {
            "target_side": side.value,
            "target_weight": float(proposal.strength) * float(proposal.confidence),
            "target_quantity": sizing.final_quantity,
            "target_hash": _stable_hash({"side": side.value, "quantity": sizing.final_quantity}),
        }
        trace = self._base_trace(
            trace_id=trace_id,
            intent_id=intent_id,
            client_order_id=client_order_id,
            snapshot=snapshot,
            symbol=symbol,
            strategy=strategy_data,
            portfolio=portfolio_data,
            sizing=sizing_data,
            rule=rule,
            factor_outputs=factor_outputs,
        )
        trace.order_request = {
            "symbol": symbol,
            "side": side.value,
            "type": OrderType.MARKET.value,
            "quantity": sizing.final_quantity,
            "clientOrderId": client_order_id,
            "reduceOnly": False,
        }
        trace.order_request_hash = _stable_hash(trace.order_request)
        trace.leverage_request = int(sizing.leverage)
        stored_trace = self.trace_store.prepare(trace)
        if stored_trace.status in {TraceStatus.CLOSED, TraceStatus.FAILED}:
            # A repeated cycle for the same closed bar must be idempotent.  A
            # terminal durable fact is never turned back into a new order just
            # because the process restarted or the pool was rescored.
            return self._durable_episode_result(stored_trace, "DURABLE_TRACE_TERMINAL")
        if stored_trace.status in {TraceStatus.ACKED, TraceStatus.FILLED}:
            # The exchange order already has a durable identity.  Reusing the
            # stored acknowledgement is safer than submitting another order;
            # a later, explicitly authorized close cycle can handle any open
            # position separately.
            return self._durable_episode_result(stored_trace, "DURABLE_TRACE_ALREADY_ACKED")
        resume_existing = stored_trace.status in {TraceStatus.UNKNOWN, TraceStatus.SUBMITTED}

        if not self.config.confirm_testnet:
            return self._record_no_action(trace, "CONFIRM_TESTNET_REQUIRED")
        if not self.config.api_key or not self.config.api_secret:
            return self._record_no_action(trace, "TESTNET_CREDENTIALS_UNAVAILABLE")
        if self.config.kill_switch_path.exists():
            return self._record_no_action(trace, "TESTNET_KILL_SWITCH_ACTIVE")
        position_before = await self._position_facts(symbol)
        if position_before is None:
            return self._record_no_action(trace, "POSITION_BEFORE_UNKNOWN")

        leverage_params = {"symbol": symbol, "leverage": int(sizing.leverage)}
        leverage_context = self.guard.build_write_context(
            intent_id=intent_id,
            trace_id=trace_id,
            symbol=symbol,
            side=side.value,
            order_type="LEVERAGE",
            quantity=sizing.final_quantity,
            notional=sizing.notional,
            leverage=str(int(sizing.leverage)),
            position_id=f"position-{symbol}",
            command_hash=_stable_hash(leverage_params),
            pool_id=snapshot.pool_id,
            pool_version=str(snapshot.version),
            pool_hash=snapshot.snapshot_hash,
            pool_symbols=snapshot.active_symbols,
        )
        leverage_result = await self.adapter.set_leverage(
            symbol,
            int(sizing.leverage),
            account_ref=order_request.account_ref,
            write_context=leverage_context,
        )
        if not leverage_result.is_success():
            status = TraceStatus.UNKNOWN if self._is_ambiguous(leverage_result) else TraceStatus.FAILED
            self.trace_store.update(
                trace_id,
                status,
                error={"reason": "LEVERAGE_SET_FAILED", **_result_error(leverage_result, "leverage set failed")},
            )
            return {"trace_id": trace_id, "status": status.value, "reason": "LEVERAGE_SET_FAILED"}
        leverage_readback = await self.adapter.read_leverage(symbol)
        if not leverage_readback.is_success() or not isinstance(leverage_readback.data, dict):
            self.trace_store.update(trace_id, TraceStatus.UNKNOWN, error={"reason": "LEVERAGE_READBACK_UNKNOWN"})
            return {"trace_id": trace_id, "status": TraceStatus.UNKNOWN.value, "reason": "LEVERAGE_READBACK_UNKNOWN"}
        actual_leverage = int(leverage_readback.data.get("leverage", 0))
        if actual_leverage != int(sizing.leverage):
            self.trace_store.update(
                trace_id,
                TraceStatus.FAILED,
                leverage_readback=actual_leverage,
                error={
                    "reason": "LEVERAGE_READBACK_MISMATCH",
                    "requested": int(sizing.leverage),
                    "actual": actual_leverage,
                },
            )
            return {"trace_id": trace_id, "status": TraceStatus.FAILED.value, "reason": "LEVERAGE_READBACK_MISMATCH"}
        self.trace_store.update(
            trace_id,
            stored_trace.status if resume_existing else TraceStatus.PREPARED,
            leverage_request=int(sizing.leverage),
            leverage_readback=actual_leverage,
        )

        order_context = self.guard.build_write_context(
            intent_id=intent_id,
            trace_id=trace_id,
            symbol=symbol,
            side=side.value,
            order_type=OrderType.MARKET.value,
            quantity=sizing.final_quantity,
            notional=sizing.notional,
            leverage=str(int(sizing.leverage)),
            position_id=f"position-{symbol}",
            client_order_id=client_order_id,
            account_exposure=str(account_exposure),
            projected_account_exposure=str(account_exposure + float(sizing.notional)),
            command_hash=trace.order_request_hash,
            pool_id=snapshot.pool_id,
            pool_version=str(snapshot.version),
            pool_hash=snapshot.snapshot_hash,
            pool_symbols=snapshot.active_symbols,
        )
        if resume_existing:
            # A prior POST may have reached the venue even when its response
            # was lost.  Query the exact durable client id first; only a
            # deterministic venue absence permits the helper to resubmit the
            # same id.  Transport UNKNOWN never reaches create_order.
            raw_ack, failure = await self._query_recover_order(
                trace,
                order_request,
                order_context,
                allow_same_id_resubmit=True,
            )
        else:
            self.trace_store.update(
                trace_id, TraceStatus.SUBMITTED, metadata={**trace.metadata, "write": "ORDER_POST_PENDING"}
            )
            raw_ack, failure = await self._submit_order(trace, order_request, order_context)
        if raw_ack is None:
            current = self.trace_store.get(trace_id)
            current_status = current.status.value if current is not None else TraceStatus.UNKNOWN.value
            return {
                "trace_id": trace_id,
                "status": current_status,
                "reason": (failure or {}).get("reason", "ORDER_UNKNOWN"),
            }
        raw_ack, lifecycle_failure = await self._settle_order_lifecycle(
            trace,
            order_request,
            order_context,
            raw_ack,
        )
        if raw_ack is None:
            return {
                "trace_id": trace_id,
                "status": TraceStatus.UNKNOWN.value,
                "reason": (lifecycle_failure or {}).get("reason", "ORDER_LIFECYCLE_UNKNOWN"),
            }
        exchange_order_id = str(raw_ack.get("orderId", ""))
        order_status = str(raw_ack.get("status", "UNKNOWN"))
        executed_qty = str(raw_ack.get("executedQty", "0"))
        self.trace_store.update(
            trace_id,
            TraceStatus.ACKED,
            exchange_order_id=exchange_order_id,
            order_ack=_jsonable(raw_ack),
            order_status=order_status,
            executed_qty=executed_qty,
        )
        if order_status == OrderStatus.FILLED.value:
            self.trace_store.update(trace_id, TraceStatus.FILLED, exchange_order_id=exchange_order_id)
        position_after = await self._position_facts(symbol)
        if position_after is None:
            self.trace_store.update(trace_id, TraceStatus.UNKNOWN, error={"reason": "POSITION_AFTER_UNKNOWN"})
            return {"trace_id": trace_id, "status": TraceStatus.UNKNOWN.value, "reason": "POSITION_AFTER_UNKNOWN"}
        before_quantity = float(position_before["quantity"])
        executed_value = _finite_float(executed_qty)
        executed_value = executed_value if executed_value is not None else 0.0
        expected_quantity = before_quantity + (executed_value if side is OrderSide.BUY else -executed_value)
        actual_quantity = float(position_after["quantity"])
        tolerance = max(float(rule.step_size) / 2.0, 1e-12)
        matched = abs(actual_quantity - expected_quantity) <= tolerance
        reconciliation = {
            "status": "MATCHED" if matched else "MISMATCHED",
            "expected_quantity": str(expected_quantity),
            "actual_quantity": str(actual_quantity),
            "tolerance": str(tolerance),
            "unresolved": [] if matched else ["POSITION_QUANTITY"],
        }
        average_price = _finite_float(raw_ack.get("avgPrice"))
        reference_price = observation.features["close"]
        slippage = {
            "status": "KNOWN" if average_price is not None and average_price > 0 else "UNKNOWN",
            "bps": ((average_price - reference_price) / reference_price * 10000.0) if average_price else None,
        }
        funding_rate = observation.features["funding_rate"]
        if not matched:
            self.trace_store.update(
                trace_id, TraceStatus.UNKNOWN, position_after=position_after, reconciliation=reconciliation
            )
            return {
                "trace_id": trace_id,
                "status": TraceStatus.UNKNOWN.value,
                "reason": "POSITION_RECONCILIATION_MISMATCH",
            }
        if executed_value <= 0:
            self.trace_store.update(
                trace_id,
                TraceStatus.FAILED,
                position_after=position_after,
                reconciliation=reconciliation,
                error={"reason": "ORDER_TERMINAL_WITHOUT_FILL", "order_status": order_status},
            )
            return {"trace_id": trace_id, "status": TraceStatus.FAILED.value, "reason": "ORDER_NOT_FILLED"}
        final_status = TraceStatus.FILLED
        current_trace = self.trace_store.get(trace_id)
        self.trace_store.update(
            trace_id,
            final_status,
            position_after=position_after,
            reconciliation=reconciliation,
            slippage=slippage,
            metadata={
                **(current_trace.metadata if current_trace is not None else trace.metadata),
                "public_funding_rate_at_decision": funding_rate,
            },
        )
        result: dict[str, Any] = {
            "trace_id": trace_id,
            "status": final_status.value,
            "order_id": exchange_order_id,
            "executed_qty": executed_qty,
        }
        if self.config.close_after_verify and actual_quantity != 0:
            close_result = await self._close_position(trace, snapshot, rule, position_after, int(sizing.leverage))
            result["close"] = close_result
            if close_result.get("trace_id"):
                result["trace_ids"] = [trace_id, str(close_result["trace_id"])]
        order_ids = [exchange_order_id]
        if isinstance(result.get("close"), dict) and result["close"].get("order_id"):
            order_ids.append(str(result["close"]["order_id"]))
        attribution = await self._execution_attribution(
            symbol,
            order_ids,
            start_time_ms=int(trace.timestamp.timestamp() * 1000),
        )
        if attribution is None:
            self.trace_store.update(trace_id, TraceStatus.UNKNOWN, error={"reason": "EXECUTION_ATTRIBUTION_UNKNOWN"})
            result["status"] = TraceStatus.UNKNOWN.value
            result["reason"] = "EXECUTION_ATTRIBUTION_UNKNOWN"
            return result
        fees, funding, pnl = attribution
        current = self.trace_store.get(trace_id)
        assert current is not None
        episode_closed = (
            self.config.close_after_verify
            and isinstance(result.get("close"), dict)
            and result["close"].get("status") == TraceStatus.CLOSED.value
        )
        attributed_status = TraceStatus.CLOSED if episode_closed else current.status
        self.trace_store.update(
            trace_id,
            attributed_status,
            fees=fees,
            funding=funding,
            pnl=pnl,
            metadata={**current.metadata, "attributed_order_ids": order_ids},
        )
        result["status"] = attributed_status.value
        return result

    async def _close_position(
        self,
        base_trace: DecisionTrace,
        snapshot: TradingPoolSnapshot,
        rule: InstrumentRuleSnapshot,
        position: dict[str, Any],
        leverage: int,
    ) -> dict[str, Any]:
        symbol = str(position["symbol"])
        amount = float(position["quantity"])
        close_side = OrderSide.SELL if amount > 0 else OrderSide.BUY
        try:
            quantity = rule.quantize_quantity(str(abs(amount)))
        except ValueError:
            return {"status": TraceStatus.FAILED.value, "reason": "CLOSE_QUANTITY_INVALID"}
        # BD-FIX (V4 campaign): quantize_quantity may render trailing zeros
        # ("0.100") while the adapter serializes the same Decimal as "0.1";
        # the guard's identity check compares the exact quantity strings and
        # the final-request hash, so both sides must use one normalized form.
        try:
            quantity = str(Decimal(quantity).normalize())
        except (InvalidOperation, TypeError, ValueError):
            return {"status": TraceStatus.FAILED.value, "reason": "CLOSE_QUANTITY_INVALID"}
        close_key = _stable_hash({"source": base_trace.trace_id, "quantity": quantity})[:18]
        trace_id = f"{base_trace.trace_id}-c"
        intent_id = f"{base_trace.intent_id}-close"
        client_order_id = f"{base_trace.client_order_id}-c"
        notional = str(float(quantity) * float(self._observations[symbol].features["close"]))
        trace = DecisionTrace(
            trace_id=trace_id,
            intent_id=intent_id,
            client_order_id=client_order_id,
            pool=copy.deepcopy(base_trace.pool),
            market_data_hash=base_trace.market_data_hash,
            factor_outputs=copy.deepcopy(base_trace.factor_outputs),
            strategy={"source_trace_id": base_trace.trace_id, "decision": "REDUCE_ONLY_CLOSE"},
            portfolio={"source_trace_id": base_trace.trace_id, "target_quantity": quantity},
            sizing={"source_trace_id": base_trace.trace_id, "final_quantity": quantity, "leverage": leverage},
            exchange_rules_hash=rule.compute_hash(),
            leverage_request=leverage,
            leverage_readback=leverage,
            metadata={**base_trace.metadata, "close_key": close_key},
        )
        order_request = OrderRequest(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId(symbol)),
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId(self.config.account_id)),
            side=close_side,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount=quantity),
            client_order_id=client_order_id,
            correlation_id=CorrelationId(trace_id),
            reduce_only=True,
        )
        trace.order_request = {
            "symbol": symbol,
            "side": close_side.value,
            "type": OrderType.MARKET.value,
            "quantity": quantity,
            "clientOrderId": client_order_id,
            "reduceOnly": True,
        }
        trace.order_request_hash = _stable_hash(trace.order_request)
        self.trace_store.prepare(trace)
        context = self.guard.build_write_context(
            intent_id=intent_id,
            trace_id=trace_id,
            symbol=symbol,
            side=close_side.value,
            order_type=OrderType.MARKET.value,
            quantity=quantity,
            notional=notional,
            leverage=str(leverage),
            position_id=base_trace.intent_id,
            reduce_only=True,
            client_order_id=client_order_id,
            command_hash=trace.order_request_hash,
            pool_id=snapshot.pool_id,
            pool_version=str(snapshot.version),
            pool_hash=snapshot.snapshot_hash,
            pool_symbols=snapshot.active_symbols,
        )
        self.trace_store.update(
            trace_id, TraceStatus.SUBMITTED, metadata={**trace.metadata, "write": "CLOSE_POST_PENDING"}
        )
        raw_ack, failure = await self._submit_order(trace, order_request, context)
        if raw_ack is None:
            return {
                "trace_id": trace_id,
                "status": TraceStatus.UNKNOWN.value,
                "reason": (failure or {}).get("reason", "CLOSE_UNKNOWN"),
            }
        raw_ack, lifecycle_failure = await self._settle_order_lifecycle(trace, order_request, context, raw_ack)
        if raw_ack is None:
            return {
                "trace_id": trace_id,
                "status": TraceStatus.UNKNOWN.value,
                "reason": (lifecycle_failure or {}).get("reason", "CLOSE_LIFECYCLE_UNKNOWN"),
            }
        order_status = str(raw_ack.get("status", "UNKNOWN"))
        self.trace_store.update(
            trace_id,
            TraceStatus.ACKED,
            exchange_order_id=str(raw_ack.get("orderId", "")),
            order_ack=_jsonable(raw_ack),
            order_status=order_status,
            executed_qty=str(raw_ack.get("executedQty", "0")),
        )
        if order_status != OrderStatus.FILLED.value:
            return {"trace_id": trace_id, "status": TraceStatus.ACKED.value}
        after = await self._position_facts(symbol)
        if after is None:
            self.trace_store.update(trace_id, TraceStatus.UNKNOWN, error={"reason": "CLOSE_POSITION_UNKNOWN"})
            return {"trace_id": trace_id, "status": TraceStatus.UNKNOWN.value}
        zero = abs(float(after["quantity"])) <= max(float(rule.step_size) / 2.0, 1e-12)
        reconciliation = {
            "status": "MATCHED" if zero else "MISMATCHED",
            "actual_quantity": str(after["quantity"]),
            "unresolved": [] if zero else ["CLOSE_REMAINDER"],
        }
        final_status = TraceStatus.CLOSED if zero else TraceStatus.FILLED
        self.trace_store.update(trace_id, final_status, position_after=after, reconciliation=reconciliation)
        return {
            "trace_id": trace_id,
            "status": final_status.value,
            "position_after": after["quantity"],
            "order_id": str(raw_ack.get("orderId", "")),
        }

    async def _run_quarantined_exit(
        self,
        symbol: str,
        snapshot: TradingPoolSnapshot,
    ) -> dict[str, Any]:
        """Close dedicated-account exposure even when a symbol is quarantined."""

        if not self.config.confirm_testnet or not self.config.api_key or not self.config.api_secret:
            return {"status": TraceStatus.UNKNOWN.value, "reason": "QUARANTINED_EXIT_NOT_AUTHORIZED"}
        position = await self._position_facts(symbol)
        if position is None:
            return {"status": TraceStatus.UNKNOWN.value, "reason": "QUARANTINED_POSITION_UNKNOWN"}
        if float(position["quantity"]) == 0:
            return {"status": TraceStatus.CLOSED.value, "reason": "QUARANTINED_ALREADY_FLAT"}
        observation = self._observations.get(symbol)
        rule = self.adapter.get_rule_snapshot(symbol)
        if observation is None or not rule.is_known or rule.is_stale:
            return {"status": TraceStatus.UNKNOWN.value, "reason": "QUARANTINED_EXIT_FACTS_UNKNOWN"}
        trace_id, intent_id, client_order_id = self._episode_ids(symbol, snapshot)
        base_trace = self._base_trace(
            trace_id=f"{trace_id}-q",
            intent_id=f"{intent_id}-q",
            client_order_id=f"{client_order_id}-q",
            snapshot=snapshot,
            symbol=symbol,
            strategy={"decision": "QUARANTINED_REDUCE_ONLY_EXIT"},
            portfolio={"target_quantity": "0"},
            rule=rule,
        )
        leverage = int(position.get("leverage") or 1)
        close = await self._close_position(base_trace, snapshot, rule, position, leverage)
        result = {**close, "reason": "QUARANTINED_REDUCE_ONLY_EXIT"}
        if close.get("status") != TraceStatus.CLOSED.value or not close.get("order_id"):
            return result
        attribution = await self._execution_attribution(
            symbol,
            [str(close["order_id"])],
            start_time_ms=int(base_trace.timestamp.timestamp() * 1000),
        )
        close_trace_id = str(close.get("trace_id", ""))
        close_trace = self.trace_store.get(close_trace_id)
        if attribution is None or close_trace is None:
            if close_trace is not None:
                self.trace_store.update(
                    close_trace_id,
                    TraceStatus.CLOSED,
                    error={"reason": "QUARANTINED_EXIT_ATTRIBUTION_UNKNOWN"},
                )
            return {**result, "status": TraceStatus.UNKNOWN.value, "reason": "QUARANTINED_EXIT_ATTRIBUTION_UNKNOWN"}
        fees, funding, pnl = attribution
        self.trace_store.update(close_trace_id, TraceStatus.CLOSED, fees=fees, funding=funding, pnl=pnl)
        return result

    async def run_once(self, *, episode_identity_namespace: str = "") -> VerificationSummary:
        if episode_identity_namespace and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", episode_identity_namespace):
            raise ValueError("episode_identity_namespace must be a bounded lowercase identifier")
        self._episode_identity_namespace = episode_identity_namespace
        startup = await self.startup()
        snapshot = self.pool.snapshot()
        quarantined_episodes = [
            await self._run_quarantined_exit(symbol, snapshot) for symbol in snapshot.quarantined_symbols
        ]
        active_episodes = [await self._run_symbol(symbol, snapshot) for symbol in snapshot.active_symbols]
        episodes = [*quarantined_episodes, *active_episodes]
        trace_ids: list[str] = []
        for item in episodes:
            if item.get("trace_ids"):
                trace_ids.extend(str(value) for value in item["trace_ids"])
            elif item.get("trace_id"):
                trace_ids.append(str(item["trace_id"]))
        if any(
            (
                item.get("status") == TraceStatus.CLOSED.value
                if self.config.close_after_verify
                else item.get("status") == TraceStatus.FILLED.value
            )
            for item in active_episodes
        ):
            status = "EPISODE_COMPLETED"
        else:
            status = "NOT_VERIFIABLE"
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + self.config.config_hash()[:12]
        summary = VerificationSummary(
            run_id=run_id,
            status=status,
            startup=startup,
            episodes=episodes,
            trace_ids=trace_ids,
        )
        summary.manifest_path = str(self.config.evidence_dir / summary.run_id / "manifest.json")
        manifest_path = self._write_manifest(summary)
        if str(manifest_path) != summary.manifest_path:
            summary.manifest_path = str(manifest_path)
        return summary

    async def reconcile_flat_account(self) -> dict[str, Any]:
        """Require fresh signed one-way, flat, order-free account truth.

        This is the between-episode gate used by the bounded soak campaign.
        Every malformed or unavailable venue fact remains UNKNOWN and blocks
        the next episode.
        """

        mode_result = await self.adapter.get_position_mode()
        account_result = await self.adapter.get_account_snapshot()
        regular_result = await self.adapter.request("GET", Endpoint.OPEN_ORDERS, signed=True)
        algo_result = await self.adapter.get_open_algo_orders()
        if not all(result.is_success() for result in (mode_result, account_result, regular_result, algo_result)):
            return {"status": "SIGNED_RECONCILIATION_UNKNOWN"}
        if not isinstance(mode_result.data, dict) or mode_result.data.get("dualSidePosition") is not False:
            return {
                "status": "HEDGE_MODE" if isinstance(mode_result.data, dict) else "POSITION_MODE_UNKNOWN",
                "position_mode": _jsonable(mode_result.data),
            }
        if (
            not isinstance(account_result.data, dict)
            or not isinstance(account_result.data.get("positions"), list)
            or not isinstance(regular_result.data, list)
            or not isinstance(algo_result.data, list)
        ):
            return {"status": "SIGNED_RECONCILIATION_UNKNOWN"}

        recovery = self._reconcile_flat_filled_traces(account_result, mode_result)
        nonzero_positions: list[dict[str, str]] = []
        try:
            for row in account_result.data["positions"]:
                if not isinstance(row, dict) or not str(row.get("symbol", "")).strip():
                    return {"status": "POSITION_STATE_UNKNOWN"}
                amount = Decimal(str(row.get("positionAmt", "")))
                if not amount.is_finite():
                    return {"status": "POSITION_STATE_UNKNOWN"}
                if amount != 0:
                    nonzero_positions.append({"symbol": str(row["symbol"]).upper(), "position_amount": str(amount)})
        except (InvalidOperation, TypeError, ValueError):
            return {"status": "POSITION_STATE_UNKNOWN"}

        report = {
            "position_mode": "ONE_WAY",
            "nonzero_positions": nonzero_positions,
            "regular_open_order_count": len(regular_result.data),
            "algo_open_order_count": len(algo_result.data),
            "unresolved_trace_count": self.trace_store.unresolved_count,
            "recovery": recovery,
        }
        if nonzero_positions:
            return {"status": "RESIDUAL_POSITION", **report}
        if regular_result.data or algo_result.data:
            return {"status": "OPEN_ORDERS", **report}
        if self.trace_store.unresolved_count:
            return {"status": "UNRESOLVED_TRACES", **report}
        return {"status": "RECONCILED_FLAT", **report}

    def _write_manifest(self, summary: VerificationSummary) -> Path:
        destination = self.config.evidence_dir / summary.run_id
        destination.mkdir(parents=True, exist_ok=True)
        revision = _git_revision()
        economic_truth = _economic_truth_assessment()
        write_configured = bool(self.config.confirm_testnet and self.config.api_key and self.config.api_secret)
        write_enabled = write_configured and not self.config.kill_switch_path.exists()
        manifest = {
            **summary.to_dict(),
            "config": self.config.redacted_dict(),
            "config_hash": self.config.config_hash(),
            "commit": revision,
            "repository_commit": revision,
            "commands": list(sys.argv),
            "real_testnet_write": self._real_order_acknowledged,
            "real_testnet_write_attempted": self._real_order_write_attempted,
            "real_testnet_write_outcome_unknown": self._real_order_outcome_unknown,
            "testnet_write_configured": write_configured,
            "testnet_write_enabled": write_enabled,
            "economic_truth": economic_truth,
            "trace_store": str(self.config.trace_path),
            "results": {
                "status": summary.status,
                "episode_count": len(summary.episodes),
                "unresolved_trace_count": self.trace_store.unresolved_count,
            },
        }
        path = destination / "manifest.json"
        temporary = destination / "manifest.json.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        return path


def build_runtime(config: VerifierConfig) -> VerificationRuntime:
    return VerificationRuntime(config)


async def run(config: VerifierConfig) -> VerificationSummary:
    runtime = build_runtime(config)
    if config.once:
        return await runtime.run_once()
    while True:
        summary = await runtime.run_once()
        if summary.status == "NOT_VERIFIABLE" and (
            not config.confirm_testnet or not config.api_key or not config.api_secret
        ):
            return summary
        await asyncio.sleep(max(60.0, runtime._interval_hours(config.interval) * 3600.0))
