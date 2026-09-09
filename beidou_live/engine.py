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
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from beidou_alpha.overlays.exits import ExitParams
from beidou_alpha.overlays.exposure import DrawdownThrottleParams, drawdown_scalar
from beidou_alpha.panel import interval_seconds
from beidou_alpha.portfolio import PortfolioParams
from beidou_alpha.registry import evidence_construction_digest
from beidou_alpha.signals import get_signal
from beidou_governance.policy import policy_digest
from beidou_live.alerts import WebhookAlerts
from beidou_live.attribution import attribute, external_flows
from beidou_live.execution import ExecutionReport, execute_order
from beidou_live.exits import ExitOverlay
from beidou_live.guards import GuardDecision, GuardParams, describe_guard_reason, evaluate_guards
from beidou_live.health import (
    CONSTRUCTION_PAYLOAD_VERSION,
    margin_mode_problems,
    min_liquidation_distance,
)
from beidou_live.inputs import latest_closes, model_inputs, required_history
from beidou_live.leverage import derive_leverage, scale_orders_to_margin
from beidou_live.ports import Clock, MarketData, SignalModel, UniverseProvider, UniverseUpdate, Venue
from beidou_live.probe import ProbeParams, probe_status
from beidou_live.rebalancer import RebalanceParams, flatten_orders, plan_rebalance
from beidou_live.reconciler import Snapshot, is_own_order, startup_reconcile, take_snapshot
from beidou_live.reports import collateral_share
from beidou_live.scheduler import (
    last_closed_bar_open_ms,
    late_seconds,
    rebalance_window_seconds,
    wait_for_bar_close,
    within_rebalance_window,
)
from beidou_live.state import LiveState, StateStore, utc_now_iso

logger = logging.getLogger(__name__)

# The public kline endpoint serves at most this many bars per request.  A model that needs more must fail loudly at
# startup instead of trading on a silently truncated window (E-042: 817 bars for a 720-bar horizon).
MAX_HISTORY_BARS = 1500


class StopRequested:
    """A cooperative stop flag (DL-L6).

    ``launchctl unload`` sends SIGTERM and SIGKILLs after ExitTimeOut (30s).  Dying wherever the
    process happens to be means dying between sending orders and recording them; this lets the loop
    finish the cycle it is in and decline to start another.  The handler that sets it is installed by
    the CLI, not here - signal handlers are process-global and a library has no business owning one.
    """

    def __init__(self) -> None:
        self.requested = False
        self.reason = ""

    def request(self, reason: str) -> None:
        self.requested = True
        self.reason = reason


class BreakerTripped(Exception):
    """The breaker decided to stop this process, and said so out loud first.

    Distinct from the errors that caused it so the CLI can exit 0 - which under
    ``KeepAlive.SuccessfulExit=false`` is what stops launchd relaunching into the same wall every
    60 seconds.  Raised only after an alert was accepted by a channel (see ``breaker_stop``).
    """


async def breaker_stop(
    alerts: Any, *, consecutive_errors: int, detail: str, original: BaseException | None = None
) -> None:
    """Announce the stop, then decide how loudly to fail.

    A clean exit has to be earned.  If a channel took the alert, raise ``BreakerTripped`` and the
    process ends quietly and stays ended.  If nothing took it - no webhook configured, or both
    channels down - the original exception propagates instead: a non-zero exit, launchd relaunches,
    and the operator eventually finds a log full of restarts.  That is worse than a clean stop and
    much better than a book that vanishes without a word (KILL-P1).
    """
    delivered = await alerts.send(
        f"北斗熔断：连续 {consecutive_errors} 次周期失败，已停止循环"
        f"（干净退出后 launchd 不会重启；恢复请执行 `beidou live run`）：{detail}",
        key="breaker",
        force=True,
    )
    if delivered:
        raise BreakerTripped(f"{consecutive_errors} consecutive cycle failures: {detail}")
    logger.error("breaker tripped but no alert channel accepted it; failing loudly instead of exiting quietly")
    if original is not None:
        raise original
    raise RuntimeError(f"{consecutive_errors} consecutive cycle failures and no alert channel: {detail}")


@dataclass(frozen=True)
class LiveConfig:
    interval: str
    history_bars: int
    universe: tuple[str, ...]
    leverage: int
    rebalance: RebalanceParams
    guards: GuardParams
    kill_switch_path: Path
    # L1-07: the configured path resolves against the working directory, so it is one of possibly
    # several files meaning "stop".  The account-scoped one goes here; the guard reads the union.
    kill_switch_paths: tuple[Path, ...] = ()
    strategy_weights: dict[str, float] = field(default_factory=dict)
    poll_attempts: int = 5
    poll_interval_seconds: float = 1.0
    grace_seconds: float = 5.0
    max_consecutive_errors: int = 12
    # DL-L4: launchd's ThrottleInterval, mirrored here because the rebalance window is derived from
    # it (deploy/com.beidou.live.plist).  A relaunch can sit in that queue before this process starts.
    throttle_interval_seconds: float = 60.0
    dry_run: bool = False
    exits: ExitParams = field(default_factory=ExitParams)
    throttle: DrawdownThrottleParams = field(default_factory=DrawdownThrottleParams)
    leverage_mode: str = "fixed"  # fixed | auto (D-016)
    margin_cap: float = 0.40
    max_leverage: int = 5
    margin_buffer: float = 0.10
    # Which symbols may trade at all.  Carried here so `construction_fingerprint` can see it: it is a
    # portfolio-construction parameter in every sense that matters and was outside the digest until
    # 2026-09-09, which meant lowering it would have changed the tradable universe with nothing on the
    # running record saying so - P10 cell B's shape, one field over.
    min_history_bars: int = 720
    universe_refresh: bool = False  # D-014: re-rank once per UTC day through the UniverseProvider
    # 2026-09-09: when the registry pins a universe, the daily re-rank still RUNS and is still recorded,
    # and it no longer decides anything.  Observation kept, decision removed - the same split the
    # governance plan applies to every other construction change, applied to the population.
    universe_pinned: bool = False
    liquidity_window: int = 24
    quarantine_after: int = 0  # D-031: rejected cycles before a symbol leaves the universe (0 = off)
    probes: tuple[ProbeParams, ...] = ()  # D-019: probe books with their automatic stop rules
    max_bar_alignment_ms: int = 60_000  # how far the wake-up may sit from a real bar boundary (D-025)
    # D-026 second half: the digest could see the caps and the bands but not the vol target that sets
    # the book's size, because LiveConfig never carried it.  Changing `vol_target` changed every weight
    # and left the construction digest identical, which is the same silence the digest was built to end.
    portfolio: PortfolioParams = field(default_factory=PortfolioParams)
    # DL-X1 / KILL-R19: the collateral mode the evidence was produced under.  Asserted at startup and
    # never set - flipping an account's margin mode under an open book has consequences the loop
    # cannot evaluate, so this refuses and hands the decision back.
    expect_multi_assets: bool = True
    # M-Q06's floor, in daily-volatility units.  Alerts; never trades - the same separation
    # `risk_budget` makes, and for the same reason: rewriting position sizing from an instrument is a
    # different risk from measuring it.  Baseline measured 2026-09-07: nearest reachable liquidation
    # 242 units (TUTUSDT), so this floor is 24x below where the book actually sits.
    min_liq_distance: float = 10.0

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
        metrics_store: Any = None,
        record_metrics: bool = True,
    ) -> None:
        self.config = config
        self.model = model
        self.market = market
        self.venue = venue
        self.clock = clock
        self.store = store
        self.alerts = alerts or WebhookAlerts("")
        # DL-Q6: the loop's own recording of the metrics it could read, which is what makes research
        # and live one source rather than two (KILL-Q11).  Optional, because a paper or offline run
        # has nothing to record for.
        self.metrics_store = metrics_store
        # DL-Q6's store is what the ARMED loop could read.  A shadow reads it (coverage gating
        # needs that) and does not write it: `MetricsStore.append` is read-modify-write through
        # one tmp path per symbol, so a second writer can publish a file the first was still
        # writing, and rows a paper process added would make M-011 compare the live decision
        # against data no live decision was made on.
        self.record_metrics = record_metrics
        self.pool = pool
        self.universe_sink = universe_sink
        # DL-G9: the construction's INPUTS, written once per process into the append-only record.
        # The Phase 0 replay found six construction changes in the live log and could attribute none of
        # them: `cycles.jsonl` and `heartbeat.json` carry a digest and nothing else, a digest does not
        # invert, and the startup heartbeat that does hold the payload is overwritten by the next start.
        # A construction can only change at startup (the engine builds its model once - KILL-Q15), so
        # writing it on this process's first cycle records every change exactly once.
        self._construction_recorded = False
        self.state: LiveState = store.load()
        if self.state.cycles > 0:  # M-004: this process is a restart, not a first start
            self.state.restarts = self.state.restarts + 1
            self.state.restarted_at = utc_now_iso()
        # 2026-09-09.  A pinned universe is a DECISION, moved by governance transaction; the persisted
        # one is the last OBSERVATION of a daily re-rank, and before the pin existed that observation
        # WAS the decision.  So the pin outranks it - and the names it drops leave the way a re-rank's
        # leavers do, reduce-only through `leaving`, rather than being orphaned holding a position no
        # cycle manages.
        #
        # Without this the pin moved `registry_digest` and nothing else: the loop would report a
        # universe it is not trading, which is KILL-Q15 with the sign flipped and harder to see, since
        # the file and the digest would agree with each other and only the positions would not.
        # Measured on 2026-09-09: pinned 18 symbols, `state.universe` won, and the loop would have
        # reported the pinned list while holding the persisted one.
        self.universe: list[str]
        if config.universe_pinned:
            self.universe = list(config.universe)
            orphans = [symbol for symbol in self.state.universe if symbol not in self.universe]
            self.state.universe = list(self.universe)
            if orphans:
                self.state.leaving = list(dict.fromkeys([*self.state.leaving, *orphans]))
        else:
            persisted = list(self.state.universe) if config.universe_refresh else []
            self.universe = persisted or list(config.universe)
        self.rules: dict[str, Any] = {}
        self.exits = ExitOverlay(config.exits, config.interval_ms)
        self._alignment_alerted = False
        # DL-L2: the error streak is a property of this process, not of the book.  Persisting it is what
        # made a tripped breaker trip again on the next start (L1-03 / KILL-R29).
        self.consecutive_errors = 0
        self.missed_rebalances = 0
        self.startup_seconds = 0.0
        self._own_orders: set[str] | None = None  # D-032, seeded lazily from the trade log
        if self.state.stopped_books:  # a probe stopped in an earlier run stays stopped across restarts
            self.model = _without_books(self.model, list(self.state.stopped_books))

    # --- lifecycle ------------------------------------------------------------
    def strategies_needing_metrics(self) -> list[str]:
        """Which enabled strategies declare they read metrics, under THEIR params.

        Under their params, not under the signal's defaults: `uses_funding` learned that lesson for
        the funding term, where a grid arm that zeroes it makes the same signal need nothing.
        """
        out: list[str] = []
        for entry in getattr(self.model, "entries", ()):
            try:
                spec = get_signal(entry.id)
            except Exception:  # a mined id resolved in another process is not a reason to refuse
                continue
            if _needs_metrics(spec, entry.params):
                out.append(entry.id)
        return sorted(out)

    async def _snapshot_metrics(self) -> dict[str, Any]:
        """One poll of the REST metrics window, recorded where the decision is made (DL-Q6)."""
        if self.metrics_store is None:
            return {"stored": {}, "reason": "no metrics store wired"}
        if not self.record_metrics:
            # Recorded, not silent: a cycle that did not write says so, in the same place a cycle
            # that did says what it wrote.
            return {"stored": {}, "reason": "not the account's process; the shared record is the armed loop's"}
        client = getattr(self.market, "client", None)
        if client is None:
            return {"stored": {}, "reason": "market feed exposes no client"}
        from beidou_data.metrics_snapshot import snapshot_metrics

        return await snapshot_metrics(client, self.metrics_store, self.managed_symbols())

    def managed_symbols(self) -> list[str]:
        """The universe plus symbols that left it but still hold a position (D-014: exits follow positions)."""
        return list(dict.fromkeys([*self.universe, *self.state.leaving]))

    async def startup(self) -> Snapshot:
        started_ms = self.clock.now_ms()
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
        # D-016's invariant, checked instead of assumed - and only in `auto`, which is the mode that
        # claims it.  `derive_leverage` picks the smallest leverage keeping initial margin at the gross
        # cap under `margin_cap`, then clamps to `max_leverage`, and that clamp silently wins: at
        # max_gross 3.0 / margin_cap 0.40 / max_leverage 5 the loop settles on 5x and runs at 60%
        # initial margin against a 40% policy without raising a word.  Today it is unreachable only
        # because 2.0 = 5 x 0.40 happens to be exact; move any one of the three and the policy becomes
        # decoration.  `fixed` is deliberately exempt: there the operator set the number and margin_cap
        # is not consulted at all (the profile's own note - "was 100% at a fixed 2x" - is that state).
        if self.config.leverage_mode == "auto":
            implied_margin = self.config.guards.max_gross / max(1, self.config.max_leverage)
            if implied_margin > self.config.margin_cap + 1e-9:
                raise RuntimeError(
                    f"max_gross {self.config.guards.max_gross} at the {self.config.max_leverage}x policy cap "
                    f"needs {implied_margin:.0%} initial margin, above the {self.config.margin_cap:.0%} "
                    "margin_cap; raise max_leverage, lower max_gross, or raise margin_cap deliberately (D-016)"
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
        # DL-X1 / KILL-R19: the second account-shape refusal, in the same place and the same shape as
        # the first.  `getattr` because paper and fake venues do not implement the probe - a venue that
        # cannot report its margin mode is not evidence that the mode is wrong.
        needs = self.strategies_needing_metrics()
        if needs:
            from beidou_data.metrics_snapshot import live_coverage_bars

            coverage = (
                live_coverage_bars(self.metrics_store, self.managed_symbols(), interval_ms=self.config.interval_ms)
                if self.metrics_store is not None
                else 0
            )
            refusal = metrics_refusal(
                needs_metrics=needs,
                live_coverage_bars=coverage,
                required_bars=self.history_bars,
            )
            if refusal is not None:
                raise RuntimeError(refusal)
        margin_probe = getattr(self.venue, "margin_mode", None)
        if callable(margin_probe):
            refusal = margin_mode_refusal(
                await margin_probe(),
                expect_multi_assets=self.config.expect_multi_assets,
                symbols=self.managed_symbols(),
            )
            if refusal is not None:
                raise RuntimeError(refusal)
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
                "registry": registry_digest(self.model),
                "dry_run": self.config.dry_run,
                "foreign_positions": sorted(snapshot.foreign_positions),
            }
        )
        if snapshot.foreign_positions:
            # DL-L5's last clause: alert, not only log.  These are open positions on the venue that the
            # loop has decided not to manage, and a line in a file nobody reads is exactly what
            # "silent" means to an operator (L1-06's family).
            names = sorted(snapshot.foreign_positions)
            logger.warning("positions outside the managed universe are left untouched: %s", names)
            await self.alerts.send(
                f"北斗启动：有 {len(names)} 个持仓不在本循环管理的 universe 内，保持不动：{', '.join(names)}",
                key="foreign-positions",
            )
        else:
            self.alerts.clear("foreign-positions")
        # AC-L5: the FILTER has been right since DL-L5 and is verified against the real venue (a
        # hand-placed `manual-…` order survived a real reconcile on 2026-09-07).  Telling anyone was
        # the missing half.  A resting order the loop did not place is either the operator's or the
        # leftover of something that crashed, and both are better heard at startup than discovered in
        # a fill.
        foreign_orders = [o.client_order_id for o in snapshot.open_orders if not is_own_order(o.client_order_id)]
        if foreign_orders:
            logger.warning("open orders this loop did not place are left alone: %s", sorted(foreign_orders))
            await self.alerts.send(
                f"北斗启动：有 {len(foreign_orders)} 个挂单不是本循环下的，保持不动："
                f"{', '.join(sorted(foreign_orders))}",
                key="foreign-orders",
            )
        else:
            self.alerts.clear("foreign-orders")
        # DL-L4: what the rebalance window is built from - measured, not assumed.
        self.startup_seconds = max(0.0, (self.clock.now_ms() - started_ms) / 1000.0)
        return snapshot

    async def run(
        self, cycles: int | None = None, *, immediate: bool = False, stop: StopRequested | None = None
    ) -> int:
        """Run ``cycles`` bar cycles (forever when None).  Returns the number of cycles that completed without error."""
        await self.startup()
        attempted = succeeded = 0
        if immediate:
            bar = last_closed_bar_open_ms(self.clock.now_ms(), self.config.interval_ms)
            window = rebalance_window_seconds(
                grace_seconds=self.config.grace_seconds,
                throttle_interval=self.config.throttle_interval_seconds,
                startup_seconds=self.startup_seconds,
            )
            age = late_seconds(bar, self.config.interval_ms, at_ms=self.clock.now_ms())
            if within_rebalance_window(seconds_since_close=age, window_seconds=window):
                attempted += 1
                if await self.guarded_cycle(bar) is not None:
                    succeeded += 1
            else:
                # Reconciliation already ran in startup(); what is skipped is only the rebalance, and
                # the skip is recorded rather than inferred - L1-01's error was counting the fills that
                # happened instead of the ones that should not have (KILL-R6).
                self._record_missed_rebalance(bar, age, window)
        stop = stop or StopRequested()
        while cycles is None or attempted < cycles:
            if stop.requested:
                logger.info("stopping cleanly: %s", stop.reason)
                break
            bar = await wait_for_bar_close(self.clock, self.config.interval_ms, self.config.grace_seconds)
            if stop.requested:  # the signal arrived while we slept through the bar
                logger.info("stopping cleanly before the cycle: %s", stop.reason)
                break
            attempted += 1
            if await self.guarded_cycle(bar) is not None:
                succeeded += 1
        return succeeded

    def _record_missed_rebalance(self, bar_open_ms: int, age_seconds: float, window_seconds: float) -> None:
        self.missed_rebalances += 1
        self.store.append_cycle(
            {
                "bar_open_ms": bar_open_ms,
                "bar": datetime.fromtimestamp(bar_open_ms / 1000, tz=UTC).isoformat(),
                "phase": "SKIPPED",
                "reason": "restart outside the rebalance window",
                "late_seconds": age_seconds,
                "window_seconds": window_seconds,
                "missed_rebalances": self.missed_rebalances,
                "targets": dict(self.state.last_targets),
                "orders": [],
                "dry_run": self.config.dry_run,
            }
        )
        self.store.heartbeat({"phase": "SKIPPED", "bar_open_ms": bar_open_ms, "late_seconds": age_seconds})
        logger.warning(
            "restart was %.1fs after the bar close (window %.1fs); reconciled but did not rebalance",
            age_seconds,
            window_seconds,
        )

    async def guarded_cycle(self, bar_open_ms: int) -> dict[str, Any] | None:
        try:
            record = await self.run_cycle(bar_open_ms)
        except Exception as exc:
            self.consecutive_errors += 1
            self.store.save(self.state)
            self.store.heartbeat(
                {
                    "phase": "ERROR",
                    "bar_open_ms": bar_open_ms,
                    "error": f"{type(exc).__name__}: {exc}",
                    "consecutive_errors": self.consecutive_errors,
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
                    "consecutive_errors": self.consecutive_errors,
                    "targets": dict(self.state.last_targets),
                    "orders": [],
                    "dry_run": self.config.dry_run,
                }
            )
            logger.exception("cycle %s failed", bar_open_ms)
            # Deduplicated on the failure's type: a venue that is down for six hours says so once an
            # hour, not six times, and the operator's channel stays readable (DL-L3 / KILL-R7).
            await self.alerts.send(
                f"北斗周期失败（连续第 {self.consecutive_errors} 次）：{type(exc).__name__}: {exc}",
                key=f"cycle-failed:{type(exc).__name__}",
            )
            if self.consecutive_errors >= self.config.max_consecutive_errors:
                await breaker_stop(
                    self.alerts,
                    consecutive_errors=self.consecutive_errors,
                    detail=f"{type(exc).__name__}: {exc}",
                    original=exc,
                )
            # M-004: back off exponentially, capped at an hour, before the next attempt.  launchd's
            # ThrottleInterval only paces process restarts; a loop that stays up and retries a failing
            # venue every cycle needs its own brake, and the plan capped it at 1h.
            await self.clock.sleep(self.backoff_seconds())
            return None
        self.consecutive_errors = 0
        self.alerts.clear("cycle-failed")
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
            usable,
            inputs.funding,
            previous=self.state.last_contributions,
            funding_history=inputs.funding_history,
            # P1-01 / DL-Q1: the cross-sectional population is the universe this cycle manages, declared
            # rather than inferred from whichever frames came back.  `leaving` names are excluded: they
            # are held only to be reduced out (D-014), and research ranks against members, not exits.
            reference_symbols=list(self.universe),
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
            kill_switch=kill_switch_engaged((config.kill_switch_path, *config.kill_switch_paths)),
            equity=snapshot.equity,
            day_start_equity=self.state.day_start_equity,
            latest_bar_ms=latest_bar_ms,
            expected_bar_ms=bar_open_ms,
            interval_ms=config.interval_ms,
            params=config.guards,
        )
        # DL-Q6: record what this loop could read, at the instant it decided.  Best-effort by
        # contract - collecting data may never be the reason a book stops trading.
        metrics_snapshot = await self._snapshot_metrics()
        record: dict[str, Any] = {
            "bar_open_ms": bar_open_ms,
            "bar": datetime.fromtimestamp(bar_open_ms / 1000, tz=UTC).isoformat(),
            "as_of_ms": latest_bar_ms,
            "equity": snapshot.equity,
            # L1-10: how much of that equity is collateral rather than USDT.  Recorded, not subtracted:
            # the ladder and the vol sizing still divide by `equity`, and changing that denominator is a
            # construction decision.  What this buys is telling a BTC-driven drawdown from a real one.
            "collateral": collateral_share(equity=snapshot.equity, usdt_equity=snapshot.account.usdt_equity),
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
            # DL-G9: the same construction, restricted to what a validation report can describe, so a
            # later reader can compare the two as strings.  Cheap enough to write every cycle (16 chars),
            # and writing it only on change would make a row's meaning depend on finding an earlier row.
            "evidence_construction": evidence_construction_of(config),
            # ...and the full payload once per process, which is once per possible change.
            **({} if self._construction_recorded else {"construction_full": construction_fingerprint(config)}),
            # DL-Q0: which registry this PROCESS is running, not which one is on disk (KILL-Q15).
            "registry": registry_digest(self.model),
            # R9, the same instrument pointed at the rules: which governance thresholds this process is
            # running under.  KILL-Q15 was a registry edited on disk while the loop held the old model for
            # 96 cycles; a policy edited on disk would be the same failure with promotions attached, and
            # the only difference is that nobody would be looking.  Recorded, never read by the loop.
            "governance": policy_digest(),
            "clock": clock,
            "external_flows": flows,
            "throttle": {"scalar": scalar, "drawdown": drawdown, "equity_hwm": hwm},
            "exit_events": exit_events,
            "probes": probes,
            "targets": decision.targets,
            # The stage-1 sizing divisor per symbol, so the daily report can show that risk IS
            # adapted per symbol - in the weight, where it belongs - next to the exchange leverage,
            # which is uniform by construction and adapts to nothing (D-037).  `getattr` because
            # this is observability: a model that cannot supply it must still be able to trade.
            "asset_vol": dict(getattr(targets, "asset_vol", {}) or {}),
            # DL-X1 / M-Q06: how far the nearest liquidation is, in the daily-vol units `exits`
            # already speaks.  Foreign positions are in it: under cross margin a liquidation is an
            # account event, and D-014's "leave them alone" is about not TRADING them, not about not
            # looking at them.  They have no `asset_vol`, which is why the view counts what it cannot
            # measure separately from what the venue says is out of reach.
            "metrics_snapshot": metrics_snapshot,
            "min_liq_distance": liquidation_view(
                positions=[*snapshot.positions.values(), *snapshot.foreign_positions.values()],
                daily_vol=daily_vol_from_annual(dict(getattr(targets, "asset_vol", {}) or {})),
            ),
            # M-018: what the crowding modifier DID this bar, not merely that its input arrived.
            # `inputs.funding_history` says a frame was fetched; it stayed true for 37 cycles while the
            # modifier was inert because the process held crowding_window 0 (D-042's correction).  Two
            # counts, because under `conviction_mode: sign` a shrink only matters when it drops the
            # score under `entry_threshold` - the rest are absorbed by the +-1 rewrite.
            "crowding": _crowding_effect(self.model, inputs),
            "orders": [],
            "skipped": [],
        }
        self._construction_recorded = True
        await self._announce_guards(decision, bar_open_ms)
        # M-Q06.  Before the skip check on purpose: a book approaching liquidation while a guard has
        # stopped it trading is exactly the state an operator needs told about, and it is the state in
        # which the loop will do nothing about it on its own.
        breach = liquidation_alert(record["min_liq_distance"], threshold=self.config.min_liq_distance)
        if breach is not None:
            await self.alerts.send(breach, key="liquidation-distance")
        if decision.skip_cycle:
            self._finish_cycle(record, targets.contributions)
            return record
        liquidity = self._liquidity(usable) if config.rebalance.max_participation > 0 else None
        decision_closes = latest_closes(usable)
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
            self.store.append_trade(
                {
                    "bar_open_ms": bar_open_ms,
                    # M-Q03 reads this: how late an entry was, per fill, so the metric can be "bar-hours
                    # held by a late entry" rather than "share of fills that were late" (KILL-R6).
                    "late_seconds": late_seconds(bar_open_ms, self.config.interval_ms, at_ms=self.clock.now_ms()),
                    # L1-04 / M-Q08: the price the BACKTEST would have entered at, recorded per fill so
                    # slippage can be measured against it.  The instrument used to read the venue mark
                    # from the cycle's snapshot, which is an index price sampled when the loop woke; the
                    # backtest enters at the execution bar's open, and in a continuous market that is the
                    # decision bar's close.  Measuring against the mark answered a question M-Q08 does
                    # not ask, and the 10 bps gate it was compared to was a fee-inclusive budget.
                    "decision_close": decision_closes.get(order.symbol),
                    **report.to_dict(),
                }
            )
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
        if self.config.universe_pinned:
            # Recorded, not adopted.  `universe.json` is deliberately NOT written either: every cited
            # report records the universe fingerprint it was produced under, so writing a new one is
            # what expired restartability daily - the loop re-ranked at about 01:00Z and the dataset
            # gate then refused the next start.  The day is still marked so this runs once a day rather
            # than every cycle; what a batch window acts on is this row.
            self.state.universe_day = day
            return {
                "proposal": list(fresh),
                "entering": entered,
                "leaving": left,
                "universe": list(self.universe),
                "adopted": False,
            }
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
            await self.alerts.send(f"北斗 universe 换手：新进 {entered}，移出 {left}")
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
                f"北斗时钟：本机与交易所相差 {skew / 1000.0:+.0f}s，距 K 线边界 {alignment / 1000.0:+.1f}s"
                + (f"；自上一周期跳变 {(skew - (previous or 0.0)) / 1000.0:+.0f}s" if jumped else "")
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
                    f"北斗：{foreign['rows']} 条流水共 {foreign['total']:+.2f} USDT 来自本循环没下过的成交"
                    f"（{', '.join(sorted(foreign['by_symbol'])) or '无标的'}），已排除在策略归因之外"
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
                f"北斗外部资金变动 {flows['total']:+.2f} USDT（{flows['rows']} 条流水 {flows['by_type']}）；"
                f"日初权益与高水位已重置为 {equity:.2f}"
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
                    f"近 {probe.window_days} 天归因盈亏 {status['pnl']:.2f}"
                    f"（占权益 {status['pnl_pct']:.4f}）已跌破 -{probe.max_loss}"
                )
                if probe.halts:
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
                    await self.alerts.send(f"北斗：探针账本 {probe.book}（{probe.strategy}）已停用 —— {reason}")
                else:
                    # §3: a main sleeve that fires is DEMOTED and recounts, it does not stop trading.
                    # Recorded and alerted all the same - the transition is a governance action taken
                    # on this row, and a row nobody is told about is not a control.
                    status["status"] = "STOP_REPORTED"
                    logger.warning("main book %s hit its stop (not halted): %s", probe.book, reason)
                    await self.alerts.send(
                        f"北斗：主账本 {probe.book}（{probe.strategy}）触发 P&L stop —— {reason}。"
                        "按 §3 降级回 probe 重新计窗口，**未停止交易**"
                    )
            statuses.append(status)
        return statuses

    def backoff_seconds(self) -> float:
        """Delay after a failed cycle: 60s doubling per consecutive error, capped at 1 hour (M-004)."""
        return float(min(3600.0, 60.0 * 2 ** max(0, self.consecutive_errors - 1)))

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
            skipped = "，本周期已跳过" if decision.skip_cycle else ""
            told = "、".join(describe_guard_reason(reason) for reason in reasons)
            await self.alerts.send(f"北斗风控触发 {bar}：{told}{skipped}")
        else:
            await self.alerts.send(f"北斗风控解除 {bar}：已恢复正常交易")
        logger.warning("guard state changed: %s -> %s", self.state.last_guard_reasons, reasons)
        self.state.last_guard_reasons = reasons

    def _finish_cycle(self, record: dict[str, Any], contributions: Mapping[str, Mapping[str, float]]) -> None:
        # Reaching here means the cycle completed (a guard skip is a completed cycle too), so the error streak
        # is over.  The count used to live on the persisted state and had to be cleared before the save, or a
        # restart in the gap loaded a phantom error; since DL-L2 it is a process attribute and that whole class
        # of bug is gone with the field - a fresh process starts at zero because it cannot do otherwise.
        self.consecutive_errors = 0
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
                "registry": registry_digest(self.model),
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


def _needs_metrics(spec: Any, params: Mapping[str, Any]) -> bool:
    predicate = getattr(spec, "needs_metrics", None)
    return bool(predicate(params)) if callable(predicate) else False


def kill_switch_engaged(paths: Sequence[Path]) -> bool:
    """Engaged if ANY of them exists.  A kill switch fails toward stopping.

    Two paths, because one of them is wherever the operator's habit points: the account-scoped file
    (L1-07) and whatever the profile configured.  Reading only the new one would silently ignore an
    operator who engaged the old one, which is the same defect this fixes, aimed the other way.
    """
    return any(path.exists() for path in paths)


def engage_kill_switches(paths: Sequence[Path], reason: str) -> list[Path]:
    """Write every path, returning those written.  Both, so there is no window where it is weaker."""
    written: list[Path] = []
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reason, encoding="utf-8")
        written.append(path)
    return written


def release_kill_switches(paths: Sequence[Path]) -> list[Path]:
    """Remove every one that exists.  Release has to clear both or the operator cannot get back in."""
    removed: list[Path] = []
    for path in paths:
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def daily_vol_from_annual(annual: Mapping[str, float]) -> dict[str, float]:
    """Turn stage 1's annualised sizing divisor into the daily unit the exit overlay is written in.

    ``asset_vol`` is annualised (``beidou_alpha.portfolio.asset_vol``); ``exits.stop_loss`` is in daily
    volatility units.  Dividing by sqrt(365) puts the liquidation distance on the same axis as the
    stop, so "the stop is 6 units away and the liquidation is 40" is a sentence an operator can read
    off one cycle row.

    Which way the floor errs is worth knowing: ``asset_vol`` is clipped at ``min_asset_vol``, so a
    genuinely quiet symbol is divided by a volatility LARGER than its own and therefore reads CLOSER
    to liquidation than it is.  Conservative, and stated rather than discovered.
    """
    out: dict[str, float] = {}
    for symbol, value in annual.items():
        try:
            vol = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(vol) or vol <= 0.0:
            continue
        out[str(symbol)] = vol / math.sqrt(365.0)
    return out


def liquidation_view(*, positions: Sequence[Any], daily_vol: Mapping[str, float]) -> dict[str, Any]:
    """M-Q06 for one cycle: how close the account is to a liquidation, and how much it cannot see.

    Observability, so it may not stop a cycle - the same contract as ``asset_vol`` above and
    ``_crowding_effect`` below.  A metric that can halt the book is a worse instrument than no metric,
    so its own failure is recorded in the row instead of raised.
    """
    empty: dict[str, Any] = {
        "min_distance": None,
        "symbol": None,
        "measured": 0,
        "unreachable": 0,
        "unmeasurable": 0,
    }
    try:
        return min_liquidation_distance(list(positions), daily_vol)
    except Exception as exc:
        return empty | {"error": f"{type(exc).__name__}: {exc}"}


def liquidation_alert(view: Mapping[str, Any], *, threshold: float) -> str | None:
    """M-Q06's failure action: the message to send, or ``None`` when there is nothing to say.

    Only a *measured* distance can breach.  "Every position is out of reach" is the account's ordinary
    state under cross margin - 14 of 18 on 2026-09-07 - and the instrument's own failure is a bug
    report, not a margin call; neither is an alarm about the book, and treating them as one would
    train the operator to ignore this channel.

    Baseline, measured rather than left as the plan's "unmeasured": the nearest reachable liquidation
    sat at 242 daily-vol units (TUTUSDT), 24x this threshold.
    """
    distance = view.get("min_distance")
    if not isinstance(distance, (int, float)) or distance >= threshold:
        return None
    return (
        f"北斗：最近的强平距离只剩 {distance:.1f} 个日波动单位（{view.get('symbol')}），"
        f"已进入 {threshold:.0f} 个单位的下限内（M-Q06）；本次有 {view.get('measured')} 个持仓可测"
    )


def metrics_refusal(*, needs_metrics: Sequence[str], live_coverage_bars: int, required_bars: int) -> str | None:
    """KILL-027, made structural: a metrics signal may not trade on data live cannot see.

    Research eats the T+1 daily metrics archive; live can only read the 30-day REST window.  Ingesting
    the archive and letting a strategy use it is the purest form of "research panel strictly larger
    than the live panel" - the defect P1-01 closed for the cross-sectional operators and KILL-027 named
    in general - and it would arrive silently, as a signal that backtests well and trades on nothing.

    So the ingestion ships with its own refusal: a strategy that declares it needs metrics does not
    start until the LIVE source can answer for the history it needs.  Costs nothing to any strategy
    shipping today, because none of them declare it.
    """
    if not needs_metrics:
        return None
    if live_coverage_bars >= required_bars:
        return None
    return (
        f"{', '.join(sorted(needs_metrics))} need metrics, and the live metrics source covers "
        f"{live_coverage_bars} of the {required_bars} bars they require; research reads the T+1 archive "
        "and live cannot, so trading this would be a research panel the live loop does not have "
        "(KILL-027)"
    )


def margin_mode_refusal(mode: Mapping[str, Any], *, expect_multi_assets: bool, symbols: Sequence[str]) -> str | None:
    """KILL-R19 at startup: the reason to refuse this account, or ``None`` to proceed.

    Scoped to the symbols this book can trade.  ``positionRisk`` returns every listed contract - 736
    rows against a universe of 18 on 2026-09-07 - so an unscoped assertion would refuse to start over
    a symbol no order will ever be sent for.

    A residual this does NOT cover, recorded rather than left to be found: the universe is re-ranked
    daily (D-014), and a symbol that enters it later is not re-checked until the next restart.
    """
    wanted = set(symbols)
    problems = margin_mode_problems(
        multi_assets=bool(mode.get("multi_assets", False)),
        isolated_symbols=[str(s) for s in mode.get("isolated_symbols", ()) if str(s) in wanted],
        expect_multi_assets=expect_multi_assets,
    )
    return "; ".join(problems) if problems else None


def _crowding_effect(model: Any, inputs: Any) -> dict[str, Any]:
    """M-018, best-effort: observability must never be able to stop a cycle.

    Mirrors `asset_vol` above - a model or a signal that cannot supply it still trades, and the daily
    report renders the absence rather than reading a missing key as a zero (D-035's rule).
    """
    try:
        from beidou_alpha.panel import Panel
        from beidou_alpha.signals.tsmom import crowding_effect

        entry = next((e for e in model.entries if e.id == "tsmom"), None)
        if entry is None or inputs.funding_history is None:
            return {"enabled": False, "reason": "tsmom not enabled" if entry is None else "no funding history"}
        panel = Panel.from_frames(inputs.bars, model.interval, funding=inputs.funding_history)
        return crowding_effect(panel, entry.params)
    except Exception as exc:  # pragma: no cover - an instrument may not stop a trading cycle
        return {"enabled": False, "reason": f"{type(exc).__name__}: {exc}"}


def registry_digest(model: Any) -> str:
    """Digest of the strategy configuration the *running process* holds (DL-Q0 / KILL-Q15).

    The engine builds its model once, at startup, and never reloads it.  So editing
    ``config/alpha_registry.yaml`` changes what the file says without changing what the loop
    trades, and nothing in the live record could tell the two apart: the construction
    fingerprint (D-026) covers the portfolio layer only, the startup evidence gate runs before
    the divergence exists, and ``live verify`` rebuilds its model from the same file it is
    supposed to be checking.  On 2026-09-04 that gap opened for 93 cycles - the loop started at
    17:21Z, ``crowding_window`` went 0 -> 72 on disk at 20:03Z, and the process kept running 0.

    Twelve hex characters per cycle, next to the construction digest, so "what ran" is answerable
    from the record instead of from a commit timestamp.
    """

    def canonical(entry: Any) -> Mapping[str, Any]:
        """Params with the signal's defaults filled in, so an omitted default and an explicit one agree.

        A generated ``mined_*`` id may not be registered in this process; its raw params are
        then the honest answer, and still change when the configuration changes.
        """
        try:
            return get_signal(str(entry.id)).canonical_params(entry.params)
        except KeyError:
            return dict(entry.params)

    payload = {
        "strategies": {
            str(entry.id): {
                "book": str(getattr(entry, "book", "main")),
                "weight": float(getattr(entry, "weight", 1.0)),
                "params": canonical(entry),
            }
            for entry in getattr(model, "entries", ())
        },
        "books": dict(sorted(getattr(model, "books", {}).items())),
        "ensemble_method": str(getattr(model, "ensemble_method", "mean")),
    }
    pinned = tuple(getattr(model, "universe", ()) or ())
    if pinned:
        # 2026-09-09.  A pinned universe decides WHICH SYMBOLS this process may hold, so a loop running
        # one while the file says another is KILL-Q15's exact shape - the failure this digest exists to
        # make visible.  It was nearly missed: `registry_fingerprint` (what a research report records)
        # and this function (what the loop reports every cycle) are two different payloads, and putting
        # the universe only in the first would have left the running record unable to tell.
        #
        # Conditional, for the reason the whole `CONSTRUCTION_PAYLOAD_VERSION` apparatus exists one
        # fingerprint over: adding the KEY unconditionally moves the digest of every registry that pins
        # nothing, and `live verify` would then report the running loop as diverged from a file
        # identical to the one it loaded.  The key appears exactly when a universe is pinned, and
        # pinning one SHOULD move the digest.
        payload["universe"] = list(pinned)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


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
        "portfolio": {
            "vol_target": config.portfolio.vol_target,
            "vol_halflife": config.portfolio.vol_halflife,
            "covariance_halflife": config.portfolio.covariance_halflife,
            "min_asset_vol": config.portfolio.min_asset_vol,
            "max_scalar": config.portfolio.max_scalar,
            # v4 (2026-09-09).  `AlphaModel.eligible` excludes any symbol with fewer than this many
            # observed bars - "new listings are excluded", in its own words - so it decides WHICH
            # SYMBOLS the book may hold, and a book holding different symbols is a different book.
            # Found by asking whether the new-listing strategy (#27) was implementable: it is not
            # without lowering this, and lowering it would have moved no digest.
            "min_history_bars": config.min_history_bars,
        },
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
            "unit_mode": config.exits.unit_mode,
            "regime_window": config.exits.regime_window,
            "regime_er_cut": config.exits.regime_er_cut,
            "regime_tp_scale": config.exits.regime_tp_scale,
            "regime_side": config.exits.regime_side,
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
    # `payload_version` is deliberately NOT part of what is hashed.  Inside, it would move the digest the
    # moment it was introduced - which is the exact failure it exists to make legible (2026-09-07).
    return {"digest": digest, "payload_version": CONSTRUCTION_PAYLOAD_VERSION, **payload}


def evidence_construction(config: LiveConfig) -> dict[str, Any]:
    """The three blocks a validation report can describe, shaped exactly as a report records them.

    DL-G9's live half.  `construction_fingerprint` above covers more than a backtest can have an
    opinion about - throttle, leverage, `strategy_weights` - so it can never equal anything a report
    writes.  This is the intersection, and it is what makes "the evidence was produced under the
    construction the loop holds" answerable from two recorded strings instead of from a config that
    only exists while the process is alive.

    It duplicates `beidou_live.config.live_overlay_blocks` in shape rather than calling it, because
    that function reads the raw profile mapping and the engine holds a parsed `LiveConfig`; a test
    holds the two together so the duplication cannot drift.
    """
    return {
        "portfolio": {
            "vol_target": config.portfolio.vol_target,
            "vol_halflife": config.portfolio.vol_halflife,
            "covariance_halflife": config.portfolio.covariance_halflife,
            "min_asset_vol": config.portfolio.min_asset_vol,
            "max_scalar": config.portfolio.max_scalar,
            "max_weight": config.guards.max_weight,
            "max_gross": config.guards.max_gross,
            "no_trade_band": config.rebalance.no_trade_band,
            "no_trade_rel_band": config.rebalance.no_trade_rel_band,
        },
        "book_guards": {
            "max_weight": config.guards.max_weight,
            "max_gross": config.guards.max_gross,
            "daily_loss_pause": config.guards.daily_loss_pause,
        },
        "exits": dict(vars(config.exits)) if config.exits.enabled else None,
    }


def evidence_construction_of(config: LiveConfig) -> str:
    blocks = evidence_construction(config)
    return evidence_construction_digest(blocks["portfolio"], blocks["book_guards"], blocks["exits"])


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
