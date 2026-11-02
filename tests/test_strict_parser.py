"""Strict imports report malformed rows instead of silently dropping money."""

import csv
from decimal import Decimal

import pytest
from beancount_dedup.models import Platform, TransactionType
from beancount_dedup.parsers.strict import build_strict_parser

ALIPAY = {
    "交易号": "order-1",
    "商家订单号": "merchant-1",
    "交易创建时间": "2024-01-15 14:00:00",
    "交易对方": "商家",
    "商品名称": "服务",
    "金额（元）": "100.00",
    "收/支": "支出",
    "交易状态": "支付成功",
    "交易方式": "余额",
}
WECHAT = {
    "交易时间": "2024-01-15 14:00:00",
    "交易类型": "商户消费",
    "交易对方": "商家",
    "商品": "服务",
    "收/支": "支出",
    "金额(元)": "¥100.00",
    "支付方式": "零钱",
    "当前状态": "支付成功",
    "交易单号": "order-1",
    "商户单号": "merchant-1",
    "备注": "",
}
BANK = {
    "交易日期": "2024-01-15",
    "交易时间": "14:00:00",
    "收入": "",
    "支出": "100.00",
    "余额": "1000.00",
    "交易对手": "商家",
    "摘要": "服务",
}
UNIONPAY = {
    "交易日期": "2024-01-15",
    "交易时间": "14:00:00",
    "交易类型": "消费",
    "交易金额": "100.00",
    "账户余额": "1000.00",
    "交易对方": "商家",
    "交易备注": "服务",
}


def write_rows(tmp_path, rows):
    path = tmp_path / "statement.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def parse_rows(tmp_path, *rows):
    return build_strict_parser().parse(str(write_rows(tmp_path, rows)))


def test_factory_registers_all_eight_supported_variants():
    assert [type(parser).__name__ for parser in build_strict_parser().parsers] == [
        "AlipayParser",
        "AlipayParserV2",
        "AlipayParserV3",
        "WechatParser",
        "CMBParser",
        "ICBCParser",
        "UnionPayParser",
        "BankParser",
    ]


@pytest.mark.parametrize(
    ("row", "platform"),
    [
        (ALIPAY, Platform.ALIPAY),
        (
            {
                "交易订单号": "order-2",
                "商户订单号": "merchant-2",
                "创建时间": "2024-01-15 14:00:00",
                "对方账户": "商家",
                "商品说明": "服务",
                "金额": "100.00",
                "收/支": "支出",
                "收/付款方式": "余额",
                "交易状态": "支付成功",
            },
            Platform.ALIPAY,
        ),
        (
            {
                "交易时间": "2024-01-15 14:00:00",
                "交易分类": "餐饮",
                "交易对方": "商家",
                "商品说明": "服务",
                "金额": "100.00",
                "收/支": "支出",
                "收付款方式": "余额",
                "交易状态": "支付成功",
            },
            Platform.ALIPAY,
        ),
        (WECHAT, Platform.WECHAT),
        (
            {
                "交易日": "2024-01-15",
                "交易时间": "14:00:00",
                "支出": "100.00",
                "存入": "",
                "余额": "1000.00",
                "交易备注": "服务",
            },
            Platform.BANK,
        ),
        (
            {
                "记账日期": "2024-01-15",
                "摘要": "服务",
                "借方发生额": "100.00",
                "贷方发生额": "",
                "余额": "1000.00",
                "对方户名": "商家",
            },
            Platform.BANK,
        ),
        (UNIONPAY, Platform.UNIONPAY),
        (BANK, Platform.BANK),
    ],
)
def test_all_eight_formats_preserve_expense_amount_and_original_row(tmp_path, row, platform):
    result = parse_rows(tmp_path, row)
    assert not result.errors
    assert len(result.transactions) == 1
    tx = result.transactions[0]
    assert tx.platform == platform
    assert tx.amount == Decimal("-100.00")
    assert tx.datetime.year == 2024
    assert tx.raw_data == row


@pytest.mark.parametrize(
    "row",
    [
        {**ALIPAY, "收/支": "收入"},
        {**WECHAT, "收/支": "收入", "交易类型": "二维码收款"},
        {**BANK, "收入": "100.00", "支出": ""},
        {**UNIONPAY, "交易类型": "收入"},
        {
            "交易日": "2024-01-15",
            "交易时间": "14:00:00",
            "支出": "",
            "存入": "100.00",
            "余额": "1000.00",
            "交易备注": "存款",
        },
        {
            "记账日期": "2024-01-15",
            "摘要": "工资",
            "借方发生额": "",
            "贷方发生额": "100.00",
            "余额": "1000.00",
            "对方户名": "公司",
        },
    ],
)
def test_confirmed_income_stays_positive_including_bank_credit_columns(tmp_path, row):
    result = parse_rows(tmp_path, row)
    assert not result.errors
    assert result.transactions[0].amount == Decimal("100.00")


@pytest.mark.parametrize("status", ["交易关闭", "支付失败", "交易取消", "已关闭"])
def test_cancelled_rows_are_filtered_even_if_other_fields_are_invalid(tmp_path, status):
    bad = {
        **ALIPAY,
        "交易号": "order-cancelled",
        "交易状态": status,
        "金额（元）": "bad amount",
        "交易创建时间": "bad date",
        "收/支": "不计收支",
    }
    result = parse_rows(tmp_path, ALIPAY, bad)
    assert not result.errors
    assert len(result.transactions) == 1
    assert result.transactions[0].raw_data["交易号"] == "order-1"


@pytest.mark.parametrize(
    ("row", "field", "value"),
    [
        (ALIPAY, "交易创建时间", "invalid-date"),
        (ALIPAY, "交易创建时间", ""),
        (ALIPAY, "交易创建时间", "01-15"),
        (WECHAT, "交易时间", "invalid-date"),
        (BANK, "交易日期", "invalid-date"),
        (UNIONPAY, "交易日期", "invalid-date"),
        (ALIPAY, "金额（元）", "bad amount"),
        (ALIPAY, "金额（元）", "NaN"),
        (ALIPAY, "金额（元）", "Infinity"),
        (ALIPAY, "金额（元）", "0.00"),
        (ALIPAY, "金额（元）", "1,2.34"),
        (WECHAT, "金额(元)", "NaN"),
        (BANK, "支出", "bad amount"),
        (UNIONPAY, "交易金额", "NaN"),
        (ALIPAY, "收/支", "不计收支"),
        (ALIPAY, "收/支", ""),
        (WECHAT, "收/支", "/"),
        (WECHAT, "收/支", ""),
        (WECHAT, "收/支", "unknown"),
        (UNIONPAY, "交易类型", "转账"),
        (ALIPAY, "交易状态", "待支付"),
        (WECHAT, "当前状态", ""),
    ],
)
def test_bad_second_transaction_is_reported_with_its_line_instead_of_silently_lost(
    tmp_path, row, field, value
):
    result = parse_rows(tmp_path, row, {**row, field: value})
    assert len(result.transactions) == 1
    assert result.parsed_rows == 1
    assert len(result.errors) == 1
    assert "第3行" in result.errors[0]


def test_explicit_transfer_keeps_source_sign_for_suspense_posting(tmp_path):
    result = parse_rows(tmp_path, {**ALIPAY, "商品名称": "提现", "收/支": "支出"})
    assert not result.errors
    assert result.transactions[0].tx_type == TransactionType.TRANSFER
    assert result.transactions[0].amount == Decimal("-100.00")


def test_blank_rows_and_known_footer_are_not_misreported_as_bad_transactions(tmp_path):
    path = write_rows(tmp_path, [ALIPAY])
    with path.open("a", encoding="utf-8") as stream:
        stream.write(",,,,,,,,,\n--------------------交易记录结束--------------------\n")
    result = build_strict_parser().parse(str(path))
    assert not result.errors
    assert len(result.transactions) == 1


def test_successful_row_with_missing_csv_cells_is_an_error(tmp_path):
    path = write_rows(tmp_path, [ALIPAY])
    with path.open("a", encoding="utf-8") as stream:
        stream.write("order-2,merchant-2,2024-01-15 14:00:00\n")
    result = build_strict_parser().parse(str(path))
    assert len(result.transactions) == 1
    assert len(result.errors) == 1
    assert "第3行" in result.errors[0]


def test_bank_conflicting_debit_credit_is_rejected(tmp_path):
    result = parse_rows(tmp_path, {**BANK, "收入": "10.00"})
    assert not result.transactions
    assert result.errors


def test_legal_currency_symbol_and_thousands_separator_are_preserved(tmp_path):
    result = parse_rows(tmp_path, {**WECHAT, "金额(元)": "¥1,234.5600"})
    assert not result.errors
    assert result.transactions[0].amount.as_tuple() == Decimal("-1234.5600").as_tuple()


def test_separator_in_corrupt_bank_date_does_not_hide_a_monetary_row(tmp_path):
    result = parse_rows(tmp_path, {**BANK, "交易日期": "-----"})
    assert not result.transactions
    assert len(result.errors) == 1
