"""Alpha V3 A0-003 attribution skeleton and report integration."""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_reporting.engine import EvidenceTier, ReportGenerator, ReportType, ReportValidator
from beidou_reporting.pnl_attribution import AttributionRecord, DecisionTrace
from beidou_shared.types import CorrelationId, SchemaVersion

TIMESTAMP = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _complete_record() -> AttributionRecord:
    return AttributionRecord(
        decision_id="decision-001",
        benchmark_id="ETH_SINGLE",
        benchmark_return=0.10,
        portfolio_return=0.084,
        expected_beta_contribution=0.05,
        realized_beta_contribution=0.04,
        gross_exposure=1.20,
        net_exposure=0.80,
        portfolio_beta=0.40,
        target_beta=0.40,
        target_gross=1.20,
        target_net=0.80,
        target_volatility=0.25,
        alpha_selection_contribution=0.03,
        timing_contribution=0.01,
        no_action_drag=-0.001,
        veto_drag=-0.001,
        degrade_drag=-0.001,
        cash_drag=-0.001,
        gross_constraint_drag=-0.001,
        beta_constraint_drag=-0.001,
        turnover_drag=-0.001,
        liquidity_drag=-0.001,
        fee_drag=-0.001,
        slippage_drag=-0.001,
        funding_drag=-0.001,
        protection_contribution=0.015,
        source_hash="source-abc",
        policy_version="attribution-policy-v1",
        schema_version=SchemaVersion("3.0.0"),
        correlation_id=CorrelationId("corr-001"),
        timestamp=TIMESTAMP,
    )


def test_attribution_record_balances_beta_gross_net_and_residual() -> None:
    record = _complete_record().finalize(residual_tolerance=1e-12)

    assert record.gross_exposure == 1.20
    assert record.net_exposure == 0.80
    assert record.portfolio_beta == 0.40
    assert record.unexplained_residual == 0.0
    assert record.evidence_tier is EvidenceTier.VERIFIED


def test_attribution_report_contains_decision_drags_and_traceable_evidence() -> None:
    record = _complete_record().finalize(residual_tolerance=1e-12)
    report = ReportGenerator().generate_pnl_attribution_report("2026-08-21T12", record)

    assert report.report_type is ReportType.PNL_ATTRIBUTION
    entries = {entry.key: entry for section in report.sections for entry in section.entries}
    assert entries["no_action_drag"].tier is EvidenceTier.VERIFIED
    assert entries["veto_drag"].tier is EvidenceTier.VERIFIED
    assert entries["gross_constraint_drag"].tier is EvidenceTier.VERIFIED
    assert entries["fee_drag"].tier is EvidenceTier.VERIFIED
    assert entries["unexplained_residual"].tier is EvidenceTier.VERIFIED
    assert ReportValidator.validate_references(report)[0]


def test_missing_attribution_evidence_is_not_verifiable_not_zero() -> None:
    record = AttributionRecord(
        decision_id="decision-unknown",
        benchmark_return=None,
        portfolio_return=0.01,
        realized_beta_contribution=None,
    ).finalize(residual_tolerance=1e-12)

    assert record.unexplained_residual is None
    assert record.no_action_drag is None
    assert record.evidence_tier is EvidenceTier.NOT_VERIFIABLE

    report = ReportGenerator().generate_pnl_attribution_report("unknown", record)
    entries = {entry.key: entry for section in report.sections for entry in section.entries}
    assert entries["no_action_drag"].value == "NOT_VERIFIABLE"
    assert entries["no_action_drag"].tier is EvidenceTier.NOT_VERIFIABLE


def test_decision_trace_can_link_market_kernel_and_attribution_without_pnl_fabrication() -> None:
    trace = DecisionTrace.from_probe(
        {
            "symbol": "ETHUSDT",
            "features": {"close": 100.0},
            "state": {"direction": "RANGING"},
            "graph_hash": "graph-1",
            "proposal_hash": "proposal-1",
            "timestamp": TIMESTAMP.isoformat(),
        }
    )

    assert trace.market_data_hash
    assert trace.state_hash
    assert trace.proposal_hash == "proposal-1"
    assert trace.attribution_record_id is None
    assert trace.status is EvidenceTier.NOT_VERIFIABLE

    linked = trace.link_attribution(_complete_record().finalize(residual_tolerance=1e-12))
    assert linked.attribution_record_id == "decision-001"
    assert linked.status is EvidenceTier.VERIFIED

    generator = ReportGenerator()
    generator.record_decision_trace(linked)
    assert generator.get_decision_traces() == [linked]
