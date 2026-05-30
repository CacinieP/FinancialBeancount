"""
Tests for beancount_dedup.models — Transaction, DedupResult, DeduplicationReport, enums.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from beancount_dedup.models import (
    Transaction,
    Platform,
    TransactionType,
    DedupStatus,
    DedupResult,
    DeduplicationReport,
)


# ── Enum tests ────────────────────────────────────────────────────────────


class TestPlatform:
    def test_values(self):
        assert Platform.UNKNOWN.value == "unknown"
        assert Platform.ALIPAY.value == "alipay"
        assert Platform.WECHAT.value == "wechat"
        assert Platform.BANK.value == "bank"

    def test_str(self):
        assert str(Platform.ALIPAY) == "alipay"
        assert str(Platform.BANK) == "bank"

    def test_from_value(self):
        assert Platform("alipay") is Platform.ALIPAY
        assert Platform("wechat") is Platform.WECHAT


class TestTransactionType:
    def test_values(self):
        assert TransactionType.EXPENSE.value == "expense"
        assert TransactionType.INCOME.value == "income"
        assert TransactionType.TRANSFER.value == "transfer"
        assert TransactionType.REFUND.value == "refund"
        assert TransactionType.UNKNOWN.value == "unknown"


class TestDedupStatus:
    def test_values(self):
        assert DedupStatus.UNIQUE.value == "unique"
        assert DedupStatus.DUPLICATE.value == "duplicate"
        assert DedupStatus.REVIEW.value == "review"
        assert DedupStatus.INTERNAL_TRANSFER.value == "internal_transfer"


# ── Transaction tests ─────────────────────────────────────────────────────


class TestTransactionCreation:
    def test_defaults(self):
        tx = Transaction()
        assert tx.id  # non-empty auto-generated id
        assert tx.platform is Platform.UNKNOWN
        assert isinstance(tx.datetime, datetime)
        assert tx.amount == Decimal("0")
        assert tx.counterparty == ""
        assert tx.description == ""
        assert tx.tx_type is TransactionType.UNKNOWN
        assert tx.status is DedupStatus.UNIQUE
        assert tx.fingerprints == {}
        assert tx.duplicate_of is None
        assert tx.match_level is None

    def test_post_init_coercion_str_to_decimal(self):
        tx = Transaction(amount="123.45")
        assert isinstance(tx.amount, Decimal)
        assert tx.amount == Decimal("123.45")

    def test_post_init_coercion_int_to_decimal(self):
        tx = Transaction(amount=50)
        assert isinstance(tx.amount, Decimal)
        assert tx.amount == Decimal("50")

    def test_post_init_coercion_float_to_decimal(self):
        tx = Transaction(amount=9.99)
        assert isinstance(tx.amount, Decimal)

    def test_post_init_coercion_str_to_platform(self):
        tx = Transaction(platform="alipay")
        assert isinstance(tx.platform, Platform)
        assert tx.platform is Platform.ALIPAY


class TestTransactionProperties:
    def test_is_expense(self):
        tx = Transaction(amount=Decimal("-50"))
        assert tx.is_expense is True
        assert tx.is_income is False

    def test_is_income(self):
        tx = Transaction(amount=Decimal("50"))
        assert tx.is_income is True
        assert tx.is_expense is False

    def test_zero_amount(self):
        tx = Transaction(amount=Decimal("0"))
        assert tx.is_expense is False
        assert tx.is_income is False

    def test_amount_abs(self):
        assert Transaction(amount=Decimal("-100")).amount_abs == Decimal("100")
        assert Transaction(amount=Decimal("100")).amount_abs == Decimal("100")
        assert Transaction(amount=Decimal("0")).amount_abs == Decimal("0")

    def test_amount_cents(self):
        assert Transaction(amount=Decimal("-100.50")).amount_cents == 10050
        assert Transaction(amount=Decimal("3.00")).amount_cents == 300
        assert Transaction(amount=Decimal("0.01")).amount_cents == 1

    def test_date_str(self):
        tx = Transaction(datetime=datetime(2024, 3, 15, 10, 30, 0))
        assert tx.date_str == "2024-03-15"

    def test_time_str(self):
        tx = Transaction(datetime=datetime(2024, 3, 15, 10, 30, 45))
        assert tx.time_str == "10:30:45"


class TestTransactionToDict:
    def test_keys_present(self):
        tx = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100"),
            counterparty="星巴克",
            tx_type=TransactionType.EXPENSE,
        )
        d = tx.to_dict()
        for key in [
            "id", "platform", "datetime", "date", "time",
            "amount", "counterparty", "description", "tx_type",
            "payment_method", "status", "fingerprints",
            "duplicate_of", "match_level",
        ]:
            assert key in d

    def test_platform_value_serialized(self):
        tx = Transaction(platform=Platform.ALIPAY)
        assert tx.to_dict()["platform"] == "alipay"

    def test_amount_serialized_as_str(self):
        tx = Transaction(amount=Decimal("-99.99"))
        assert tx.to_dict()["amount"] == "-99.99"


# ── DedupResult tests ─────────────────────────────────────────────────────


class TestDedupResult:
    def test_creation_defaults(self):
        tx = Transaction()
        result = DedupResult(
            transaction=tx,
            status=DedupStatus.UNIQUE,
            fingerprints={},
        )
        assert result.kept is True
        assert result.duplicate_of is None
        assert result.match_level is None
        assert result.review_reason is None

    def test_to_dict(self):
        tx = Transaction(id="abc123")
        result = DedupResult(
            transaction=tx,
            status=DedupStatus.DUPLICATE,
            fingerprints={"L1": "hash1"},
            duplicate_of=tx,
            match_level="L1",
            kept=False,
            review_reason="exact match",
        )
        d = result.to_dict()
        assert d["status"] == "duplicate"
        assert d["match_level"] == "L1"
        assert d["kept"] is False
        assert d["review_reason"] == "exact match"
        assert d["duplicate_of"] == "abc123"
        assert d["fingerprints"] == {"L1": "hash1"}


# ── DeduplicationReport tests ─────────────────────────────────────────────


class TestDeduplicationReport:
    def _make_result(self, status, platform_value="alipay"):
        tx = Transaction(platform=Platform(platform_value))
        return DedupResult(transaction=tx, status=status, fingerprints={})

    def test_add_result_unique(self):
        report = DeduplicationReport()
        report.add_result(self._make_result(DedupStatus.UNIQUE))
        assert report.total_input == 1
        assert report.unique_count == 1

    def test_add_result_duplicate(self):
        report = DeduplicationReport()
        report.add_result(self._make_result(DedupStatus.DUPLICATE))
        assert report.duplicate_count == 1

    def test_add_result_review(self):
        report = DeduplicationReport()
        report.add_result(self._make_result(DedupStatus.REVIEW))
        assert report.review_count == 1

    def test_add_result_internal_transfer(self):
        report = DeduplicationReport()
        report.add_result(self._make_result(DedupStatus.INTERNAL_TRANSFER))
        assert report.internal_transfer_count == 1

    def test_add_result_counts_by_platform(self):
        report = DeduplicationReport()
        report.add_result(self._make_result(DedupStatus.UNIQUE, "alipay"))
        report.add_result(self._make_result(DedupStatus.UNIQUE, "wechat"))
        report.add_result(self._make_result(DedupStatus.UNIQUE, "bank"))
        assert report.by_platform["alipay"] == 1
        assert report.by_platform["wechat"] == 1
        assert report.by_platform["bank"] == 1

    def test_to_dict(self):
        report = DeduplicationReport()
        report.add_result(self._make_result(DedupStatus.UNIQUE))
        report.add_result(self._make_result(DedupStatus.DUPLICATE))
        d = report.to_dict()
        assert d["total_input"] == 2
        assert d["unique_count"] == 1
        assert d["duplicate_count"] == 1
        assert "duplicate_rate" in d

    def test_to_dict_duplicate_rate_zero_safe(self):
        report = DeduplicationReport()
        d = report.to_dict()
        assert "0.0%" in d["duplicate_rate"]

    def test_str_contains_key_info(self):
        report = DeduplicationReport()
        report.add_result(self._make_result(DedupStatus.UNIQUE, "alipay"))
        report.add_result(self._make_result(DedupStatus.DUPLICATE, "bank"))
        text = str(report)
        assert "去重报告" in text
        assert "总输入交易" in text
        assert "支付宝" in text
        assert "唯一交易" in text
        assert "重复交易" in text
