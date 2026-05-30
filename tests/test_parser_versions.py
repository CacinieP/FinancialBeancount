"""
Tests for parser format version detection, multi-variant support, and BOM handling.

Covers:
- AlipayParser (v1, v2, v3) format detection and format_version property
- AlipayParserV3 parsing
- WechatParser (2023, 2024) format detection and format_version property
- BOM character handling in both parsers
"""

import os
import tempfile
from decimal import Decimal

from beancount_dedup.parsers.alipay_parser import AlipayParser, AlipayParserV3
from beancount_dedup.parsers.wechat_parser import WechatParser, _strip_bom

# ── Alipay format version detection ──────────────────────────────────────


class TestAlipayFormatVersion:
    """Test that AlipayParser detects all known format variants."""

    V1_HEADERS = [
        "交易号",
        "商家订单号",
        "交易创建时间",
        "付款时间",
        "最近修改时间",
        "交易来源地",
        "类型",
        "交易对方",
        "商品名称",
        "金额（元）",
        "收/支",
        "交易状态",
        "服务费（元）",
        "成功退款（元）",
        "备注",
        "资金状态",
        "交易方式",
    ]

    V2_HEADERS = [
        "交易订单号",
        "商户订单号",
        "创建时间",
        "对方账户",
        "商品说明",
        "金额",
        "收/付款方式",
        "交易状态",
        "备注",
    ]

    V3_HEADERS = [
        "交易时间",
        "交易分类",
        "交易对方",
        "商品说明",
        "金额",
        "收/支",
        "收付款方式",
        "交易状态",
    ]

    def test_v1_detected(self):
        parser = AlipayParser()
        assert parser.detect_format(self.V1_HEADERS) is True
        assert parser.format_version == "alipay_v1"

    def test_v2_detected(self):
        parser = AlipayParser()
        assert parser.detect_format(self.V2_HEADERS) is True
        assert parser.format_version == "alipay_v2"

    def test_v3_detected(self):
        parser = AlipayParser()
        assert parser.detect_format(self.V3_HEADERS) is True
        assert parser.format_version == "alipay_v3"

    def test_invalid_headers_not_detected(self):
        parser = AlipayParser()
        headers = ["日期", "时间", "收入", "支出", "余额", "交易对手", "摘要"]
        assert parser.detect_format(headers) is False
        assert parser.format_version is None

    def test_empty_headers(self):
        parser = AlipayParser()
        assert parser.detect_format([]) is False
        assert parser.format_version is None

    def test_format_version_initially_none(self):
        parser = AlipayParser()
        assert parser.format_version is None

    def test_detect_format_resets_version_on_new_call(self):
        """detect_format should update version on each call."""
        parser = AlipayParser()
        parser.detect_format(self.V1_HEADERS)
        assert parser.format_version == "alipay_v1"

        parser.detect_format(self.V3_HEADERS)
        assert parser.format_version == "alipay_v3"

        parser.detect_format(["unknown", "headers"])
        # On failure, the version from the last successful match should remain
        # because detect_format returns False without setting _format_version
        # Actually let's check behavior: on failed detect, version stays from previous
        # since we only set _format_version inside the matching loop
        # The version should still be alipay_v3 because failure does not reset it
        assert parser.format_version == "alipay_v3"

    def test_v2_non_standard_warning_logged(self, caplog):
        """Non-standard format variants should produce a warning log."""
        import logging

        with caplog.at_level(logging.WARNING, logger="beancount_dedup.parsers.alipay_parser"):
            parser = AlipayParser()
            parser.detect_format(self.V2_HEADERS)
        assert "non-standard" in caplog.text.lower() or "Using non-standard" in caplog.text

    def test_v3_non_standard_warning_logged(self, caplog):
        """Non-standard format variants should produce a warning log."""
        import logging

        with caplog.at_level(logging.WARNING, logger="beancount_dedup.parsers.alipay_parser"):
            parser = AlipayParser()
            parser.detect_format(self.V3_HEADERS)
        assert "non-standard" in caplog.text.lower() or "Using non-standard" in caplog.text

    def test_v1_no_warning(self, caplog):
        """V1 (standard) format should not produce a warning log."""
        import logging

        with caplog.at_level(logging.WARNING, logger="beancount_dedup.parsers.alipay_parser"):
            parser = AlipayParser()
            parser.detect_format(self.V1_HEADERS)
        assert "non-standard" not in caplog.text.lower()


# ── AlipayParserV3 parsing ───────────────────────────────────────────────


class TestAlipayV3Parsing:
    """Test that AlipayParserV3 correctly parses V3-format rows."""

    def test_parse_expense_row(self):
        parser = AlipayParserV3()
        row = {
            "交易时间": "2024-03-10 12:30:00",
            "交易分类": "餐饮美食",
            "交易对方": "星巴克",
            "商品说明": "拿铁咖啡",
            "金额": "100.00",
            "收/支": "支出",
            "收付款方式": "花呗",
            "交易状态": "支付成功",
        }
        tx = parser.parse_row(row, line_num=1)
        assert tx is not None
        assert tx.counterparty == "星巴克"
        assert tx.description == "拿铁咖啡"
        assert tx.amount == Decimal("-100.00")
        assert tx.payment_method == "花呗"
        assert tx.datetime.year == 2024
        assert tx.datetime.month == 3
        assert tx.datetime.day == 10

    def test_parse_income_row(self):
        parser = AlipayParserV3()
        row = {
            "交易时间": "2024-03-11 09:00:00",
            "交易分类": "转账",
            "交易对方": "张三",
            "商品说明": "红包",
            "金额": "50.00",
            "收/支": "收入",
            "收付款方式": "余额",
            "交易状态": "交易成功",
        }
        tx = parser.parse_row(row, line_num=1)
        assert tx is not None
        assert tx.amount == Decimal("50.00")

    def test_skip_unsuccessful_status(self):
        parser = AlipayParserV3()
        row = {
            "交易时间": "2024-03-10 12:30:00",
            "交易分类": "餐饮美食",
            "交易对方": "星巴克",
            "商品说明": "拿铁咖啡",
            "金额": "100.00",
            "收/支": "支出",
            "收付款方式": "花呗",
            "交易状态": "交易关闭",
        }
        tx = parser.parse_row(row, line_num=1)
        # "交易关闭" is now kept (with raw_status) for refund detection
        assert tx is not None
        assert tx.raw_status == "交易关闭"


# ── AlipayParserV3 full CSV parse ────────────────────────────────────────


class TestAlipayV3CSVParse:
    """Test full CSV file parsing with AlipayParserV3."""

    V3_CSV_DATA = (
        "交易时间,交易分类,交易对方,商品说明,金额,收/支,收付款方式,交易状态\n"
        "2024-03-10 12:30:00,餐饮美食,星巴克,拿铁咖啡,100.00,支出,花呗,支付成功\n"
        "2024-03-11 09:00:00,转账,张三,红包,50.00,收入,余额,交易成功\n"
    )

    def _write_csv(self, content):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as f:
            f.write(content)
            return f.name

    def test_full_csv_parse(self):
        path = self._write_csv(self.V3_CSV_DATA)
        try:
            parser = AlipayParserV3()
            result = parser.parse(path)
            assert result.parsed_rows == 2
            assert len(result.transactions) == 2
            assert result.transactions[0].counterparty == "星巴克"
            assert result.transactions[0].amount == Decimal("-100.00")
            assert result.transactions[1].counterparty == "张三"
            assert result.transactions[1].amount == Decimal("50.00")
        finally:
            os.unlink(path)


# ── WeChat format version detection ──────────────────────────────────────


class TestWechatFormatVersion:
    """Test that WechatParser detects 2023 and 2024 format variants."""

    WECHAT_2023_HEADERS = [
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

    WECHAT_2024_HEADERS = [
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
        "是否为专场交易",
    ]

    def test_2023_detected(self):
        parser = WechatParser()
        assert parser.detect_format(self.WECHAT_2023_HEADERS) is True
        assert parser.format_version == "wechat_2023"

    def test_2024_detected(self):
        parser = WechatParser()
        assert parser.detect_format(self.WECHAT_2024_HEADERS) is True
        assert parser.format_version == "wechat_2024"

    def test_invalid_headers_not_detected(self):
        parser = WechatParser()
        headers = ["交易号", "商家订单号", "交易创建时间"]
        assert parser.detect_format(headers) is False
        assert parser.format_version is None

    def test_empty_headers(self):
        parser = WechatParser()
        assert parser.detect_format([]) is False
        assert parser.format_version is None

    def test_format_version_initially_none(self):
        parser = WechatParser()
        assert parser.format_version is None

    def test_format_version_logs_info(self, caplog):
        """WechatParser should log the detected format version."""
        import logging

        with caplog.at_level(logging.INFO, logger="beancount_dedup.parsers.wechat_parser"):
            parser = WechatParser()
            parser.detect_format(self.WECHAT_2024_HEADERS)
        assert "wechat_2024" in caplog.text

    def test_2024_header_has_special_column(self):
        """The 2024+ format includes the '是否为专场交易' column."""
        parser = WechatParser()
        assert parser.detect_format(self.WECHAT_2024_HEADERS) is True
        # The special column should not break parsing
        assert parser.format_version == "wechat_2024"


# ── WeChat 2024+ full CSV parse ──────────────────────────────────────────


class TestWechat2024CSVParse:
    """Test full CSV file parsing with WechatParser for 2024+ format."""

    WECHAT_2024_CSV_DATA = (
        "微信支付账单明细\n"
        "微信昵称：[测试用户]\n"
        "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,"
        "交易单号,商户单号,备注,是否为专场交易\n"
        "2024-06-15 14:02:00,商户消费,星巴克咖啡,饮品,支出,100.00,零钱,"
        "支付成功,4200001234567890123456789123,,,否\n"
        "2024-06-15 19:00:00,商户消费,滴滴出行,打车,支出,35.00,零钱,"
        "支付成功,4200001234567890123456789124,,,否\n"
    )

    def _write_csv(self, content):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as f:
            f.write(content)
            return f.name

    def test_2024_csv_parse(self):
        path = self._write_csv(self.WECHAT_2024_CSV_DATA)
        try:
            parser = WechatParser()
            result = parser.parse(path)
            assert result.parsed_rows == 2
            assert len(result.transactions) == 2
            tx0 = result.transactions[0]
            assert tx0.counterparty == "星巴克咖啡"
            assert tx0.amount == Decimal("-100.00")
            assert tx0.datetime.year == 2024
            assert tx0.datetime.month == 6
        finally:
            os.unlink(path)


# ── BOM handling ─────────────────────────────────────────────────────────


class TestBOMHandling:
    """Test BOM and BOM-like character handling."""

    def test_strip_utf8_bom(self):
        assert _strip_bom("﻿交易时间") == "交易时间"

    def test_strip_multiple_bom_chars(self):
        assert _strip_bom("﻿﻿交易时间") == "交易时间"

    def test_strip_zero_width_space(self):
        assert _strip_bom("\u200b交易时间") == "交易时间"

    def test_strip_left_to_right_mark(self):
        assert _strip_bom("‎交易时间") == "交易时间"

    def test_strip_right_to_left_mark(self):
        assert _strip_bom("‏交易时间") == "交易时间"

    def test_no_bom_unchanged(self):
        assert _strip_bom("交易时间") == "交易时间"

    def test_empty_string(self):
        assert _strip_bom("") == ""

    def test_only_bom_chars(self):
        assert _strip_bom("﻿﻿﻿") == ""

    def test_bom_in_middle_not_stripped(self):
        """Only leading BOM characters should be stripped."""
        text = "交易﻿时间"
        assert _strip_bom(text) == text

    def test_alipay_detect_format_with_bom_in_headers(self):
        """AlipayParser should handle headers with BOM prefix."""
        parser = AlipayParser()
        # V1 header with BOM on first column
        headers = [
            "﻿交易号",
            "商家订单号",
            "交易创建时间",
            "交易对方",
            "金额（元）",
            "收/支",
            "交易状态",
        ]
        assert parser.detect_format(headers) is True

    def test_wechat_detect_format_with_bom_in_headers(self):
        """WechatParser should handle headers with BOM prefix."""
        parser = WechatParser()
        headers = [
            "﻿交易时间",
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
        assert parser.detect_format(headers) is True
        assert parser.format_version == "wechat_2023"

    def test_wechat_parse_csv_with_bom(self):
        """WechatParser should correctly parse a CSV file with BOM."""
        # UTF-8 BOM + standard WeChat CSV
        content = (
            "﻿交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,"
            "当前状态,交易单号,商户单号,备注\n"
            "2024-01-15 14:02:00,商户消费,星巴克,饮品,支出,100.00,零钱,"
            "支付成功,4200001234567890123456789123,,\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as f:
            f.write(content)
            path = f.name
        try:
            parser = WechatParser()
            result = parser.parse(path)
            assert result.parsed_rows == 1
            assert len(result.transactions) == 1
            assert result.transactions[0].counterparty == "星巴克"
            assert result.transactions[0].amount == Decimal("-100.00")
        finally:
            os.unlink(path)

    def test_wechat_parse_csv_with_multiple_bom_chars(self):
        """WechatParser should handle multiple BOM-like chars at the start."""
        # Multiple BOM-like chars + header + data
        content = (
            "﻿\u200b‎交易时间,交易类型,交易对方,商品,收/支,金额(元),"
            "支付方式,当前状态,交易单号,商户单号,备注\n"
            "2024-01-15 14:02:00,商户消费,星巴克,饮品,支出,100.00,零钱,"
            "支付成功,4200001234567890123456789123,,\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as f:
            f.write(content)
            path = f.name
        try:
            parser = WechatParser()
            result = parser.parse(path)
            assert result.parsed_rows == 1
        finally:
            os.unlink(path)

    def test_wechat_parse_csv_with_bom_and_prefix_lines(self):
        """WechatParser should handle BOM with prefix metadata lines."""
        content = (
            "微信支付账单明细\n"
            "﻿交易时间,交易类型,交易对方,商品,收/支,金额(元),"
            "支付方式,当前状态,交易单号,商户单号,备注\n"
            "2024-01-15 14:02:00,商户消费,星巴克,饮品,支出,100.00,零钱,"
            "支付成功,4200001234567890123456789123,,\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as f:
            f.write(content)
            path = f.name
        try:
            parser = WechatParser()
            result = parser.parse(path)
            assert result.parsed_rows == 1
        finally:
            os.unlink(path)
