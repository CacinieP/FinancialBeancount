"""
交易模型定义
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class Platform(Enum):
    """交易平台"""

    UNKNOWN = "unknown"  # 未知
    ALIPAY = "alipay"  # 支付宝
    WECHAT = "wechat"  # 微信支付
    BANK = "bank"  # 银行卡

    def __str__(self):
        return self.value


class TransactionType(Enum):
    """交易类型"""

    EXPENSE = "expense"  # 支出
    INCOME = "income"  # 收入
    TRANSFER = "transfer"  # 内部转账
    REFUND = "refund"  # 退款
    UNKNOWN = "unknown"  # 未知


class DedupStatus(Enum):
    """去重状态"""

    UNIQUE = "unique"  # 唯一交易
    DUPLICATE = "duplicate"  # 重复（已去重）
    REVIEW = "review"  # 需要人工复核
    INTERNAL_TRANSFER = "internal_transfer"  # 内部转账


@dataclass
class Transaction:
    """
    交易记录数据类

    Attributes:
        id: 唯一标识符
        platform: 交易平台
        datetime: 交易时间
        amount: 金额（支出为负，收入为正）
        counterparty: 交易对手方
        description: 交易描述
        raw_data: 原始数据（保留原始CSV行）
        tx_type: 交易类型
        payment_method: 支付方式（如：建设银行尾号8888）
        status: 交易状态
        source_file: 来源文件名
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    platform: Platform = Platform.UNKNOWN
    datetime: datetime = field(default_factory=datetime.now)
    amount: Decimal = Decimal("0")
    counterparty: str = ""
    description: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)
    tx_type: TransactionType = TransactionType.UNKNOWN
    payment_method: str | None = None
    status: DedupStatus = DedupStatus.UNIQUE
    source_file: str = ""

    # 去重相关字段
    fingerprints: dict[str, str] = field(default_factory=dict)
    duplicate_of: str | None = None  # 指向重复的交易ID
    match_level: str | None = None  # L1/L2/L3

    def __post_init__(self):
        """初始化后处理"""
        if isinstance(self.amount, (int, float, str)):
            self.amount = Decimal(str(self.amount))
        if isinstance(self.platform, str):
            self.platform = Platform(self.platform)

    @property
    def is_expense(self) -> bool:
        """是否为支出"""
        return self.amount < 0

    @property
    def is_income(self) -> bool:
        """是否为收入"""
        return self.amount > 0

    @property
    def amount_abs(self) -> Decimal:
        """绝对金额"""
        return abs(self.amount)

    @property
    def amount_cents(self) -> int:
        """金额（分）"""
        return int(self.amount_abs * 100)

    @property
    def date_str(self) -> str:
        """日期字符串 yyyy-mm-dd"""
        return self.datetime.strftime("%Y-%m-%d")

    @property
    def time_str(self) -> str:
        """时间字符串 HH:MM:SS"""
        return self.datetime.strftime("%H:%M:%S")

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "platform": self.platform.value,
            "datetime": self.datetime.isoformat(),
            "date": self.date_str,
            "time": self.time_str,
            "amount": str(self.amount),
            "counterparty": self.counterparty,
            "description": self.description,
            "tx_type": self.tx_type.value,
            "payment_method": self.payment_method,
            "status": self.status.value,
            "fingerprints": self.fingerprints,
            "duplicate_of": self.duplicate_of,
            "match_level": self.match_level,
        }

    def __repr__(self):
        return (
            f"Transaction({self.id}, {self.platform.value}, "
            f"{self.date_str} {self.time_str}, "
            f"{'-' if self.is_expense else '+'}{self.amount_abs}, "
            f"{self.counterparty[:20]}...)"
        )


@dataclass
class DedupResult:
    """去重结果"""

    transaction: Transaction
    status: DedupStatus
    fingerprints: dict[str, Any]
    duplicate_of: Transaction | None = None
    match_level: str | None = None
    kept: bool = True  # 在重复对中是否被保留
    review_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "transaction": self.transaction.to_dict(),
            "status": self.status.value,
            "fingerprints": self.fingerprints,
            "duplicate_of": self.duplicate_of.id if self.duplicate_of else None,
            "match_level": self.match_level,
            "kept": self.kept,
            "review_reason": self.review_reason,
        }


@dataclass
class DeduplicationReport:
    """去重报告"""

    total_input: int = 0
    unique_count: int = 0
    duplicate_count: int = 0
    review_count: int = 0
    internal_transfer_count: int = 0

    by_platform: dict[str, int] = field(
        default_factory=lambda: {"alipay": 0, "wechat": 0, "bank": 0}
    )
    duplicates_detail: list[dict] = field(default_factory=list)
    review_queue: list[dict] = field(default_factory=list)

    def add_result(self, result: DedupResult):
        """添加去重结果到报告"""
        self.total_input += 1
        self.by_platform[result.transaction.platform.value] += 1

        if result.status == DedupStatus.UNIQUE:
            self.unique_count += 1
        elif result.status == DedupStatus.DUPLICATE:
            self.duplicate_count += 1
        elif result.status == DedupStatus.REVIEW:
            self.review_count += 1
        elif result.status == DedupStatus.INTERNAL_TRANSFER:
            self.internal_transfer_count += 1

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "total_input": self.total_input,
            "unique_count": self.unique_count,
            "duplicate_count": self.duplicate_count,
            "review_count": self.review_count,
            "internal_transfer_count": self.internal_transfer_count,
            "by_platform": self.by_platform,
            "duplicate_rate": f"{self.duplicate_count / max(self.total_input, 1) * 100:.1f}%",
        }

    def __str__(self):
        lines = [
            "=" * 50,
            "去重报告",
            "=" * 50,
            f"总输入交易: {self.total_input}",
            f"  - 支付宝: {self.by_platform.get('alipay', 0)}",
            f"  - 微信:   {self.by_platform.get('wechat', 0)}",
            f"  - 银行卡: {self.by_platform.get('bank', 0)}",
            "-" * 50,
            f"唯一交易:   {self.unique_count}",
            f"重复交易:   {self.duplicate_count} (已去重)",
            f"待复核:     {self.review_count}",
            f"内部转账:   {self.internal_transfer_count}",
            f"去重率:     {self.duplicate_count / max(self.total_input, 1) * 100:.1f}%",
            "=" * 50,
        ]
        return "\n".join(lines)
