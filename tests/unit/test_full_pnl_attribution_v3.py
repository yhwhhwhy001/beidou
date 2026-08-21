from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from beidou_reporting.engine import EvidenceTier
from beidou_reporting.pnl_attribution import AttributionRecord
from beidou_shared.types import CorrelationId


def _full_record() -> AttributionRecord:
    return AttributionRecord(
        decision_id="full-001",
        benchmark_id="ETH_SINGLE",
        benchmark_return=0.02,
        portfolio_return=0.01,
        realized_beta_contribution=0.004,
        gross_exposure=0.6,
        net_exposure=0.2,
        portfolio_beta=0.2,
        target_beta=0.2,
        target_gross=0.6,
        target_net=0.2,
        target_volatility=0.2,
        alpha_contributions=(("trend-v3", 0.004), ("breakout-v3", 0.002)),
        alpha_selection_contribution=0.006,
        timing_contribution=0.0,
        no_action_drag=0.0,
        veto_drag=0.0,
        degrade_drag=0.0,
        cash_drag=0.0,
        gross_constraint_drag=0.0,
        beta_constraint_drag=0.0,
        turnover_drag=-0.0002,
        liquidity_drag=0.0,
        fee_drag=-0.0003,
        slippage_drag=-0.0002,
        funding_drag=-0.0001,
        protection_contribution=0.0008,
        benchmark_snapshot_hash="benchmark-hash",
        alpha_forecast_hash="alpha-hash",
        ensemble_forecast_hash="ensemble-hash",
        exposure_target_hash="exposure-hash",
        portfolio_target_hash="portfolio-hash",
        source_hash="attribution-source",
        policy_version="attribution-policy-v3",
        correlation_id=CorrelationId("corr-full-001"),
        timestamp=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )


def test_full_attribution_requires_per_alpha_and_lineage_hashes() -> None:
    record = _full_record().finalize(residual_tolerance=1e-12, require_full=True)

    assert record.evidence_tier is EvidenceTier.VERIFIED
    assert record.is_full_attribution_complete
    assert dict(record.alpha_contributions)["trend-v3"] == 0.004


def test_full_attribution_missing_lineage_is_not_verified() -> None:
    record = replace(_full_record(), portfolio_target_hash="")

    assert record.finalize(residual_tolerance=1e-12, require_full=True).evidence_tier is EvidenceTier.NOT_VERIFIABLE
