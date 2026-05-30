"""
云闪付（银联）账单解析器

支持云闪付 APP 导出的 CSV 格式，常见表头：
- 交易日期,交易时间,交易类型,交易金额,账户余额,交易对方,交易备注
- 交易日期,交易时间,交易类型,对方账号,交易金额,余额,备注
"""

import logging
import re
from decimal import Decimal

from ..models import Platform, Transaction, TransactionType
from .base import BaseParser

logger = logging.getLogger(__name__)


class UnionPayParser(BaseParser):
    """
    云闪付（银联）账单解析器

    解析云闪付 APP 导出的 CSV 账单文件。
    """

    # 已知的表头变体（每种格式为一个完整表头列表）
    HEADER_VARIANTS = [
        ["交易日期", "交易时间", "交易类型", "交易金额", "账户余额", "交易对方", "交易备注"],
        ["交易日期", "交易时间", "交易类型", "对方账号", "交易金额", "余额", "备注"],
    ]

    # 关键词映射
    DATE_KEYWORDS = ["交易日期", "日期"]
    TIME_KEYWORDS = ["交易时间", "时间"]
    AMOUNT_KEYWORDS = ["交易金额", "金额"]
    BALANCE_KEYWORDS = ["账户余额", "余额"]
    COUNTERPARTY_KEYWORDS = ["交易对方", "对方账号", "对方"]
    DESC_KEYWORDS = ["交易备注", "备注", "交易类型"]
    TYPE_KEYWORDS = ["交易类型"]

    # 云闪付特有信号词（用于检测格式）
    PLATFORM_SIGNALS = ["云闪付", "银联", "UnionPay", "unionpay"]

    def __init__(self):
        super().__init__(Platform.UNIONPAY)

    def detect_format(self, headers: list[str]) -> bool:
        """
        检测是否为云闪付账单格式。

        匹配策略：
        1. 完全匹配已知表头变体
        2. 包含日期 + 金额 + 交易对方/备注 的关键词组合，
           且不含支付宝/微信的独有特征
        """
        header_set = set(h for h in headers if h is not None)

        # 排除支付宝/微信特征
        alipay_signals = {"交易号", "商家订单号", "收/支"}
        wechat_signals = {"交易单号", "商户单号", "当前状态"}

        if header_set & alipay_signals or header_set & wechat_signals:
            return False

        # 尝试完全匹配已知表头变体
        for variant in self.HEADER_VARIANTS:
            if header_set >= set(variant):
                logger.debug("UnionPay format matched variant: %s", variant)
                return True

        # 宽松匹配：必须包含日期 + 金额 + 交易对方，且有"交易类型"
        has_date = any(any(kw in h for kw in self.DATE_KEYWORDS) for h in header_set)
        has_amount = any(any(kw in h for kw in self.AMOUNT_KEYWORDS) for h in header_set)
        has_counterparty = any(
            any(kw in h for kw in self.COUNTERPARTY_KEYWORDS) for h in header_set
        )
        has_type = any(any(kw in h for kw in self.TYPE_KEYWORDS) for h in header_set)

        # 排除通用银行格式：银行格式通常有收入/支出分列，云闪付通常只有一个金额列
        bank_income_signals = {"收入", "贷方金额", "借方金额", "贷方发生额", "借方发生额"}
        has_bank_split = bool(header_set & bank_income_signals)

        if has_date and has_amount and has_counterparty and has_type and not has_bank_split:
            logger.debug("UnionPay format matched via keyword combination")
            return True

        return False

    def _find_field(self, row: dict[str, str], keywords: list[str]) -> str | None:
        """根据关键词查找字段值"""
        for key, value in row.items():
            if any(kw in key for kw in keywords):
                return value.strip() if value else None
        return None

    def _find_field_key(self, row: dict[str, str], keywords: list[str]) -> str | None:
        """根据关键词查找字段名"""
        for key in row:
            if any(kw in key for kw in keywords):
                return key
        return None

    def parse_row(self, row: dict[str, str], line_num: int) -> Transaction | None:
        """解析单行数据"""
        # 查找日期和时间
        date_str = self._find_field(row, self.DATE_KEYWORDS)
        time_str = self._find_field(row, self.TIME_KEYWORDS) or ""

        if not date_str:
            return None

        try:
            dt = self._parse_datetime(date_str, time_str)
        except ValueError:
            logger.debug("UnionPay: 无法解析日期 (行 %d): %s %s", line_num, date_str, time_str)
            return None

        # 解析金额
        amount_str = self._find_field(row, self.AMOUNT_KEYWORDS)
        if not amount_str:
            return None

        amount = self._clean_amount(amount_str)

        # 云闪付金额通常不带符号，正数为支出，需结合交易类型判断方向
        tx_type_str = self._find_field(row, self.TYPE_KEYWORDS) or ""

        amount = self._determine_amount_direction(amount, tx_type_str)

        # 跳过零金额交易
        if amount == 0:
            return None

        # 获取交易对方和描述
        counterparty = self._find_field(row, self.COUNTERPARTY_KEYWORDS) or "未知"
        description = self._find_field(row, self.DESC_KEYWORDS) or ""

        # 清理交易对手名称
        counterparty = self._clean_counterparty(counterparty)

        # 判断交易类型
        tx_type = self._determine_type(counterparty, description, tx_type_str, amount)

        return Transaction(
            platform=Platform.UNIONPAY,
            datetime=dt,
            amount=amount,
            counterparty=counterparty,
            description=description,
            raw_data=row,
            tx_type=tx_type,
            payment_method="云闪付",
        )

    def _determine_amount_direction(self, amount: Decimal, tx_type_str: str) -> Decimal:
        """
        根据交易类型判断金额正负方向。

        云闪付导出的金额通常为正数，需要根据交易类型判断是收入还是支出。
        """
        income_keywords = ["收入", "退款", "充值", "转入", "红包", "返还", "退款入账"]
        expense_keywords = ["支出", "消费", "支付", "转出", "提现", "购买", "缴费"]

        text = tx_type_str.lower()

        if any(kw in text for kw in expense_keywords):
            return -abs(amount)
        elif any(kw in text for kw in income_keywords):
            return abs(amount)

        # 如果金额已经是负数，保持原样
        if amount < 0:
            return amount

        # 默认假设为支出
        return -abs(amount)

    def _clean_counterparty(self, counterparty: str) -> str:
        """清理交易对手名称"""
        if not counterparty:
            return "未知"

        # 移除常见前缀
        prefixes = ["支付宝-", "财付通-", "微信支付-", "微信-", "银联-", "网联-", "快钱-"]
        for prefix in prefixes:
            if counterparty.startswith(prefix):
                counterparty = counterparty[len(prefix) :]

        # 移除尾号信息（如"张三(1234)"）
        counterparty = re.sub(r"\(\d+\)$", "", counterparty).strip()

        return counterparty

    def _determine_type(
        self,
        counterparty: str,
        description: str,
        tx_type_str: str,
        amount: Decimal,
    ) -> TransactionType:
        """判断交易类型"""
        text = f"{counterparty} {description} {tx_type_str}"

        # 退款
        if any(kw in text for kw in ["退款", "退回", "退款入账"]):
            return TransactionType.REFUND

        # 转账/充值/提现
        transfer_keywords = ["充值", "提现", "转账", "转入", "转出"]
        if any(kw in text for kw in transfer_keywords):
            return TransactionType.TRANSFER

        # 根据金额方向判断
        if amount > 0:
            return TransactionType.INCOME
        elif amount < 0:
            return TransactionType.EXPENSE

        return TransactionType.UNKNOWN
