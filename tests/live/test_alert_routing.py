"""What pages the operator vs what only lands in the daily report.

`com.beidou.check` ran red hourly for three days on a TRUE but unactionable line: two construction
changes had landed on 2026-09-04 and a promotion cannot be un-done.  It also double-posted to the
alert channel every hour: two writers on one channel, and a dedup window then equal to the job's own
period, which is a coin flip rather than a suppression.  A standing governance fact is exactly
the repeated alert that `beidou_live/alerts.py` warns makes the one that matters get missed, so it
no longer pages at all: it stays in the report's Evidence window block, where a human reads it at
review time.  Operational alerts - drift, risk budget, risk adaptation - are untouched.
"""

from __future__ import annotations

from beidou_live.reports import daily_alerts


def _payload(**extra: object) -> dict:
    return {"drift": {}, "income_drift": {}, "risk_budget": {}, "risk_adaptation": {}, "evidence_window": {}, **extra}


def test_construction_cadence_is_a_notice_and_never_pages() -> None:
    alerts, notices = daily_alerts(_payload(evidence_window={"changes_7d": 2}))
    assert alerts == [], "a promotion count cannot be acted on; paging hourly only buries real alerts"
    assert len(notices) == 1 and "2 次构造变更" in notices[0]


def test_one_promotion_a_week_is_not_even_a_notice() -> None:
    assert daily_alerts(_payload(evidence_window={"changes_7d": 1})) == ([], [])


def test_operational_alerts_still_page() -> None:
    alerts, notices = daily_alerts(_payload(risk_budget={"status": "ALERT", "reasons": ["drawdown 36%"]}))
    assert notices == []
    assert len(alerts) == 1 and "风险预算告警" in alerts[0] and "drawdown 36%" in alerts[0]


def test_a_notice_does_not_swallow_an_alert_raised_in_the_same_report() -> None:
    alerts, notices = daily_alerts(
        _payload(
            evidence_window={"changes_7d": 3},
            drift={"status": "ALERT", "reasons": ["realised 0.2 vs expected 1.5"]},
        )
    )
    assert len(alerts) == 1 and "权益漂移告警" in alerts[0]
    assert len(notices) == 1 and "3 次构造变更" in notices[0]


def test_income_drift_keeps_its_per_strategy_z_detail() -> None:
    """The z-score fallback only fires when `reasons` is empty; M-010 reads per-strategy, not equity."""
    alerts, _ = daily_alerts(
        _payload(income_drift={"status": "ALERT", "by_strategy": {"tsmom": {"z": -3.4}, "flow": {"z": -0.2}}})
    )
    assert len(alerts) == 1
    assert "策略收益漂移告警" in alerts[0] and "tsmom: z=-3.4" in alerts[0] and "flow" not in alerts[0]
