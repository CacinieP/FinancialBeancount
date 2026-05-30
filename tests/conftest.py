"""
Shared fixtures and test data for the FinancialBeancount test suite.
"""

import pytest
from datetime import datetime
from decimal import Decimal

from beancount_dedup.models import Transaction, Platform, TransactionType, DedupStatus
from beancount_dedup.fingerprinter import TransactionFingerprinter
from beancount_dedup.deduplicator import DeduplicationEngine
from beancount_dedup.account_classifier import BeancountAccountClassifier
from beancount_dedup.exporters.beancount import BeancountExporter


# ── CSV test data (copied from test_e2e_pipeline.py) ──────────────────────

ALIPAY_CSV_DATA = """交易号,商家订单号,交易创建时间,付款时间,最近修改时间,交易来源地,类型,交易对方,商品名称,金额（元）,收/支,交易状态,服务费（元）,成功退款（元）,备注,资金状态,交易方式
2024011522001234567890123456,,2024-01-15 14:00:00,2024-01-15 14:00:00,2024-01-15 14:00:00,APP,餐饮美食,星巴克,拿铁咖啡,100.00,支出,支付成功,0.00,,,余额,余额支付
2024011522001234567890123457,,2024-01-15 18:30:00,2024-01-15 18:30:00,2024-01-15 18:30:00,APP,餐饮美食,麦当劳,午餐套餐,50.00,支出,支付成功,0.00,,,余额,余额支付
2024011610123456789012345678,,2024-01-16 10:00:00,2024-01-16 10:00:00,2024-01-16 10:00:00,APP,日常充值,建设银行(1234),充值,1000.00,收入,支付成功,0.00,,,余额,银行卡
"""

WECHAT_CSV_DATA = """交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
2024-01-15 14:02:00,商户消费,星巴克咖啡,饮品,支出,100.00,零钱,支付成功,4200001234567890123456789123,,
2024-01-15 19:00:00,商户消费,滴滴出行,打车,支出,35.00,零钱,支付成功,4200001234567890123456789124,,
"""

BANK_CSV_DATA = """交易日期,交易时间,收入,支出,余额,交易对手,摘要
2024-01-15,14:02:30,,100.00,5000.00,支付宝-星巴克,快捷支付
2024-01-15,18:32:00,,50.00,4900.00,财付通-麦当劳,快捷支付
2024-01-16,10:01:00,,1000.00,3900.00,支付宝充值,转账
"""


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def fingerprinter():
    """Fresh TransactionFingerprinter instance."""
    return TransactionFingerprinter()


@pytest.fixture
def engine():
    """Fresh DeduplicationEngine instance."""
    return DeduplicationEngine()


@pytest.fixture
def classifier():
    """Fresh BeancountAccountClassifier instance."""
    return BeancountAccountClassifier()


@pytest.fixture
def exporter():
    """Fresh BeancountExporter instance with classifier enabled."""
    return BeancountExporter(use_classifier=True)


@pytest.fixture
def sample_expense_alipay():
    """Sample expense transaction on Alipay."""
    return Transaction(
        platform=Platform.ALIPAY,
        datetime=datetime(2024, 1, 15, 14, 0, 0),
        amount=Decimal("-100.00"),
        counterparty="星巴克",
        description="拿铁咖啡",
        tx_type=TransactionType.EXPENSE,
    )


@pytest.fixture
def sample_expense_bank():
    """Sample expense transaction on Bank (matching the Alipay one)."""
    return Transaction(
        platform=Platform.BANK,
        datetime=datetime(2024, 1, 15, 14, 0, 0),
        amount=Decimal("-100.00"),
        counterparty="支付宝-星巴克",
        description="快捷支付",
        tx_type=TransactionType.EXPENSE,
    )


@pytest.fixture
def sample_income_alipay():
    """Sample income transaction on Alipay."""
    return Transaction(
        platform=Platform.ALIPAY,
        datetime=datetime(2024, 1, 16, 10, 0, 0),
        amount=Decimal("1000.00"),
        counterparty="建设银行(1234)",
        description="充值",
        tx_type=TransactionType.TRANSFER,
    )
