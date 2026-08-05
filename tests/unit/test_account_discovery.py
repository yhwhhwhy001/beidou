"""PKG-00A: Account Discovery 测试。查询失败≠空仓。"""

from beidou_exchange.account_discovery import (
    AccountCapabilityChecker,
    AccountCapabilityReport,
    AccountQueryResult,
    AccountQueryStatus,
)
from beidou_shared.types import AccountId, ResultStatus, VenueId


class TestAccountQueryResult:
    def test_success_is_definitive(self):
        r = AccountQueryResult(status=AccountQueryStatus.SUCCESS)
        assert r.is_definitive()

    def test_empty_is_definitive(self):
        r = AccountQueryResult(status=AccountQueryStatus.EMPTY)
        assert r.is_definitive()

    def test_unknown_is_not_definitive(self):
        r = AccountQueryResult(status=AccountQueryStatus.UNKNOWN)
        assert not r.is_definitive()

    def test_error_should_fail_closed(self):
        r = AccountQueryResult(status=AccountQueryStatus.ERROR)
        assert r.should_fail_closed()

    def test_unauthorized_should_fail_closed(self):
        r = AccountQueryResult(status=AccountQueryStatus.UNAUTHORIZED)
        assert r.should_fail_closed()

    def test_success_does_not_fail_closed(self):
        r = AccountQueryResult(status=AccountQueryStatus.SUCCESS)
        assert not r.should_fail_closed()


class TestAccountCapabilityChecker:
    def test_classify_success_with_data(self):
        checker = AccountCapabilityChecker()
        result = checker.classify_result(ResultStatus.SUCCESS, {"btc": "1.0"}, None)
        assert result == AccountQueryStatus.KNOWN_NONEMPTY

    def test_classify_success_empty_data(self):
        checker = AccountCapabilityChecker()
        result = checker.classify_result(ResultStatus.SUCCESS, {}, None)
        assert result == AccountQueryStatus.EMPTY

    def test_classify_empty(self):
        checker = AccountCapabilityChecker()
        result = checker.classify_result(ResultStatus.EMPTY, None, None)
        assert result == AccountQueryStatus.EMPTY

    def test_classify_error_unauthorized(self):
        checker = AccountCapabilityChecker()
        result = checker.classify_result(ResultStatus.ERROR, None, "Unauthorized access")
        assert result == AccountQueryStatus.UNAUTHORIZED

    def test_classify_error_rate_limit(self):
        checker = AccountCapabilityChecker()
        result = checker.classify_result(ResultStatus.ERROR, None, "Rate limit exceeded")
        assert result == AccountQueryStatus.RATE_LIMITED

    def test_classify_unknown(self):
        checker = AccountCapabilityChecker()
        result = checker.classify_result(ResultStatus.UNKNOWN, None, None)
        assert result == AccountQueryStatus.UNKNOWN

    def test_withdraw_permission_detected(self):
        checker = AccountCapabilityChecker()
        result = checker.validate_withdraw_permission(True)
        assert result.status == AccountQueryStatus.KNOWN_NONEMPTY
        assert "WITHDRAW" in result.error_message

    def test_withdraw_permission_clean(self):
        checker = AccountCapabilityChecker()
        result = checker.validate_withdraw_permission(False)
        assert result.status == AccountQueryStatus.EMPTY


class TestAccountCapabilityReport:
    def make_report(self, **overrides) -> AccountCapabilityReport:
        defaults = {
            "venue_id": VenueId("BINANCE"),
            "account_id": AccountId("test_account"),
            "can_read_balances": AccountQueryResult(status=AccountQueryStatus.KNOWN_NONEMPTY),
            "can_read_positions": AccountQueryResult(status=AccountQueryStatus.KNOWN_NONEMPTY),
            "can_read_open_orders": AccountQueryResult(status=AccountQueryStatus.KNOWN_NONEMPTY),
            "can_trade": AccountQueryResult(status=AccountQueryStatus.KNOWN_NONEMPTY),
            "can_withdraw": AccountQueryResult(status=AccountQueryStatus.EMPTY),
            "ip_whitelisted": True,
        }
        defaults.update(overrides)
        return AccountCapabilityReport(**defaults)

    def test_safe_for_trading(self):
        report = self.make_report()
        assert report.is_safe_for_trading()
        assert len(report.trading_blockers()) == 0

    def test_withdraw_permission_blocks(self):
        report = self.make_report(
            can_withdraw=AccountQueryResult(status=AccountQueryStatus.KNOWN_NONEMPTY, error_message="WITHDRAW")
        )
        assert not report.is_safe_for_trading()
        assert "WITHDRAW_PERMISSION_DETECTED" in report.trading_blockers()

    def test_balance_unknown_blocks(self):
        report = self.make_report(can_read_balances=AccountQueryResult(status=AccountQueryStatus.UNKNOWN))
        assert not report.is_safe_for_trading()
        assert "BALANCE_QUERY_FAILED" in report.trading_blockers()

    def test_position_unknown_blocks(self):
        report = self.make_report(can_read_positions=AccountQueryResult(status=AccountQueryStatus.ERROR))
        assert not report.is_safe_for_trading()

    def test_owner_disconnected_no_auto_unlock(self):
        checker = AccountCapabilityChecker()
        report = self.make_report(can_read_balances=AccountQueryResult(status=AccountQueryStatus.UNKNOWN))
        result = checker.owner_disconnected_check(report)
        assert result == ResultStatus.UNKNOWN
