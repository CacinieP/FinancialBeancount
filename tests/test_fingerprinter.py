"""
Tests for beancount_dedup.fingerprinter — TransactionFingerprinter.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from beancount_dedup.models import Transaction, Platform
from beancount_dedup.fingerprinter import TransactionFingerprinter


# ── Fingerprint structure ─────────────────────────────────────────────────


class TestFingerprintGeneration:
    def test_l1_l2_l3_keys_present(self, fingerprinter, sample_expense_alipay):
        fp = fingerprinter.generate_fingerprints(sample_expense_alipay)
        assert "L1" in fp
        assert "L2" in fp
        assert "L3" in fp
        assert "raw" in fp

    def test_l1_is_md5_hex(self, fingerprinter, sample_expense_alipay):
        fp = fingerprinter.generate_fingerprints(sample_expense_alipay)
        assert len(fp["L1"]) == 32
        assert all(c in "0123456789abcdef" for c in fp["L1"])

    def test_l2_is_md5_hex(self, fingerprinter, sample_expense_alipay):
        fp = fingerprinter.generate_fingerprints(sample_expense_alipay)
        assert len(fp["L2"]) == 32

    def test_l3_components(self, fingerprinter, sample_expense_alipay):
        fp = fingerprinter.generate_fingerprints(sample_expense_alipay)
        l3 = fp["L3"]
        assert "date" in l3
        assert "amount_cents" in l3
        assert "is_expense" in l3
        assert "timestamp" in l3
        assert "platform" in l3

    def test_l3_amount_cents_value(self, fingerprinter):
        tx = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.50"),
            counterparty="测试",
        )
        fp = fingerprinter.generate_fingerprints(tx)
        assert fp["L3"]["amount_cents"] == 10050

    def test_raw_contains_normalized_counterparty(self, fingerprinter, sample_expense_alipay):
        fp = fingerprinter.generate_fingerprints(sample_expense_alipay)
        assert fp["raw"]["normalized_counterparty"] == "星巴克"

    def test_l1_alt_empty_for_non_bank(self, fingerprinter, sample_expense_alipay):
        fp = fingerprinter.generate_fingerprints(sample_expense_alipay)
        assert fp["L1_alt"] == []

    def test_l1_alt_present_for_bank_midnight(self, fingerprinter):
        tx = Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 15, 1, 30, 0),
            amount=Decimal("-200"),
            counterparty="测试",
        )
        fp = fingerprinter.generate_fingerprints(tx)
        assert len(fp["L1_alt"]) == 1
        assert len(fp["L2_alt"]) == 1


# ── Merchant normalization ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("支付宝-星巴克", "星巴克"),
        ("STARBUCKS COFFEE", "星巴克"),
        ("财付通-麦当劳", "麦当劳"),
        ("滴滴出行", "滴滴出行"),
        ("滴滴快车", "滴滴出行"),
        ("北京小桔科技", "滴滴出行"),
        ("京东商城", "京东"),
        ("kfc", "肯德基"),
        ("McDonald's", "麦当劳"),
        ("美团点评", "美团"),
        ("", "未知"),
    ],
)
def test_normalize_counterparty(fingerprinter, raw, expected):
    result = fingerprinter.normalize_counterparty(raw, Platform.ALIPAY)
    assert result == expected


# ── Date normalization ────────────────────────────────────────────────────


class TestNormalizeDate:
    def test_regular_date_returns_single(self, fingerprinter):
        dt = datetime(2024, 3, 15, 10, 0, 0)
        dates = fingerprinter.normalize_date(dt, Platform.ALIPAY)
        assert dates == ["2024-03-15"]

    def test_bank_midnight_returns_two(self, fingerprinter):
        dt = datetime(2024, 3, 15, 1, 30, 0)
        dates = fingerprinter.normalize_date(dt, Platform.BANK)
        assert len(dates) == 2
        assert "2024-03-15" in dates
        assert "2024-03-14" in dates

    def test_bank_at_2am_returns_single(self, fingerprinter):
        dt = datetime(2024, 3, 15, 2, 0, 0)
        dates = fingerprinter.normalize_date(dt, Platform.BANK)
        assert dates == ["2024-03-15"]

    def test_bank_just_before_2am(self, fingerprinter):
        dt = datetime(2024, 3, 15, 1, 59, 59)
        dates = fingerprinter.normalize_date(dt, Platform.BANK)
        assert len(dates) == 2


# ── L2 match ──────────────────────────────────────────────────────────────


class TestCheckL2Match:
    def test_valid_alipay_bank_combo(self, fingerprinter):
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100"),
            counterparty="星巴克",
        )
        tx2 = Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 15, 14, 1, 0),
            amount=Decimal("-100"),
            counterparty="支付宝-星巴克",
        )
        fp1 = fingerprinter.generate_fingerprints(tx1)
        fp2 = fingerprinter.generate_fingerprints(tx2)
        assert fingerprinter.check_l2_match(tx1, tx2, fp1, fp2) is True

    def test_same_platform_rejected(self, fingerprinter):
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100"),
            counterparty="A",
        )
        tx2 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 1, 0),
            amount=Decimal("-100"),
            counterparty="B",
        )
        fp1 = fingerprinter.generate_fingerprints(tx1)
        fp2 = fingerprinter.generate_fingerprints(tx2)
        assert fingerprinter.check_l2_match(tx1, tx2, fp1, fp2) is False

    def test_time_window_exceeded(self, fingerprinter):
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100"),
            counterparty="星巴克",
        )
        tx2 = Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 15, 14, 5, 0),
            amount=Decimal("-100"),
            counterparty="支付宝-星巴克",
        )
        fp1 = fingerprinter.generate_fingerprints(tx1)
        fp2 = fingerprinter.generate_fingerprints(tx2)
        assert fingerprinter.check_l2_match(tx1, tx2, fp1, fp2) is False

    def test_amount_mismatch(self, fingerprinter):
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100"),
            counterparty="星巴克",
        )
        tx2 = Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-200"),
            counterparty="支付宝-星巴克",
        )
        fp1 = fingerprinter.generate_fingerprints(tx1)
        fp2 = fingerprinter.generate_fingerprints(tx2)
        assert fingerprinter.check_l2_match(tx1, tx2, fp1, fp2) is False

    def test_direction_mismatch(self, fingerprinter):
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100"),
            counterparty="星巴克",
        )
        tx2 = Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("100"),
            counterparty="支付宝-星巴克",
        )
        fp1 = fingerprinter.generate_fingerprints(tx1)
        fp2 = fingerprinter.generate_fingerprints(tx2)
        assert fingerprinter.check_l2_match(tx1, tx2, fp1, fp2) is False


# ── L3 match ──────────────────────────────────────────────────────────────


class TestCheckL3Match:
    def test_cross_day_same_amount(self, fingerprinter):
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 23, 58, 0),
            amount=Decimal("-200"),
            counterparty="京东",
        )
        tx2 = Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 16, 0, 1, 0),
            amount=Decimal("-200"),
            counterparty="支付宝-京东",
        )
        fp1 = fingerprinter.generate_fingerprints(tx1)
        fp2 = fingerprinter.generate_fingerprints(tx2)
        match = fingerprinter.check_l3_match(tx1, tx2, fp1, fp2)
        assert match is not None
        assert match["match_type"] == "CROSS_DAY"
        assert match["needs_review"] is True

    def test_amount_diff_within_tolerance(self, fingerprinter):
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="测试",
        )
        tx2 = Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 15, 14, 0, 30),
            amount=Decimal("-100.50"),
            counterparty="测试",
        )
        fp1 = fingerprinter.generate_fingerprints(tx1)
        fp2 = fingerprinter.generate_fingerprints(tx2)
        match = fingerprinter.check_l3_match(tx1, tx2, fp1, fp2)
        assert match is not None
        assert match["match_type"] == "AMOUNT_DIFF"
        assert match["amount_diff_cents"] == 50

    def test_beyond_time_window_returns_none(self, fingerprinter):
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 1, 12, 0, 0),
            amount=Decimal("-100"),
            counterparty="测试",
        )
        tx2 = Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 3, 12, 0, 0),
            amount=Decimal("-100"),
            counterparty="测试",
        )
        fp1 = fingerprinter.generate_fingerprints(tx1)
        fp2 = fingerprinter.generate_fingerprints(tx2)
        assert fingerprinter.check_l3_match(tx1, tx2, fp1, fp2) is None

    def test_amount_beyond_tolerance_returns_none(self, fingerprinter):
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="测试",
        )
        tx2 = Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 15, 14, 0, 30),
            amount=Decimal("-500.00"),
            counterparty="测试",
        )
        fp1 = fingerprinter.generate_fingerprints(tx1)
        fp2 = fingerprinter.generate_fingerprints(tx2)
        assert fingerprinter.check_l3_match(tx1, tx2, fp1, fp2) is None


# ── Direction helper ──────────────────────────────────────────────────────


class TestDirection:
    def test_expense_is_out(self, fingerprinter):
        tx = Transaction(amount=Decimal("-50"))
        assert fingerprinter.get_direction(tx) == "OUT"

    def test_income_is_in(self, fingerprinter):
        tx = Transaction(amount=Decimal("50"))
        assert fingerprinter.get_direction(tx) == "IN"
