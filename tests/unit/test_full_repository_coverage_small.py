"""Behavioral coverage for small shared, market, lifecycle, and core boundaries."""

from __future__ import annotations

import asyncio
import builtins
import json
import math
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import beidou_core.health as health_module
from beidou_core.expression_component import EXPRESSION_BINDINGS, ExpressionComponent
from beidou_core.guard import EnvironmentGuard
from beidou_core.health import HealthServer
from beidou_data.klines import KLineGenerator
from beidou_data.market import (
    BarIntegrity,
    BarSequenceValidator,
    BookLevel,
    ClosedBarNormalizer,
    MarketEvent,
    MarketEventType,
    OrderBookSnapshot,
    RawLayer,
)
from beidou_data.orderbook import (
    OrderBookDiff as SequenceDiff,
)
from beidou_data.orderbook import (
    OrderBookLevel as SequenceLevel,
)
from beidou_data.orderbook import (
    OrderBookManager,
    WarmingWindow,
)
from beidou_data.orderbook import (
    OrderBookSnapshot as SequenceSnapshot,
)
from beidou_data.trading_pool_lifecycle import InstrumentScore, PoolStatus, TradingPool
from beidou_shared.config import ConfigError, ConfigProvider, Environment, redact_database_url
from beidou_shared.contracts import ContractRegistration, get_contract_registry
from beidou_shared.environment_profile import (
    EnvironmentProfile,
    EnvironmentVariant,
    SafetyBypassError,
    detect_forbidden_bypass,
)
from beidou_shared.types import InstrumentId, Price, Quantity, SchemaVersion, VenueId, VenueInstrument


def _venue_instrument() -> VenueInstrument:
    return VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))


def _raw_bar(*, closed: Any = True, open_time: Any = None, close_time: Any = None, **overrides: Any) -> dict[str, Any]:
    start = open_time or datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = close_time or start + timedelta(minutes=1)
    return {
        "open_time": start,
        "close_time": end,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "volume": "2",
        "is_closed": closed,
        "trade_count": 4,
        "quote_volume": "200",
        "taker_buy_volume": "1",
        **overrides,
    }


def _context(close: float, timeframe: str = "1m") -> dict[str, Any]:
    return {
        "features": {"close": close, "high": close + 1, "low": close - 1, "volume": 10.0},
        "instrument_id": "BTCUSDT",
        "venue_id": "BINANCE",
        "timeframe": timeframe,
    }


def _get(base: str, path: str) -> tuple[int, dict[str, Any] | str]:
    try:
        with urlopen(base + path, timeout=2) as response:  # noqa: S310 - fixed localhost test endpoint
            payload = response.read()
            return response.status, json.loads(payload)
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        finally:
            exc.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_config_provider_redaction_and_load_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert redact_database_url("") == ""
    assert redact_database_url("sqlite:///state.db") == "sqlite:///state.db"
    assert redact_database_url("postgresql://user:pw@[2001:db8::1]:5432/db").startswith(
        "postgresql://user@[2001:db8::1]:5432"
    )
    assert redact_database_url("postgresql://user:pw@localhost:not-a-port") == "<invalid-database-url>"

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    actual = config_dir / "env.paper.yaml"
    actual.write_text(
        """environment: paper
production_ladder:
  levels:
    - name: L0_PAPER
      gate: G0
      max_capital: 0
      max_leverage: 0
      min_unattended_hours: 0
""",
        encoding="utf-8",
    )
    (config_dir / "env.paper.yaml.example").write_text("environment: paper\n", encoding="utf-8")
    provider = ConfigProvider(str(config_dir))
    configured = provider.load(environment="paper")
    assert configured.source == "env-file:paper"
    assert configured.capital_ladder.levels[0].name == "L0_PAPER"
    actual.unlink()
    template = provider.load(environment="paper")
    assert template.source == "template:paper"
    assert template.environment is Environment.PAPER
    assert provider.load(explicit_path=str(tmp_path / "missing.yaml")).environment is Environment.SAFETY_ONLY

    with pytest.raises(ConfigError, match="Failed to load config"):
        provider._load_from_file(str(config_dir), source="directory")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = provider._parse_and_validate(
        {
            "environment": "testnet",
            "database": "invalid-shape",
            "postgresql": "invalid-shape",
        },
        source="direct",
    )
    assert settings.database.url == ""
    assert settings.source == "direct"


def test_config_provider_validation_warnings_and_live_endpoint_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    raw = {
        "environment": {"name": "mainnet"},
        "exchange": {
            "binance_usdm": {
                "rest_base_url": "https://testnet.binancefuture.com",
                "api_key_ref": "key-" + "a1" * 20,
                "api_secret_ref": "secret-" + "b2" * 20,
            }
        },
    }
    with (
        pytest.warns(UserWarning, match="plaintext value detected"),
        pytest.raises(ConfigError, match="cannot use testnet URL"),
    ):
        ConfigProvider()._parse_and_validate(raw, source="live-test")

    with pytest.raises(ConfigError, match="Invalid environment"):
        ConfigProvider()._parse_and_validate({"environment": "not-valid"}, source="invalid")


def test_environment_profile_contract_and_bypass_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEIDOU_ENV", raising=False)
    monkeypatch.delenv("BEIDOU_ENDPOINT", raising=False)
    monkeypatch.delenv("BEIDOU_CREDENTIAL_ID", raising=False)
    monkeypatch.delenv("BEIDOU_CAPITAL_CAP", raising=False)
    profile = EnvironmentProfile.from_env()
    assert profile.use_testnet_endpoint is False
    assert profile.is_variant(EnvironmentVariant.PAPER) is True
    profile.assert_safety_parity(EnvironmentProfile(variant=EnvironmentVariant.LIVE))
    assert detect_forbidden_bypass("if_testnet", location="beidou_core/engine.py")
    assert detect_forbidden_bypass("if_testnet", location="beidou_shared/environment_profile.py") == []
    assert detect_forbidden_bypass("testnet_override", location="test_policy.py") == []
    error = SafetyBypassError("engine.py:10", "testnet_override")
    assert error.location == "engine.py:10"
    assert "SAFETY_BYPASS" in str(error)
    monkeypatch.setenv("BEIDOU_ENV", "testnet")
    monkeypatch.setenv("BEIDOU_CAPITAL_CAP", "123.5")
    testnet = EnvironmentProfile.from_env()
    assert testnet.use_testnet_endpoint is True
    assert testnet.capital_cap == 123.5


def test_contract_registry_lookup_and_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    import beidou_shared.contracts as contracts_module

    registration = ContractRegistration(contract_name="Order", schema_version=SchemaVersion("2.0"))
    registry = contracts_module.ContractRegistry()
    registry.register(registration)
    assert registry.get_version("Order", SchemaVersion("missing")) is None
    monkeypatch.setattr(contracts_module, "_global_registry", None)
    first = get_contract_registry()
    assert get_contract_registry() is first


def test_market_snapshot_and_normalizer_integrity_edges() -> None:
    vi = _venue_instrument()
    empty = OrderBookSnapshot(venue_instrument=vi)
    assert empty.mid_price() is None
    assert empty.spread() is None
    assert empty.spread_bps() is None
    zero_ask = OrderBookSnapshot(
        venue_instrument=vi,
        bids=[BookLevel(Price(amount="1"), Quantity(amount="1"))],
        asks=[BookLevel(Price(amount="0"), Quantity(amount="1"))],
    )
    assert zero_ask.spread_bps() is None
    populated = OrderBookSnapshot(
        venue_instrument=vi,
        bids=[BookLevel(Price(amount="99"), Quantity(amount="2"))],
        asks=[BookLevel(Price(amount="101"), Quantity(amount="3"))],
    )
    assert populated.spread_bps() > 0
    populated.apply_update("BUY", Price(amount="99"), Quantity(amount="4"))
    populated.apply_update("SELL", Price(amount="100"), Quantity(amount="1"))
    assert populated.asks[0].price.amount == "100"
    assert empty.imbalance() == 0.0

    normalizer = ClosedBarNormalizer()
    for raw, detail in (({}, "Missing open_time"), ({"open_time": 1}, "Missing open_time")):
        result = normalizer.normalize(raw, vi)
        assert result.status is BarIntegrity.INVALID
        assert detail in result.detail

    invalid_inputs = [
        {"open": True},
        {"open": "bad"},
        {"open": "nan"},
        {"volume": "-1"},
        {"high": "98"},
        {"low": "102", "high": "103"},
    ]
    for update in invalid_inputs:
        result = normalizer.normalize(_raw_bar(**update), vi)
        assert result.status is BarIntegrity.INVALID
        assert result.bar is None

    not_closed = normalizer.normalize(_raw_bar(closed="false"), vi)
    assert not_closed.status is BarIntegrity.NOT_CLOSED
    closed = normalizer.normalize(_raw_bar(closed="true"), vi)
    assert closed.status is BarIntegrity.OK
    duplicate = normalizer.normalize(_raw_bar(closed=True), vi)
    assert duplicate.status is BarIntegrity.DUPLICATE
    timestamp_bar = normalizer.normalize(_raw_bar(open_time=1_767_229_200_000, close_time=1_767_229_260_000), vi)
    assert timestamp_bar.status is BarIntegrity.OK
    assert normalizer.normalize(_raw_bar(trade_count="bad"), vi).status is BarIntegrity.INVALID
    revised = normalizer.revise(closed.bar, _raw_bar(close="101"))
    assert revised.status is BarIntegrity.REVISION
    assert revised.bar is not None and revised.bar.revision == 1
    bad_revision = normalizer.revise(closed.bar, _raw_bar(open="bad"))
    assert bad_revision.status is BarIntegrity.INVALID


def test_market_sequence_validation_and_raw_layer() -> None:
    vi = _venue_instrument()
    normalizer = ClosedBarNormalizer()
    base = normalizer.normalize(_raw_bar(), vi).bar
    assert base is not None
    validator = BarSequenceValidator(max_stale_seconds=10)
    now = base.available_at + timedelta(seconds=1)
    assert validator.validate(base, now=now) is BarIntegrity.OK
    stale = normalizer.normalize(_raw_bar(open_time=base.open_time + timedelta(minutes=1)), vi).bar
    assert stale is not None
    assert validator.validate(stale, now=stale.available_at + timedelta(seconds=11)) is BarIntegrity.STALE
    out_of_order = normalizer.normalize(_raw_bar(open_time=base.open_time - timedelta(minutes=1)), vi).bar
    assert out_of_order is not None
    assert validator.validate(out_of_order, now=out_of_order.available_at) is BarIntegrity.OUT_OF_ORDER
    gap = normalizer.normalize(_raw_bar(open_time=base.open_time + timedelta(minutes=10)), vi).bar
    assert gap is not None
    assert validator.validate(gap, now=gap.available_at) is BarIntegrity.GAP_DETECTED
    assert BarSequenceValidator._interval_seconds("15m") == 900.0
    assert BarSequenceValidator._interval_seconds("bad") == 60.0
    assert BarSequenceValidator._interval_seconds("2x") == 60.0

    event = MarketEvent(
        event_type=MarketEventType.TRADE,
        venue_instrument=vi,
        event_time=base.open_time,
        schema_version=SchemaVersion("2.0.0"),
    )
    layer = RawLayer()
    layer.ingest(event)
    assert layer.replay_range(base.open_time - timedelta(seconds=1), base.open_time, VenueId("OTHER")) == []
    assert layer.replay_range(base.open_time - timedelta(seconds=1), base.open_time, VenueId("BINANCE")) == [event]


def test_orderbook_sequence_snapshot_diff_guards() -> None:
    manager = OrderBookManager(max_depth=2)
    snapshot = SequenceSnapshot(
        instrument_id="BTCUSDT",
        venue_id="BINANCE",
        bids=[SequenceLevel(100, 2), SequenceLevel(99, 1)],
        asks=[SequenceLevel(101, 2)],
        sequence=10,
    )
    manager.apply_snapshot(snapshot)
    assert manager.apply_diff(SequenceDiff("BTCUSDT", 10, 9, [], [])) is True
    assert manager.apply_diff(SequenceDiff("BTCUSDT", 12, 10, [], [])) is False
    assert manager.apply_diff(SequenceDiff("BTCUSDT", 9, 8, [], [])) is True
    assert (
        manager.apply_diff(
            SequenceDiff(
                "BTCUSDT",
                11,
                10,
                [SequenceLevel(0, 0), SequenceLevel(100, 0), SequenceLevel(98, 3)],
                [SequenceLevel(0, 0), SequenceLevel(101, 0), SequenceLevel(102, 3)],
            )
        )
        is True
    )
    assert manager.get_snapshot() is not None
    assert manager.get_snapshot().sequence == 11
    assert [level.price for level in manager.get_snapshot().bids] == [99, 98]

    empty_snapshot = SequenceSnapshot("BTCUSDT", "BINANCE", [], [], sequence=1)
    assert empty_snapshot.best_bid() == 0.0
    assert math.isinf(empty_snapshot.best_ask())
    nonpositive_ask = SequenceSnapshot("BTCUSDT", "BINANCE", [], [SequenceLevel(-1, 1)], sequence=1)
    assert nonpositive_ask.spread_bps() == 0.0
    assert nonpositive_ask.mid_price() == -0.5
    regular_snapshot = SequenceSnapshot(
        "BTCUSDT", "BINANCE", [SequenceLevel(100, 1)], [SequenceLevel(101, 1)], sequence=1
    )
    assert regular_snapshot.spread_bps() > 0
    assert regular_snapshot.mid_price() == 100.5

    no_snapshot = OrderBookManager()
    assert no_snapshot.needs_resync() is True
    assert no_snapshot.apply_diff(SequenceDiff("BTCUSDT", 1, 0, [], [])) is False
    window = WarmingWindow("BTCUSDT", min_warmup_seconds=2, min_events=2)
    window.start(10.0)
    window.record_event(11.0)
    assert window.can_trade() is False
    window.record_event(12.0)
    window.sequence_validated = True
    assert window.can_trade() is True


def test_kline_generator_late_revision_and_daily_bucket() -> None:
    vi = _venue_instrument()
    generator = KLineGenerator("1d")
    start = datetime(2026, 1, 1, 12, 3, tzinfo=timezone.utc)
    generator.process_tick(vi, Price(amount="100"), Quantity(amount="1"), start)
    generator.process_tick(
        vi,
        Price(amount="101"),
        Quantity(amount="1"),
        start + timedelta(days=1, seconds=1),
    )
    assert generator.get_klines(vi)
    assert (
        generator.process_tick(
            vi,
            Price(amount="99"),
            Quantity(amount="1"),
            start - timedelta(days=1),
        )
        is None
    )
    assert generator.rejected_ticks == 1
    current = generator.get_current_bar(vi)
    assert current is not None
    generator._current.clear()
    assert generator.process_tick(vi, Price(amount="98"), Quantity(amount="1"), start - timedelta(days=2)) is None
    assert generator.rejected_ticks == 2
    with pytest.raises(ValueError, match="No kline"):
        generator.revise(vi, datetime(2020, 1, 1, tzinfo=timezone.utc), current)
    revised = generator.revise(vi, generator.get_klines(vi)[0].open_time, generator.get_klines(vi)[0])
    assert revised.revision_number == 1


def test_trading_pool_restore_persistence_and_fail_closed_lifecycle() -> None:
    invalid_state = [
        {"instrument_id": "", "status": "ACTIVE"},
        {
            "instrument_id": "BAD",
            "status": "UNKNOWN",
            "score_detail": {
                "observing_since": "bad",
                "promoted_at": "bad",
                "scores": ["bad", 0.4],
                "historical_seed": {"days": 2},
                "quarantine_reason": "old",
            },
        },
    ]
    restored = TradingPool(initial_state=invalid_state)
    bad = restored.add("BAD")
    assert bad.status is PoolStatus.OBSERVING
    assert bad.scores[0].overall == 0.4

    events: list[dict[str, Any]] = []
    pool = TradingPool(max_instruments=1, event_sink=events.append)
    pool.add("BTCUSDT")
    pool.add("ETHUSDT")
    assert pool.score("missing", InstrumentScore("missing")) is None
    entry = pool._pool["BTCUSDT"]
    entry.min_observation_hours = 0
    pool.score("BTCUSDT", InstrumentScore("BTCUSDT", 0.8, 0.8, 0.8, 0.8, 0.8))
    assert pool.try_promote("BTCUSDT") is True
    assert pool.activate("BTCUSDT") is True
    assert pool.is_tradable("BTCUSDT") is True
    pool.set_max_position_notional("BTCUSDT", 100)
    pool.update_capacity("BTCUSDT", 150)
    assert pool.is_tradable("BTCUSDT") is False
    assert pool.activate("ETHUSDT") is False
    pool.quarantine("missing", "not-found")
    pool.quarantine("BTCUSDT", "manual")
    assert pool._pool["BTCUSDT"].quarantine_reason == "manual"
    assert events

    failing_pool = TradingPool(event_sink=lambda _event: (_ for _ in ()).throw(RuntimeError("disk")))
    failing_pool.add("X")
    failing_pool.score("X", InstrumentScore("X", 0.7, 0.7, 0.7, 0.7, 0.7))
    assert failing_pool._pool["X"].scores
    for weights in (
        {"spread": 1},
        {"spread": "bad", "depth": 0, "volume": 0, "stability": 0, "capacity": 0},
    ):
        failing_pool.set_score_weights(weights)  # type: ignore[arg-type]


def test_trading_pool_quarantine_recovery_and_score_guards() -> None:
    pool = TradingPool()
    for instrument in ("none", "nan", "range", "low", "promoted"):
        pool.add(instrument).min_observation_hours = 0
    assert pool.try_promote("missing") is False
    pool._pool["promoted"].status = PoolStatus.PROMOTED
    assert pool.try_promote("promoted") is False
    assert pool.try_promote("none") is False
    pool._pool["nan"].scores.append(InstrumentScore("nan", overall=math.nan))
    assert pool.try_promote("nan") is False
    score = InstrumentScore("range", overall=2.0)
    score.spread_score = score.depth_score = score.volume_score = score.stability_score = score.capacity_score = 0.8
    pool._pool["range"].scores.append(score)
    assert pool.try_promote("range") is False
    low = InstrumentScore("low", overall=0.2)
    low.spread_score = low.depth_score = low.volume_score = low.stability_score = low.capacity_score = 0.2
    pool._pool["low"].scores.append(low)
    assert pool.try_promote("low") is False
    attribute_nan = InstrumentScore("low", overall=0.8)
    attribute_nan.spread_score = math.nan
    pool._pool["low"].scores[-1] = attribute_nan
    assert pool.try_promote("low") is False

    quarantined = pool._pool["none"]
    quarantined.status = PoolStatus.QUARANTINED
    quarantined.scores = [InstrumentScore("none", overall=0.1) for _ in range(3)]
    assert pool.try_promote("none") is False
    quarantined.scores = [InstrumentScore("none", overall=0.8) for _ in range(3)]
    for item in quarantined.scores:
        item.spread_score = item.depth_score = item.volume_score = item.stability_score = item.capacity_score = 0.8
    assert pool.try_promote("none") is True
    assert pool.activate("none") is True
    pool.update_capacity("none", 5)
    assert pool.is_tradable("none") is True
    assert pool.seed_historical_observation("none", 0.8, evidence={"days": 1}) is False


def test_trading_pool_restore_and_capacity_rejection_edges() -> None:
    naive = datetime.fromisoformat("2026-01-01").isoformat()
    pool = TradingPool(
        max_instruments=1,
        initial_state=[
            {
                "instrument_id": "RESTORED",
                "status": "ACTIVE",
                "score_detail": {"observing_since": naive, "promoted_at": naive, "scores": [0.8]},
            }
        ],
    )
    restored = pool._pool["RESTORED"]
    assert restored.observing_since.tzinfo is not None
    assert restored.promoted_at is not None and restored.promoted_at.tzinfo is not None
    restored.status = PoolStatus.PROMOTED
    assert pool.activate("RESTORED") is True
    assert pool.is_tradable("unknown") is False
    pool.add("BLOCKED")
    pool._pool["BLOCKED"].status = PoolStatus.PROMOTED
    assert pool.activate("BLOCKED") is False
    pool.add("QUARANTINED")
    assert pool.seed_historical_observation("QUARANTINED", 0.8, evidence={"days": "bad"}) is False
    assert pool._pool["QUARANTINED"].historical_seed is None


def test_expression_component_bind_backfill_and_failure_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    EXPRESSION_BINDINGS.pop("bound", None)
    ExpressionComponent.bind("bound", "close", "FILTER")
    bound = ExpressionComponent(factor_id="bound")
    assert bound.validate() is True
    with pytest.raises(ValueError, match="requires factor_id"):
        ExpressionComponent(expression_string="close")
    monkeypatch.setenv("BEIDOU_FACTOR_BACKFILL_BARS", "bad")
    malformed = ExpressionComponent(factor_id="malformed", expression_string="close")
    assert malformed._backfill_lookback == 400

    async def good_source(_symbol: str, _timeframe: str, _lookback: int) -> list[dict[str, Any]]:
        return [
            {"close": str(100 + index), "high": str(101 + index), "low": str(99 + index), "volume": "2"}
            for index in range(35)
        ] + [{"close": "bad"}, {"close": "nan"}, None]

    async def short_source(*_args: Any) -> list[dict[str, Any]]:
        return [{"close": "100"}] * 2

    async def failing_source(*_args: Any) -> list[dict[str, Any]]:
        raise RuntimeError("backfill unavailable")

    async def run() -> None:
        comp = ExpressionComponent(factor_id="backfill", expression_string="close")
        comp.set_backfill_source(good_source)
        signal = await comp.generate(_context(134.0, "5m"))
        assert signal.direction.value in {"NO_ACTION", "LONG", "SHORT"}
        assert ("BTCUSDT", "5m") in comp._backfill_attempted
        assert await comp._maybe_backfill("BTCUSDT", "5m", comp._hist_for("5m")) is False

        short = ExpressionComponent(factor_id="short", expression_string="close")
        short.set_backfill_source(short_source)
        assert (await short._maybe_backfill("BTCUSDT", "1m", short._hist_for("1m"))) is False
        bad = ExpressionComponent(factor_id="bad", expression_string="close")
        bad.set_backfill_source(failing_source)
        assert await bad._maybe_backfill("BTCUSDT", "1m", bad._hist_for("1m")) is False
        missing = await comp.generate({"features": {}, "instrument_id": "BTCUSDT"})
        assert missing.direction.value == "NO_ACTION"
        comp._expr = SimpleNamespace(evaluate_series=lambda _features: [float("nan")])
        nonfinite = await comp.generate(_context(135.0, "15m"))
        assert nonfinite.direction.value == "NO_ACTION"
        features = comp._build_feature_dict(
            {"close": [0.0, 100.0], "high": [1.0, 101.0], "low": [-1.0, 99.0], "volume": [1.0, 2.0], "value": []}
        )
        assert math.isnan(features["log_return"][1])
        assert math.isnan(features["spread"][0])

        invalid_expression = ExpressionComponent(factor_id="invalid", expression_string="missing_primitive(close)")
        assert invalid_expression.validate() is False
        assert (await invalid_expression.generate(_context(100.0))).direction.value == "NO_ACTION"

        nonfinite_rows = ExpressionComponent(factor_id="nonfinite-rows", expression_string="close")
        nonfinite_rows.set_backfill_source(good_source)
        nonfinite_rows._backfill_source = lambda *_args: good_source("BTCUSDT", "1m", 400)
        original_evaluator = nonfinite_rows._expr
        nonfinite_rows._expr = SimpleNamespace(evaluate_series=lambda _features: [1.0, "bad", float("nan")])
        assert await nonfinite_rows._maybe_backfill("BTCUSDT", "1m", nonfinite_rows._hist_for("1m")) is True
        assert original_evaluator is not None

        no_expression = ExpressionComponent(factor_id="no-expression", expression_string="missing_primitive(close)")
        no_expression.set_backfill_source(good_source)
        assert await no_expression._maybe_backfill("BTCUSDT", "1m", no_expression._hist_for("1m")) is False

        async def fill_and_generate(component: ExpressionComponent, values: list[float]) -> Any:
            history = component._hist_for("1m")
            history["close"].extend([100.0] * 30)
            history["high"].extend([101.0] * 30)
            history["low"].extend([99.0] * 30)
            history["volume"].extend([1.0] * 30)
            history["value"].extend(values)
            return await component.generate(_context(100.0))

        failing = ExpressionComponent(factor_id="failing", expression_string="close")
        failing._expr = SimpleNamespace(evaluate_series=lambda _features: (_ for _ in ()).throw(RuntimeError("eval")))
        failing_signal = await fill_and_generate(failing, [])
        assert failing_signal.direction.value == "NO_ACTION"

        warming = ExpressionComponent(factor_id="warming", expression_string="close")
        warming._expr = SimpleNamespace(evaluate_series=lambda _features: [1.0])
        warming_signal = await fill_and_generate(warming, [])
        assert warming_signal.direction.value == "NO_ACTION"

        flat = ExpressionComponent(factor_id="flat", expression_string="close")
        flat._expr = SimpleNamespace(evaluate_series=lambda _features: [1.0])
        flat_signal = await fill_and_generate(flat, [1.0] * 29)
        assert flat_signal.direction.value == "NO_ACTION"

    asyncio.run(run())


def test_health_server_resume_and_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    server = HealthServer(port=_free_port())
    server.set_resume_handler(lambda: (True, "AUTHORIZED"))
    server.start()
    base = f"http://127.0.0.1:{server._port}"
    try:
        status, body = _get(base, "/resume")
        assert status == 200 and isinstance(body, dict) and body["resumed"] is True
        server.set_resume_handler(lambda: (False, "STALE"))
        status, body = _get(base, "/resume")
        assert status == 403 and isinstance(body, dict) and body["reason"] == "STALE"
    finally:
        server.stop()
    unwired = HealthServer(port=_free_port())
    unwired.start()
    try:
        status, body = _get(f"http://127.0.0.1:{unwired._port}", "/resume")
        assert status == 503 and isinstance(body, dict) and body["reason"] == "RESUME_HANDLER_NOT_WIRED"
    finally:
        unwired.stop()
    stopped = HealthServer(port=_free_port())
    stopped.stop()

    class BusyHTTPServer:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise OSError("busy")

    monkeypatch.setattr(health_module, "HTTPServer", BusyHTTPServer)
    monkeypatch.setattr(health_module.time, "sleep", lambda _seconds: None)
    with pytest.raises(OSError, match="unavailable after 3 retries"):
        HealthServer(port=_free_port()).start()


def test_health_stop_joins_live_thread() -> None:
    class FakeServer:
        def __init__(self) -> None:
            self.closed = False

        def shutdown(self) -> None:
            self.closed = True

        def server_close(self) -> None:
            self.closed = True

    class FakeThread:
        def __init__(self) -> None:
            self.joined = False

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float) -> None:
            assert timeout == 2.0
            self.joined = True

    server = HealthServer()
    fake_thread = FakeThread()
    server._server = FakeServer()
    server._thread = fake_thread  # type: ignore[assignment]
    server.stop()
    assert fake_thread.joined is True
    assert server._server is None


def test_environment_guard_remaining_failure_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("environment: paper\n", encoding="utf-8")
    configured = EnvironmentGuard(mode="paper", config_path=str(config), commit="fixed")
    assert configured._compute_config_hash() != "NO_CONFIG"
    assert configured._get_git_commit() == "fixed"
    assert configured.run_all_checks(persist_audit=False).status.value == "PASS"

    missing_secret = EnvironmentGuard(
        mode="testnet", rest_url="https://testnet.binancefuture.com", api_key="a" * 64, api_secret=""
    )
    assert missing_secret.check_trading_credentials() is False
    assert missing_secret.check_write_mode_requirements() is False
    assert EnvironmentGuard(mode="paper").check_write_mode_requirements() is True

    import beidou_core.guard as guard_module

    monkeypatch.setattr(
        guard_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )
    assert EnvironmentGuard(mode="paper")._get_git_commit() == "UNKNOWN"
    unreadable = tmp_path / "unreadable.txt"
    unreadable.write_text("ALL PASS", encoding="utf-8")
    excluded = tmp_path / ".venv"
    excluded.mkdir()
    (excluded / "ignored.md").write_text("ALL PASS", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "ignored.md").write_text("ALL PASS", encoding="utf-8")
    (tmp_path / "binary.pyc").write_text("ALL PASS", encoding="utf-8")
    original_open = builtins.open

    def selective_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path).endswith("unreadable.txt"):
            raise PermissionError("denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(guard_module, "open", selective_open, raising=False)
    assert EnvironmentGuard.scan_for_forbidden_claims(str(tmp_path)) == []
