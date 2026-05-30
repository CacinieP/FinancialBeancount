"""
Tests for beancount_dedup.parsers.unionpay_parser — format detection, row parsing, full CSV parse.
"""

import os
import tempfile
from decimal import Decimal

from beancount_dedup.models import Platform, TransactionType
from beancount_dedup.parsers.base import AutoParser
from beancount_dedup.parsers.unionpay_parser import UnionPayParser

# ── Sample CSV data ──────────────────────────────────────────────────────

UNIONPAY_CSV_V1 = """交易日期,交易时间,交易类型,交易金额,账户余额,交易对方,交易备注
2024-03-10,12:30:45,消费,35.00,1250.50,星巴克,咖啡
2024-03-10,18:15:00,支付,88.00,1162.50,美团外卖,晚餐
2024-03-11,09:00:00,充值,500.00,1662.50,中国工商银行,余额充值
2024-03-11,14:20:00,退款,35.00,1697.50,星巴克,订单退款
"""

UNIONPAY_CSV_V2 = """交易日期,交易时间,交易类型,对方账号,交易金额,余额,备注
2024-03-10,12:30:45,消费,6222********1234,35.00,1250.50,咖啡消费
2024-03-10,18:15:00,支付,6222********5678,88.00,1162.50,晚餐外卖
"""

UNIONPAY_CSV_INCOME = """交易日期,交易时间,交易类型,交易金额,账户余额,交易对方,交易备注
2024-03-12,10:00:00,收入,2000.00,3697.50,某公司,工资
2024-03-12,15:00:00,红包,8.88,3706.38,好友,红包
"""

UNIONPAY_CSV_EMPTY_AMOUNT = """交易日期,交易时间,交易类型,交易金额,账户余额,交易对方,交易备注
2024-03-10,12:30:45,消费,,1250.50,星巴克,咖啡
"""

UNIONPAY_CSV_NO_DATE = """交易日期,交易时间,交易类型,交易金额,账户余额,交易对方,交易备注
,12:30:45,消费,35.00,1250.50,星巴克,咖啡
"""

UNIONPAY_CSV_ALILOOKALIKE = """交易号,商家订单号,交易创建时间,交易对方,金额（元）,收/支,交易状态
2024011522001234567890123456,,2024-01-15 14:00:00,星巴克,100.00,支出,支付成功
"""

UNIONPAY_CSV_WECHATLOOKALIKE = """交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
2024-01-15 14:02:00,商户消费,星巴克咖啡,饮品,支出,100.00,零钱,支付成功,4200001234567890123456789123,,
"""


# ── detect_format ─────────────────────────────────────────────────────────


class TestUnionPayDetectFormat:
    def test_valid_headers_v1(self):
        parser = UnionPayParser()
        headers = [
            "交易日期",
            "交易时间",
            "交易类型",
            "交易金额",
            "账户余额",
            "交易对方",
            "交易备注",
        ]
        assert parser.detect_format(headers) is True

    def test_valid_headers_v2(self):
        parser = UnionPayParser()
        headers = ["交易日期", "交易时间", "交易类型", "对方账号", "交易金额", "余额", "备注"]
        assert parser.detect_format(headers) is True

    def test_invalid_headers_alipay(self):
        parser = UnionPayParser()
        headers = [
            "交易号",
            "商家订单号",
            "交易创建时间",
            "交易对方",
            "金额（元）",
            "收/支",
            "交易状态",
        ]
        assert parser.detect_format(headers) is False

    def test_invalid_headers_wechat(self):
        parser = UnionPayParser()
        headers = [
            "交易时间",
            "交易类型",
            "交易对方",
            "商品",
            "收/支",
            "金额(元)",
            "支付方式",
            "当前状态",
            "交易单号",
            "商户单号",
            "备注",
        ]
        assert parser.detect_format(headers) is False

    def test_invalid_headers_bank_split(self):
        """Bank-style headers with separate income/expense columns should not match."""
        parser = UnionPayParser()
        headers = ["交易日期", "交易时间", "收入", "支出", "余额", "交易对手", "摘要"]
        assert parser.detect_format(headers) is False

    def test_empty_headers(self):
        parser = UnionPayParser()
        assert parser.detect_format([]) is False

    def test_none_in_headers(self):
        parser = UnionPayParser()
        headers = [None, "交易日期", "交易时间"]
        assert parser.detect_format(headers) is False

    def test_loose_match_with_keywords(self):
        """Loose match: date + amount + counterparty + type, no bank split columns."""
        parser = UnionPayParser()
        headers = ["交易日期", "交易时间", "交易类型", "交易金额", "交易对方", "备注"]
        assert parser.detect_format(headers) is True


# ── parse_row ─────────────────────────────────────────────────────────────


class TestUnionPayParseRow:
    def _make_row(self, **overrides):
        """Helper to build a row dict with sensible defaults."""
        row = {
            "交易日期": "2024-03-10",
            "交易时间": "12:30:45",
            "交易类型": "消费",
            "交易金额": "35.00",
            "账户余额": "1250.50",
            "交易对方": "星巴克",
            "交易备注": "咖啡",
        }
        row.update(overrides)
        return row

    def test_expense_row(self):
        parser = UnionPayParser()
        row = self._make_row()
        tx = parser.parse_row(row, line_num=2)
        assert tx is not None
        assert tx.amount == Decimal("-35.00")
        assert tx.counterparty == "星巴克"
        assert tx.platform == Platform.UNIONPAY
        assert tx.tx_type == TransactionType.EXPENSE
        assert tx.payment_method == "云闪付"

    def test_refund_row(self):
        parser = UnionPayParser()
        row = self._make_row(交易类型="退款", 交易备注="订单退款")
        tx = parser.parse_row(row, line_num=3)
        assert tx is not None
        assert tx.amount == Decimal("35.00")
        assert tx.tx_type == TransactionType.REFUND

    def test_transfer_row(self):
        parser = UnionPayParser()
        row = self._make_row(
            交易类型="充值", 交易对方="中国工商银行", 交易金额="500.00", 交易备注="余额充值"
        )
        tx = parser.parse_row(row, line_num=4)
        assert tx is not None
        assert tx.amount == Decimal("500.00")
        assert tx.tx_type == TransactionType.TRANSFER

    def test_income_row(self):
        parser = UnionPayParser()
        row = self._make_row(
            交易类型="收入", 交易对方="某公司", 交易金额="2000.00", 交易备注="工资"
        )
        tx = parser.parse_row(row, line_num=5)
        assert tx is not None
        assert tx.amount == Decimal("2000.00")
        assert tx.tx_type == TransactionType.INCOME

    def test_empty_amount_skipped(self):
        parser = UnionPayParser()
        row = self._make_row(交易金额="")
        tx = parser.parse_row(row, line_num=2)
        assert tx is None

    def test_empty_date_skipped(self):
        parser = UnionPayParser()
        row = self._make_row(交易日期="")
        tx = parser.parse_row(row, line_num=2)
        assert tx is None

    def test_counterparty_prefix_cleaned(self):
        parser = UnionPayParser()
        row = self._make_row(交易对方="支付宝-京东商城")
        tx = parser.parse_row(row, line_num=2)
        assert tx is not None
        assert tx.counterparty == "京东商城"

    def test_counterparty_tail_number_removed(self):
        parser = UnionPayParser()
        row = self._make_row(交易对方="张三(1234)")
        tx = parser.parse_row(row, line_num=2)
        assert tx is not None
        assert tx.counterparty == "张三"

    def test_v2_format_row(self):
        parser = UnionPayParser()
        row = {
            "交易日期": "2024-03-10",
            "交易时间": "12:30:45",
            "交易类型": "消费",
            "对方账号": "6222********1234",
            "交易金额": "35.00",
            "余额": "1250.50",
            "备注": "咖啡消费",
        }
        tx = parser.parse_row(row, line_num=2)
        assert tx is not None
        assert tx.amount == Decimal("-35.00")
        assert tx.counterparty == "6222********1234"


# ── Full CSV file parse ──────────────────────────────────────────────────


class TestUnionPayParseCSV:
    def _write_csv(self, content):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as f:
            f.write(content)
            return f.name

    def test_parse_v1_csv(self):
        path = self._write_csv(UNIONPAY_CSV_V1)
        try:
            parser = UnionPayParser()
            result = parser.parse(path)
            assert result.parsed_rows == 4
            assert len(result.transactions) == 4
            # First row: expense
            tx0 = result.transactions[0]
            assert tx0.amount == Decimal("-35.00")
            assert tx0.counterparty == "星巴克"
            assert tx0.tx_type == TransactionType.EXPENSE
            # Refund row
            tx_refund = [t for t in result.transactions if t.tx_type == TransactionType.REFUND]
            assert len(tx_refund) == 1
            assert tx_refund[0].amount == Decimal("35.00")
        finally:
            os.unlink(path)

    def test_parse_v2_csv(self):
        path = self._write_csv(UNIONPAY_CSV_V2)
        try:
            parser = UnionPayParser()
            result = parser.parse(path)
            assert result.parsed_rows == 2
            assert len(result.transactions) == 2
            tx0 = result.transactions[0]
            assert tx0.amount == Decimal("-35.00")
        finally:
            os.unlink(path)

    def test_parse_income_csv(self):
        path = self._write_csv(UNIONPAY_CSV_INCOME)
        try:
            parser = UnionPayParser()
            result = parser.parse(path)
            assert result.parsed_rows == 2
            tx0 = result.transactions[0]
            assert tx0.amount == Decimal("2000.00")
            assert tx0.tx_type == TransactionType.INCOME
            tx1 = result.transactions[1]
            assert tx1.amount == Decimal("8.88")
            assert tx1.tx_type == TransactionType.INCOME
        finally:
            os.unlink(path)

    def test_empty_amount_rows_skipped(self):
        path = self._write_csv(UNIONPAY_CSV_EMPTY_AMOUNT)
        try:
            parser = UnionPayParser()
            result = parser.parse(path)
            assert result.parsed_rows == 0
            assert len(result.transactions) == 0
        finally:
            os.unlink(path)

    def test_no_date_rows_skipped(self):
        path = self._write_csv(UNIONPAY_CSV_NO_DATE)
        try:
            parser = UnionPayParser()
            result = parser.parse(path)
            assert result.parsed_rows == 0
            assert len(result.transactions) == 0
        finally:
            os.unlink(path)

    def test_auto_parser_unionpay(self):
        path = self._write_csv(UNIONPAY_CSV_V1)
        try:
            auto = AutoParser()
            auto.register(UnionPayParser())
            result = auto.parse(path)
            assert result.parsed_rows > 0
            assert result.transactions[0].platform == Platform.UNIONPAY
        finally:
            os.unlink(path)

    def test_auto_parser_does_not_match_alipay(self):
        path = self._write_csv(UNIONPAY_CSV_ALILOOKALIKE)
        try:
            auto = AutoParser()
            auto.register(UnionPayParser())
            result = auto.parse(path)
            assert len(result.errors) > 0
        finally:
            os.unlink(path)

    def test_auto_parser_does_not_match_wechat(self):
        path = self._write_csv(UNIONPAY_CSV_WECHATLOOKALIKE)
        try:
            auto = AutoParser()
            auto.register(UnionPayParser())
            result = auto.parse(path)
            assert len(result.errors) > 0
        finally:
            os.unlink(path)


# ── Amount direction logic ───────────────────────────────────────────────


class TestAmountDirection:
    def test_expense_negative(self):
        parser = UnionPayParser()
        row = {
            "交易日期": "2024-03-10",
            "交易时间": "12:00:00",
            "交易类型": "消费",
            "交易金额": "100.00",
            "账户余额": "900.00",
            "交易对方": "商家",
            "交易备注": "",
        }
        tx = parser.parse_row(row, line_num=2)
        assert tx is not None
        assert tx.amount == Decimal("-100.00")

    def test_income_positive(self):
        parser = UnionPayParser()
        row = {
            "交易日期": "2024-03-10",
            "交易时间": "12:00:00",
            "交易类型": "收入",
            "交易金额": "100.00",
            "账户余额": "1100.00",
            "交易对方": "雇主",
            "交易备注": "",
        }
        tx = parser.parse_row(row, line_num=2)
        assert tx is not None
        assert tx.amount == Decimal("100.00")

    def test_negative_amount_preserved(self):
        """If the CSV already contains a negative amount, keep it."""
        parser = UnionPayParser()
        row = {
            "交易日期": "2024-03-10",
            "交易时间": "12:00:00",
            "交易类型": "其他",
            "交易金额": "-50.00",
            "账户余额": "950.00",
            "交易对方": "商家",
            "交易备注": "",
        }
        tx = parser.parse_row(row, line_num=2)
        assert tx is not None
        assert tx.amount == Decimal("-50.00")
