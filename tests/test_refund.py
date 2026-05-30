"""
Tests for beancount_dedup.refund — RefundDetector, CancelledTransactionFilter,
and integration with parsed Alipay transactions.
"""

import os
import tempfile
from datetime import datetime
from decimal import Decimal

import pytest
from beancount_dedup.models import Platform, Transaction, TransactionType
from beancount_dedup.parsers.alipay_parser import AlipayParser
from beancount_dedup.refund import (
    CancelledTransactionFilter,
    RefundDetector,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_tx(
    counterparty: str = "测试商家",
    amount: Decimal = Decimal("-100.00"),
    dt: datetime | None = None,
    tx_type: TransactionType = TransactionType.EXPENSE,
    raw_status: str = "",
    platform: Platform = Platform.ALIPAY,
) -> Transaction:
    """Quick factory for Transaction objects used in tests."""
    return Transaction(
        platform=platform,
        datetime=dt or datetime(2024, 3, 1, 12, 0, 0),
        amount=amount,
        counterparty=counterparty,
        description="测试交易",
        tx_type=tx_type,
        raw_status=raw_status,
    )


# ── RefundDetector ───────────────────────────────────────────────────────────


class TestRefundDetectorPairing:
    """RefundDetector pairs a refund transaction with the original expense."""

    def test_basic_refund_pairing(self):
        """Same counterparty + same amount abs value within 30 days => paired."""
        original = _make_tx(
            counterparty="星巴克",
            amount=Decimal("-100.00"),
            dt=datetime(2024, 3, 1, 10, 0, 0),
            tx_type=TransactionType.EXPENSE,
        )
        refund = _make_tx(
            counterparty="星巴克",
            amount=Decimal("100.00"),
            dt=datetime(2024, 3, 5, 10, 0, 0),
            tx_type=TransactionType.REFUND,
        )

        detector = RefundDetector()
        report = detector.detect([original, refund])

        assert report.paired_count == 1
        assert report.cancelled_count == 0
        assert len(report.unpaired_refunds) == 0

        pair = report.refund_pairs[0]
        assert pair.original is original
        assert pair.refund is refund
        assert original.refund_of is None  # original doesn't point to anything
        assert refund.refund_of == original.id

    def test_refund_by_raw_status(self):
        """Transactions with raw_status indicating refund are detected even if tx_type is not REFUND."""
        original = _make_tx(
            counterparty="麦当劳",
            amount=Decimal("-50.00"),
            dt=datetime(2024, 5, 1, 12, 0, 0),
            tx_type=TransactionType.EXPENSE,
        )
        refund = _make_tx(
            counterparty="麦当劳",
            amount=Decimal("50.00"),
            dt=datetime(2024, 5, 3, 12, 0, 0),
            tx_type=TransactionType.INCOME,
            raw_status="退款成功",
        )

        detector = RefundDetector()
        report = detector.detect([original, refund])

        assert report.paired_count == 1

    def test_no_pairing_beyond_window(self):
        """Refund beyond 30 days should remain unpaired."""
        original = _make_tx(
            counterparty="肯德基",
            amount=Decimal("-80.00"),
            dt=datetime(2024, 1, 1, 12, 0, 0),
            tx_type=TransactionType.EXPENSE,
        )
        refund = _make_tx(
            counterparty="肯德基",
            amount=Decimal("80.00"),
            dt=datetime(2024, 3, 1, 12, 0, 0),  # 59 days later
            tx_type=TransactionType.REFUND,
        )

        detector = RefundDetector(match_window_days=30)
        report = detector.detect([original, refund])

        assert report.paired_count == 0
        assert len(report.unpaired_refunds) == 1

    def test_no_pairing_wrong_counterparty(self):
        """Different counterparty => no pair."""
        original = _make_tx(
            counterparty="星巴克",
            amount=Decimal("-100.00"),
            dt=datetime(2024, 3, 1, 10, 0, 0),
        )
        refund = _make_tx(
            counterparty="瑞幸咖啡",
            amount=Decimal("100.00"),
            dt=datetime(2024, 3, 2, 10, 0, 0),
            tx_type=TransactionType.REFUND,
        )

        detector = RefundDetector()
        report = detector.detect([original, refund])

        assert report.paired_count == 0

    def test_no_pairing_wrong_amount(self):
        """Different amount => no pair."""
        original = _make_tx(
            counterparty="星巴克",
            amount=Decimal("-100.00"),
            dt=datetime(2024, 3, 1, 10, 0, 0),
        )
        refund = _make_tx(
            counterparty="星巴克",
            amount=Decimal("80.00"),
            dt=datetime(2024, 3, 2, 10, 0, 0),
            tx_type=TransactionType.REFUND,
        )

        detector = RefundDetector()
        report = detector.detect([original, refund])

        assert report.paired_count == 0

    def test_partial_counterparty_match(self):
        """Shorter counterparty name contained in longer name => paired."""
        original = _make_tx(
            counterparty="星巴克",
            amount=Decimal("-50.00"),
            dt=datetime(2024, 3, 1, 10, 0, 0),
        )
        refund = _make_tx(
            counterparty="星巴克咖啡",
            amount=Decimal("50.00"),
            dt=datetime(2024, 3, 2, 10, 0, 0),
            tx_type=TransactionType.REFUND,
        )

        detector = RefundDetector()
        report = detector.detect([original, refund])

        assert report.paired_count == 1

    def test_multiple_refunds_pick_closest(self):
        """When multiple candidates exist, pick the one closest to the refund date."""
        original_old = _make_tx(
            counterparty="必胜客",
            amount=Decimal("-200.00"),
            dt=datetime(2024, 2, 1, 10, 0, 0),
        )
        original_recent = _make_tx(
            counterparty="必胜客",
            amount=Decimal("-200.00"),
            dt=datetime(2024, 2, 25, 10, 0, 0),
        )
        refund = _make_tx(
            counterparty="必胜客",
            amount=Decimal("200.00"),
            dt=datetime(2024, 3, 1, 10, 0, 0),
            tx_type=TransactionType.REFUND,
        )

        detector = RefundDetector()
        report = detector.detect([original_old, original_recent, refund])

        assert report.paired_count == 1
        assert report.refund_pairs[0].original is original_recent

    def test_cancelled_transaction_detected(self):
        """Transactions with raw_status in CANCELLED_STATUSES are flagged."""
        cancelled = _make_tx(
            counterparty="美团",
            amount=Decimal("-30.00"),
            raw_status="交易关闭",
        )

        detector = RefundDetector()
        report = detector.detect([cancelled])

        assert report.cancelled_count == 1
        assert "cancelled" in cancelled.tags

    def test_refund_tags_applied(self):
        """Paired transactions get 'refunded' and 'refund' tags."""
        original = _make_tx(
            counterparty="盒马",
            amount=Decimal("-60.00"),
            dt=datetime(2024, 3, 1, 10, 0, 0),
        )
        refund = _make_tx(
            counterparty="盒马",
            amount=Decimal("60.00"),
            dt=datetime(2024, 3, 3, 10, 0, 0),
            tx_type=TransactionType.REFUND,
        )

        detector = RefundDetector()
        report = detector.detect([original, refund])

        assert "refunded" in original.tags
        assert "refund" in refund.tags

    def test_refund_report_str(self):
        """RefundReport __str__ produces readable output."""
        original = _make_tx(
            counterparty="星巴克",
            amount=Decimal("-100.00"),
            dt=datetime(2024, 3, 1, 10, 0, 0),
        )
        refund = _make_tx(
            counterparty="星巴克",
            amount=Decimal("100.00"),
            dt=datetime(2024, 3, 5, 10, 0, 0),
            tx_type=TransactionType.REFUND,
        )

        detector = RefundDetector()
        report = detector.detect([original, refund])

        text = str(report)
        assert "退款检测报告" in text
        assert "已配对退款: 1" in text

    def test_refund_report_to_dict(self):
        """RefundReport.to_dict() includes expected keys."""
        original = _make_tx(
            counterparty="星巴克",
            amount=Decimal("-100.00"),
            dt=datetime(2024, 3, 1, 10, 0, 0),
        )
        refund = _make_tx(
            counterparty="星巴克",
            amount=Decimal("100.00"),
            dt=datetime(2024, 3, 5, 10, 0, 0),
            tx_type=TransactionType.REFUND,
        )

        detector = RefundDetector()
        report = detector.detect([original, refund])
        d = report.to_dict()

        assert d["paired_refunds"] == 1
        assert d["cancelled_transactions"] == 0
        assert len(d["pairs"]) == 1
        assert d["pairs"][0]["original_id"] == original.id


# ── CancelledTransactionFilter ───────────────────────────────────────────────


class TestCancelledTransactionFilter:
    """CancelledTransactionFilter in 'filter' and 'mark' modes."""

    def test_filter_mode_removes_cancelled(self):
        """'filter' mode returns only non-cancelled transactions."""
        tx_ok = _make_tx(counterparty="正常", amount=Decimal("-50.00"))
        tx_cancelled = _make_tx(
            counterparty="已取消",
            amount=Decimal("-30.00"),
            raw_status="交易关闭",
        )

        f = CancelledTransactionFilter(mode="filter")
        result = f.filter([tx_ok, tx_cancelled])

        assert len(result) == 1
        assert result[0] is tx_ok

    def test_filter_mode_keeps_all_on_normal(self):
        """'filter' mode keeps transactions without cancel status."""
        tx1 = _make_tx(counterparty="A", amount=Decimal("-10.00"))
        tx2 = _make_tx(counterparty="B", amount=Decimal("-20.00"))

        f = CancelledTransactionFilter(mode="filter")
        result = f.filter([tx1, tx2])

        assert len(result) == 2

    def test_mark_mode_tags_cancelled(self):
        """'mark' mode keeps all transactions but adds #cancelled tag."""
        tx_ok = _make_tx(counterparty="正常", amount=Decimal("-50.00"))
        tx_cancelled = _make_tx(
            counterparty="已取消",
            amount=Decimal("-30.00"),
            raw_status="交易关闭",
        )

        f = CancelledTransactionFilter(mode="mark")
        result = f.filter([tx_ok, tx_cancelled])

        assert len(result) == 2
        assert "cancelled" not in tx_ok.tags
        assert "cancelled" in tx_cancelled.tags

    def test_mark_mode_respects_existing_tags(self):
        """'mark' mode adds tag without removing others."""
        tx = _make_tx(counterparty="测试", amount=Decimal("-10.00"), raw_status="交易关闭")
        tx.tags.add("important")

        f = CancelledTransactionFilter(mode="mark")
        f.filter([tx])

        assert "cancelled" in tx.tags
        assert "important" in tx.tags

    def test_invalid_mode_raises(self):
        """Invalid mode raises ValueError."""
        with pytest.raises(ValueError, match="Invalid mode"):
            CancelledTransactionFilter(mode="delete")

    def test_filter_mode_detects_tagged_cancelled(self):
        """'filter' mode also drops transactions pre-tagged as cancelled."""
        tx = _make_tx(counterparty="测试", amount=Decimal("-10.00"))
        tx.tags.add("cancelled")

        f = CancelledTransactionFilter(mode="filter")
        result = f.filter([tx])

        assert len(result) == 0


# ── Integration with Alipay parsed transactions ──────────────────────────────


# CSV data with a "交易关闭" row and a "退款成功" row
ALIPAY_CSV_WITH_REFUND_CANCEL = """交易号,商家订单号,交易创建时间,付款时间,最近修改时间,交易来源地,类型,交易对方,商品名称,金额（元）,收/支,交易状态,服务费（元）,成功退款（元）,备注,资金状态,交易方式
TX001,,2024-03-01 10:00:00,2024-03-01 10:00:00,2024-03-01 10:00:00,APP,餐饮美食,星巴克,拿铁咖啡,100.00,支出,支付成功,0.00,,,余额,余额支付
TX002,,2024-03-02 10:00:00,2024-03-02 10:00:00,2024-03-02 10:00:00,APP,餐饮美食,瑞幸咖啡,生椰拿铁,30.00,支出,交易关闭,0.00,,,余额,余额支付
TX003,,2024-03-05 10:00:00,2024-03-05 10:00:00,2024-03-05 10:00:00,APP,餐饮美食,星巴克,退款-拿铁咖啡,100.00,收入,退款成功,0.00,,,余额,余额支付
TX004,,2024-03-06 10:00:00,2024-03-06 10:00:00,2024-03-06 10:00:00,APP,餐饮美食,必胜客,披萨,80.00,支出,支付成功,0.00,,,余额,余额支付
TX005,,2024-03-07 10:00:00,2024-03-07 10:00:00,2024-03-07 10:00:00,APP,餐饮美食,必胜客,退款-披萨,80.00,收入,退款成功,0.00,,,余额,余额支付
TX006,,2024-03-08 10:00:00,2024-03-08 10:00:00,2024-03-08 10:00:00,APP,餐饮美食,海底捞,火锅,200.00,支出,退款中,0.00,,,余额,余额支付
"""


class TestAlipayIntegration:
    """Integration: parse Alipay CSV with refund/cancel statuses, then run RefundDetector."""

    def _write_csv(self, content: str) -> str:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as f:
            f.write(content)
            return f.name

    def test_alipay_parses_cancelled_and_refund_rows(self):
        """Parser should return rows with '交易关闭', '退款成功', and '退款中' statuses."""
        path = self._write_csv(ALIPAY_CSV_WITH_REFUND_CANCEL)
        try:
            parser = AlipayParser()
            result = parser.parse(path)
            # Should parse 6 rows (TX001-TX006)
            assert result.parsed_rows == 6
        finally:
            os.unlink(path)

    def test_alipay_raw_status_preserved(self):
        """Parsed transactions should carry raw_status from the CSV."""
        path = self._write_csv(ALIPAY_CSV_WITH_REFUND_CANCEL)
        try:
            parser = AlipayParser()
            result = parser.parse(path)

            statuses = {tx.raw_status for tx in result.transactions}
            assert "支付成功" in statuses
            assert "交易关闭" in statuses
            assert "退款成功" in statuses
            assert "退款中" in statuses
        finally:
            os.unlink(path)

    def test_refund_detector_on_parsed_alipay_data(self):
        """Full pipeline: parse -> RefundDetector -> verify pairing and cancellation."""
        path = self._write_csv(ALIPAY_CSV_WITH_REFUND_CANCEL)
        try:
            parser = AlipayParser()
            result = parser.parse(path)
            transactions = result.transactions

            detector = RefundDetector()
            report = detector.detect(transactions)

            # TX002 (瑞幸咖啡, 交易关闭) should be cancelled
            assert report.cancelled_count == 1
            cancelled_tx = report.cancelled_transactions[0]
            assert cancelled_tx.counterparty == "瑞幸咖啡"
            assert "cancelled" in cancelled_tx.tags

            # TX001 (星巴克) + TX003 (退款) should be paired
            # TX004 (必胜客) + TX005 (退款) should be paired
            assert report.paired_count == 2

            # TX006 (海底捞, 退款中) is a refund pending with no original => unpaired
            assert len(report.unpaired_refunds) == 1
            assert report.unpaired_refunds[0].counterparty == "海底捞"
        finally:
            os.unlink(path)

    def test_cancelled_filter_on_parsed_alipay_data(self):
        """CancelledTransactionFilter 'filter' mode drops the 交易关闭 row."""
        path = self._write_csv(ALIPAY_CSV_WITH_REFUND_CANCEL)
        try:
            parser = AlipayParser()
            result = parser.parse(path)
            transactions = result.transactions

            # First detect refunds so cancelled tags are applied
            detector = RefundDetector()
            detector.detect(transactions)

            # Now filter
            cf = CancelledTransactionFilter(mode="filter")
            filtered = cf.filter(transactions)

            # Should have dropped TX002 (瑞幸咖啡, 交易关闭)
            counterparties = [tx.counterparty for tx in filtered]
            assert "瑞幸咖啡" not in counterparties
            assert "星巴克" in counterparties
            assert "必胜客" in counterparties
        finally:
            os.unlink(path)

    def test_cancelled_mark_on_parsed_alipay_data(self):
        """CancelledTransactionFilter 'mark' mode keeps all rows, tags cancelled."""
        path = self._write_csv(ALIPAY_CSV_WITH_REFUND_CANCEL)
        try:
            parser = AlipayParser()
            result = parser.parse(path)
            transactions = result.transactions

            detector = RefundDetector()
            detector.detect(transactions)

            cf = CancelledTransactionFilter(mode="mark")
            marked = cf.filter(transactions)

            assert len(marked) == 6
            # Find the cancelled one
            cancelled = [tx for tx in marked if tx.counterparty == "瑞幸咖啡"]
            assert len(cancelled) == 1
            assert "cancelled" in cancelled[0].tags
        finally:
            os.unlink(path)

    def test_transaction_to_dict_includes_new_fields(self):
        """Transaction.to_dict() includes raw_status, tags, and refund_of."""
        path = self._write_csv(ALIPAY_CSV_WITH_REFUND_CANCEL)
        try:
            parser = AlipayParser()
            result = parser.parse(path)
            transactions = result.transactions

            detector = RefundDetector()
            detector.detect(transactions)

            # Find the refund for 星巴克 (TX003)
            refund_tx = [
                tx
                for tx in transactions
                if tx.counterparty == "星巴克" and tx.tx_type == TransactionType.REFUND
            ]
            assert len(refund_tx) == 1

            d = refund_tx[0].to_dict()
            assert "raw_status" in d
            assert d["raw_status"] == "退款成功"
            assert "tags" in d
            assert "refund" in d["tags"]
            assert "refund_of" in d
            assert d["refund_of"] is not None
        finally:
            os.unlink(path)
