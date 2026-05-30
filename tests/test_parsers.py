"""
Tests for beancount_dedup.parsers — format detection, amount/date parsing.
"""

import csv
import os
import tempfile

import pytest
from decimal import Decimal

from beancount_dedup.parsers.alipay_parser import AlipayParser
from beancount_dedup.parsers.wechat_parser import WechatParser
from beancount_dedup.parsers.bank_parser import BankParser
from beancount_dedup.parsers.base import BaseParser, ParseResult, AutoParser

from tests.conftest import ALIPAY_CSV_DATA, WECHAT_CSV_DATA, BANK_CSV_DATA


# ── detect_format ─────────────────────────────────────────────────────────


class TestAlipayDetectFormat:
    def test_valid_headers(self):
        parser = AlipayParser()
        headers = [
            "交易号", "商家订单号", "交易创建时间", "付款时间",
            "最近修改时间", "交易来源地", "类型", "交易对方",
            "商品名称", "金额（元）", "收/支", "交易状态",
            "服务费（元）", "成功退款（元）", "备注", "资金状态", "交易方式",
        ]
        assert parser.detect_format(headers) is True

    def test_minimal_valid_headers(self):
        parser = AlipayParser()
        headers = [
            "交易号", "商家订单号", "交易创建时间", "交易对方",
            "金额（元）", "收/支", "交易状态", "其他列",
        ]
        assert parser.detect_format(headers) is True

    def test_invalid_headers(self):
        parser = AlipayParser()
        headers = ["日期", "时间", "收入", "支出", "余额", "交易对手", "摘要"]
        assert parser.detect_format(headers) is False

    def test_empty_headers(self):
        parser = AlipayParser()
        assert parser.detect_format([]) is False

    def test_wechat_headers_not_detected(self):
        parser = AlipayParser()
        headers = [
            "交易时间", "交易类型", "交易对方", "商品",
            "收/支", "金额(元)", "支付方式", "当前状态",
            "交易单号", "商户单号", "备注",
        ]
        assert parser.detect_format(headers) is False


class TestWechatDetectFormat:
    def test_valid_headers(self):
        parser = WechatParser()
        headers = [
            "交易时间", "交易类型", "交易对方", "商品",
            "收/支", "金额(元)", "支付方式", "当前状态",
            "交易单号", "商户单号", "备注",
        ]
        assert parser.detect_format(headers) is True

    def test_invalid_headers(self):
        parser = WechatParser()
        headers = ["交易号", "商家订单号", "交易创建时间"]
        assert parser.detect_format(headers) is False


class TestBankDetectFormat:
    def test_valid_headers(self):
        parser = BankParser()
        headers = ["交易日期", "交易时间", "收入", "支出", "余额", "交易对手", "摘要"]
        assert parser.detect_format(headers) is True

    def test_invalid_headers_alipay(self):
        parser = BankParser()
        headers = [
            "交易号", "商家订单号", "交易创建时间", "交易对方",
            "金额（元）", "收/支", "交易状态",
        ]
        assert parser.detect_format(headers) is False

    def test_invalid_headers_wechat(self):
        parser = BankParser()
        headers = [
            "交易时间", "交易类型", "交易对方", "商品",
            "收/支", "金额(元)", "当前状态", "交易单号",
        ]
        assert parser.detect_format(headers) is False


# ── BaseParser._clean_amount ──────────────────────────────────────────────


class TestCleanAmount:
    @pytest.fixture
    def parser(self):
        """Use AlipayParser as a concrete subclass to access _clean_amount."""
        return AlipayParser()

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("¥100.00", Decimal("100.00")),
            ("-50.5", Decimal("-50.5")),
            ("+1,234.56", Decimal("1234.56")),
            ("￥200", Decimal("200")),
            ("  42.00  ", Decimal("42.00")),
            ("", Decimal("0")),
            ("abc", Decimal("0")),
        ],
    )
    def test_various_formats(self, parser, raw, expected):
        result = parser._clean_amount(raw)
        assert result == expected


# ── BaseParser._parse_datetime ────────────────────────────────────────────


class TestParseDatetime:
    @pytest.fixture
    def parser(self):
        return AlipayParser()

    def test_standard_datetime(self, parser):
        dt = parser._parse_datetime("2024-01-15 14:30:00")
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 14
        assert dt.minute == 30

    def test_date_only(self, parser):
        dt = parser._parse_datetime("2024-01-15")
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15

    def test_date_and_time_separate(self, parser):
        dt = parser._parse_datetime("2024-01-15", "14:30:00")
        assert dt.hour == 14
        assert dt.minute == 30

    def test_slash_format(self, parser):
        dt = parser._parse_datetime("2024/01/15 14:30")
        assert dt.year == 2024
        assert dt.month == 1

    def test_invalid_raises(self, parser):
        with pytest.raises(ValueError):
            parser._parse_datetime("not-a-date")


# ── Full CSV file parse (with tempfile) ──────────────────────────────────


class TestParseCSVFiles:
    def _write_csv(self, content):
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        )
        f.write(content)
        f.close()
        return f.name

    def test_alipay_csv_parse(self):
        path = self._write_csv(ALIPAY_CSV_DATA)
        try:
            parser = AlipayParser()
            result = parser.parse(path)
            assert result.parsed_rows > 0
            assert len(result.transactions) > 0
            tx = result.transactions[0]
            assert tx.counterparty == "星巴克"
            assert tx.amount == Decimal("-100.00")
        finally:
            os.unlink(path)

    def test_bank_csv_parse(self):
        path = self._write_csv(BANK_CSV_DATA)
        try:
            parser = BankParser()
            result = parser.parse(path)
            assert result.parsed_rows > 0
            assert len(result.transactions) > 0
        finally:
            os.unlink(path)

    def test_wechat_csv_parse(self):
        # WechatParser has a custom parse() that looks for header line
        path = self._write_csv(WECHAT_CSV_DATA)
        try:
            parser = WechatParser()
            result = parser.parse(path)
            assert result.parsed_rows > 0
            assert len(result.transactions) > 0
        finally:
            os.unlink(path)

    def test_auto_parser_alipay(self):
        path = self._write_csv(ALIPAY_CSV_DATA)
        try:
            auto = AutoParser()
            auto.register(AlipayParser())
            auto.register(WechatParser())
            auto.register(BankParser())
            result = auto.parse(path)
            assert result.parsed_rows > 0
        finally:
            os.unlink(path)

    def test_auto_parser_unsupported(self):
        path = self._write_csv("a,b,c\n1,2,3\n")
        try:
            auto = AutoParser()
            auto.register(AlipayParser())
            auto.register(WechatParser())
            auto.register(BankParser())
            result = auto.parse(path)
            assert len(result.errors) > 0
        finally:
            os.unlink(path)


# ── ParseResult __str__ ───────────────────────────────────────────────────


class TestParseResultStr:
    def test_str(self):
        pr = ParseResult(total_rows=10, parsed_rows=8, errors=["err1"])
        s = str(pr)
        assert "10" in s
        assert "8" in s
