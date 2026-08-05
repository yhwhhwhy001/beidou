"""PKG-30B 报告引擎测试。日报/周报/事故报告、NOT_VERIFIABLE 语义、证据导出。"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from beidou_shared.types import (
    AccountId, CorrelationId, InstrumentId, ModelId, MonetaryValue,
    SchemaVersion, StrategyId, VenueId,
)
from beidou_reporting.engine import (
    ReportType,
    ReportStatus,
    EvidenceTier,
    ReportReference,
    EvidenceEntry,
    ReportSection,
    Report,
    ReportGenerator,
    ReportValidator,
)


class TestReportTypes:
    """报告类型和枚举测试。"""

    def test_all_report_types_defined(self):
        types = list(ReportType)
        assert ReportType.DAILY in types
        assert ReportType.WEEKLY in types
        assert ReportType.INCIDENT in types
        assert ReportType.FACTOR_ANALYSIS in types

    def test_evidence_tiers(self):
        assert EvidenceTier.VERIFIED.value == "VERIFIED"
        assert EvidenceTier.NOT_VERIFIABLE.value == "NOT_VERIFIABLE"
        assert EvidenceTier.MISSING.value == "MISSING"


class TestEvidenceEntry:
    """证据条目测试。"""

    def test_verified_entry(self):
        entry = EvidenceEntry(
            key="test_key", value="test_value", tier=EvidenceTier.VERIFIED,
            source_ref=ReportReference(
                source="ledger", query_id="q1",
                schema_version=SchemaVersion("1.0.0"),
                timestamp=datetime.now(timezone.utc),
            ),
        )
        assert entry.tier == EvidenceTier.VERIFIED
        assert entry.key == "test_key"

    def test_not_verifiable_entry(self):
        entry = EvidenceEntry(
            key="missing_data", value="NOT_VERIFIABLE",
            tier=EvidenceTier.NOT_VERIFIABLE,
        )
        assert entry.tier == EvidenceTier.NOT_VERIFIABLE


class TestReportSection:
    """报告章节测试。"""

    def test_all_verified(self):
        section = ReportSection(title="Test Section")
        section.entries.append(EvidenceEntry(key="k1", value="v1", tier=EvidenceTier.VERIFIED))
        section.entries.append(EvidenceEntry(key="k2", value="v2", tier=EvidenceTier.VERIFIED))
        assert section.has_all_verified()

    def test_partial_coverage(self):
        section = ReportSection(title="Partial")
        section.entries.append(EvidenceEntry(key="k1", value="v1", tier=EvidenceTier.VERIFIED))
        section.entries.append(EvidenceEntry(key="k2", value="NOT_VERIFIABLE", tier=EvidenceTier.NOT_VERIFIABLE))
        assert not section.has_all_verified()
        assert "k2" in section.missing_keys()

    def test_empty_section(self):
        section = ReportSection(title="Empty")
        assert section.has_all_verified()  # vacuously true


class TestReport:
    """报告主体测试。"""

    def test_overall_tier_verified(self):
        report = Report(report_id="r1", report_type=ReportType.DAILY, title="Test")
        s1 = ReportSection(title="S1")
        s1.entries.append(EvidenceEntry(key="k", value="v", tier=EvidenceTier.VERIFIED))
        s1.status = EvidenceTier.VERIFIED
        report.add_section(s1)
        assert report.overall_tier() == EvidenceTier.VERIFIED

    def test_overall_tier_missing(self):
        report = Report(report_id="r2", report_type=ReportType.DAILY, title="Test")
        s1 = ReportSection(title="S1")
        s1.entries.append(EvidenceEntry(key="k", value="v", tier=EvidenceTier.VERIFIED))
        s1.status = EvidenceTier.VERIFIED
        report.add_section(s1)
        s2 = ReportSection(title="S2")
        s2.entries.append(EvidenceEntry(key="k2", value="v2", tier=EvidenceTier.MISSING))
        s2.status = EvidenceTier.MISSING
        report.add_section(s2)
        assert report.overall_tier() == EvidenceTier.MISSING

    def test_freeze_generates_checksum(self):
        report = Report(report_id="r3", report_type=ReportType.DAILY, title="Test")
        s = ReportSection(title="S")
        s.entries.append(EvidenceEntry(key="k", value="v", tier=EvidenceTier.VERIFIED))
        s.status = EvidenceTier.VERIFIED
        report.add_section(s)
        checksum = report.freeze()
        assert len(checksum) == 64  # SHA256
        assert report.status == ReportStatus.SIGNED

    def test_export_dict_structure(self):
        report = Report(
            report_id="r4", report_type=ReportType.DAILY, title="Export Test",
            period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 1, 1, 23, 59, 59, tzinfo=timezone.utc),
        )
        s = ReportSection(title="S")
        s.entries.append(EvidenceEntry(key="k", value="v", tier=EvidenceTier.VERIFIED))
        s.status = EvidenceTier.VERIFIED
        report.add_section(s)
        report.freeze()
        d = report.to_export_dict()
        assert d["report_id"] == "r4"
        assert d["overall_tier"] == "VERIFIED"
        assert len(d["sections"]) == 1
        assert d["checksum"] == report.checksum


class TestReportGenerator:
    """报告生成器测试。"""

    def test_daily_report_complete(self):
        gen = ReportGenerator()
        date = datetime(2026, 1, 15, tzinfo=timezone.utc)
        report = gen.generate_daily_report(
            date=date,
            strategies=[StrategyId("s1")],
            account_id=AccountId("acc1"),
            venue_id=VenueId("BINANCE"),
            pnl=MonetaryValue(amount="100.50"),
            positions={InstrumentId("BTCUSDT"): 1.5},
            risk_events_24h=0,
            reconciliation_passed=True,
            active_incidents=[],
            data_version=SchemaVersion("2.0.0"),
        )
        assert report.report_type == ReportType.DAILY
        assert report.overall_tier() == EvidenceTier.VERIFIED
        assert len(report.sections) >= 4
        assert len(report.references) >= 1

    def test_daily_report_missing_data(self):
        gen = ReportGenerator()
        date = datetime(2026, 1, 15, tzinfo=timezone.utc)
        report = gen.generate_daily_report(
            date=date,
            strategies=[StrategyId("s1")],
            account_id=AccountId("acc1"),
            venue_id=VenueId("BINANCE"),
            pnl=None,
            positions=None,
            risk_events_24h=0,
            reconciliation_passed=None,
        )
        # PnL missing + positions missing + reconciliation missing
        assert report.overall_tier() == EvidenceTier.NOT_VERIFIABLE

    def test_weekly_report_coverage(self):
        gen = ReportGenerator()
        week_start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        # 只有 5 天的日报
        daily_reports = []
        for i in range(5):
            date = week_start + timedelta(days=i)
            r = Report(
                report_id=f"daily-{date.strftime('%Y%m%d')}",
                report_type=ReportType.DAILY,
                title=f"日报 {date}",
                period_start=date,
            )
            r.freeze()
            daily_reports.append(r)

        weekly = gen.generate_weekly_report(week_start, daily_reports)
        assert weekly.report_type == ReportType.WEEKLY
        assert weekly.overall_tier() != EvidenceTier.VERIFIED  # 5/7 coverage
        assert weekly.overall_tier() == EvidenceTier.PARTIALLY_VERIFIED
        assert len(weekly.sections) >= 1

    def test_incident_report_with_resolution(self):
        gen = ReportGenerator()
        report = gen.generate_incident_report(
            incident_id="inc-001",
            title="Test Incident",
            description="Something went wrong",
            severity="CRITICAL",
            detected_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            auto_action="LOCK",
            resolution="Issue resolved by restart",
            evidence=[{"key": "log", "value": "error trace"}],
            data_version=SchemaVersion("2.0.0"),
        )
        assert report.report_type == ReportType.INCIDENT
        assert report.overall_tier() == EvidenceTier.VERIFIED

    def test_incident_report_unresolved(self):
        gen = ReportGenerator()
        report = gen.generate_incident_report(
            incident_id="inc-002",
            title="Unresolved Incident",
            description="Still investigating",
            severity="HIGH",
            detected_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            auto_action="PAUSE_TRADING",
            resolution=None,
        )
        assert report.overall_tier() == EvidenceTier.NOT_VERIFIABLE

    def test_strategy_performance_report(self):
        gen = ReportGenerator()
        report = gen.generate_strategy_performance_report(
            strategy_id=StrategyId("momentum-001"),
            evaluation_period="2026-Q1",
            sharpe=1.8,
            annualized_return_pct=25.0,
            max_drawdown_pct=15.0,
            win_rate=0.55,
            total_pnl=MonetaryValue(amount="5000.00"),
            active_factors=["factor_a", "factor_b"],
            champion_model=ModelId("model-v3"),
        )
        assert report.report_type == ReportType.STRATEGY_PERFORMANCE
        assert report.overall_tier() == EvidenceTier.VERIFIED

    def test_strategy_performance_report_incomplete(self):
        gen = ReportGenerator()
        report = gen.generate_strategy_performance_report(
            strategy_id=StrategyId("momentum-002"),
            evaluation_period="2026-Q1",
            sharpe=None,
        )
        assert report.overall_tier() == EvidenceTier.NOT_VERIFIABLE

    def test_cost_attribution_report(self):
        gen = ReportGenerator()
        report = gen.generate_cost_attribution_report(
            period="2026-01",
            total_fees=MonetaryValue(amount="250.00"),
            total_slippage_bps=5.0,
            total_impact_bps=2.0,
            funding_rate_pnl=MonetaryValue(amount="-30.00"),
            venue_breakdown={
                VenueId("BINANCE"): MonetaryValue(amount="200.00"),
                VenueId("OKX"): MonetaryValue(amount="50.00"),
            },
        )
        assert report.report_type == ReportType.COST_ATTRIBUTION

    def test_export_evidence_package(self):
        gen = ReportGenerator()
        date = datetime(2026, 1, 15, tzinfo=timezone.utc)
        report = gen.generate_daily_report(
            date=date, strategies=[StrategyId("s1")],
            account_id=AccountId("acc1"), venue_id=VenueId("BINANCE"),
            pnl=MonetaryValue(amount="100"), risk_events_24h=0,
            reconciliation_passed=True, positions={},
            data_version=SchemaVersion("2.0.0"),
        )
        package = gen.export_evidence_package(report)
        assert package["report_id"] == report.report_id
        assert package["checksum"] == report.checksum
        assert report.status == ReportStatus.EXPORTED

    def test_get_daily_reports(self):
        gen = ReportGenerator()
        date = datetime(2026, 1, 15, tzinfo=timezone.utc)
        gen.generate_daily_report(
            date=date, strategies=[StrategyId("s1")],
            account_id=AccountId("acc1"), venue_id=VenueId("BINANCE"),
            pnl=MonetaryValue(amount="100"), risk_events_24h=0,
            reconciliation_passed=True, positions={},
        )
        assert len(gen.get_daily_reports()) == 1

    def test_get_reports_by_type(self):
        gen = ReportGenerator()
        date = datetime(2026, 1, 15, tzinfo=timezone.utc)
        gen.generate_daily_report(
            date=date, strategies=[StrategyId("s1")],
            account_id=AccountId("acc1"), venue_id=VenueId("BINANCE"),
            pnl=MonetaryValue(amount="100"), risk_events_24h=0,
            reconciliation_passed=True, positions={},
        )
        dailies = gen.get_reports_by_type(ReportType.DAILY)
        assert len(dailies) == 1
        assert dailies[0].report_type == ReportType.DAILY


class TestReportValidator:
    """报告验证器测试。"""

    def test_validate_with_references(self):
        report = Report(report_id="rv1", report_type=ReportType.DAILY, title="VTest")
        report.references.append(ReportReference(
            source="ledger", query_id="q1", schema_version=SchemaVersion("2.0.0"),
            timestamp=datetime.now(timezone.utc),
            correlation_id=CorrelationId("corr-1"),
        ))
        ok, issues = ReportValidator.validate_references(report)
        assert ok
        assert len(issues) == 0

    def test_validate_missing_references(self):
        report = Report(report_id="rv2", report_type=ReportType.DAILY, title="VTest")
        ok, issues = ReportValidator.validate_references(report)
        assert not ok

    def test_not_verifiable_coverage(self):
        report = Report(report_id="rv3", report_type=ReportType.DAILY, title="Test")
        s = ReportSection(title="S")
        s.entries.append(EvidenceEntry(key="k1", value="v", tier=EvidenceTier.VERIFIED))
        s.entries.append(EvidenceEntry(key="k2", value="nv", tier=EvidenceTier.NOT_VERIFIABLE))
        s.entries.append(EvidenceEntry(key="k3", value="m", tier=EvidenceTier.MISSING))
        report.add_section(s)
        counts = ReportValidator.check_not_verifiable_coverage(report)
        assert counts["VERIFIED"] == 1
        assert counts["NOT_VERIFIABLE"] == 1
        assert counts["MISSING"] == 1

    def test_safe_for_decision(self):
        report = Report(report_id="rv4", report_type=ReportType.DAILY, title="Test")
        s = ReportSection(title="S")
        s.entries.append(EvidenceEntry(key="k", value="v", tier=EvidenceTier.VERIFIED))
        s.status = EvidenceTier.VERIFIED
        report.add_section(s)
        assert ReportValidator.safe_for_decision(report)

    def test_not_safe_for_decision(self):
        report = Report(report_id="rv5", report_type=ReportType.DAILY, title="Test")
        s = ReportSection(title="S")
        s.entries.append(EvidenceEntry(key="k", value="nv", tier=EvidenceTier.NOT_VERIFIABLE))
        s.status = EvidenceTier.NOT_VERIFIABLE
        report.add_section(s)
        assert not ReportValidator.safe_for_decision(report)
