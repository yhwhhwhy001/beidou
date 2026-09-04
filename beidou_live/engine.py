"""The bar-driven live loop.

startup: rules -> one-way mode check -> cancel stale orders -> leverage -> snapshot
cycle:   (new UTC day: universe refresh) -> closed bars (mainnet) -> venue snapshot
         -> income since last cycle (attribution; external cash flows re-baseline) -> probe stop rules
         -> model targets (seeded with last cycle's) -> drawdown throttle -> exit overlay -> guards
         -> plan (participation cap) -> margin scaling -> execute -> persist state/heartbeat
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from beidou_alpha.overlays.exits import ExitParams
from beidou_alpha.overlays.exposure import DrawdownThrottleParams, drawdown_scalar
from beidou_alpha.panel import interval_seconds
from beidou_live.alerts import WebhookAlerts
from beidou_live.attribution import attribute, external_flows
from beidou_live.execution import ExecutionReport, execute_order
from beidou_live.exits import ExitOverlay
from beidou_live.guards import GuardDecision, GuardParams, evaluate_guards
from beidou_live.inputs import latest_closes, model_inputs, required_history
from beidou_live.leverage import derive_leverage, scale_orders_to_margin
from beidou_live.ports import Clock, MarketData, SignalModel, UniverseProvider, UniverseUpdate, Venue
from beidou_live.probe import ProbeParams, probe_status
from beidou_live.rebalancer import RebalanceParams, flatten_orders, plan_rebalance
from beidou_live.reconciler import Snapshot, startup_reconcile, take_snapshot
from beidou_live.scheduler import last_closed_bar_open_ms, wait_for_bar_close
from beidou_live.state import LiveState, StateStore, utc_now_iso

logger = logging.getLogger(__name__)

# The public kline endpoint serves at most this many bars per request.  A model that needs more must fail loudly at
# startup instead of trading on a silently truncated window (E-042: 817 bars for a 720-bar horizon).
MAX_HISTORY_BARS = 1500


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
    exits: ExitParams = field(default_factory=ExitParams)
    throttle: DrawdownThrottleParams = field(default_factory=DrawdownThrottleParams)
    leverage_mode: str = "fixed"  # fixed | auto (D-016)
    margin_cap: float = 0.40
    max_leverage: int = 5
    margin_buffer: float = 0.10
    universe_refresh: bool = False  # D-014: re-rank once per UTC day through the UniverseProvider
    liquidity_window: int = 24
    quarantine_after: int = 0  # D-031: rejected cycles before a symbol leaves the universe (0 = off)
    probes: tuple[ProbeParams, ...] = ()  # D-019: probe books with their automatic stop rules
    max_bar_alignment_ms: int = 60_000  # how far the wake-up may sit from a real bar boundary (D-025)

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
        pool: UniverseProvider | None = None,
        universe_sink: Callable[[UniverseUpdate], None] | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self.market = market
        self.venue = venue
        self.clock = clock
        self.store = store
        self.alerts = alerts or WebhookAlerts("")
        self.pool = pool
        self.universe_sink = universe_sink
        self.state: LiveState = store.load()
        if self.state.cycles > 0:  # M-004: this process is a restart, not a first start
            self.state.restarts = self.state.restarts + 1
            self.state.restarted_at = utc_now_iso()
        persisted = list(self.state.universe) if config.universe_refresh else []
        self.universe: list[str] = persisted or list(config.universe)
        self.rules: dict[str, Any] = {}
        self.exits = ExitOverlay(config.exits, config.interval_ms)
        self._alignment_alerted = False
        self._own_orders: set[str] | None = None  # D-032, seeded lazily from the trade log
        if self.state.stopped_books:  # a probe stopped in an earlier run stays stopped across restarts
            self.model = _without_books(self.model, list(self.state.stopped_books))

    # --- lifecycle ------------------------------------------------------------
    def managed_symbols(self) -> list[str]:
        """The universe plus symbols that left it but still hold a position (D-014: exits follow positions)."""
        return list(dict.fromkeys([*self.universe, *self.state.leaving]))

    async def startup(self) -> Snapshot:
        if self.history_bars > MAX_HISTORY_BARS:
            raise RuntimeError(
                f"the model needs {self.history_bars} closed bars per cycle (min_history "
                f"{getattr(self.model, 'min_history_bars', 0)} + warmup {getattr(self.model, 'warmup_bars', 0)}) "
                f"but the market data port serves at most {MAX_HISTORY_BARS}"
            )
        if bool(getattr(self.model, "needs_funding", False)) and not callable(
            getattr(self.market, "funding_history", None)
        ):
            raise RuntimeError(
                "an enabled signal reads funding history but the market data port cannot supply it; "
                "refusing to trade an unvalidated configuration (KILL-027)"
            )
        sync = getattr(self.venue, "sync_clock", None)
        if callable(sync):
            # Establish the venue offset deterministically instead of leaving it to the first -1021 to
            # discover: the income window (D-030) reads it, and a zero offset there is a silent hour of
            # missing attribution rather than a loud error.
            try:
                logger.info("venue clock offset: %+.1fs", int(await sync()) / 1000.0)
            except Exception as exc:
                logger.warning("could not sync the venue clock (%s); the income window falls back to the host", exc)
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
        snapshot = await startup_reconcile(
            self.venue, self.managed_symbols(), cancel_stale_orders=not self.config.dry_run
        )
        if not snapshot.account.can_trade:
            raise RuntimeError("venue reports canTrade=false for this account")
        self.state.leaving = [symbol for symbol in self.state.leaving if symbol in snapshot.positions]
        if not self.config.dry_run:
            await self._ensure_leverage(self.managed_symbols(), reassert=True)
        self._roll_day(self.clock.now_ms(), snapshot.equity)
        if self.state.last_income_ms is None:
            self.state.last_income_ms = self.clock.now_ms()
        self.state.equity_hwm = max(self.state.equity_hwm or snapshot.equity, snapshot.equity)
        self.store.save(self.state)
        self.store.heartbeat(
            {
                "phase": "STARTED",
                "equity": snapshot.equity,
                "universe": self.universe,
                "leaving": list(self.state.leaving),
                "leverage": dict(self.state.leverage_set),
                "history_bars": self.history_bars,
                "construction": construction_fingerprint(self.config),
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
        """Run ``cycles`` bar cycles (forever when None).  Returns the number of cycles that completed without error."""
        await self.startup()
        attempted = succeeded = 0
        if immediate:
            attempted += 1
            if (
                await self.guarded_cycle(last_closed_bar_open_ms(self.clock.now_ms(), self.config.interval_ms))
                is not None
            ):
                succeeded += 1
        while cycles is None or attempted < cycles:
            bar = await wait_for_bar_close(self.clock, self.config.interval_ms, self.config.grace_seconds)
            attempted += 1
            if await self.guarded_cycle(bar) is not None:
                succeeded += 1
        return succeeded

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
            # A failed cycle must leave a durable row: the heartbeat is overwritten by the next cycle, so
            # without this the only trace of an outage is a log line (that is how the -1023 on 2026-09-04
            # left no record).  No `equity` key, so the drift check keeps ignoring it; `bar_open_ms` puts it
            # in the right day, and the targets still in force are carried so the daily report stays readable.
            self.store.append_cycle(
                {
                    "bar_open_ms": bar_open_ms,
                    "bar": datetime.fromtimestamp(bar_open_ms / 1000, tz=UTC).isoformat(),
                    "phase": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                    "consecutive_errors": self.state.consecutive_errors,
                    "targets": dict(self.state.last_targets),
                    "orders": [],
                    "dry_run": self.config.dry_run,
                }
            )
            logger.exception("cycle %s failed", bar_open_ms)
            await self.alerts.send(
                f"beidou cycle failed ({self.state.consecutive_errors}x): {type(exc).__name__}: {exc}"
            )
            if self.state.consecutive_errors >= self.config.max_consecutive_errors:
                raise
            # M-004: back off exponentially, capped at an hour, before the next attempt.  launchd's
            # ThrottleInterval only paces process restarts; a loop that stays up and retries a failing
            # venue every cycle needs its own brake, and the plan capped it at 1h.
            await self.clock.sleep(self.backoff_seconds())
            return None
        self.state.consecutive_errors = 0
        return record

    # --- one bar ----------------------------------------------------------------
    @property
    def history_bars(self) -> int:
        """Closed bars requested per cycle: the listing-age filter plus the model's warmup under its real params."""
        return required_history(self.model, self.config.history_bars)

    async def run_cycle(self, bar_open_ms: int) -> dict[str, Any]:
        config = self.config
        universe_update = await self._maybe_refresh_universe(bar_open_ms)
        managed = self.managed_symbols()
        inputs = await model_inputs(self.market, self.model, managed, config.interval, self.history_bars)
        usable = inputs.bars
        mark = getattr(self.venue, "mark", None)
        if callable(mark):  # paper venue: marks follow the newest closed bar
            mark(latest_closes(usable))
        clock = await self._clock_skew()
        snapshot = await take_snapshot(self.venue, managed)
        self._roll_day(bar_open_ms, snapshot.equity)
        self.state.leaving = [symbol for symbol in self.state.leaving if symbol in snapshot.positions]
        flows = (
            {"total": 0.0, "rows": 0, "by_type": {}, "rebaselined": False}
            if config.dry_run
            else await self._ingest_income(bar_open_ms, snapshot.equity)
        )
        probes = await self._check_probes(bar_open_ms)
        targets = self.model.targets(
            usable, inputs.funding, previous=self.state.last_contributions, funding_history=inputs.funding_history
        )
        latest_bar_ms = int(targets.as_of.timestamp() * 1000)
        # exposure throttle (D-015): one scalar on the whole book, driven by venue equity vs its high-water mark
        hwm = max(self.state.equity_hwm or snapshot.equity, snapshot.equity)
        self.state.equity_hwm = hwm
        drawdown = 0.0 if hwm <= 0 else max(0.0, 1.0 - snapshot.equity / hwm)
        scalar = drawdown_scalar(drawdown, config.throttle)
        raw = {symbol: float(weight) for symbol, weight in targets.weights.items()}
        for symbol in managed:
            raw.setdefault(symbol, 0.0)
        # D-014's exit side follows positions, but its ENTRY side follows the universe.  Reading `leaving`
        # here instead of the universe left a hole: `leaving` is filtered to symbols that still hold a
        # position a few lines above, so the cycle after a departing symbol is flattened it is still in
        # `managed` (that list was built before the filter), the model still scores it, and nothing zeroes
        # it - the loop opened a fresh 952 USDT long in a symbol that had left the pool the day before.
        pool = set(self.universe)
        for symbol in raw:
            if symbol not in pool:
                raw[symbol] = 0.0
        throttled = {symbol: weight * scalar for symbol, weight in raw.items()}
        # exit overlay (D-012): venue positions are the reference, state persists across restarts
        adjusted, exit_states, exit_events = self.exits.apply(
            throttled,
            positions=snapshot.positions,
            bars=usable,
            states=self.state.exit_states,
            bar_open_ms=bar_open_ms,
        )
        if self.exits.enabled:
            self.state.exit_states = {**self.state.exit_states, **exit_states}
        decision = evaluate_guards(
            adjusted,
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
            "gross_before": snapshot.gross_notional(),
            # M-007 is about the standing book, not about what one cycle's orders ask for: record the
            # initial margin the positions already consume so the daily report can read a real series.
            "margin_usage": snapshot.margin_usage(self.state.leverage_set, config.leverage),
            "margin_fields_reliable": snapshot.account.margin_fields_reliable,
            "guard_reasons": list(decision.reasons),
            "skip": decision.skip_cycle,
            "dry_run": config.dry_run,
            "universe": list(self.universe),
            "leaving": list(self.state.leaving),
            "universe_update": universe_update,
            "inputs": inputs.to_dict(),
            "construction": construction_fingerprint(config)["digest"],
            "clock": clock,
            "external_flows": flows,
            "throttle": {"scalar": scalar, "drawdown": drawdown, "equity_hwm": hwm},
            "exit_events": exit_events,
            "probes": probes,
            "targets": decision.targets,
            "orders": [],
            "skipped": [],
        }
        await self._announce_guards(decision, bar_open_ms)
        if decision.skip_cycle:
            self._finish_cycle(record, targets.contributions)
            return record
        liquidity = self._liquidity(usable) if config.rebalance.max_participation > 0 else None
        orders, skipped = plan_rebalance(
            decision.targets,
            managed_symbols=managed,
            equity=snapshot.equity,
            positions=snapshot.positions,
            prices=snapshot.prices,
            rules=self.rules,
            bar_open_ms=bar_open_ms,
            params=config.rebalance,
            liquidity=liquidity,
        )
        if orders:
            orders, margin = scale_orders_to_margin(
                orders,
                snapshot.available_margin(self.state.leverage_set, config.leverage),
                self.state.leverage_set,
                self.rules,
                buffer=config.margin_buffer,
                default_leverage=config.leverage,
            )
            record["margin"] = margin
            skipped.extend(margin.get("dropped", []))
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
            self._remember_order(report)
            record["orders"].append(report.to_dict())
        record["quarantined"] = self._quarantine(reports)
        record["summary"] = _summarize(reports, orders if config.dry_run else [])
        self._finish_cycle(record, targets.contributions)
        return record

    async def flatten(self) -> list[ExecutionReport]:
        """Close every managed position with reduce-only market orders (``beidou live flatten``)."""
        self.rules = await self.venue.rules()
        snapshot = await take_snapshot(self.venue, self.managed_symbols())
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
            self._remember_order(report)
        return reports

    # --- helpers ----------------------------------------------------------------
    def _quarantine(self, reports: Sequence[ExecutionReport]) -> list[str]:
        """D-031: a symbol the venue keeps rejecting leaves the universe and takes the reduce-only exit path.

        Evidence, not suspicion.  A streak only advances when the same cycle placed a non-rejected order
        somewhere else, so an account-wide condition - margin, clock skew, an IP ban - can never empty the
        pool through this path; that belongs to the guards, which own stopping the whole book.  The cost of
        that choice is a blind spot: a cycle whose single order is rejected proves nothing about the symbol,
        so it does not count.  A cycle that places no order for a symbol is silent rather than exonerating,
        so the streak survives the no-trade band.  Recovery is the pool's decision alone: a quarantined
        symbol returns only when the daily refresh selects it again.

        Pre-registered falsifier (2026-09-04): if this fires more than twice in 30 days with no venue
        incident behind it, ``quarantine_after`` is too tight and the rule is what is broken, not the symbol.
        """
        after = self.config.quarantine_after
        placed = {report.order.symbol for report in reports}
        rejected = {report.order.symbol for report in reports if report.status == "REJECTED"}
        if after <= 0 or not rejected or rejected == placed:
            return []
        streak = {symbol: count for symbol, count in self.state.reject_streak.items() if symbol not in placed}
        streak.update({symbol: self.state.reject_streak.get(symbol, 0) + 1 for symbol in rejected})
        hit = sorted(symbol for symbol, count in streak.items() if count >= after and symbol in self.universe)
        self.state.reject_streak = {symbol: count for symbol, count in streak.items() if symbol not in hit}
        if hit:
            self.universe = [symbol for symbol in self.universe if symbol not in hit]
            self.state.universe = list(self.universe)
            self.state.leaving = list(dict.fromkeys([*self.state.leaving, *hit]))
            logger.warning("quarantined after %d rejected cycles, exiting reduce-only: %s", after, hit)
        return hit

    async def _maybe_refresh_universe(self, bar_open_ms: int) -> dict[str, Any] | None:
        """Once per UTC day: re-rank through the pool; what leaves is flattened, what enters waits for history."""
        if not self.config.universe_refresh or self.pool is None:
            return None
        day = datetime.fromtimestamp(bar_open_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        if self.state.universe_day == day:
            return None
        try:
            update = await self.pool.select(self.universe, self.rules)
        except Exception as exc:  # keep trading the previous universe (T-P05)
            logger.warning("universe refresh failed (%s); keeping %s", exc, self.universe)
            return {"error": f"{type(exc).__name__}: {exc}", "universe": list(self.universe)}
        fresh = [symbol for symbol in update.symbols if symbol in self.rules and self.rules[symbol].tradable]
        if not fresh:
            logger.warning("universe refresh returned no tradable symbols; keeping %s", self.universe)
            return {"error": "EMPTY_UNIVERSE", "universe": list(self.universe)}
        left = [symbol for symbol in self.universe if symbol not in fresh]
        entered = [symbol for symbol in fresh if symbol not in self.universe]
        self.universe = fresh
        self.state.universe = list(fresh)
        self.state.universe_day = day
        self.state.leaving = list(dict.fromkeys([*self.state.leaving, *left]))
        if entered and not self.config.dry_run:
            await self._ensure_leverage(entered)
        if self.universe_sink is not None:
            try:
                self.universe_sink(update)
            except Exception as exc:  # persistence of the selection is best effort
                logger.warning("could not persist universe update: %s", exc)
        if entered or left:
            logger.info("universe refreshed: entered=%s left=%s", entered, left)
            await self.alerts.send(f"beidou universe refresh: entered={entered} left={left}")
        return {**update.to_dict(), "entered": entered, "left": left, "day": day}

    async def _leverage_targets(self, symbols: Sequence[str]) -> dict[str, int]:
        if self.config.leverage_mode != "auto":
            return dict.fromkeys(symbols, self.config.leverage)
        brackets: Mapping[str, int] = {}
        probe = getattr(self.venue, "leverage_brackets", None)
        if callable(probe):
            try:
                brackets = await probe()
            except Exception as exc:
                logger.warning("leverage brackets unavailable (%s); using policy caps only", exc)
        return {
            symbol: derive_leverage(
                self.config.guards.max_gross, self.config.margin_cap, self.config.max_leverage, brackets.get(symbol)
            )
            for symbol in symbols
        }

    async def _ensure_leverage(self, symbols: Sequence[str], *, reassert: bool = False) -> None:
        """Set the derived leverage on the venue; a refusal keeps the old setting and never stops the loop.

        ``state.leverage_set`` records what this loop last SENT, never what the venue holds.  Every
        endpoint that could report the setting back reads 0 or null on demo-fapi - positionRisk v2 and
        v3, the /fapi/v2/account rows, /fapi/v1/symbolConfig, all four checked 2026-09-04 - so a drift
        this loop did not cause is invisible here, and skipping the POST because the *record* already
        matches makes that cache authoritative over the venue.  It is not: an account reset takes the
        venue's setting back to its default and leaves the record untouched, and that reset is a thing
        this account does - on 2026-09-04 fifteen positions vanished between two cycles with no order
        from this loop, after which the leverage the record claimed was never re-sent.  ``reassert``
        posts unconditionally; startup does, because the POST is idempotent, costs one weight unit per
        symbol, and is the only channel that can make the venue agree with the record.
        """
        wanted = await self._leverage_targets(symbols)
        for symbol in symbols:
            if not reassert and self.state.leverage_set.get(symbol) == wanted[symbol]:
                continue
            try:
                applied = await self.venue.set_leverage(symbol, wanted[symbol])
            except (
                Exception
            ) as exc:  # e.g. -4028 invalid leverage for this symbol: margin math falls back to config.leverage
                logger.warning(
                    "leverage %sx refused for %s (%s); keeping the venue setting", wanted[symbol], symbol, exc
                )
                continue
            self.state.leverage_set[symbol] = int(applied)

    def _liquidity(self, bars: Mapping[str, pd.DataFrame]) -> dict[str, float]:
        """Average quote volume per bar over the trailing window (falls back to volume x close)."""
        out: dict[str, float] = {}
        window = max(1, self.config.liquidity_window)
        for symbol, frame in bars.items():
            tail = frame.tail(window)
            if "quote_volume" in tail.columns:
                value = float(tail["quote_volume"].astype(float).mean())
            else:
                value = float((tail["volume"].astype(float) * tail["close"].astype(float)).mean())
            if value > 0:
                out[symbol] = value
        return out

    async def _clock_skew(self) -> dict[str, Any]:
        """Where the host clock sits relative to the venue's, and whether that endangers the cycle (D-025).

        The operator's decision is that the host clock is the reference, so a *constant* offset is an
        accepted state, not a fault: with an offset close to a whole number of intervals the loop still
        wakes just after a real bar close and trades the bar that just closed.  What is dangerous is the
        remainder - how far the wake-up sits from a real bar boundary - and a *jump*, which remaps every
        label and can send the income watermark backwards (the -1023 of 2026-09-04).  Both are watched
        here; the raw offset is recorded but never alerted on.  The probe is free (the port already asks
        the venue for its time) and must never be why a cycle fails, so every error is swallowed.
        """
        probe = getattr(self.market, "server_time_ms", None)
        if not callable(probe):
            return {"skew_ms": None, "alignment_ms": None, "whole_bars": None, "beyond_tolerance": False}
        try:
            before = self.clock.now_ms()
            server = int(await probe())
            after = self.clock.now_ms()
        except Exception as exc:
            logger.warning("clock skew probe failed (%s); continuing", exc)
            return {"skew_ms": None, "alignment_ms": None, "whole_bars": None, "beyond_tolerance": False}
        interval = self.config.interval_ms
        skew = float(server - (before + after) / 2)
        alignment = ((skew + interval / 2) % interval) - interval / 2  # signed distance to a bar boundary
        beyond = abs(alignment) > self.config.max_bar_alignment_ms
        previous = self.state.last_clock_skew_ms
        jumped = previous is not None and abs(skew - previous) > interval / 2
        self.state.last_clock_skew_ms = skew
        if beyond:
            logger.warning(
                "the wake-up sits %.1fs from a real bar boundary (offset %.0fs); the loop may act on a bar "
                "that has not closed at the venue",
                alignment / 1000.0,
                skew / 1000.0,
            )
        if jumped:
            logger.warning(
                "host clock jumped %.0fs since the last cycle; labels before and after differ",
                (skew - (previous or 0.0)) / 1000.0,
            )
        if (beyond and not self._alignment_alerted) or jumped:
            self._alignment_alerted = True
            await self.alerts.send(
                f"beidou clock: offset {skew / 1000.0:+.0f}s, {alignment / 1000.0:+.1f}s from a bar boundary"
                + (f", jumped {(skew - (previous or 0.0)) / 1000.0:+.0f}s since the last cycle" if jumped else "")
            )
        if not beyond:
            self._alignment_alerted = False
        return {
            "skew_ms": skew,
            "alignment_ms": alignment,
            "whole_bars": round(skew / interval),
            "beyond_tolerance": beyond,
            "jumped": jumped,
        }

    def _remember_order(self, report: ExecutionReport) -> None:
        """Keep the D-032 order-id set current within a run; a fill of this order must not read as foreign."""
        if report.ack is not None and report.ack.order_id:
            self._placed_order_ids().add(str(report.ack.order_id))

    async def _own_trade_ids(self, since: int, now: int) -> set[str] | None:
        """Trade ids in the window that came from an order this loop placed (D-032).

        An income row names a ``tradeId`` and nothing else about provenance; a userTrades row carries that
        id plus the ``orderId``, and every order the loop placed is in trades.jsonl with its order id.  So
        the join is income.tradeId -> userTrades.orderId -> our own order ids.

        Returns ``None`` when the reconciliation cannot be done - the venue has no userTrades, or the call
        failed.  ``None`` means "attribute everything as before", which is the old behaviour: a monitor
        that cannot run must not silently reclassify a whole cycle's P&L as somebody else's.
        """
        probe = getattr(self.venue, "user_trades", None)
        if not callable(probe):
            return None
        try:
            fills = await probe(since, now)
        except Exception as exc:  # never a reason for a cycle to fail
            logger.warning("userTrades unavailable (%s); attributing every fill to the book", exc)
            return None
        return {str(fill.get("id")) for fill in fills if str(fill.get("orderId")) in self._placed_order_ids()}

    def _placed_order_ids(self) -> set[str]:
        """Order ids this loop got an ack for, from its own append-only trade log."""
        if self._own_orders is None:
            self._own_orders = {
                str(row["order_id"]) for row in self.store.read_jsonl(self.store.trades_path) if row.get("order_id")
            }
        return self._own_orders

    def venue_now_ms(self) -> int:
        """Now on the venue's clock, falling back to the host's for venues that do not keep an offset."""
        probe = getattr(self.venue, "venue_time_ms", None)
        if not callable(probe):
            return self.clock.now_ms()
        try:
            return int(probe())
        except Exception as exc:  # never a reason for a cycle to fail
            logger.warning("venue clock unavailable (%s); using the host clock for the income window", exc)
            return self.clock.now_ms()

    async def _ingest_income(self, bar_open_ms: int, equity: float) -> dict[str, Any]:
        """Income rows since the last cycle: strategy attribution plus external cash-flow detection.

        Trading income is attributed to the strategies that held the book at
        the previous cycle (its bar is recorded on the row).  Deposits,
        withdrawals and demo-account resets (E-044: a reset showed up as
        TRANSFER rows with the positions simply gone) make equity
        non-comparable, so they re-baseline the day-start equity and the
        high-water mark and are flagged on the cycle record for the drift
        check to skip.

        Both ends of the window are on the *venue's* clock (D-030).  ``startTime``/``endTime`` are query
        parameters, so unlike a signed request's ``timestamp`` they are not corrected by the -1021 resync:
        with the host an hour behind the venue (measured 2026-09-04) the loop asked for an hour-old window
        and could not see anything more recent.  Proven the same day: after a manual flatten, 96 rows worth
        +23.98 USDT sat at venue times 07:51-07:53 while the loop was querying [06:53, 06:58] and ingesting
        nothing.  Income was not lost - it arrived an hour late, attributed to the book held an hour after
        it was earned, which is exactly the corruption M-010 cannot tolerate.  The first cycle after this
        change queries from the old host-basis watermark to venue-now, one wider window that recovers the
        gap; from then on the watermark is venue-basis and the window is contiguous.
        """
        now = self.venue_now_ms()
        since = self.state.last_income_ms or now
        if since > now:
            # The watermark is in the future, so [since, now] would be rejected with -1023 and abort the
            # whole cycle before it can trade; moving the watermark back would silently drop the income in
            # between.  Skip ingestion for this cycle, keep the watermark, and let the cycle trade.  Reached
            # on 2026-09-04 when the host clock moved backwards by 3,612 s, and still reachable now that the
            # window is venue-basis: the venue's clock can move too, and a venue that cannot be reached
            # falls this back to the host's.
            skew = since - now
            logger.warning("income watermark is %d ms ahead of the clock; skipping ingestion this cycle", skew)
            return {"total": 0.0, "rows": 0, "by_type": {}, "rebaselined": False, "clock_skew_ms": skew}
        rows = await self.venue.income(since, now)
        flows = external_flows(rows)
        flows["rebaselined"] = False
        if rows and self.state.last_contributions:
            own_trade_ids = await self._own_trade_ids(since, now)
            result = attribute(
                rows, self.state.last_contributions, self.config.strategy_weights, own_trade_ids=own_trade_ids
            )
            foreign = result["foreign"]
            if foreign["rows"]:
                logger.warning(
                    "%d income row(s) totalling %+.2f USDT came from fills this loop did not place (%s); "
                    "reported under `foreign`, kept out of the strategy series",
                    foreign["rows"],
                    foreign["total"],
                    ", ".join(sorted(foreign["by_symbol"])) or "no symbol",
                )
                await self.alerts.send(
                    f"beidou: {foreign['rows']} foreign fill row(s) {foreign['total']:+.2f} USDT "
                    f"({', '.join(sorted(foreign['by_symbol'])) or 'no symbol'}) excluded from strategy attribution"
                )
            if result["by_symbol"] or foreign["rows"]:
                self.store.append_attribution(
                    {"bar_open_ms": self.state.last_bar_ms or bar_open_ms, "since_ms": since, "until_ms": now, **result}
                )
        self.state.last_income_ms = now
        if flows["rows"] > 0:
            flows["rebaselined"] = True
            logger.warning(
                "external cash flow %+.2f over %d row(s) %s since %s; day start and high-water mark re-based to %.2f",
                flows["total"],
                flows["rows"],
                flows["by_type"],
                datetime.fromtimestamp(since / 1000, tz=UTC).isoformat(timespec="seconds"),
                equity,
            )
            self.state.day_start_equity = equity
            self.state.equity_hwm = equity
            await self.alerts.send(
                f"beidou external cash flow {flows['total']:+.2f} USDT ({flows['rows']} rows {flows['by_type']}); "
                f"equity re-based to {equity:.2f}"
            )
        return flows

    async def _check_probes(self, bar_open_ms: int) -> list[dict[str, Any]]:
        """D-019: evaluate every probe book's stop rule on the attributed P&L; a stopped book leaves the model."""
        if not self.config.probes:
            return []
        rows = self.store.read_jsonl(self.store.attribution_path)
        now = self.clock.now_ms()
        statuses: list[dict[str, Any]] = []
        for probe in self.config.probes:
            stopped = self.state.stopped_books.get(probe.book)
            if stopped is not None:
                statuses.append({"book": probe.book, "strategy": probe.strategy, "status": "STOPPED", **stopped})
                continue
            status = probe_status(probe, rows, equity=self.state.last_equity, now_ms=now)
            if status["stop"]:
                reason = (
                    f"trailing {probe.window_days}d attributed P&L {status['pnl']:.2f} "
                    f"({status['pnl_pct']:.4f} of equity) <= -{probe.max_loss}"
                )
                self.state.stopped_books[probe.book] = {
                    "at": utc_now_iso(),
                    "bar_open_ms": bar_open_ms,
                    "pnl": status["pnl"],
                    "pnl_pct": status["pnl_pct"],
                    "reason": reason,
                }
                self.model = _without_books(self.model, [probe.book])
                status["status"] = "STOPPED"
                logger.warning("probe book %s stopped: %s", probe.book, reason)
                await self.alerts.send(f"beidou: probe book {probe.book} ({probe.strategy}) stopped - {reason}")
            statuses.append(status)
        return statuses

    def backoff_seconds(self) -> float:
        """Delay after a failed cycle: 60s doubling per consecutive error, capped at 1 hour (M-004)."""
        return float(min(3600.0, 60.0 * 2 ** max(0, self.state.consecutive_errors - 1)))

    async def _announce_guards(self, decision: GuardDecision, bar_open_ms: int) -> None:
        """Alert on every change of the guard state, in both directions (M-001).

        Edge-triggered: a stale-data stretch or an engaged kill switch alerts once when it
        starts and once when it clears, rather than every hour or never.  Without this a
        guard could stop the book trading for a whole day and leave nothing but a heartbeat
        field, which is the opposite of what "unattended" is supposed to mean.
        """
        reasons = list(decision.reasons)
        if reasons == self.state.last_guard_reasons:
            return
        bar = datetime.fromtimestamp(bar_open_ms / 1000, tz=UTC).isoformat()
        if reasons:
            skipped = " and the cycle was skipped" if decision.skip_cycle else ""
            await self.alerts.send(f"beidou guards at {bar}: {', '.join(reasons)}{skipped}")
        else:
            await self.alerts.send(f"beidou guards cleared at {bar}: trading normally again")
        logger.warning("guard state changed: %s -> %s", self.state.last_guard_reasons, reasons)
        self.state.last_guard_reasons = reasons

    def _finish_cycle(self, record: dict[str, Any], contributions: Mapping[str, Mapping[str, float]]) -> None:
        # Reaching here means the cycle completed (a guard skip is a completed cycle too), so the error streak
        # is over.  It has to be cleared *before* the save: clearing it in ``guarded_cycle`` afterwards left the
        # stale count on disk until the next cycle wrote, and a restart in that window loaded a phantom error.
        self.state.consecutive_errors = 0
        # T-L07: every position change must trace back to (bar, strategy, score, weight).  The score lives in
        # `contributions`, which state.json overwrites every cycle, so without this line the trail is gone within
        # the hour; cycles.jsonl is append-only and already carries the bar and the final weights.
        record["contributions"] = {
            strategy: {symbol: float(value) for symbol, value in values.items() if value}
            for strategy, values in contributions.items()
        }
        self.state.last_bar_ms = int(record["bar_open_ms"])
        self.state.last_targets = dict(record["targets"])
        # per-strategy memory for the hold seed (D-005): symbols that left the managed set keep their last
        # contribution, as the backtest's forward-fill does; strategies no longer in the model are dropped.
        self.state.last_contributions = {
            strategy: {**self.state.last_contributions.get(strategy, {}), **{s: float(v) for s, v in values.items()}}
            for strategy, values in contributions.items()
        }
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
                "exit_events": len(record.get("exit_events") or []),
                "external_flows": (record.get("external_flows") or {}).get("total", 0.0),
                "clock_skew_ms": (record.get("clock") or {}).get("skew_ms"),
                "clock_alignment_ms": (record.get("clock") or {}).get("alignment_ms"),
                "throttle_scalar": (record.get("throttle") or {}).get("scalar", 1.0),
                "universe_size": len(self.universe),
                "history_bars": self.history_bars,
                "construction": construction_fingerprint(self.config)["digest"][:12],
                "probes": {str(p["book"]): str(p["status"]) for p in (record.get("probes") or [])},
                "next_bar_close_ms": int(record["bar_open_ms"]) + 2 * self.config.interval_ms,
                "dry_run": self.config.dry_run,
            }
        )

    def _roll_day(self, now_ms: int, equity: float) -> None:
        day = datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        if self.state.day != day or self.state.day_start_equity is None:
            self.state.day = day
            self.state.day_start_equity = equity


def construction_fingerprint(config: LiveConfig) -> dict[str, Any]:
    """Digest of the portfolio construction a cycle ran under (D-026).

    The registry fingerprint identifies the *signals*; the startup evidence gate compares *strategy*
    params.  Neither can see the construction - the vol target, the caps, the no-trade bands, the exit
    overlay, the throttle - even though changing one of them changes every weight.  Adopting P10 cell B
    on 2026-09-04 made that concrete: the live band moved to 0.40 while tsmom's cited evidence had been
    validated at 0.25, and nothing in the running record said so.  This does not gate anything; it makes
    a live record self-describing, which is what a later reader needs.
    """
    payload = {
        "guards": {
            "max_gross": config.guards.max_gross,
            "max_weight": config.guards.max_weight,
            "daily_loss_pause": config.guards.daily_loss_pause,
            "stale_bars_max": config.guards.stale_bars_max,
        },
        "rebalance": {
            "no_trade_band": config.rebalance.no_trade_band,
            "no_trade_rel_band": config.rebalance.no_trade_rel_band,
            "max_participation": config.rebalance.max_participation,
            "max_order_notional": config.rebalance.max_order_notional,
        },
        "exits": {
            "stop_loss": config.exits.stop_loss,
            "trailing_stop": config.exits.trailing_stop,
            "take_profit": config.exits.take_profit,
            "cooldown_bars": config.exits.cooldown_bars,
            "vol_halflife": config.exits.vol_halflife,
        },
        "throttle": {
            "enabled": config.throttle.enabled,
            "start": config.throttle.start,
            "stop": config.throttle.stop,
            "floor": config.throttle.floor,
        },
        "leverage": {
            "mode": config.leverage_mode,
            "margin_cap": config.margin_cap,
            "max_leverage": config.max_leverage,
            "margin_buffer": config.margin_buffer,
        },
        "strategy_weights": dict(sorted(config.strategy_weights.items())),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return {"digest": digest, **payload}


def _without_books(model: SignalModel, books: Sequence[str]) -> SignalModel:
    """Drop stopped probe books from a model that supports it (``AlphaModel.without_books``)."""
    drop = getattr(model, "without_books", None)
    if not books or not callable(drop):
        return model
    reduced: SignalModel = drop(list(books))
    return reduced


def _summarize(reports: Sequence[ExecutionReport], planned: Sequence[Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for report in reports:
        counts[report.status] = counts.get(report.status, 0) + 1
    traded = sum(float(r.ack.executed_qty) * (r.ack.avg_price or r.order.price) for r in reports if r.ack is not None)
    return {"statuses": counts, "traded_notional": traded, "planned": len(planned) + len(reports)}
