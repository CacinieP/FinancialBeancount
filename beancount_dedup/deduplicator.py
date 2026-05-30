"""
去重引擎 - 三级哈希去重策略
"""

import logging
from collections import defaultdict
from datetime import date, timedelta

from .fingerprinter import TransactionFingerprinter
from .models import DeduplicationReport, DedupResult, DedupStatus, Platform, Transaction

logger = logging.getLogger(__name__)


class DeduplicationEngine:
    """
    交易去重引擎

    优先级：支付宝 > 微信 > 银行卡
    三级匹配策略：L1精确 -> L2宽松 -> L3模糊
    """

    # 平台优先级（数值越高越优先保留）
    PLATFORM_PRIORITY = {
        Platform.ALIPAY: 3,
        Platform.WECHAT: 2,
        Platform.BANK: 1,
    }

    # 内部转账关键词（用于识别非消费交易）
    TRANSFER_KEYWORDS = {
        "alipay": ["充值", "提现", "转到余额宝", "信用卡还款", "转账到银行卡", "零钱"],
        "wechat": ["充值", "提现", "零钱通", "信用卡还款"],
        "bank": [
            "支付宝充值",
            "财付通充值",
            "快捷支付退款",
            "支付宝提现入账",
            "微信零钱充值",
            "微信零钱提现",
        ],
    }

    def __init__(self, fingerprinter: TransactionFingerprinter | None = None):
        self.fingerprinter = fingerprinter or TransactionFingerprinter()

        # 三级索引
        self.l1_index: dict[str, list[Transaction]] = defaultdict(list)
        self.l2_index: dict[str, list[Transaction]] = defaultdict(list)

        # L3 日期桶索引：date -> list[Transaction]
        # 只搜索 ±1 天桶内的交易，将 O(N²) 降为 O(K)，K 为相邻两天内的交易数
        self.date_buckets: dict[date, list[Transaction]] = defaultdict(list)

        # 待复核队列
        self.review_queue: list[tuple[Transaction, Transaction, str]] = []

        # 已处理的交易记录
        self.processed: list[Transaction] = []
        self.results: list[DedupResult] = []

    def is_internal_transfer(self, tx: Transaction) -> bool:
        """
        检测是否是内部转账（非消费交易）

        内部转账不应被视为重复，但也不记为支出/收入
        """
        keywords = self.TRANSFER_KEYWORDS.get(tx.platform.value, [])
        text = f"{tx.description} {tx.counterparty}"
        return any(kw in text for kw in keywords)

    def detect_transfer_pair(self, tx1: Transaction, tx2: Transaction) -> bool:
        """
        检测是否是内部转账的一对（如 银行卡出 -> 支付宝入）
        """
        # 金额相同，方向相反
        if tx1.amount_cents != tx2.amount_cents:
            return False
        if tx1.is_expense == tx2.is_expense:
            return False

        # 平台组合必须是 银行 <-> 第三方支付
        platforms = {tx1.platform, tx2.platform}
        if Platform.BANK not in platforms:
            return False

        # 检查是否包含转账关键词
        return self.is_internal_transfer(tx1) or self.is_internal_transfer(tx2)

    def get_priority(self, tx: Transaction) -> int:
        """获取平台优先级"""
        return self.PLATFORM_PRIORITY.get(tx.platform, 0)

    def resolve_priority(
        self, tx1: Transaction, tx2: Transaction
    ) -> tuple[Transaction, Transaction]:
        """
        决定保留哪个交易

        返回: (keeper, discarder)
        """
        p1 = self.get_priority(tx1)
        p2 = self.get_priority(tx2)

        if p1 > p2:
            return tx1, tx2
        elif p2 > p1:
            return tx2, tx1
        else:
            # 同优先级，保留信息更完整的（对手方+描述长度）
            info_score_1 = len(tx1.counterparty) + len(tx1.description)
            info_score_2 = len(tx2.counterparty) + len(tx2.description)
            return (tx1, tx2) if info_score_1 >= info_score_2 else (tx2, tx1)

    def _check_l1_duplicate(self, tx: Transaction, fingerprints: dict) -> Transaction | None:
        """
        L1精确匹配检查

        检查主指纹和备选指纹（用于跨天匹配）

        增加时间窗口保护：如果两笔交易时间差过大（如2分钟），
        即使指纹相同，也认为是独立交易（如连续刷地铁）
        """
        time_window_seconds = 120  # 2分钟时间窗口（更严格，避免连续相同交易误判）

        # 检查主L1指纹
        l1_hash = fingerprints["L1"]
        if l1_hash in self.l1_index:
            for candidate in self.l1_index[l1_hash]:
                # 重复的交易应该有相同方向（都是支出或都是收入）
                # 并且平台不同（同一平台内的重复需要特殊处理）
                if tx.platform != candidate.platform:
                    # 检查时间差，如果时间差过大，可能是独立交易
                    time_diff = abs((tx.datetime - candidate.datetime).total_seconds())
                    if time_diff <= time_window_seconds:
                        return candidate

        # 检查备选L1指纹（跨天场景）
        for alt_hash in fingerprints.get("L1_alt", []):
            if alt_hash in self.l1_index:
                for candidate in self.l1_index[alt_hash]:
                    if tx.platform != candidate.platform:
                        time_diff = abs((tx.datetime - candidate.datetime).total_seconds())
                        if time_diff <= time_window_seconds:
                            return candidate

        return None

    def _check_l2_duplicate(self, tx: Transaction, fingerprints: dict) -> Transaction | None:
        """
        L2宽松匹配检查
        """
        # 检查主L2指纹
        l2_hash = fingerprints["L2"]
        if l2_hash in self.l2_index:
            for candidate in self.l2_index[l2_hash]:
                # 跳过同平台的L2匹配（同平台内应该能用L1匹配）
                if tx.platform == candidate.platform:
                    continue
                if self.fingerprinter.check_l2_match(
                    tx, candidate, fingerprints, candidate.fingerprints
                ):
                    return candidate

        # 检查备选L2指纹
        for alt_hash in fingerprints.get("L2_alt", []):
            if alt_hash in self.l2_index:
                for candidate in self.l2_index[alt_hash]:
                    if tx.platform == candidate.platform:
                        continue
                    if self.fingerprinter.check_l2_match(
                        tx, candidate, fingerprints, candidate.fingerprints
                    ):
                        return candidate

        return None

    def _check_l3_candidates(
        self, tx: Transaction, fingerprints: dict
    ) -> list[tuple[Transaction, dict]]:
        """
        L3模糊匹配搜索（优化版）

        使用日期桶索引，只搜索交易日期 ±1 天内的候选交易。
        将复杂度从 O(N²) 降为 O(K)，K 为相邻两天内的交易数。

        返回可能的匹配列表
        """
        candidates = []
        seen_ids: set[str] = set()

        tx_date = tx.datetime.date()
        # 搜索 ±1 天的桶（覆盖跨天场景）
        for delta in (timedelta(days=-1), timedelta(days=0), timedelta(days=1)):
            bucket_key = tx_date + delta
            for candidate in self.date_buckets.get(bucket_key, []):
                if candidate.id in seen_ids:
                    continue
                seen_ids.add(candidate.id)

                cand_fingerprints = candidate.fingerprints
                if not cand_fingerprints or "L3" not in cand_fingerprints:
                    continue

                match_info = self.fingerprinter.check_l3_match(
                    tx, candidate, fingerprints, cand_fingerprints
                )
                if match_info:
                    candidates.append((candidate, match_info))

        return candidates

    def add_transaction(self, tx: Transaction) -> DedupResult:
        """
        添加单笔交易进行去重

        处理流程：
        1. 生成指纹
        2. L1精确匹配
        3. L2宽松匹配
        4. L3模糊匹配（标记复核）
        5. 存入索引
        """
        # 生成指纹
        fingerprints = self.fingerprinter.generate_fingerprints(tx)
        tx.fingerprints = fingerprints

        # 初始化结果
        result = DedupResult(
            transaction=tx, status=DedupStatus.UNIQUE, fingerprints=fingerprints, kept=True
        )

        # 检查是否是内部转账
        if self.is_internal_transfer(tx):
            result.status = DedupStatus.INTERNAL_TRANSFER
            result.review_reason = "内部转账/充值提现"
            logger.debug("内部转账: %s %s %s", tx.platform.value, tx.amount, tx.description[:30])

        # ========== L1 精确匹配 ==========
        if result.status == DedupStatus.UNIQUE:
            l1_match = self._check_l1_duplicate(tx, fingerprints)
            if l1_match:
                # 检查是否是内部转账对
                if self.detect_transfer_pair(tx, l1_match):
                    result.status = DedupStatus.INTERNAL_TRANSFER
                    result.duplicate_of = l1_match
                    result.match_level = "L1_TRANSFER"
                    result.review_reason = "内部转账对（L1匹配）"
                    logger.debug(
                        "L1内部转账对: %s <-> %s", tx.description[:20], l1_match.description[:20]
                    )
                else:
                    keeper, _discarder = self.resolve_priority(tx, l1_match)
                    is_kept = tx == keeper
                    result.status = DedupStatus.DUPLICATE if not is_kept else DedupStatus.UNIQUE
                    result.duplicate_of = l1_match if not is_kept else None
                    result.match_level = "L1"
                    result.kept = is_kept
                    tx.status = result.status
                    tx.duplicate_of = l1_match.id if not is_kept else None
                    tx.match_level = "L1"

                    # 更新已存在交易的标记（如果是新交易被保留，则旧交易标记为丢弃）
                    if is_kept:
                        # 新交易被保留，旧交易标记为重复
                        l1_match.status = DedupStatus.DUPLICATE
                        l1_match.duplicate_of = tx.id
                        l1_match.match_level = "L1"

                    logger.info(
                        "L1精确匹配: %s [%s] %s <-> %s [%s] %s (保留%s)",
                        tx.platform.value,
                        tx.amount,
                        tx.description[:20],
                        l1_match.platform.value,
                        l1_match.amount,
                        l1_match.description[:20],
                        "新" if is_kept else "旧",
                    )
                    # L1匹配成功，跳过后续检查（无论是否保留）
                    return result

        # ========== L2 宽松匹配 ==========
        if result.status == DedupStatus.UNIQUE:
            l2_match = self._check_l2_duplicate(tx, fingerprints)
            if l2_match:
                keeper, _discarder = self.resolve_priority(tx, l2_match)
                is_kept = tx == keeper
                result.status = DedupStatus.DUPLICATE if not is_kept else DedupStatus.UNIQUE
                result.duplicate_of = l2_match if not is_kept else None
                result.match_level = "L2"
                result.kept = is_kept
                tx.status = result.status
                tx.duplicate_of = l2_match.id if not is_kept else None
                tx.match_level = "L2"

                # 更新已存在交易的标记
                if is_kept:
                    l2_match.status = DedupStatus.DUPLICATE
                    l2_match.duplicate_of = tx.id
                    l2_match.match_level = "L2"

                logger.info(
                    "L2宽松匹配: %s [%s] <-> %s [%s] (保留%s)",
                    tx.platform.value,
                    tx.amount,
                    l2_match.platform.value,
                    l2_match.amount,
                    "新" if is_kept else "旧",
                )
                # L2匹配成功，跳过后续检查（无论是否保留）
                return result

        # ========== L3 模糊匹配 ==========
        if result.status == DedupStatus.UNIQUE:
            l3_candidates = self._check_l3_candidates(tx, fingerprints)
            if l3_candidates:
                # 找到最可能的匹配（时间差最小的）
                best_match, best_info = min(l3_candidates, key=lambda x: x[1]["time_diff_seconds"])
                result.status = DedupStatus.REVIEW
                result.duplicate_of = best_match
                result.match_level = "L3"
                result.review_reason = best_info["reason"]
                tx.status = DedupStatus.REVIEW
                tx.duplicate_of = best_match.id
                tx.match_level = "L3"
                self.review_queue.append((tx, best_match, best_info["reason"]))
                logger.warning(
                    "L3模糊匹配需复核: %s <-> %s (%s)",
                    tx.description[:20],
                    best_match.description[:20],
                    best_info["reason"],
                )

        # 存入索引
        if result.status != DedupStatus.DUPLICATE or result.kept:
            self.l1_index[fingerprints["L1"]].append(tx)
            self.l2_index[fingerprints["L2"]].append(tx)
            for alt_l1 in fingerprints.get("L1_alt", []):
                self.l1_index[alt_l1].append(tx)
            for alt_l2 in fingerprints.get("L2_alt", []):
                self.l2_index[alt_l2].append(tx)
            # 日期桶索引（L3 优化）
            self.date_buckets[tx.datetime.date()].append(tx)

        self.processed.append(tx)
        self.results.append(result)
        return result

    def add_transactions(self, transactions: list[Transaction]) -> list[DedupResult]:
        """批量添加交易"""
        results = []
        for tx in transactions:
            result = self.add_transaction(tx)
            results.append(result)
        return results

    def add_transactions_incremental(
        self, transactions: list[Transaction], seen_fingerprints: set[str]
    ) -> list[DedupResult]:
        """
        Incrementally add transactions, skipping those whose L1 fingerprint
        has already been seen in a previous run.

        Returns results only for newly-processed transactions.
        """
        results: list[DedupResult] = []
        for tx in transactions:
            # Generate fingerprints early so we can check the L1 hash.
            fingerprints = self.fingerprinter.generate_fingerprints(tx)
            tx.fingerprints = fingerprints

            l1_main = fingerprints["L1"]
            l1_alts = fingerprints.get("L1_alt", [])
            all_l1 = {l1_main} | set(l1_alts)

            if all_l1 & seen_fingerprints:
                # At least one L1 fingerprint was seen before — skip.
                logger.debug("Skipping already-seen transaction: %s", tx)
                continue

            result = self.add_transaction(tx)
            results.append(result)

            # Record the new L1 fingerprints so they can be persisted later.
            seen_fingerprints.update(all_l1)

        return results

    def get_l1_fingerprints(self) -> set[str]:
        """Return all L1 fingerprints currently held in the index."""
        return set(self.l1_index.keys())

    def get_unique_transactions(self) -> list[Transaction]:
        """获取去重后的唯一交易列表"""
        return [
            tx
            for tx in self.processed
            if tx.status in (DedupStatus.UNIQUE, DedupStatus.REVIEW, DedupStatus.INTERNAL_TRANSFER)
        ]

    def get_duplicates(self) -> list[Transaction]:
        """获取被标记为重复的交易"""
        return [tx for tx in self.processed if tx.status == DedupStatus.DUPLICATE]

    def get_review_queue(self) -> list[tuple[Transaction, Transaction, str]]:
        """获取需要复核的交易对"""
        return self.review_queue

    def generate_report(self) -> DeduplicationReport:
        """生成去重报告"""
        report = DeduplicationReport()
        for result in self.results:
            report.add_result(result)

        # 添加详细重复信息
        for tx in self.get_duplicates():
            original = next((t for t in self.processed if t.id == tx.duplicate_of), None)
            if original:
                report.duplicates_detail.append(
                    {
                        "discarded_id": tx.id,
                        "discarded_platform": tx.platform.value,
                        "discarded_desc": f"{tx.counterparty} {tx.description}"[:50],
                        "kept_id": original.id,
                        "kept_platform": original.platform.value,
                        "kept_desc": f"{original.counterparty} {original.description}"[:50],
                        "match_level": tx.match_level,
                    }
                )

        # 添加复核队列信息
        for tx1, tx2, reason in self.review_queue:
            report.review_queue.append(
                {
                    "tx1_id": tx1.id,
                    "tx1_platform": tx1.platform.value,
                    "tx1_desc": f"{tx1.counterparty} {tx1.description}"[:50],
                    "tx2_id": tx2.id,
                    "tx2_platform": tx2.platform.value,
                    "tx2_desc": f"{tx2.counterparty} {tx2.description}"[:50],
                    "reason": reason,
                }
            )

        return report

    def reset(self):
        """重置引擎状态"""
        self.l1_index.clear()
        self.l2_index.clear()
        self.date_buckets.clear()
        self.review_queue.clear()
        self.processed.clear()
        self.results.clear()
