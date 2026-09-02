from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parent / "tools"
sys.path.insert(0, str(TOOLS))

from run_frozen_alpha_baseline import (  # noqa: E402
    apply_explicit_costs,
    run_baseline,
    validate_signal_timing,
)

HOUR_MS = 3_600_000


def test_signal_timing_requires_closed_bar_before_next_open_execution() -> None:
    source_open = 1_785_542_400_000

    assert validate_signal_timing(source_open, HOUR_MS, source_open + HOUR_MS) == source_open + HOUR_MS
    with pytest.raises(ValueError, match="LOOKAHEAD_OR_SAME_BAR_EXECUTION"):
        validate_signal_timing(source_open, HOUR_MS, source_open + HOUR_MS - 1)


def test_explicit_cost_model_charges_turnover_and_adverse_hourly_funding() -> None:
    gross = (0.01, 0.0, -0.005)
    positions = (0.5, 0.5, -0.5)

    result = apply_explicit_costs(
        gross,
        positions,
        turnover_cost_bps=6.0,
        adverse_hourly_funding_bps=0.125,
    )

    assert result[0] == pytest.approx(0.01 - 0.5 * 6.0 / 10_000 - 0.5 * 0.125 / 10_000)
    assert result[1] == pytest.approx(0.0 - 0.5 * 0.125 / 10_000)
    assert result[2] == pytest.approx(-0.005 - 1.0 * 6.0 / 10_000 - 0.5 * 0.125 / 10_000)


def test_frozen_baseline_is_causal_reproducible_and_non_promotable() -> None:
    report = run_baseline()

    assert report["status"] == "DIAGNOSTIC_ONLY"
    assert report["decision"] == "DO_NOT_PROMOTE"
    assert report["dataset"]["status"] == "PASS"
    assert report["dataset"]["symbols"] == ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
    assert report["timing_audit"]["lookahead_violations"] == 0
    assert report["timing_audit"]["same_bar_executions"] == 0
    assert report["portfolio"]["bars"] == 669
    assert report["oos"]["status"] == "INVALIDATED_FOR_PROMOTION"
    assert "PRESEAL_RESULT_INSPECTION" in report["oos"]["reasons"]
    assert report["economic_truth"]["overall"] == "NOT_EVALUATED"
    assert report["economic_truth"]["e0_by_symbol"] == dict.fromkeys(
        report["dataset"]["symbols"], "NOT_EVALUATED"
    )
    for symbol in report["dataset"]["symbols"]:
        assert report["per_symbol"][symbol]["rows"] == 720
        assert report["per_symbol"][symbol]["bars"] == 669
        assert report["per_symbol"][symbol]["market_data_status"] == "PASS"
        assert report["per_symbol"][symbol]["subthreshold_nonzero_targets"] > 0
