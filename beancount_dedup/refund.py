"""
退款/取消交易检测与处理

提供：
- RefundDetector: 将退款交易与原始交易配对（按交易对手 + 金额匹配，30天内）
- CancelledTransactionFilter: 过滤或标记已取消/失败的交易
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .models import Transaction, TransactionType

logger = logging.getLogger(__name__)

# 匹配窗口：退款交易必须在原始交易的 N 天之内
DEFAULT_MATCH_WINDOW_DAYS = 30

# 被视为"已取消/已关闭"的原始交易状态
CANCELLED_STATUSES = {
    "交易关闭",
    "已关闭",
    "交易取消",
    "已取消",
    "支付失败",
    "交易失败",
    "已退货",
}

# 被视为"退款进行中"的状态
REFUND_PENDING_STATUSES = {
    "退款中",
    "退款处理中",
    "退款申请中",
    "对方已退还",
    "已退款",
}

# 被视为"退款已完成"的状态
REFUND_COMPLETED_STATUSES = {
    "退款成功",
    "已全额退款",
    "部分退款",
}


@dataclass
class RefundPair:
    """一笔退款与原始交易的配对"""

    original: Transaction
    refund: Transaction
    match_method: str = "counterparty+amount"  # 匹配方式描述


@dataclass
class RefundReport:
    """退款检测报告"""

    total_input: int = 0
    refund_pairs: list[RefundPair] = field(default_factory=list)
    cancelled_transactions: list[Transaction] = field(default_factory=list)
    unpaired_refunds: list[Transaction] = field(default_factory=list)

    @property
    def paired_count(self) -> int:
        return len(self.refund_pairs)

    @property
    def cancelled_count(self) -> int:
        return len(self.cancelled_transactions)

    @property
    def total_refund_amount(self) -> Decimal:
        return sum(
            (pair.refund.amount for pair in self.refund_pairs),
            Decimal("0"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_input": self.total_input,
            "paired_refunds": self.paired_count,
            "cancelled_transactions": self.cancelled_count,
            "unpaired_refunds": len(self.unpaired_refunds),
            "total_refund_amount": str(self.total_refund_amount),
            "pairs": [
                {
                    "original_id": p.original.id,
                    "refund_id": p.refund.id,
                    "counterparty": p.original.counterparty,
                    "original_amount": str(p.original.amount),
                    "refund_amount": str(p.refund.amount),
                    "original_date": p.original.date_str,
                    "refund_date": p.refund.date_str,
                    "match_method": p.match_method,
                }
                for p in self.refund_pairs
            ],
        }

    def __str__(self) -> str:
        lines = [
            "=" * 50,
            "退款检测报告",
            "=" * 50,
            f"总输入交易: {self.total_input}",
            f"已配对退款: {self.paired_count}",
            f"已取消交易: {self.cancelled_count}",
            f"未配对退款: {len(self.unpaired_refunds)}",
            f"退款总金额: {self.total_refund_amount}",
            "-" * 50,
        ]
        for pair in self.refund_pairs:
            lines.append(
                f"  {pair.original.counterparty}: "
                f"{pair.original.amount} -> {pair.refund.amount} "
                f"({pair.original.date_str} ~ {pair.refund.date_str})"
            )
        if self.cancelled_transactions:
            lines.append("-" * 50)
            lines.append("已取消交易:")
            for tx in self.cancelled_transactions:
                lines.append(f"  [{tx.date_str}] {tx.counterparty} {tx.amount} ({tx.raw_status})")
        lines.append("=" * 50)
        return "\n".join(lines)


class RefundDetector:
    """
    退款交易检测器

    通过交易对手 + 金额匹配（在 30 天窗口内）将退款交易与原始支出交易配对。
    同时识别已取消/已关闭的交易。
    """

    def __init__(self, match_window_days: int = DEFAULT_MATCH_WINDOW_DAYS):
        self.match_window_days = match_window_days

    def detect(self, transactions: list[Transaction]) -> RefundReport:
        """
        对交易列表执行退款检测，返回退款报告。

        检测步骤:
        1. 识别已取消/关闭的交易（raw_status 在 CANCELLED_STATUSES 中）
        2. 识别退款交易（tx_type == REFUND 或 raw_status 表明退款）
        3. 将退款交易与原始支出交易配对（同交易对手 + 同金额绝对值 + 30天内）
        4. 在原始交易上标记 cancelled/refunded 标签
        """
        report = RefundReport(total_input=len(transactions))

        # --- Step 1: 收集已取消交易 ---
        cancelled = []
        remaining = []
        for tx in transactions:
            if tx.raw_status in CANCELLED_STATUSES:
                tx.tags.add("cancelled")
                cancelled.append(tx)
            else:
                remaining.append(tx)
        report.cancelled_transactions = cancelled

        # --- Step 2: 分离退款 vs 非退款 ---
        refunds: list[Transaction] = []
        non_refunds: list[Transaction] = []
        for tx in remaining:
            if self._is_refund(tx):
                refunds.append(tx)
            else:
                non_refunds.append(tx)

        # --- Step 3: 配对 ---
        paired_original_ids: set[str] = set()
        paired_refund_ids: set[str] = set()

        for refund_tx in refunds:
            best_match = self._find_original(refund_tx, non_refunds, paired_original_ids)
            if best_match is not None:
                pair = RefundPair(original=best_match, refund=refund_tx)
                report.refund_pairs.append(pair)

                best_match.tags.add("refunded")
                refund_tx.refund_of = best_match.id
                refund_tx.tags.add("refund")

                paired_original_ids.add(best_match.id)
                paired_refund_ids.add(refund_tx.id)
            else:
                report.unpaired_refunds.append(refund_tx)
                refund_tx.tags.add("unpaired-refund")

        return report

    def _is_refund(self, tx: Transaction) -> bool:
        """判断交易是否为退款"""
        if tx.tx_type == TransactionType.REFUND:
            return True
        if tx.raw_status in REFUND_COMPLETED_STATUSES | REFUND_PENDING_STATUSES:
            return True
        # 如果描述或交易对手包含退款关键词且金额为正（收到退款）
        text = f"{tx.counterparty} {tx.description}"
        if any(kw in text for kw in ["退款", "退还", "退回"]) and tx.amount > 0:
            return True
        return False

    def _find_original(
        self,
        refund_tx: Transaction,
        candidates: list[Transaction],
        already_paired: set[str],
    ) -> Transaction | None:
        """
        为退款交易查找原始支出交易。

        匹配条件:
        - 交易对手一致
        - 金额绝对值相等
        - 原始交易日期在退款交易的 self.match_window_days 天之前
        """
        window_start = refund_tx.datetime - timedelta(days=self.match_window_days)
        refund_abs = abs(refund_tx.amount)

        best: Transaction | None = None
        best_dt: datetime | None = None

        for cand in candidates:
            if cand.id in already_paired:
                continue
            # 金额绝对值必须相等
            if abs(cand.amount) != refund_abs:
                continue
            # 交易对手必须匹配
            if not self._counterparty_match(refund_tx.counterparty, cand.counterparty):
                continue
            # 原始交易必须在退款之前且在窗口内
            if not (window_start <= cand.datetime <= refund_tx.datetime):
                continue
            # 选择最近的原始交易
            if best_dt is None or cand.datetime > best_dt:
                best = cand
                best_dt = cand.datetime

        return best

    @staticmethod
    def _counterparty_match(a: str, b: str) -> bool:
        """
        判断两个交易对手名称是否匹配。

        支持部分匹配以应对平台间名称差异（如 "星巴克" vs "星巴克咖啡"）。
        """
        if not a or not b:
            return False
        a_clean = a.strip().lower()
        b_clean = b.strip().lower()
        if a_clean == b_clean:
            return True
        # 较短名称是较长名称的子串
        if a_clean in b_clean or b_clean in a_clean:
            return True
        return False


class CancelledTransactionFilter:
    """
    已取消/失败交易的过滤器

    模式:
    - "filter": 完全跳过已取消/失败的交易
    - "mark": 保留交易但添加 #cancelled 标签
    """

    def __init__(self, mode: str = "filter"):
        if mode not in ("filter", "mark"):
            raise ValueError(f"Invalid mode: {mode!r}. Must be 'filter' or 'mark'.")
        self.mode = mode

    def filter(self, transactions: list[Transaction]) -> list[Transaction]:
        """
        根据 mode 处理交易列表。

        "filter": 返回不含已取消交易的列表
        "mark":   返回全部交易，但已取消的带 #cancelled 标签
        """
        if self.mode == "filter":
            return [tx for tx in transactions if not self._is_cancelled(tx)]
        else:
            for tx in transactions:
                if self._is_cancelled(tx):
                    tx.tags.add("cancelled")
            return list(transactions)

    @staticmethod
    def _is_cancelled(tx: Transaction) -> bool:
        """判断交易是否应被视为已取消/失败"""
        # 明确的取消状态
        if tx.raw_status in CANCELLED_STATUSES:
            return True
        # 已被退款检测器标记
        if "cancelled" in tx.tags:
            return True
        return False
