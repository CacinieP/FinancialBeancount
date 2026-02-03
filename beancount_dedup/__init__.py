"""
Beancount 多平台账单去重工具

支持支付宝、微信、银行卡账单的导入、去重和导出为 Beancount 格式。
支持从 PDF、XLSX 等格式自动转换为 CSV。
"""

__version__ = "0.1.0"

from .models import Transaction, Platform
from .fingerprinter import TransactionFingerprinter
from .deduplicator import DeduplicationEngine, DedupResult
from .converters import AutoConverter, convert_to_csv
from .account_classifier import (
    BeancountAccountClassifier,
    AccountType,
    AssetCategory,
    ExpenseCategory,
    IncomeCategory,
)

__all__ = [
    "Transaction",
    "Platform",
    "TransactionFingerprinter",
    "DeduplicationEngine",
    "DedupResult",
    "AutoConverter",
    "convert_to_csv",
    "BeancountAccountClassifier",
    "AccountType",
    "AssetCategory",
    "ExpenseCategory",
    "IncomeCategory",
]
