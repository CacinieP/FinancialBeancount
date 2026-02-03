"""
银行卡账单解析器（通用）

支持多种银行格式，通过关键词检测
"""

import re
from typing import Dict, List, Optional
from decimal import Decimal

from .base import BaseParser
from ..models import Transaction, Platform, TransactionType


class BankParser(BaseParser):
    """
    通用银行卡账单解析器
    
    尝试兼容多种银行格式：
    - 交易日期,交易时间,收入,支出,余额,交易对手,摘要
    - 日期,时间,借方金额,贷方金额,余额,对方户名,备注
    - 记账日期,交易时间,交易金额,账户余额,交易类型,对方账户
    """
    
    # 银行CSV表头特征（可能的变体）
    DATE_KEYWORDS = ["日期", "交易日期", "记账日期", "记账日", "Date"]
    TIME_KEYWORDS = ["时间", "交易时间", "Time"]
    INCOME_KEYWORDS = ["收入", "贷方金额", "借方", "转入金额", "收入金额", "Credit"]
    EXPENSE_KEYWORDS = ["支出", "借方金额", "贷方", "转出金额", "支出金额", "Debit"]
    COUNTERPARTY_KEYWORDS = ["交易对手", "对方户名", "对方账户", "交易对方", "Counterparty", "对方名称"]
    DESC_KEYWORDS = ["摘要", "备注", "用途", "交易类型", "Description", "附言"]
    
    def __init__(self):
        super().__init__(Platform.BANK)
        self.bank_name: Optional[str] = None
    
    def detect_format(self, headers: List[str]) -> bool:
        """检测是否为银行卡账单格式"""
        header_set = set(h for h in headers if h is not None)

        # 检查是否有日期字段
        has_date = any(
            any(kw in h for kw in self.DATE_KEYWORDS)
            for h in header_set
        )

        # 检查是否有金额字段（收入或支出）
        has_amount = any(
            any(kw in h for kw in self.INCOME_KEYWORDS + self.EXPENSE_KEYWORDS)
            for h in header_set
        )

        # 排除支付宝/微信特征
        alipay_signals = ["交易号", "商家订单号", "收/支"]
        wechat_signals = ["交易单号", "商户单号", "当前状态"]

        is_alipay = any(sig in header_set for sig in alipay_signals)
        is_wechat = any(sig in header_set for sig in wechat_signals)

        return has_date and has_amount and not is_alipay and not is_wechat
    
    def _find_field(self, row: Dict[str, str], keywords: List[str]) -> Optional[str]:
        """根据关键词查找字段值"""
        for key, value in row.items():
            if any(kw in key for kw in keywords):
                return value.strip() if value else None
        return None
    
    def _find_field_key(self, row: Dict[str, str], keywords: List[str]) -> Optional[str]:
        """根据关键词查找字段名"""
        for key in row.keys():
            if any(kw in key for kw in keywords):
                return key
        return None
    
    def parse_row(self, row: Dict[str, str], line_num: int) -> Optional[Transaction]:
        """解析单行数据"""
        
        # 查找日期和时间
        date_str = self._find_field(row, self.DATE_KEYWORDS)
        time_str = self._find_field(row, self.TIME_KEYWORDS) or ""
        
        if not date_str:
            return None
        
        try:
            dt = self._parse_datetime(date_str, time_str)
        except ValueError:
            return None
        
        # 解析金额
        amount = Decimal("0")
        direction = ""
        
        # 尝试收入字段
        income_str = self._find_field(row, self.INCOME_KEYWORDS)
        if income_str:
            income = self._clean_amount(income_str)
            if income > 0:
                amount = income
                direction = "收入"
        
        # 如果没有收入，尝试支出字段
        if amount == 0:
            expense_str = self._find_field(row, self.EXPENSE_KEYWORDS)
            if expense_str:
                expense = self._clean_amount(expense_str)
                if expense > 0:
                    amount = -expense
                    direction = "支出"
        
        # 如果仍然没有，查找单一金额字段（带+-符号）
        if amount == 0:
            for key in ["交易金额", "金额", "发生额", "Amount"]:
                if key in row:
                    amt_str = row[key].strip()
                    if amt_str:
                        amount = self._clean_amount(amt_str)
                        direction = "收入" if amount > 0 else "支出"
                        break
        
        # 跳过零金额交易
        if amount == 0:
            return None
        
        # 获取交易对手和描述
        counterparty = self._find_field(row, self.COUNTERPARTY_KEYWORDS) or "未知"
        description = self._find_field(row, self.DESC_KEYWORDS) or ""
        
        # 清理银行特有的描述格式
        counterparty = self._clean_counterparty(counterparty)
        
        # 判断交易类型
        tx_type = self._determine_type(counterparty, description, direction)
        
        return Transaction(
            platform=Platform.BANK,
            datetime=dt,
            amount=amount,
            counterparty=counterparty,
            description=description,
            raw_data=row,
            tx_type=tx_type,
            payment_method=f"银行卡({self.bank_name or 'Unknown'})",
        )
    
    def _clean_counterparty(self, counterparty: str) -> str:
        """清理交易对手名称"""
        if not counterparty:
            return "未知"
        
        # 移除常见前缀
        prefixes = [
            "支付宝-", "财付通-", "微信支付-", "微信-", 
            "银联-", "网联-", "快钱-"
        ]
        
        for prefix in prefixes:
            if counterparty.startswith(prefix):
                counterparty = counterparty[len(prefix):]
        
        # 移除尾号信息（如"张三(1234)"）
        counterparty = re.sub(r'\(\d+\)$', '', counterparty).strip()
        
        return counterparty
    
    def _determine_type(self, counterparty: str, description: str, direction: str) -> TransactionType:
        """判断交易类型"""
        text = f"{counterparty} {description}"
        
        # 退款
        if any(kw in text for kw in ["退款", "退回", "退款入账", "支付宝提现入账"]):
            return TransactionType.REFUND
        
        # 转账/充值/提现
        transfer_keywords = ["充值", "提现", "转账", "支付宝", "财付通", "微信零钱"]
        if any(kw in text for kw in transfer_keywords):
            return TransactionType.TRANSFER
        
        # 根据方向判断
        if direction == "收入":
            return TransactionType.INCOME
        elif direction == "支出":
            return TransactionType.EXPENSE
        
        return TransactionType.UNKNOWN


class CMBParser(BankParser):
    """招商银行专用解析器"""
    
    def __init__(self):
        super().__init__()
        self.bank_name = "CMB"
    
    def detect_format(self, headers: List[str]) -> bool:
        """检测招行格式"""
        header_set = set(headers)
        cmb_signals = ["交易日", "交易时间", "支出", "存入", "余额", "交易备注"]
        return all(sig in header_set for sig in cmb_signals[:2]) and "支出" in header_set


class ICBCParser(BankParser):
    """工商银行专用解析器"""
    
    def __init__(self):
        super().__init__()
        self.bank_name = "ICBC"
    
    def detect_format(self, headers: List[str]) -> bool:
        """检测工行格式"""
        header_set = set(headers)
        icbc_signals = ["记账日期", "摘要", "借方发生额", "贷方发生额"]
        return sum(1 for sig in icbc_signals if sig in header_set) >= 3
