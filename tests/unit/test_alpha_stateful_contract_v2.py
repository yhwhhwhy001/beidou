"""ARO-06A: stateful Alpha target, provider, and audit contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from apps.alpha_app import BoundLocalData, OfflineAlphaApp, OfflineAlphaResult, StatefulOfflineAlphaResult
from beidou_shared.contracts.alpha_execution import (
    AlphaStateContractError,
    AlphaTargetSemantics,
    BoundCostSnapshot,
    BoundPositionSnapshot,
)
from beidou_shared.contracts.experiment import DatasetRef
from beidou_shared.types import InstrumentId, OrderSide, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha import TrendAlpha
from beidou_strategy.alpha.contracts import AlphaForecast

DECISION_AT = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
POSITION_SOURCE = "a" * 64
COST_SOURCE = "b" * 64


def _data(*, decision_at: datetime = DECISION_AT) -> BoundLocalData:
    return BoundLocalData(
        dataset=DatasetRef("stateful-local", SchemaVersion("1"), "dataset-content"),
        instrument_id=InstrumentId("BTCUSDT"),
        venue_id=VenueId("LOCAL"),
        closes=tuple(100.0 + index * 0.25 for index in range(60)),
        observed_at=decision_at,
    )


def _position(**changes: Any) -> BoundPositionSnapshot:
    values = {
        "strategy_id": StrategyId("offline-alpha-v1"),
        "instrument_id": InstrumentId("BTCUSDT"),
        "venue_id": VenueId("LOCAL"),
        "current_weight": -0.35,
        "observed_at": DECISION_AT - timedelta(minutes=1),
        "valid_until": DECISION_AT + timedelta(minutes=1),
        "source_artifact_digest": POSITION_SOURCE,
        "source_class": "STRATEGY_SLEEVE_POSITION_FACT",
    }
    return BoundPositionSnapshot(**(values | changes))


def _costs(**changes: Any) -> BoundCostSnapshot:
    values = {
        "instrument_id": InstrumentId("BTCUSDT"),
        "venue_id": VenueId("LOCAL"),
        "expected_fee_bps": 1.0,
        "expected_slippage_bps": 2.0,
        "expected_funding_bps": -0.25,
        "observed_at": DECISION_AT - timedelta(minutes=2),
        "valid_until": DECISION_AT + timedelta(minutes=2),
        "source_artifact_digest": COST_SOURCE,
        "source_class": "COST_MODEL_EVIDENCE",
    }
    return BoundCostSnapshot(**(values | changes))


def _forecast(*, side: str | OrderSide = "NO_ACTION", raw_score: float = 0.1) -> AlphaForecast:
    expected_return = raw_score * 0.02
    total_cost_bps = 1.0 + 2.0 - 0.25
    return AlphaForecast(
        alpha_id="fixed-test-alpha",
        strategy_id=StrategyId("offline-alpha-v1"),
        instrument_id=InstrumentId("BTCUSDT"),
        venue_id=VenueId("LOCAL"),
        side=side,
        raw_score=raw_score,
        expected_return=expected_return,
        expected_return_after_cost=expected_return - total_cost_bps / 10000.0,
        expected_volatility=0.1,
        horizon_seconds=3600,
        probability_positive=0.5,
        confidence=0.5,
        uncertainty=0.5,
        expected_fee_bps=1.0,
        expected_slippage_bps=2.0,
        expected_funding_bps=-0.25,
        market_beta=None,
        regime_fit=0.5,
        capacity_score=1.0,
        model_version=SchemaVersion("3.0.0"),
        policy_version="fixed-policy",
        feature_hash="pure-market-features",
        timestamp=DECISION_AT,
        cost_source_hash=COST_SOURCE,
    )


class _FixedAlpha:
    def __init__(self, forecast: AlphaForecast) -> None:
        self.forecast = forecast

    def generate_forecast(self, _context: dict[str, object]) -> AlphaForecast:
        return self.forecast


class _InvalidAlpha:
    def generate_forecast(self, _context: dict[str, object]) -> object:
        return object()


class _RecordingTrendAlpha:
    def __init__(self) -> None:
        self.contexts: list[dict[str, object]] = []
        self.forecasts: list[AlphaForecast] = []

    def generate_forecast(self, context: dict[str, object]) -> AlphaForecast:
        self.contexts.append(context)
        forecast = TrendAlpha().generate_forecast(context)
        self.forecasts.append(forecast)
        return forecast


def _error_code(exc: pytest.ExceptionInfo[AlphaStateContractError]) -> str:
    return exc.value.code


def test_snapshot_bindings_cover_values_scope_source_and_validity() -> None:
    position = _position()
    cost = _costs()
    assert len(position.binding_digest) == 64
    assert len(cost.binding_digest) == 64
    assert replace(position, current_weight=0.25).binding_digest != position.binding_digest
    changed_validity = replace(position, valid_until=position.valid_until + timedelta(seconds=1))
    assert changed_validity.binding_digest != position.binding_digest
    assert replace(cost, expected_fee_bps=1.1).binding_digest != cost.binding_digest
    assert replace(cost, observed_at=cost.observed_at - timedelta(seconds=1)).binding_digest != cost.binding_digest


@pytest.mark.parametrize(
    ("factory", "changes", "code"),
    [
        (_position, {"strategy_id": StrategyId(" ")}, "POSITION_SCOPE_INVALID"),
        (_position, {"observed_at": DECISION_AT.replace(tzinfo=None)}, "POSITION_TIMESTAMP_NOT_AWARE"),
        (_position, {"current_weight": float("nan")}, "POSITION_VALUE_NOT_FINITE"),
        (_position, {"current_weight": "0.25"}, "POSITION_VALUE_NOT_FINITE"),
        (_position, {"source_artifact_digest": "0" * 64}, "POSITION_SOURCE_DIGEST_INVALID"),
        (_position, {"source_artifact_digest": int("1" * 64)}, "POSITION_SOURCE_DIGEST_INVALID"),
        (_position, {"source_class": "UNKNOWN"}, "POSITION_SOURCE_CLASS_INVALID"),
        (_costs, {"expected_fee_bps": -0.1}, "COST_VALUE_NEGATIVE"),
        (_costs, {"expected_funding_bps": float("inf")}, "COST_VALUE_NOT_FINITE"),
        (_costs, {"valid_until": DECISION_AT - timedelta(minutes=3)}, "COST_VALIDITY_WINDOW_INVALID"),
        (_costs, {"source_artifact_digest": "not-a-digest"}, "COST_SOURCE_DIGEST_INVALID"),
        (_costs, {"source_class": "EXECUTION_ONLY"}, "COST_SOURCE_CLASS_INVALID"),
    ],
)
def test_snapshot_construction_fails_with_typed_codes(factory: Any, changes: dict[str, object], code: str) -> None:
    with pytest.raises(AlphaStateContractError) as exc:
        factory(**changes)
    assert _error_code(exc) == code


def test_no_action_preserves_verified_position_and_returns_complete_binding() -> None:
    result = OfflineAlphaApp(alpha=_FixedAlpha(_forecast())).evaluate_stateful(
        _data(), position=_position(), costs=_costs()
    )
    assert isinstance(result, StatefulOfflineAlphaResult)
    assert result.target.target_weight == pytest.approx(-0.35)
    assert result.forecast_side == "NO_ACTION"
    assert result.target_semantics is AlphaTargetSemantics.PRESERVE_VERIFIED_POSITION
    assert result.decision_at == DECISION_AT
    assert result.position_binding_digest == _position().binding_digest
    assert result.cost_binding_digest == _costs().binding_digest
    assert result.position_observed_at == _position().observed_at
    assert result.position_valid_until == _position().valid_until
    assert result.cost_observed_at == _costs().observed_at
    assert result.cost_valid_until == _costs().valid_until
    assert len(result.result_binding_digest) == 64


@pytest.mark.parametrize(("side", "score"), [(OrderSide.BUY, 0.4), (OrderSide.SELL, -0.4)])
def test_directional_forecast_uses_exact_raw_score(side: OrderSide, score: float) -> None:
    result = OfflineAlphaApp(alpha=_FixedAlpha(_forecast(side=side, raw_score=score))).evaluate_stateful(
        _data(), position=_position(), costs=_costs()
    )
    assert result.target.target_weight == pytest.approx(score)
    assert result.target_semantics is AlphaTargetSemantics.FORECAST_RAW_SCORE


@pytest.mark.parametrize(
    ("position", "costs", "code"),
    [
        (None, _costs(), "POSITION_SNAPSHOT_REQUIRED"),
        (_position(), None, "COST_SNAPSHOT_REQUIRED"),
        (
            _position(
                observed_at=DECISION_AT + timedelta(seconds=1),
                valid_until=DECISION_AT + timedelta(minutes=1),
            ),
            _costs(),
            "POSITION_FACT_FROM_FUTURE",
        ),
        (
            _position(
                observed_at=DECISION_AT - timedelta(minutes=2),
                valid_until=DECISION_AT - timedelta(seconds=1),
            ),
            _costs(),
            "POSITION_FACT_EXPIRED",
        ),
        (_position(instrument_id=InstrumentId("ETHUSDT")), _costs(), "POSITION_SCOPE_MISMATCH"),
        (_position(), _costs(observed_at=DECISION_AT + timedelta(seconds=1)), "COST_FACT_FROM_FUTURE"),
        (
            _position(),
            _costs(
                observed_at=DECISION_AT - timedelta(minutes=3),
                valid_until=DECISION_AT - timedelta(seconds=1),
            ),
            "COST_FACT_EXPIRED",
        ),
        (_position(), _costs(venue_id=VenueId("BINANCE_USDM")), "COST_SCOPE_MISMATCH"),
    ],
)
def test_missing_stale_future_and_cross_scope_inputs_fail_closed(
    position: BoundPositionSnapshot | None, costs: BoundCostSnapshot | None, code: str
) -> None:
    with pytest.raises(AlphaStateContractError) as exc:
        OfflineAlphaApp(alpha=_FixedAlpha(_forecast())).evaluate_stateful(_data(), position=position, costs=costs)
    assert _error_code(exc) == code


@pytest.mark.parametrize(
    ("forecast", "code"),
    [
        (replace(_forecast(), strategy_id=StrategyId("other")), "FORECAST_SCOPE_BINDING_MISMATCH"),
        (replace(_forecast(), instrument_id=InstrumentId("ETHUSDT")), "FORECAST_SCOPE_BINDING_MISMATCH"),
        (replace(_forecast(), venue_id=VenueId("BINANCE_USDM")), "FORECAST_SCOPE_BINDING_MISMATCH"),
        (replace(_forecast(), timestamp=DECISION_AT + timedelta(seconds=1)), "FORECAST_TIMESTAMP_BINDING_MISMATCH"),
        (_forecast(side=OrderSide.BUY, raw_score=-0.2), "FORECAST_DIRECTION_BINDING_MISMATCH"),
        (_forecast(side=OrderSide.SELL, raw_score=0.2), "FORECAST_DIRECTION_BINDING_MISMATCH"),
        (_forecast(side=OrderSide.BUY, raw_score=1.1), "FORECAST_DIRECTION_BINDING_MISMATCH"),
        (replace(_forecast(), side="FLAT"), "UNSUPPORTED_FORECAST_SIDE"),
        (replace(_forecast(), cost_source_hash="c" * 64), "FORECAST_COST_BINDING_MISMATCH"),
        (replace(_forecast(), expected_fee_bps=1.5), "FORECAST_COST_BINDING_MISMATCH"),
        (
            replace(_forecast(), expected_return_after_cost=float(_forecast().expected_return_after_cost) + 0.01),
            "FORECAST_COST_ARITHMETIC_INVALID",
        ),
    ],
)
def test_adversarial_provider_cannot_cross_scope_time_direction_or_cost_boundary(
    forecast: AlphaForecast, code: str
) -> None:
    with pytest.raises(AlphaStateContractError) as exc:
        OfflineAlphaApp(alpha=_FixedAlpha(forecast)).evaluate_stateful(_data(), position=_position(), costs=_costs())
    assert _error_code(exc) == code


def test_adversarial_provider_must_return_the_exact_forecast_contract() -> None:
    with pytest.raises(AlphaStateContractError) as exc:
        OfflineAlphaApp(alpha=_InvalidAlpha()).evaluate_stateful(_data(), position=_position(), costs=_costs())
    assert _error_code(exc) == "FORECAST_TYPE_INVALID"


def test_adversarial_provider_nonzero_cost_arithmetic_delta_fails_closed() -> None:
    exact = float(_forecast().expected_return_after_cost)
    forecast = replace(_forecast(), expected_return_after_cost=exact + 5e-13)

    with pytest.raises(AlphaStateContractError) as exc:
        OfflineAlphaApp(alpha=_FixedAlpha(forecast)).evaluate_stateful(_data(), position=_position(), costs=_costs())

    assert _error_code(exc) == "FORECAST_COST_ARITHMETIC_INVALID"


def test_v2_market_feature_hash_is_cost_independent_and_signal_matches_legacy() -> None:
    recorder = _RecordingTrendAlpha()
    app = OfflineAlphaApp(alpha=recorder)
    legacy = app.evaluate(_data())
    stateful_a = app.evaluate_stateful(_data(), position=_position(), costs=_costs())
    stateful_b = app.evaluate_stateful(
        _data(), position=_position(), costs=_costs(expected_fee_bps=3.0, source_artifact_digest="c" * 64)
    )
    legacy_forecast, v2_a, v2_b = recorder.forecasts
    assert v2_a.raw_score == pytest.approx(legacy_forecast.raw_score)
    assert v2_a.side == legacy_forecast.side
    assert recorder.contexts[1]["features"]["feature_hash"] == recorder.contexts[2]["features"]["feature_hash"]
    assert v2_a.forecast_hash != v2_b.forecast_hash
    assert stateful_a.cost_binding_digest != stateful_b.cost_binding_digest
    assert legacy.target.target_weight == pytest.approx(legacy_forecast.raw_score)


def test_legacy_result_shape_is_unchanged_and_contract_is_machine_visible() -> None:
    assert set(OfflineAlphaResult.__dataclass_fields__) == {"dataset", "target", "row_count"}
    assert OfflineAlphaApp.LEGACY_CONTRACT_STATUS == "LEGACY_DIAGNOSTIC_ONLY"
