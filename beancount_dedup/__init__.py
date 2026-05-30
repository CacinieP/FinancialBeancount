"""
Beancount 多平台账单去重工具

支持支付宝、微信、银行卡账单的导入、去重和导出为 Beancount 格式。
支持从 PDF、XLSX 等格式自动转换为 CSV。
"""

__version__ = "0.2.0"

from .account_classifier import (
    AccountType,
    AssetCategory,
    BeancountAccountClassifier,
    ExpenseCategory,
    IncomeCategory,
)
from .config import AppConfig, load_config
from .converters import AutoConverter, convert_to_csv
from .deduplicator import DeduplicationEngine, DedupResult
from .fingerprinter import TransactionFingerprinter
from .models import Platform, Transaction
from .state import StateStore

__all__ = [
    "AccountType",
    "AppConfig",
    "AssetCategory",
    "AutoConverter",
    "BeancountAccountClassifier",
    "DedupResult",
    "DeduplicationEngine",
    "ExpenseCategory",
    "IncomeCategory",
    "Platform",
    "StateStore",
    "Transaction",
    "TransactionFingerprinter",
    "convert_to_csv",
    "load_config",
]
