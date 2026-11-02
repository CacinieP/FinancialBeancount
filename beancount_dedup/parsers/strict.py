"""Audited parser instances for imports that must never silently lose money.

The historical parser API stays permissive for compatibility. CLI and beangulp
use this factory: cancelled rows and recognizable summaries are skipped, while
malformed or ambiguous transaction rows become ParseResult.errors.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models import Platform, Transaction
from ..refund import CANCELLED_STATUSES
from .alipay_parser import AlipayParser, AlipayParserV2, AlipayParserV3
from .bank_parser import BankParser, CMBParser, ICBCParser
from .base import AutoParser, BaseParser
from .unionpay_parser import UnionPayParser
from .wechat_parser import WechatParser

_STATUS_FIELDS = ("交易状态", "当前状态", "状态", "交易结果")
_DATE_FIELDS = (
    "交易创建时间",
    "创建时间",
    "交易日期",
    "交易日",
    "记账日期",
    "记账日",
    "日期",
    "Date",
)
_TIME_FIELDS = ("交易时间", "时间", "Time")
_CREDIT_FIELDS = (
    "收入",
    "存入",
    "贷方金额",
    "贷方发生额",
    "转入金额",
    "收入金额",
    "Credit",
    "贷方",
)
_DEBIT_FIELDS = ("支出", "支取", "借方金额", "借方发生额", "转出金额", "支出金额", "Debit", "借方")
_AMOUNT_FIELDS = ("金额（元）", "金额(元)", "交易金额", "金额", "发生额", "Amount")
_NUMBER = re.compile(
    r"(?:[+-]?[¥￥$]?|[¥￥$][+-]?)\s*(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)"
)
_SUMMARY = re.compile(
    r"^(?:微信支付账单明细列表结束|交易记录结束|账单明细结束|交易明细结束|"
    r"(?:本次|共|总)?(?:交易|记录|收支)?(?:合计|汇总|总计|统计)|"
    r"导出时间|导出日期|下载时间|说明[:：]|注[:：]|温馨提示)"
)


def _find(
    row: dict[str, str], fields: tuple[str, ...], *, substring: bool = False
) -> tuple[str, str] | None:
    for name in fields:
        if name in row:
            return name, row[name]
    if substring:
        for name in fields:
            for key, value in row.items():
                if name in key:
                    return key, value
    return None


def _number(value: str, *, allow_blank: bool = False) -> Decimal:
    if allow_blank and value in {"", "/", "-", "--"}:
        return Decimal(0)
    if not value or not _NUMBER.fullmatch(value):
        raise ValueError("金额缺失或格式无效")
    cleaned = re.sub(r"[¥￥$,\s]", "", value)
    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("金额格式无效") from exc
    if not number.is_finite():
        raise ValueError("金额必须为有限数值")
    return number


def _direction(row: dict[str, str], *, required: bool) -> str | None:
    if "收/支" not in row and not required:
        return None
    direction = row.get("收/支", "")
    if direction not in {"收入", "支出"}:
        raise ValueError("收/支方向不明确，不能推断资产增减")
    return direction


def _is_summary(row: dict[str, str]) -> bool:
    populated = [value for value in row.values() if value]
    if not populated:
        return True
    # A malformed successful transaction must not masquerade as a footer.
    if any(row.get(name) for name in _STATUS_FIELDS) or row.get("收/支") in {"收入", "支出"}:
        return False
    first = populated[0].strip("-=_ \t")
    return (not first and len(populated) == 1) or bool(_SUMMARY.match(first))


def _normalize_bank(row: dict[str, str]) -> tuple[dict[str, str], Decimal]:
    credit = _find(row, _CREDIT_FIELDS, substring=True)
    debit = _find(row, _DEBIT_FIELDS, substring=True)
    direction = _direction(row, required=False)
    if credit or debit:
        incoming = _number(credit[1], allow_blank=True) if credit else Decimal(0)
        outgoing = _number(debit[1], allow_blank=True) if debit else Decimal(0)
        if incoming < 0 or outgoing < 0 or (incoming and outgoing):
            raise ValueError("银行收入/支出列同时有金额或符号冲突，需人工确认")
        expected = incoming if incoming else -outgoing
        if direction and expected and ((expected > 0) != (direction == "收入")):
            raise ValueError("银行金额列与收/支方向冲突")
    else:
        field = _find(row, _AMOUNT_FIELDS)
        if field is None:
            raise ValueError("交易金额缺失")
        amount = _number(field[1])
        if direction:
            expected = abs(amount) if direction == "收入" else -abs(amount)
        elif field[1].lstrip("¥￥$ ").startswith(("+", "-")):
            expected = amount
        else:
            raise ValueError("单列银行金额缺少明确的正负方向")
        incoming = expected if expected > 0 else Decimal(0)
        outgoing = -expected if expected < 0 else Decimal(0)
    # Canonical columns come first: legacy substring matching otherwise confuses
    # 借方发生额 with income, or does not recognize CMB's 交易日/存入.
    date_field = _find(row, _DATE_FIELDS, substring=True)
    time_field = _find(row, _TIME_FIELDS)
    normalized = {
        "交易日期": date_field[1] if date_field else "",
        "交易时间": time_field[1] if time_field else "",
        "收入": str(incoming),
        "支出": str(outgoing),
        **row,
    }
    normalized["交易日期"] = date_field[1] if date_field else ""
    normalized["收入"] = str(incoming)
    normalized["支出"] = str(outgoing)
    if "交易备注" in row and "摘要" not in row:
        normalized["摘要"] = row["交易备注"]
    return normalized, expected


def _prepare(parser: BaseParser, row: dict[str, str]) -> tuple[dict[str, str], Decimal]:
    if parser.platform == Platform.BANK:
        return _normalize_bank(row)
    normalized = dict(row)
    if parser.platform == Platform.ALIPAY:
        aliases = {
            "创建时间": "交易创建时间",
            "交易时间": "交易创建时间",
            "对方账户": "交易对方",
            "商品说明": "商品名称",
            "收/付款方式": "交易方式",
            "收付款方式": "交易方式",
            "交易订单号": "交易号",
            "商户订单号": "商家订单号",
        }
        for source, target in aliases.items():
            if source in row and target not in normalized:
                normalized[target] = row[source]
        amount_field = _find(row, _AMOUNT_FIELDS)
        if amount_field is not None:
            normalized["金额（元）"] = amount_field[1]
    amount_field = _find(normalized, _AMOUNT_FIELDS)
    if amount_field is None:
        raise ValueError("交易金额缺失")
    amount = _number(amount_field[1])
    if parser.platform in {Platform.ALIPAY, Platform.WECHAT}:
        direction = _direction(normalized, required=True)
        expected = abs(amount) if direction == "收入" else -abs(amount)
        handled = (
            AlipayParser.HANDLED_STATUSES
            if parser.platform == Platform.ALIPAY
            else WechatParser.HANDLED_STATUSES
        )
        allowed = handled - CANCELLED_STATUSES - {"退款中"}
        status = next((normalized[name] for name in _STATUS_FIELDS if normalized.get(name)), "")
        if status not in allowed:
            raise ValueError("交易状态缺失、未确认或不受支持")
    else:
        direction = _direction(normalized, required=False)
        kind = normalized.get("交易类型", "")
        expense = any(
            word in kind for word in ("支出", "消费", "支付", "转出", "提现", "购买", "缴费")
        )
        income = any(
            word in kind for word in ("收入", "退款", "充值", "转入", "红包", "返还", "退款入账")
        )
        if direction:
            expected = abs(amount) if direction == "收入" else -abs(amount)
        elif expense != income:
            expected = -abs(amount) if expense else abs(amount)
        elif not expense and amount_field[1].lstrip("¥￥$ ").startswith(("+", "-")):
            expected = amount
        else:
            raise ValueError("云闪付交易类型无法确定收支方向")
        # Feed the known sign through a recognized type for legacy parsing;
        # preserve original classification keywords wherever possible.
        if not expense and not income:
            normalized["交易类型"] = ("支出 " if expected < 0 else "收入 ") + kind
    return normalized, expected


def _audit(parser: BaseParser) -> BaseParser:
    original = parser.parse_row
    original_detect = parser.detect_format

    def detect_format(headers: list[str]) -> bool:
        names = {name.strip() for name in headers if isinstance(name, str)}
        if parser.platform == Platform.ALIPAY:
            alipay_signal = bool(
                names
                & {"交易号", "商家订单号", "交易订单号", "商户订单号", "收付款方式", "收/付款方式"}
            )
            if "交易状态" not in names or not alipay_signal:
                return False
        elif parser.platform == Platform.WECHAT:
            # The historical three-of-six heuristic also accepts UnionPay.
            if "收/支" not in names or not names & {"金额(元)", "金额"}:
                return False
        return original_detect(headers)

    def parse_row(row: dict[str, Any], line_num: int) -> Transaction | None:
        clean = {
            key.strip(): value.strip() if isinstance(value, str) else ""
            for key, value in row.items()
            if isinstance(key, str)
        }
        if any(clean.get(name) in CANCELLED_STATUSES for name in _STATUS_FIELDS):
            return None
        if _is_summary(clean):
            return None
        try:
            if any(not isinstance(value, str) for value in row.values()):
                raise ValueError("CSV列缺失或多余，无法可靠解析")
            prepared, expected = _prepare(parser, clean)
            if not expected.is_finite() or not expected:
                raise ValueError("交易金额必须为非零有限数值")
            # Missing years must not be filled using today's date by dateutil.
            date_fields = ("交易时间",) if parser.platform == Platform.WECHAT else _DATE_FIELDS
            source_date = _find(prepared, date_fields, substring=True)
            if not source_date or not re.search(
                r"(?<!\d)\d{4}(?!\d)|^\d{8}(?:\s|$)", source_date[1]
            ):
                raise ValueError("交易日期缺失或不完整")
            tx = original(prepared, line_num)
            if tx is None:
                raise ValueError("交易行无法解析，已阻止静默丢失")
            if not tx.amount.is_finite() or not tx.amount or tx.amount != expected:
                raise ValueError("解析金额或收支方向与原始账单不一致")
            if not tx.counterparty.strip() and not tx.description.strip():
                raise ValueError("交易对方和交易描述均缺失")
            tx.raw_data = dict(row)
            return tx
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"第{line_num}行：{exc}") from exc

    parser.detect_format = detect_format  # type: ignore[method-assign]
    parser.parse_row = parse_row  # type: ignore[method-assign]
    return parser


def build_strict_parser() -> AutoParser:
    """Return the eight supported parser variants with per-row loss auditing."""
    parser = AutoParser()
    for variant in (
        AlipayParser(),
        AlipayParserV2(),
        AlipayParserV3(),
        WechatParser(),
        CMBParser(),
        ICBCParser(),
        UnionPayParser(),
        BankParser(),
    ):
        parser.register(_audit(variant))
    return parser
