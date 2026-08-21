"""报告引擎与证据导出 — 策略/因子/模型/成本/归因/生命周期报告。

PKG-30B: 自动日报、周报、事故报告；数据不足时显示 NOT_VERIFIABLE；
报告数值可追溯到权威查询和版本；报告任务失败不影响安全控制面。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from typing import Any

from beidou_shared.types import (
    AccountId,
    CorrelationId,
    InstrumentId,
    ModelId,
    MonetaryValue,
    SchemaVersion,
    StrategyId,
    VenueId,
)


class ReportType(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    INCIDENT = "INCIDENT"
    STRATEGY_PERFORMANCE = "STRATEGY_PERFORMANCE"
    FACTOR_ANALYSIS = "FACTOR_ANALYSIS"
    MODEL_DRIFT = "MODEL_DRIFT"
    COST_ATTRIBUTION = "COST_ATTRIBUTION"
    PNL_ATTRIBUTION = "PNL_ATTRIBUTION"
    LIFECYCLE_AUDIT = "LIFECYCLE_AUDIT"
    RECONCILIATION = "RECONCILIATION"


class ReportStatus(str, Enum):
    GENERATED = "GENERATED"
    SIGNED = "SIGNED"
    EXPORTED = "EXPORTED"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


class EvidenceTier(str, Enum):
    """证据等级。NOT_VERIFIABLE = 数据不足不可验证。"""

    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class ReportReference:
    """报告数据溯源引用 — 指向权威查询和版本。"""

    source: str  # e.g., "ledger", "reconciliation", "model_registry"
    query_id: str
    schema_version: SchemaVersion
    timestamp: datetime
    correlation_id: CorrelationId | None = None


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    """证据条目。不可变数据点，可追溯到来源。"""

    key: str
    value: Any
    tier: EvidenceTier
    source_ref: ReportReference | None = None
    notes: str = ""


@dataclass
class ReportSection:
    """报告章节。每个章节有独立的证据级别。"""

    title: str
    entries: list[EvidenceEntry] = field(default_factory=list)
    summary: str = ""
    status: EvidenceTier = EvidenceTier.NOT_VERIFIABLE

    def has_all_verified(self) -> bool:
        return all(e.tier == EvidenceTier.VERIFIED for e in self.entries)

    def missing_keys(self) -> list[str]:
        return [e.key for e in self.entries if e.tier in (EvidenceTier.MISSING, EvidenceTier.NOT_VERIFIABLE)]


@dataclass
class Report:
    """报告主体。不可变快照，支持冻结导出。"""

    report_id: str
    report_type: ReportType
    title: str
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    period_start: datetime | None = None
    period_end: datetime | None = None
    sections: list[ReportSection] = field(default_factory=list)
    references: list[ReportReference] = field(default_factory=list)
    checksum: str = ""
    status: ReportStatus = ReportStatus.GENERATED
    correlation_id: CorrelationId | None = None

    def overall_tier(self) -> EvidenceTier:
        tiers = {s.status for s in self.sections}
        if EvidenceTier.MISSING in tiers:
            return EvidenceTier.MISSING
        if EvidenceTier.NOT_VERIFIABLE in tiers:
            return EvidenceTier.NOT_VERIFIABLE
        if EvidenceTier.PARTIALLY_VERIFIED in tiers:
            return EvidenceTier.PARTIALLY_VERIFIED
        return EvidenceTier.VERIFIED

    def add_section(self, section: ReportSection) -> None:
        self.sections.append(section)

    def freeze(self) -> str:
        """冻结报告并生成 checksum。"""
        payload = f"{self.report_id}:{self.report_type}:{self.title}"
        for s in self.sections:
            payload += f":{s.title}:{len(s.entries)}"
        self.checksum = sha256(payload.encode()).hexdigest()
        self.status = ReportStatus.SIGNED
        return self.checksum

    def to_export_dict(self) -> dict[str, Any]:
        """导出冻结证据包。"""
        return {
            "report_id": self.report_id,
            "report_type": self.report_type.value,
            "title": self.title,
            "generated_at": self.generated_at.isoformat(),
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "overall_tier": self.overall_tier().value,
            "checksum": self.checksum,
            "status": self.status.value,
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
            "sections": [
                {
                    "title": s.title,
                    "summary": s.summary,
                    "status": s.status.value,
                    "missing_keys": s.missing_keys(),
                    "entries": [{"key": e.key, "value": str(e.value), "tier": e.tier.value} for e in s.entries],
                }
                for s in self.sections
            ],
            "references": [
                {
                    "source": r.source,
                    "query_id": r.query_id,
                    "schema_version": r.schema_version,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in self.references
            ],
        }


class ReportGenerator:
    """报告生成器 — 自动日报、周报、事故报告。"""

    def __init__(self) -> None:
        self._daily_reports: list[Report] = []
        self._weekly_reports: list[Report] = []
        self._incident_reports: list[Report] = []
        self._custom_reports: list[Report] = []
        self._attribution_reports: list[Report] = []
        self._decision_traces: list[Any] = []
        self._evidence_exports: list[dict[str, Any]] = []

    def generate_daily_report(
        self,
        date: datetime,
        strategies: list[StrategyId],
        account_id: AccountId,
        venue_id: VenueId,
        pnl: MonetaryValue | None = None,
        positions: dict[InstrumentId, float] | None = None,
        risk_events_24h: int = 0,
        reconciliation_passed: bool | None = None,
        active_incidents: list[str] | None = None,
        data_version: SchemaVersion | None = None,
        correlation_id: CorrelationId | None = None,
    ) -> Report:
        """生成日报。缺失数据以 NOT_VERIFIABLE 标记。"""
        report = Report(
            report_id=f"daily-{date.strftime('%Y%m%d')}",
            report_type=ReportType.DAILY,
            title=f"北斗日报 {date.strftime('%Y-%m-%d')}",
            period_start=date.replace(hour=0, minute=0, second=0),
            period_end=date.replace(hour=23, minute=59, second=59),
            correlation_id=correlation_id,
        )

        # PnL section
        pnl_section = ReportSection(title="PnL")
        pnl_section.entries.append(
            EvidenceEntry(
                key="daily_pnl",
                value=str(pnl.amount) if pnl else "NOT_VERIFIABLE",
                tier=EvidenceTier.VERIFIED if pnl else EvidenceTier.NOT_VERIFIABLE,
                source_ref=ReportReference(
                    source="ledger",
                    query_id="daily_pnl",
                    schema_version=data_version or SchemaVersion("unknown"),
                    timestamp=date,
                    correlation_id=correlation_id,
                ),
            )
        )
        pnl_section.status = EvidenceTier.VERIFIED if pnl else EvidenceTier.NOT_VERIFIABLE
        pnl_section.summary = f"日 PnL: {pnl.amount if pnl else 'NOT_VERIFIABLE'}"
        report.add_section(pnl_section)

        # Positions section
        pos_section = ReportSection(title="Positions")
        if positions:
            for inst_id, qty in positions.items():
                pos_section.entries.append(
                    EvidenceEntry(
                        key=f"position_{inst_id}",
                        value=str(qty),
                        tier=EvidenceTier.VERIFIED,
                    )
                )
            pos_section.status = EvidenceTier.VERIFIED
            pos_section.summary = f"持有 {len(positions)} 个仓位"
        else:
            pos_section.entries.append(
                EvidenceEntry(
                    key="positions",
                    value="NOT_VERIFIABLE",
                    tier=EvidenceTier.NOT_VERIFIABLE,
                )
            )
            pos_section.status = EvidenceTier.NOT_VERIFIABLE
            pos_section.summary = "仓位数据: NOT_VERIFIABLE"
        report.add_section(pos_section)

        # Risk section
        risk_section = ReportSection(title="Risk Events (24h)")
        risk_section.entries.append(
            EvidenceEntry(
                key="risk_events_24h",
                value=str(risk_events_24h),
                tier=EvidenceTier.VERIFIED,
            )
        )
        risk_section.status = EvidenceTier.VERIFIED
        risk_section.summary = f"过去24h风险事件: {risk_events_24h}"
        report.add_section(risk_section)

        # Reconciliation section
        recon_section = ReportSection(title="Account Reconciliation")
        if reconciliation_passed is not None:
            recon_section.entries.append(
                EvidenceEntry(
                    key="reconciliation_passed",
                    value=str(reconciliation_passed),
                    tier=EvidenceTier.VERIFIED,
                )
            )
            recon_section.status = EvidenceTier.VERIFIED
            recon_section.summary = "对账通过" if reconciliation_passed else "对账差异"
        else:
            recon_section.entries.append(
                EvidenceEntry(
                    key="reconciliation_passed",
                    value="NOT_VERIFIABLE",
                    tier=EvidenceTier.NOT_VERIFIABLE,
                )
            )
            recon_section.status = EvidenceTier.NOT_VERIFIABLE
            recon_section.summary = "对账数据不足"
        report.add_section(recon_section)

        # Incidents section
        inc_section = ReportSection(title="Active Incidents")
        if active_incidents:
            for inc in active_incidents:
                inc_section.entries.append(
                    EvidenceEntry(
                        key=f"incident_{inc}",
                        value="ACTIVE",
                        tier=EvidenceTier.VERIFIED,
                    )
                )
            inc_section.status = EvidenceTier.VERIFIED
            inc_section.summary = f"活跃事故: {len(active_incidents)}"
        else:
            inc_section.entries.append(
                EvidenceEntry(
                    key="active_incidents",
                    value="NONE",
                    tier=EvidenceTier.VERIFIED,
                )
            )
            inc_section.status = EvidenceTier.VERIFIED
            inc_section.summary = "无活跃事故"
        report.add_section(inc_section)

        # Data version reference
        if data_version:
            report.references.append(
                ReportReference(
                    source="data_version",
                    query_id="daily_report",
                    schema_version=data_version,
                    timestamp=date,
                    correlation_id=correlation_id,
                )
            )

        report.freeze()
        self._daily_reports.append(report)
        return report

    def generate_weekly_report(
        self,
        week_start: datetime,
        daily_reports: list[Report],
        correlation_id: CorrelationId | None = None,
    ) -> Report:
        """生成周报。汇总7天日报，标记缺失天数。"""
        report = Report(
            report_id=f"weekly-{week_start.strftime('%Y%W')}",
            report_type=ReportType.WEEKLY,
            title=f"北斗周报 {week_start.strftime('%Y-%m-%d')}",
            period_start=week_start,
            period_end=week_start + timedelta(days=7),
            correlation_id=correlation_id,
        )

        # Daily report coverage
        coverage = ReportSection(title="日报覆盖")
        days_with_data = len(daily_reports)
        for i in range(7):
            day = week_start + timedelta(days=i)
            has_report = any(r.period_start and r.period_start.date() == day.date() for r in daily_reports)
            coverage.entries.append(
                EvidenceEntry(
                    key=f"report_{day.strftime('%Y%m%d')}",
                    value="PRESENT" if has_report else "MISSING",
                    tier=EvidenceTier.VERIFIED if has_report else EvidenceTier.MISSING,
                )
            )
        coverage.status = EvidenceTier.VERIFIED if days_with_data == 7 else EvidenceTier.PARTIALLY_VERIFIED
        coverage.summary = f"日报覆盖: {days_with_data}/7 天"
        report.add_section(coverage)

        # Weekly summary section
        summary = ReportSection(title="周度摘要")
        missing_days = 7 - days_with_data
        summary.entries.append(
            EvidenceEntry(
                key="days_covered",
                value=str(days_with_data),
                tier=EvidenceTier.VERIFIED,
            )
        )
        summary.entries.append(
            EvidenceEntry(
                key="days_missing",
                value=str(missing_days),
                tier=EvidenceTier.VERIFIED if missing_days == 0 else EvidenceTier.NOT_VERIFIABLE,
            )
        )
        summary.status = EvidenceTier.VERIFIED if missing_days == 0 else EvidenceTier.PARTIALLY_VERIFIED
        summary.summary = f"{days_with_data}/7 天完整数据"
        report.add_section(summary)

        report.freeze()
        self._weekly_reports.append(report)
        return report

    def generate_incident_report(
        self,
        incident_id: str,
        title: str,
        description: str,
        severity: str,
        detected_at: datetime,
        auto_action: str,
        resolution: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        correlation_id: CorrelationId | None = None,
        data_version: SchemaVersion | None = None,
    ) -> Report:
        """生成事故报告。包含完整时间线和证据。"""
        report = Report(
            report_id=f"incident-{incident_id}",
            report_type=ReportType.INCIDENT,
            title=f"事故报告: {title}",
            period_start=detected_at,
            correlation_id=correlation_id,
        )

        # Incident details
        detail_section = ReportSection(title="事故详情")
        detail_section.entries.append(
            EvidenceEntry(
                key="incident_id",
                value=incident_id,
                tier=EvidenceTier.VERIFIED,
            )
        )
        detail_section.entries.append(
            EvidenceEntry(
                key="severity",
                value=severity,
                tier=EvidenceTier.VERIFIED,
            )
        )
        detail_section.entries.append(
            EvidenceEntry(
                key="detected_at",
                value=detected_at.isoformat(),
                tier=EvidenceTier.VERIFIED,
            )
        )
        detail_section.entries.append(
            EvidenceEntry(
                key="auto_action",
                value=auto_action,
                tier=EvidenceTier.VERIFIED,
            )
        )
        detail_section.entries.append(
            EvidenceEntry(
                key="description",
                value=description,
                tier=EvidenceTier.VERIFIED,
            )
        )
        detail_section.status = EvidenceTier.VERIFIED
        report.add_section(detail_section)

        # Resolution section
        res_section = ReportSection(title="处理结果")
        res_section.entries.append(
            EvidenceEntry(
                key="resolution",
                value=resolution if resolution else "NOT_VERIFIABLE",
                tier=EvidenceTier.VERIFIED if resolution else EvidenceTier.NOT_VERIFIABLE,
            )
        )
        res_section.status = EvidenceTier.VERIFIED if resolution else EvidenceTier.NOT_VERIFIABLE
        res_section.summary = resolution if resolution else "尚未解决"
        report.add_section(res_section)

        # Evidence section
        ev_section = ReportSection(title="证据快照")
        if evidence:
            for i, ev in enumerate(evidence):
                ev_section.entries.append(
                    EvidenceEntry(
                        key=f"evidence_{i}",
                        value=ev,
                        tier=EvidenceTier.VERIFIED,
                    )
                )
            ev_section.status = EvidenceTier.VERIFIED
        else:
            ev_section.entries.append(
                EvidenceEntry(
                    key="evidence",
                    value="NOT_VERIFIABLE",
                    tier=EvidenceTier.NOT_VERIFIABLE,
                )
            )
            ev_section.status = EvidenceTier.NOT_VERIFIABLE
        report.add_section(ev_section)

        if data_version:
            report.references.append(
                ReportReference(
                    source="data_version",
                    query_id="incident_report",
                    schema_version=data_version,
                    timestamp=detected_at,
                    correlation_id=correlation_id,
                )
            )

        report.freeze()
        self._incident_reports.append(report)
        return report

    def generate_strategy_performance_report(
        self,
        strategy_id: StrategyId,
        evaluation_period: str,
        sharpe: float | None = None,
        annualized_return_pct: float | None = None,
        max_drawdown_pct: float | None = None,
        win_rate: float | None = None,
        total_pnl: MonetaryValue | None = None,
        active_factors: list[str] | None = None,
        champion_model: ModelId | None = None,
        correlation_id: CorrelationId | None = None,
    ) -> Report:
        """生成策略绩效报告。"""
        report = Report(
            report_id=f"strategy-perf-{strategy_id}-{evaluation_period}",
            report_type=ReportType.STRATEGY_PERFORMANCE,
            title=f"策略绩效: {strategy_id} ({evaluation_period})",
            correlation_id=correlation_id,
        )

        metrics = ReportSection(title="绩效指标")
        perf_fields = {
            "sharpe": sharpe,
            "annualized_return_pct": annualized_return_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "win_rate": win_rate,
        }
        for key, val in perf_fields.items():
            metrics.entries.append(
                EvidenceEntry(
                    key=key,
                    value=str(val) if val is not None else "NOT_VERIFIABLE",
                    tier=EvidenceTier.VERIFIED if val is not None else EvidenceTier.NOT_VERIFIABLE,
                )
            )
        metrics.status = (
            EvidenceTier.VERIFIED if all(v is not None for v in perf_fields.values()) else EvidenceTier.NOT_VERIFIABLE
        )
        report.add_section(metrics)

        # Factors section
        factor_section = ReportSection(title="活跃因子")
        if active_factors:
            for f in active_factors:
                factor_section.entries.append(
                    EvidenceEntry(
                        key=f"factor_{f}",
                        value="ACTIVE",
                        tier=EvidenceTier.VERIFIED,
                    )
                )
            factor_section.status = EvidenceTier.VERIFIED
        else:
            factor_section.entries.append(
                EvidenceEntry(
                    key="factors",
                    value="NOT_VERIFIABLE",
                    tier=EvidenceTier.NOT_VERIFIABLE,
                )
            )
            factor_section.status = EvidenceTier.NOT_VERIFIABLE
        report.add_section(factor_section)

        # Model section
        model_section = ReportSection(title="Champion Model")
        model_section.entries.append(
            EvidenceEntry(
                key="champion_model",
                value=str(champion_model) if champion_model else "NOT_VERIFIABLE",
                tier=EvidenceTier.VERIFIED if champion_model else EvidenceTier.NOT_VERIFIABLE,
            )
        )
        model_section.status = EvidenceTier.VERIFIED if champion_model else EvidenceTier.NOT_VERIFIABLE
        report.add_section(model_section)

        report.freeze()
        self._custom_reports.append(report)
        return report

    def generate_cost_attribution_report(
        self,
        period: str,
        total_fees: MonetaryValue | None = None,
        total_slippage_bps: float | None = None,
        total_impact_bps: float | None = None,
        funding_rate_pnl: MonetaryValue | None = None,
        venue_breakdown: dict[VenueId, MonetaryValue] | None = None,
        correlation_id: CorrelationId | None = None,
    ) -> Report:
        """生成成本归因报告。"""
        report = Report(
            report_id=f"cost-attribution-{period}",
            report_type=ReportType.COST_ATTRIBUTION,
            title=f"成本归因 ({period})",
            correlation_id=correlation_id,
        )

        cost_section = ReportSection(title="交易成本分解")
        cost_fields = {
            "total_fees": str(total_fees.amount) if total_fees else None,
            "total_slippage_bps": str(total_slippage_bps) if total_slippage_bps else None,
            "total_impact_bps": str(total_impact_bps) if total_impact_bps else None,
            "funding_rate_pnl": str(funding_rate_pnl.amount) if funding_rate_pnl else None,
        }
        for key, val in cost_fields.items():
            cost_section.entries.append(
                EvidenceEntry(
                    key=key,
                    value=val if val else "NOT_VERIFIABLE",
                    tier=EvidenceTier.VERIFIED if val else EvidenceTier.NOT_VERIFIABLE,
                )
            )
        cost_section.status = EvidenceTier.VERIFIED if all(cost_fields.values()) else EvidenceTier.NOT_VERIFIABLE
        report.add_section(cost_section)

        if venue_breakdown:
            venue_section = ReportSection(title="按交易所分解")
            for venue, fee in venue_breakdown.items():
                venue_section.entries.append(
                    EvidenceEntry(
                        key=f"venue_{venue}",
                        value=str(fee.amount),
                        tier=EvidenceTier.VERIFIED,
                    )
                )
            venue_section.status = EvidenceTier.VERIFIED
            report.add_section(venue_section)

        report.freeze()
        self._custom_reports.append(report)
        return report

    def generate_pnl_attribution_report(self, period: str, record: Any) -> Report:
        """Generate an additive Alpha V3 PnL attribution report.

        The caller must finalize the record with an explicit residual policy.
        Unknown fields remain NOT_VERIFIABLE in the report; this method never
        fills missing economic contributions with zero.
        """
        from .pnl_attribution import AttributionRecord

        if not isinstance(record, AttributionRecord):
            raise TypeError("record must be an AttributionRecord")

        report = Report(
            report_id=f"pnl-attribution-{period}-{record.decision_id}",
            report_type=ReportType.PNL_ATTRIBUTION,
            title=f"PnL 归因 ({period})",
            period_start=record.timestamp,
            period_end=record.timestamp,
            correlation_id=record.correlation_id,
        )

        references = list(record.evidence_references)
        if record.source_hash and record.correlation_id:
            references.append(
                ReportReference(
                    source="v3_attribution",
                    query_id=record.source_hash,
                    schema_version=record.schema_version,
                    timestamp=record.timestamp,
                    correlation_id=record.correlation_id,
                )
            )
        report.references.extend(references)

        section = ReportSection(title="Alpha V3 PnL Attribution")
        source_ref = references[0] if references else None
        for key, value in record.field_values().items():
            missing = value is None or value == ""
            entry_tier = EvidenceTier.NOT_VERIFIABLE if missing else record.evidence_tier
            section.entries.append(
                EvidenceEntry(
                    key=key,
                    value="NOT_VERIFIABLE" if missing else value,
                    tier=entry_tier,
                    source_ref=source_ref,
                )
            )
        section.status = record.evidence_tier
        section.summary = (
            "归因余额已通过残差策略验证"
            if record.evidence_tier is EvidenceTier.VERIFIED
            else "归因证据不完整，保留 NOT_VERIFIABLE 字段"
        )
        report.add_section(section)
        report.freeze()
        self._attribution_reports.append(report)
        self._custom_reports.append(report)
        return report

    def generate_attribution_report(self, period: str, record: Any) -> Report:
        """Compatibility alias for callers using the shorter report name."""
        return self.generate_pnl_attribution_report(period, record)

    def record_decision_trace(self, trace: Any) -> None:
        """Store read-only V2 lineage for later attribution joining."""
        from .pnl_attribution import DecisionTrace

        if not isinstance(trace, DecisionTrace):
            raise TypeError("trace must be a DecisionTrace")
        self._decision_traces.append(trace)

    def get_decision_traces(self) -> list[Any]:
        return list(self._decision_traces)

    def export_evidence_package(self, report: Report) -> dict[str, Any]:
        """导出冻结证据包 — JSON 格式，包含所有溯源引用。"""
        package = report.to_export_dict()
        self._evidence_exports.append(package)
        report.status = ReportStatus.EXPORTED
        return package

    def get_daily_reports(self) -> list[Report]:
        return list(self._daily_reports)

    def get_weekly_reports(self) -> list[Report]:
        return list(self._weekly_reports)

    def get_incident_reports(self) -> list[Report]:
        return list(self._incident_reports)

    def get_pnl_attribution_reports(self) -> list[Report]:
        return list(self._attribution_reports)

    def get_reports_by_type(self, report_type: ReportType) -> list[Report]:
        mapping = {
            ReportType.DAILY: self._daily_reports,
            ReportType.WEEKLY: self._weekly_reports,
            ReportType.INCIDENT: self._incident_reports,
            ReportType.PNL_ATTRIBUTION: self._attribution_reports,
        }
        return list(mapping.get(report_type, self._custom_reports))


class ReportValidator:
    """报告验证器 — 确保报告数值可追溯到权威查询和版本。"""

    @staticmethod
    def validate_references(report: Report) -> tuple[bool, list[str]]:
        """验证所有报告引用是否完整。"""
        issues: list[str] = []
        if not report.references:
            issues.append("No data version references — cannot trace to authoritative source")
        for ref in report.references:
            if ref.schema_version == SchemaVersion("unknown"):
                issues.append(f"Unknown schema version in {ref.source}")
            if ref.correlation_id is None:
                issues.append(f"Missing correlation_id in {ref.source}")
        return len(issues) == 0, issues

    @staticmethod
    def check_not_verifiable_coverage(report: Report) -> dict[str, int]:
        """统计 NOT_VERIFIABLE 覆盖情况。"""
        counts: dict[str, int] = {"VERIFIED": 0, "NOT_VERIFIABLE": 0, "MISSING": 0}
        for section in report.sections:
            for entry in section.entries:
                counts[entry.tier.value] = counts.get(entry.tier.value, 0) + 1
        return counts

    @staticmethod
    def safe_for_decision(report: Report) -> bool:
        """报告是否足够安全用于决策。"""
        return report.overall_tier() not in (EvidenceTier.MISSING, EvidenceTier.NOT_VERIFIABLE)
