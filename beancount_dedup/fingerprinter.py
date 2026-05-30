"""
交易指纹生成器 - 三级哈希策略
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from .models import Platform, Transaction

logger = logging.getLogger(__name__)


class TransactionFingerprinter:
    """
    交易指纹生成器

    采用三级指纹策略：
    - L1: 精确匹配（日期|金额|对手方|方向）
    - L2: 宽松匹配（日期|金额|方向），忽略对手方
    - L3: 模糊匹配组件（用于跨天/手续费场景）
    """

    # 商户名称归一化映射表
    MERCHANT_MAP = {
        # 星巴克
        "starbucks": "星巴克",
        "starbucks coffee": "星巴克",
        "starbucks corporation": "星巴克",
        "星巴克咖啡": "星巴克",
        # 麦当劳
        "mcdonalds": "麦当劳",
        "mcdonald's": "麦当劳",
        "麦当劳中国": "麦当劳",
        "麦当劳餐厅": "麦当劳",
        # 肯德基
        "kfc": "肯德基",
        "kentucky fried chicken": "肯德基",
        # 滴滴
        "滴滴": "滴滴出行",
        "didi": "滴滴出行",
        "didi chuxing": "滴滴出行",
        "滴滴快车": "滴滴出行",
        "滴滴专车": "滴滴出行",
        "北京小桔科技": "滴滴出行",
        # 京东
        "jd": "京东",
        "jd.com": "京东",
        "京东商城": "京东",
        # 淘宝
        "taobao": "淘宝",
        "淘宝": "淘宝",
        "浙江淘宝": "淘宝",
        # 天猫
        "tmall": "天猫",
        "天猫": "天猫",
        # 美团
        "meituan": "美团",
        "美团": "美团",
        "美团点评": "美团",
        # 拼多多
        "pdd": "拼多多",
        "拼多多": "拼多多",
    }

    # 需要移除的平台前缀
    PLATFORM_PREFIXES = [
        "支付宝-",
        "财付通-",
        "微信支付-",
        "微信-",
        "银联-",
        "支付宝（中国）网络技术有限公司-",
        "快钱支付-",
    ]

    def __init__(
        self,
        time_window_l2: int = 120,  # L2时间窗口：2分钟
        time_window_l3: int = 86400,  # L3时间窗口：1天
        amount_tolerance_l3: int = 100,  # L3金额容差：100分=1元
    ):
        self.time_window_l2 = time_window_l2
        self.time_window_l3 = time_window_l3
        self.amount_tolerance_l3 = amount_tolerance_l3

    def normalize_counterparty(self, name: str, platform: Platform) -> str:
        """
        归一化交易对手方名称

        1. 统一大小写
        2. 移除平台前缀（如"支付宝-星巴克" -> "星巴克"）
        3. 商户名称映射
        4. 清理特殊字符
        """
        if not name:
            return "未知"

        # 统一大小写
        normalized = name.strip()

        # 移除平台前缀
        for prefix in self.PLATFORM_PREFIXES:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
                break

        # 转换为小写进行映射匹配
        lower_name = normalized.lower()
        for key, value in self.MERCHANT_MAP.items():
            if key in lower_name or lower_name in key:
                return value

        # 清理特殊字符（保留中英文数字）
        normalized = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", normalized)

        return normalized if normalized else "未知"

    def normalize_date(self, dt: datetime, platform: Platform) -> list[str]:
        """
        归一化日期

        对于银行卡的凌晨交易（00:00-02:00），同时返回前一天日期
        用于处理跨天记账的情况

        Returns:
            日期字符串列表（yyyy-mm-dd）
        """
        dates = [dt.strftime("%Y-%m-%d")]

        # 银行卡凌晨交易可能是前一天的延迟记账
        if platform == Platform.BANK and dt.hour < 2:
            prev_day = dt - timedelta(days=1)
            dates.append(prev_day.strftime("%Y-%m-%d"))

        return dates

    def get_direction(self, tx: Transaction) -> str:
        """
        获取交易方向

        Returns:
            "OUT" - 支出
            "IN" - 收入
        """
        return "OUT" if tx.is_expense else "IN"

    def generate_fingerprints(self, tx: Transaction) -> dict[str, Any]:
        """
        为交易生成三级指纹

        Returns:
            {
                "L1": "md5_hash",           # 精确匹配
                "L2": "md5_hash",           # 宽松匹配
                "L3": {                      # 模糊匹配组件
                    "date": "yyyy-mm-dd",
                    "amount_cents": int,
                    "is_expense": bool,
                    "timestamp": float,
                    "platform": str
                },
                "raw": {                     # 原始指纹数据（用于调试）
                    "l1_strings": [...],
                    "l2_strings": [...]
                }
            }
        """
        counterparty = self.normalize_counterparty(tx.counterparty, tx.platform)
        direction = self.get_direction(tx)
        amount_cents = tx.amount_cents
        dates = self.normalize_date(tx.datetime, tx.platform)
        timestamp = tx.datetime.timestamp()

        logger.debug(
            "生成指纹: platform=%s amount=%s direction=%s counterparty=%s dates=%s",
            tx.platform.value,
            amount_cents,
            direction,
            counterparty[:20],
            dates,
        )

        # L1: 精确匹配指纹（可能有多个日期变体）
        l1_hashes = []
        l1_raws = []
        for date_str in dates:
            l1_raw = f"{date_str}|{amount_cents}|{counterparty}|{direction}"
            l1_hash = hashlib.md5(l1_raw.encode("utf-8")).hexdigest()
            l1_hashes.append(l1_hash)
            l1_raws.append(l1_raw)

        # L2: 宽松匹配指纹（忽略对手方）
        l2_hashes = []
        l2_raws = []
        for date_str in dates:
            l2_raw = f"{date_str}|{amount_cents}|{direction}"
            l2_hash = hashlib.md5(l2_raw.encode("utf-8")).hexdigest()
            l2_hashes.append(l2_hash)
            l2_raws.append(l2_raw)

        # L3: 模糊匹配组件（动态计算）
        l3_components = {
            "date": dates[0],  # 主日期
            "alt_dates": dates[1:] if len(dates) > 1 else [],
            "amount_cents": amount_cents,
            "is_expense": tx.is_expense,
            "timestamp": timestamp,
            "platform": tx.platform.value,
        }

        return {
            "L1": l1_hashes[0],  # 主L1指纹
            "L1_alt": l1_hashes[1:] if len(l1_hashes) > 1 else [],
            "L2": l2_hashes[0],  # 主L2指纹
            "L2_alt": l2_hashes[1:] if len(l2_hashes) > 1 else [],
            "L3": l3_components,
            "raw": {
                "l1_strings": l1_raws,
                "l2_strings": l2_raws,
                "normalized_counterparty": counterparty,
            },
        }

    def check_l2_match(self, tx1: Transaction, tx2: Transaction, fp1: dict, fp2: dict) -> bool:
        """
        L2匹配的二次验证

        条件：
        1. 时间差在允许范围内（5分钟）
        2. 平台组合合法（第三方支付 vs 银行）
        3. 金额一致
        4. 方向相同（同一笔交易的两个记录）
        """
        l3_1 = fp1["L3"]
        l3_2 = fp2["L3"]

        # 检查时间差
        time_diff = abs(l3_1["timestamp"] - l3_2["timestamp"])
        if time_diff > self.time_window_l2:
            return False

        # 检查平台组合
        platforms = {l3_1["platform"], l3_2["platform"]}
        valid_combos = [
            {"alipay", "bank"},
            {"wechat", "bank"},
            {"alipay", "wechat"},
        ]
        if platforms not in valid_combos:
            return False

        # 检查金额
        if l3_1["amount_cents"] != l3_2["amount_cents"]:
            return False

        # 检查方向（必须相同，同一笔交易的两个记录）
        return l3_1["is_expense"] == l3_2["is_expense"]

    def check_l3_match(
        self, tx1: Transaction, tx2: Transaction, fp1: dict, fp2: dict
    ) -> dict | None:
        """
        L3模糊匹配检测

        用于检测：
        - 跨天交易（时间差较大但金额相同）
        - 手续费差异（金额差在容差内）
        - 需要人工复核的可疑重复

        Returns:
            匹配信息字典，如果不匹配返回None
        """
        l3_1 = fp1["L3"]
        l3_2 = fp2["L3"]

        # 检查时间差（允许跨天）
        time_diff = abs(l3_1["timestamp"] - l3_2["timestamp"])
        if time_diff > self.time_window_l3:
            return None

        # 检查金额差异
        amount_diff = abs(l3_1["amount_cents"] - l3_2["amount_cents"])
        if amount_diff > self.amount_tolerance_l3:
            return None

        # 平台组合检查
        platforms = {l3_1["platform"], l3_2["platform"]}

        # 判断匹配类型
        if amount_diff == 0:
            match_type = "CROSS_DAY"  # 跨天相同金额
        else:
            match_type = "AMOUNT_DIFF"  # 金额有差异（可能含手续费）

        return {
            "match_type": match_type,
            "time_diff_seconds": time_diff,
            "amount_diff_cents": amount_diff,
            "platforms": list(platforms),
            "needs_review": True,
            "reason": f"L3模糊匹配: {match_type}, 时间差{time_diff / 60:.1f}分钟, 金额差{amount_diff / 100:.2f}元",
        }
