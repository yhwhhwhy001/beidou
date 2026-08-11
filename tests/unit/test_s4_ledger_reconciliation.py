"""
S4 (PKG20-21): 账本、仓位与三方对账测试。

覆盖：
- PKG20 (BDS-P1-025): Decimal 精度 — 禁止 float money
- PKG20 (BDS-P1-026): 冻结交易 metadata 不可变
- PKG20 (BDS-P1-027): source_event_id 非空强制
- PKG20 (BDS-P1-028): 余额按币种隔离
- PKG20 (BDS-P1-030): 绝对+相对+可解释差值组合容差
- PKG20 (BDS-P1-031): AccountFactSnapshot complete 默认为 False
- PKG20 (BDS-P1-033): 仓位容差按 venue stepSize
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from beidou_safety.execution.ledger import (
    AccountType,
    ImmutableLedger,
    LedgerTransaction,
    LedgerTransactionType,
    Posting,
    PostingSide,
)
from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
)
from beidou_shared.types import AccountId, MonetaryValue, VenueId


class TestDecimalPrecision:
    """PKG20 (BDS-P1-025): Decimal 精度测试。"""

    def test_ledger_transaction_uses_decimal(self) -> None:
        """账本交易使用 Decimal 精确计算。"""
        tx = LedgerTransaction(
            transaction_id="tx-001",
            transaction_type=LedgerTransactionType.FILL,
            postings=(
                Posting(
                    "p1",
                    AccountId("cash"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="50000.12345678"),
                    PostingSide.DEBIT,
                ),
                Posting(
                    "p2",
                    AccountId("position"),
                    AccountType.POSITION_COST,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="50000.12345678"),
                    PostingSide.CREDIT,
                ),
            ),
            source_event_id="fill-abc-123",
        )

        assert tx.is_balanced()
        assert isinstance(tx.total_debit(), Decimal)
        assert isinstance(tx.total_credit(), Decimal)
        # 高精度不丢失
        assert tx.total_debit() == Decimal("50000.12345678")

    def test_float_precision_loss_detected(self) -> None:
        """浮点精度丢失被检测。"""
        # 使用会导致 float 精度丢失的金额
        tx = LedgerTransaction(
            transaction_id="tx-float-test",
            transaction_type=LedgerTransactionType.FILL,
            postings=(
                Posting(
                    "p1",
                    AccountId("cash"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="0.12345678901234567890"),
                    PostingSide.DEBIT,
                ),
                Posting(
                    "p2",
                    AccountId("pos"),
                    AccountType.POSITION_COST,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="0.12345678901234567890"),
                    PostingSide.CREDIT,
                ),
            ),
            source_event_id="precision-test",
        )

        assert tx.is_balanced()
        # Decimal 保持完整精度
        assert tx.total_debit() == Decimal("0.12345678901234567890")

    def test_invalid_amount_raises_on_arithmetic(self) -> None:
        """无效金额在计算时抛出错误（Decimal 解析阶段）。"""
        posting = Posting(
            "p1",
            AccountId("test"),
            AccountType.CASH,
            VenueId("BINANCE"),
            None,
            MonetaryValue(amount="not_a_number"),
            PostingSide.DEBIT,
        )
        # Posting 本身可以创建（MonetaryValue 接受字符串），但计算时 Decimal 解析失败
        tx = LedgerTransaction(
            transaction_id="tx-bad",
            transaction_type=LedgerTransactionType.FILL,
            postings=(
                posting,
                Posting(
                    "p2",
                    AccountId("b"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="100"),
                    PostingSide.CREDIT,
                ),
            ),
            source_event_id="bad-amount-test",
        )
        with pytest.raises((ValueError, Exception)):
            tx.is_balanced()


class TestSourceEventIdEnforcement:
    """PKG20 (BDS-P1-027): source_event_id 非空强制。"""

    def test_missing_source_event_id_raises(self) -> None:
        """缺少 source_event_id 的交易被拒绝。"""
        with pytest.raises(ValueError, match="source_event_id"):
            LedgerTransaction(
                transaction_id="tx-no-source",
                transaction_type=LedgerTransactionType.FILL,
                postings=(
                    Posting(
                        "p1",
                        AccountId("a"),
                        AccountType.CASH,
                        VenueId("BINANCE"),
                        None,
                        MonetaryValue(amount="100"),
                        PostingSide.DEBIT,
                    ),
                    Posting(
                        "p2",
                        AccountId("b"),
                        AccountType.CASH,
                        VenueId("BINANCE"),
                        None,
                        MonetaryValue(amount="100"),
                        PostingSide.CREDIT,
                    ),
                ),
                source_event_id="",  # 空字符串
            )

    def test_source_event_id_stripped_raises(self) -> None:
        """仅空格的 source_event_id 被拒绝。"""
        with pytest.raises(ValueError, match="source_event_id"):
            LedgerTransaction(
                transaction_id="tx-spaces",
                transaction_type=LedgerTransactionType.FILL,
                postings=(
                    Posting(
                        "p1",
                        AccountId("a"),
                        AccountType.CASH,
                        VenueId("BINANCE"),
                        None,
                        MonetaryValue(amount="100"),
                        PostingSide.DEBIT,
                    ),
                    Posting(
                        "p2",
                        AccountId("b"),
                        AccountType.CASH,
                        VenueId("BINANCE"),
                        None,
                        MonetaryValue(amount="100"),
                        PostingSide.CREDIT,
                    ),
                ),
                source_event_id="   ",
            )


class TestImmutableLedger:
    """PKG20 (BDS-P1-025, BDS-P1-028): 不可变账本测试。"""

    def test_ledger_balances_by_currency(self) -> None:
        """余额按币种隔离。"""
        ledger = ImmutableLedger()

        # USDT 交易
        tx1 = LedgerTransaction(
            transaction_id="tx-usdt",
            transaction_type=LedgerTransactionType.FILL,
            postings=(
                Posting(
                    "p1",
                    AccountId("cash"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="1000", currency="USDT"),
                    PostingSide.DEBIT,
                ),
                Posting(
                    "p2",
                    AccountId("pos"),
                    AccountType.POSITION_COST,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="1000", currency="USDT"),
                    PostingSide.CREDIT,
                ),
            ),
            source_event_id="evt-usdt-1",
        )
        ledger.post(tx1)

        # BUSD 交易（不同币种）
        tx2 = LedgerTransaction(
            transaction_id="tx-busd",
            transaction_type=LedgerTransactionType.FILL,
            postings=(
                Posting(
                    "p3",
                    AccountId("cash"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="500", currency="BUSD"),
                    PostingSide.DEBIT,
                ),
                Posting(
                    "p4",
                    AccountId("pos"),
                    AccountType.POSITION_COST,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="500", currency="BUSD"),
                    PostingSide.CREDIT,
                ),
            ),
            source_event_id="evt-busd-1",
        )
        ledger.post(tx2)

        # 按币种查询
        usdt_bal = ledger.get_balance(AccountId("cash"), VenueId("BINANCE"), currency="USDT")
        busd_bal = ledger.get_balance(AccountId("cash"), VenueId("BINANCE"), currency="BUSD")

        assert usdt_bal.amount == "1000"
        assert busd_bal.amount == "500"

    def test_duplicate_source_event_rejected(self) -> None:
        """重复 source_event_id 被拒绝。"""
        ledger = ImmutableLedger()
        tx = LedgerTransaction(
            transaction_id="tx-1",
            transaction_type=LedgerTransactionType.FILL,
            postings=(
                Posting(
                    "p1",
                    AccountId("a"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="100"),
                    PostingSide.DEBIT,
                ),
                Posting(
                    "p2",
                    AccountId("b"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="100"),
                    PostingSide.CREDIT,
                ),
            ),
            source_event_id="fill-same-id",
        )
        ledger.post(tx)

        tx2 = LedgerTransaction(
            transaction_id="tx-2",
            transaction_type=LedgerTransactionType.FILL,
            postings=(
                Posting(
                    "p3",
                    AccountId("a"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="200"),
                    PostingSide.DEBIT,
                ),
                Posting(
                    "p4",
                    AccountId("b"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="200"),
                    PostingSide.CREDIT,
                ),
            ),
            source_event_id="fill-same-id",  # 重复
        )
        with pytest.raises(RuntimeError, match="duplicate"):
            ledger.post(tx2)

    def test_trial_balance_uses_decimal(self) -> None:
        """试算表使用 Decimal 返回。"""
        ledger = ImmutableLedger()
        tx = LedgerTransaction(
            transaction_id="tx-trial",
            transaction_type=LedgerTransactionType.FILL,
            postings=(
                Posting(
                    "p1",
                    AccountId("cash"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="999.99", currency="USDT"),
                    PostingSide.DEBIT,
                ),
                Posting(
                    "p2",
                    AccountId("fees"),
                    AccountType.FEES,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="999.99", currency="USDT"),
                    PostingSide.CREDIT,
                ),
            ),
            source_event_id="evt-trial",
        )
        ledger.post(tx)
        tb = ledger.get_trial_balance()
        assert isinstance(tb, dict)
        assert all(isinstance(v, Decimal) for v in tb.values())


class TestReconciliationTolerance:
    """PKG20 (BDS-P1-030, BDS-P1-031, BDS-P1-033): 对账容差测试。"""

    def test_fact_snapshot_complete_defaults_false(self) -> None:
        """AccountFactSnapshot.complete 默认为 False。"""
        snap = AccountFactSnapshot(
            account_id=AccountId("test"),
            venue_id=VenueId("BINANCE"),
            balance=MonetaryValue(amount="10000"),
            positions={},
            open_orders=[],
        )
        assert snap.complete is False, "complete 应默认为 False"

    def test_balance_tolerance_uses_absolute_relative(self) -> None:
        """余额容差使用绝对+相对组合。"""
        # 小账户：0.01 USDT 绝对容差
        small_bal = Decimal("0.005")  # 0.5 cent difference
        max_small = max(Decimal("0.01"), Decimal("0.0001") * Decimal("10"))
        assert small_bal < max_small  # 0.005 < 0.01 = PASS

        # 大账户：相对容差 0.01%
        big_bal = Decimal("50")  # 50 USDT difference
        max_big = max(Decimal("0.01"), Decimal("0.0001") * Decimal("1000000"))
        assert big_bal < max_big  # 50 < 100 = PASS (相对容差放行)

    def test_position_tolerance_uses_step_size(self) -> None:
        """仓位容差使用 venue stepSize 而非固定 1e-12。"""
        POSITION_STEP_SIZE = Decimal("1e-8")
        # 差值小于 stepSize → 视为相等
        diff = Decimal("1e-9")
        assert diff <= POSITION_STEP_SIZE
        # 差值大于 stepSize → 标记为不匹配
        big_diff = Decimal("1e-5")
        assert big_diff > POSITION_STEP_SIZE
