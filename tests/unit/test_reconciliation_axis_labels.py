"""对账消息标签: 三条轴必须各用真实来源名, 不再硬编码 system/exchange。"""

from datetime import datetime, timezone
from decimal import Decimal

from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationEngine,
    ReconciliationStatus,
)
from beidou_shared.types import AccountId, MonetaryValue, VenueId


def _facts(balance: str, source: str) -> AccountFactSnapshot:
    return AccountFactSnapshot(
        account_id=AccountId("acct"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount=balance),
        positions={},
        open_orders=[],
        timestamp=datetime.now(timezone.utc),
        source=source,
        fact_version="v1",
        complete=True,
    )


def test_compare_uses_custom_labels_in_balance_mismatch() -> None:
    engine = ReconciliationEngine(balance_rel_tolerance=Decimal("0.01"))
    result = engine.compare(
        _facts("100", "A"), _facts("500", "B"),
        left_label="exchange", right_label="event_stream",
    )
    assert result.status is ReconciliationStatus.MISMATCHED
    line = next(d for d in result.differences if d.startswith("Balance mismatch"))
    assert "exchange=100" in line
    assert "event_stream=500" in line
    assert "system=" not in line


def test_compare_default_labels_stay_backward_compatible() -> None:
    engine = ReconciliationEngine(balance_rel_tolerance=Decimal("0.01"))
    result = engine.compare(_facts("100", "A"), _facts("500", "B"))
    line = next(d for d in result.differences if d.startswith("Balance mismatch"))
    assert "system=100" in line and "exchange=500" in line
