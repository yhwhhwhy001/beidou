"""PKG20: 余额相对容差环境化 — testnet 1%，live/canary 保持 0.01% 严格。

demo-fapi 为共享测试账户（外部活动漂移 ~0.14 USDT/分钟），0.01% 容差数分钟
即失效；引擎默认 0.0001（0.01%）严格语义不变，testnet 由调用方传入 1%。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationEngine,
    ReconciliationStatus,
)
from beidou_shared.types import AccountId, MonetaryValue, VenueId

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)

# system=10000.00 / exchange=10002.00 → diff=2.0：
# 0.01% 容差拒（0.0001 × 10002.00 = 1.0002 < 2.0），1% 容差过（100.02 > 2.0）。
SYSTEM_BALANCE = "10000.00"
EXCHANGE_BALANCE = "10002.00"


def _facts(balance: str, *, source: str, timestamp: datetime | None = None) -> AccountFactSnapshot:
    return AccountFactSnapshot(
        account_id=AccountId("acct"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount=balance),
        positions={},
        open_orders=[],
        # 引擎实例路径以 datetime.now() 为对账时钟，必须使用新鲜时间戳避免 STALE
        timestamp=timestamp or datetime.now(timezone.utc),
        source=source,
        fact_version="v1",
        complete=True,
    )


def test_default_tolerance_is_strict_and_rejects_2_usdt_drift() -> None:
    engine = ReconciliationEngine()
    assert engine._balance_rel_tolerance == Decimal("0.0001")
    engine.update_system_facts(_facts(SYSTEM_BALANCE, source="SYSTEM"))
    engine.update_exchange_facts(_facts(EXCHANGE_BALANCE, source="EXCHANGE"))

    result = engine.reconcile(AccountId("acct"), VenueId("BINANCE"))

    assert result.matched is False
    assert result.status is ReconciliationStatus.MISMATCHED
    balance_diff = next(difference for difference in result.differences if "Balance mismatch" in difference)
    assert "diff=2.000000" in balance_diff
    assert "tolerance=1.000200" in balance_diff  # 0.01% × 10002.00，Decimal 精确


def test_testnet_tolerance_accepts_2_usdt_drift_on_shared_demo_account() -> None:
    engine = ReconciliationEngine(balance_rel_tolerance=Decimal("0.01"))
    assert engine._balance_rel_tolerance == Decimal("0.01")
    engine.update_system_facts(_facts(SYSTEM_BALANCE, source="SYSTEM"))
    engine.update_exchange_facts(_facts(EXCHANGE_BALANCE, source="EXCHANGE"))

    result = engine.reconcile(AccountId("acct"), VenueId("BINANCE"))

    assert result.matched is True
    assert result.status is ReconciliationStatus.MATCHED
    assert not any("Balance mismatch" in difference for difference in result.differences)


def test_compare_three_way_propagates_engine_balance_rel_tolerance() -> None:
    engine = ReconciliationEngine(balance_rel_tolerance=Decimal("0.01"))
    engine.update_system_facts(_facts(SYSTEM_BALANCE, source="SYSTEM"))
    engine.update_exchange_facts(_facts(EXCHANGE_BALANCE, source="EXCHANGE"))
    engine.update_event_facts(_facts(EXCHANGE_BALANCE, source="EVENT_STREAM"))

    result = engine.reconcile_three_way(AccountId("acct"), VenueId("BINANCE"))

    assert result.matched is True
    assert result.status is ReconciliationStatus.MATCHED


def test_compare_three_way_default_stays_strict() -> None:
    engine = ReconciliationEngine()
    engine.update_system_facts(_facts(SYSTEM_BALANCE, source="SYSTEM"))
    engine.update_exchange_facts(_facts(EXCHANGE_BALANCE, source="EXCHANGE"))
    engine.update_event_facts(_facts(EXCHANGE_BALANCE, source="EVENT_STREAM"))

    result = engine.reconcile_three_way(AccountId("acct"), VenueId("BINANCE"))

    assert result.matched is False
    assert result.status is ReconciliationStatus.MISMATCHED
    assert any("Balance mismatch" in difference for difference in result.differences)


def test_static_compare_defaults_strict_and_accepts_explicit_tolerance() -> None:
    system = _facts(SYSTEM_BALANCE, source="SYSTEM", timestamp=NOW)
    exchange = _facts(EXCHANGE_BALANCE, source="EXCHANGE", timestamp=NOW)

    strict = ReconciliationEngine.compare(system, exchange, now=NOW)
    assert strict.matched is False
    assert any("Balance mismatch" in difference for difference in strict.differences)

    testnet = ReconciliationEngine.compare(system, exchange, now=NOW, balance_rel_tolerance=Decimal("0.01"))
    assert testnet.matched is True
    assert testnet.status is ReconciliationStatus.MATCHED
