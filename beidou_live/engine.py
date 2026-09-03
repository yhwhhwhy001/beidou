"""The bar-driven live loop.

startup: rules -> one-way mode check -> cancel stale orders -> leverage -> snapshot
cycle:   closed bars (mainnet) -> model targets -> venue snapshot -> guards -> plan -> execute
         -> attribute income -> persist state/heartbeat
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beidou_alpha.panel import interval_seconds
from beidou_live.alerts import WebhookAlerts
from beidou_live.attribution import attribute
from beidou_live.execution import ExecutionReport, execute_order
from beidou_live.guards import GuardParams, evaluate_guards
from beidou_live.ports import Clock, MarketData, SignalModel, Venue
from beidou_live.rebalancer import RebalanceParams, flatten_orders, plan_rebalance
from beidou_live.reconciler import Snapshot, startup_reconcile, take_snapshot
from beidou_live.scheduler import last_closed_bar_open_ms, wait_for_bar_close
from beidou_live.state import LiveState, StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveConfig:
    interval: str
    history_bars: int
    universe: tuple[str, ...]
    leverage: int
    rebalance: RebalanceParams
    guards: GuardParams
    kill_switch_path: Path
    strategy_weights: dict[str, float] = field(default_factory=dict)
    poll_attempts: int = 5
    poll_interval_seconds: float = 1.0
    grace_seconds: float = 5.0
    max_consecutive_errors: int = 12
    dry_run: bool = False

    @property
    def interval_ms(self) -> int:
        return interval_seconds(self.interval) * 1000


class LiveEngine:
    def __init__(
        self,
        config: LiveConfig,
        *,
        model: SignalModel,
        market: MarketData,
        venue: Venue,
        clock: Clock,
        store: StateStore,
        alerts: WebhookAlerts | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self.market = market
        self.venue = venue
        self.clock = clock
        self.store = store
        self.alerts = alerts or WebhookAlerts("")
        self.state: LiveState = store.load()
        self.universe: list[str] = list(config.universe)
        self.rules: dict[str, Any] = {}

    # --- lifecycle ------------------------------------------------------------
    async def startup(self) -> Snapshot:
        self.rules = await self.venue.rules()
        tradable = [symbol for symbol in self.universe if symbol in self.rules and self.rules[symbol].tradable]
        dropped = sorted(set(self.universe) - set(tradable))
        if dropped:
            logger.warning("dropping non-tradable symbols from universe: %s", dropped)
        if not tradable:
            raise RuntimeError("no tradable symbols in the universe")
        self.universe = tradable
        hedge_probe = getattr(self.venue, "hedge_mode", None)
        if callable(hedge_probe) and await hedge_probe():
            raise RuntimeError("account is in hedge (dual-side) position mode; switch to one-way mode first")
        snapshot = await startup_reconcile(self.venue, self.universe, cancel_stale_orders=not self.config.dry_run)
        if not snapshot.account.can_trade:
            raise RuntimeError("venue reports canTrade=false for this account")
        if not self.config.dry_run:
            for symbol in self.universe:
                if self.state.leverage_set.get(symbol) != self.config.leverage:
                    applied = await self.venue.set_leverage(symbol, self.config.leverage)
                    self.state.leverage_set[symbol] = int(applied)
        self._roll_day(self.clock.now_ms(), snapshot.equity)
        if self.state.last_income_ms is None:
            self.state.last_income_ms = self.clock.now_ms()
        self.store.save(self.state)
        self.store.heartbeat(
            {
                "phase": "STARTED",
                "equity": snapshot.equity,
                "universe": self.universe,
                "dry_run": self.config.dry_run,
                "foreign_positions": sorted(snapshot.foreign_positions),
            }
        )
        if snapshot.foreign_positions:
            logger.warning(
                "positions outside the managed universe are left untouched: %s", sorted(snapshot.foreign_positions)
            )
        return snapshot

    async def run(self, cycles: int | None = None, *, immediate: bool = False) -> int:
        await self.startup()
        done = 0
        if immediate:
            await self.guarded_cycle(last_closed_bar_open_ms(self.clock.now_ms(), self.config.interval_ms))
            done += 1
        while cycles is None or done < cycles:
            bar = await wait_for_bar_close(self.clock, self.config.interval_ms, self.config.grace_seconds)
            await self.guarded_cycle(bar)
            done += 1
        return done

    async def guarded_cycle(self, bar_open_ms: int) -> dict[str, Any] | None:
        try:
            record = await self.run_cycle(bar_open_ms)
        except Exception as exc:
            self.state.consecutive_errors += 1
            self.store.save(self.state)
            self.store.heartbeat(
                {
                    "phase": "ERROR",
                    "bar_open_ms": bar_open_ms,
                    "error": f"{type(exc).__name__}: {exc}",
                    "consecutive_errors": self.state.consecutive_errors,
                }
            )
            logger.exception("cycle %s failed", bar_open_ms)
            await self.alerts.send(
                f"beidou cycle failed ({self.state.consecutive_errors}x): {type(exc).__name__}: {exc}"
            )
            if self.state.consecutive_errors >= self.config.max_consecutive_errors:
                raise
            return None
        self.state.consecutive_errors = 0
        return record

    # --- one bar ----------------------------------------------------------------
    async def run_cycle(self, bar_open_ms: int) -> dict[str, Any]:
        config = self.config
        bars = await self.market.closed_bars(self.universe, config.interval, config.history_bars)
        usable = {symbol: frame for symbol, frame in bars.items() if frame is not None and len(frame) >= 2}
        if not usable:
            raise RuntimeError("no closed bars returned for the universe")
        funding = await self.market.funding_rates(self.universe)
        targets = self.model.targets(usable, funding)
        latest_bar_ms = int(targets.as_of.timestamp() * 1000)
        snapshot = await take_snapshot(self.venue, self.universe)
        self._roll_day(bar_open_ms, snapshot.equity)
        decision = evaluate_guards(
            targets.weights,
            current_weights=snapshot.weights(),
            kill_switch=config.kill_switch_path.exists(),
            equity=snapshot.equity,
            day_start_equity=self.state.day_start_equity,
            latest_bar_ms=latest_bar_ms,
            expected_bar_ms=bar_open_ms,
            interval_ms=config.interval_ms,
            params=config.guards,
        )
        record: dict[str, Any] = {
            "bar_open_ms": bar_open_ms,
            "bar": datetime.fromtimestamp(bar_open_ms / 1000, tz=UTC).isoformat(),
            "as_of_ms": latest_bar_ms,
            "equity": snapshot.equity,
            "gross_before": snapshot.account.gross_notional(),
            "guard_reasons": list(decision.reasons),
            "skip": decision.skip_cycle,
            "dry_run": config.dry_run,
            "targets": decision.targets,
            "orders": [],
            "skipped": [],
        }
        if decision.skip_cycle:
            self._finish_cycle(record, targets.contributions)
            return record
        orders, skipped = plan_rebalance(
            decision.targets,
            managed_symbols=self.universe,
            equity=snapshot.equity,
            positions=snapshot.positions,
            prices=snapshot.prices,
            rules=self.rules,
            bar_open_ms=bar_open_ms,
            params=config.rebalance,
        )
        record["skipped"] = skipped
        reports: list[ExecutionReport] = []
        for order in orders:
            if config.dry_run:
                record["orders"].append({**order.to_dict(), "status": "DRY_RUN"})
                continue
            report = await execute_order(
                self.venue,
                order,
                self.clock,
                poll_attempts=config.poll_attempts,
                poll_interval_seconds=config.poll_interval_seconds,
            )
            reports.append(report)
            self.store.append_trade({"bar_open_ms": bar_open_ms, **report.to_dict()})
            record["orders"].append(report.to_dict())
        if not config.dry_run:
            await self._attribute(bar_open_ms)
        record["summary"] = _summarize(reports, orders if config.dry_run else [])
        self._finish_cycle(record, targets.contributions)
        return record

    async def flatten(self) -> list[ExecutionReport]:
        """Close every managed position with reduce-only market orders (``beidou live flatten``)."""
        self.rules = await self.venue.rules()
        snapshot = await take_snapshot(self.venue, self.universe)
        reports: list[ExecutionReport] = []
        for order in flatten_orders(
            {**snapshot.positions, **snapshot.foreign_positions}, self.rules, self.clock.now_ms()
        ):
            report = await execute_order(
                self.venue,
                order,
                self.clock,
                poll_attempts=self.config.poll_attempts,
                poll_interval_seconds=self.config.poll_interval_seconds,
            )
            reports.append(report)
            self.store.append_trade({"bar_open_ms": None, "flatten": True, **report.to_dict()})
        return reports

    # --- helpers ----------------------------------------------------------------
    async def _attribute(self, bar_open_ms: int) -> None:
        now = self.clock.now_ms()
        since = self.state.last_income_ms or now
        rows = await self.venue.income(since, now)
        if rows and self.state.last_contributions:
            result = attribute(rows, self.state.last_contributions, self.config.strategy_weights)
            self.store.append_attribution({"bar_open_ms": bar_open_ms, "since_ms": since, "until_ms": now, **result})
        self.state.last_income_ms = now

    def _finish_cycle(self, record: dict[str, Any], contributions: Mapping[str, Mapping[str, float]]) -> None:
        self.state.last_bar_ms = int(record["bar_open_ms"])
        self.state.last_targets = dict(record["targets"])
        self.state.last_contributions = {k: dict(v) for k, v in contributions.items()}
        self.state.last_equity = float(record["equity"])
        self.state.cycles += 1
        self.store.save(self.state)
        self.store.append_cycle(record)
        self.store.heartbeat(
            {
                "phase": "SKIPPED" if record["skip"] else "OK",
                "bar_open_ms": record["bar_open_ms"],
                "equity": record["equity"],
                "orders": len(record["orders"]),
                "guard_reasons": record["guard_reasons"],
                "next_bar_close_ms": int(record["bar_open_ms"]) + 2 * self.config.interval_ms,
                "dry_run": self.config.dry_run,
            }
        )

    def _roll_day(self, now_ms: int, equity: float) -> None:
        day = datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        if self.state.day != day or self.state.day_start_equity is None:
            self.state.day = day
            self.state.day_start_equity = equity


def _summarize(reports: Sequence[ExecutionReport], planned: Sequence[Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for report in reports:
        counts[report.status] = counts.get(report.status, 0) + 1
    traded = sum(float(r.ack.executed_qty) * (r.ack.avg_price or r.order.price) for r in reports if r.ack is not None)
    return {"statuses": counts, "traded_notional": traded, "planned": len(planned) + len(reports)}
