"""
支付宝账单解析器

支付宝CSV格式（示例）：
交易号,商家订单号,交易创建时间,付款时间,最近修改时间,交易来源地,类型,交易对方,商品名称,金额（元）,收/支,交易状态,服务费（元）,成功退款（元）,备注,资金状态,交易方式
"""

import logging

from ..models import Platform, Transaction, TransactionType
from .base import BaseParser

logger = logging.getLogger(__name__)


class AlipayParser(BaseParser):
    """支付宝账单解析器"""

    # 支付宝CSV表头特征 (V1)
    HEADER_SIGNATURES = [
        "交易号",
        "商家订单号",
        "交易创建时间",
        "交易对方",
        "金额",
        "收/支",
        "交易状态",
        "金额（元）",
    ]

    # 所有已知格式的表头特征集合。
    # 每个条目是 (版本名, 必须存在的判别字段列表, 用于宽松匹配的签名列表)。
    # 判别字段（required）全部存在时才认定该版本；签名用于日志和兜底。
    HEADER_REGISTRY: list[tuple[str, list[str], list[str]]] = [
        (
            "alipay_v2",
            ["交易订单号", "创建时间", "对方账户", "收/付款方式"],
            [
                "交易订单号",
                "商户订单号",
                "创建时间",
                "对方账户",
                "金额",
                "收/付款方式",
                "交易状态",
            ],
        ),
        (
            "alipay_v3",
            ["交易分类", "商品说明", "收付款方式", "交易时间"],
            [
                "交易时间",
                "交易分类",
                "交易对方",
                "商品说明",
                "金额",
                "收/支",
                "收付款方式",
                "交易状态",
            ],
        ),
        (
            "alipay_v1",
            ["交易号", "商家订单号", "交易创建时间", "金额（元）"],
            [
                "交易号",
                "商家订单号",
                "交易创建时间",
                "交易对方",
                "金额",
                "收/支",
                "交易状态",
                "金额（元）",
            ],
        ),
    ]

    def __init__(self):
        super().__init__(Platform.ALIPAY)
        self._format_version: str | None = None

    @property
    def format_version(self) -> str | None:
        """返回检测到的格式版本标识（如 'alipay_v1', 'alipay_v2', 'alipay_v3'）"""
        return self._format_version

    def detect_format(self, headers: list[str]) -> bool:
        """
        检测是否为支付宝账单格式

        按优先级依次匹配 HEADER_REGISTRY 中所有已知格式。
        先检查判别字段（required）是否全部存在，再确认签名匹配过半。
        如果匹配的不是 V1（标准格式），输出 warning 日志。
        """
        header_set = set(h for h in headers if h is not None)

        for version_name, required, signatures in self.HEADER_REGISTRY:
            # 所有判别字段必须存在
            if not all(sig in header_set for sig in required):
                continue
            # 签名匹配过半（用于日志和二次确认）
            matched = sum(1 for sig in signatures if sig in header_set)
            if matched >= len(signatures) // 2:
                self._format_version = version_name
                if version_name != "alipay_v1":
                    logger.warning(
                        "Using non-standard Alipay format variant: %s",
                        version_name,
                    )
                logger.debug(
                    "Alipay format detected as %s (matched %d/%d signatures)",
                    version_name,
                    matched,
                    len(signatures),
                )
                return True

        # 兜底：回退到原始 HEADER_SIGNATURES 检测
        matched = sum(1 for sig in self.HEADER_SIGNATURES if sig in header_set)
        if matched >= len(self.HEADER_SIGNATURES) // 2:
            self._format_version = "alipay_v1"
            return True

        return False

    # 所有已知的支付宝交易状态（包括 "交易关闭" 和 "退款中"）
    HANDLED_STATUSES = {
        "交易成功",
        "退款成功",
        "已全额退款",
        "支付成功",
        "交易关闭",
        "退款中",
    }

    def parse_row(self, row: dict[str, str], line_num: int) -> Transaction | None:
        """解析单行数据"""

        # 获取交易状态
        tx_status = row.get("交易状态", "").strip()

        # 跳过不在已处理状态列表中的交易
        if tx_status not in self.HANDLED_STATUSES:
            return None

        # 解析金额和方向
        amount_str = row.get("金额（元）", "0")
        direction = row.get("收/支", "")

        amount = self._clean_amount(amount_str)

        # 根据收支方向调整金额符号
        if direction == "支出":
            amount = -abs(amount)
        elif direction == "收入":
            amount = abs(amount)
        elif direction == "不计收支":
            # 内部转账（如提现、充值）
            amount = abs(amount)  # 保持正值，后续通过关键词判断
        else:
            # 无法识别方向，跳过
            return None

        # 解析时间
        time_str = row.get("交易创建时间", "")
        if not time_str:
            return None

        try:
            dt = self._parse_datetime(time_str)
        except ValueError:
            return None

        # 获取交易信息
        counterparty = row.get("交易对方", "").strip()
        description = row.get("商品名称", "").strip()
        if not description:
            description = row.get("备注", "").strip()

        payment_method = row.get("交易方式", "").strip()

        # 判断交易类型
        tx_type = self._determine_type(counterparty, description, direction)

        return Transaction(
            platform=Platform.ALIPAY,
            datetime=dt,
            amount=amount,
            counterparty=counterparty,
            description=description,
            raw_data=row,
            tx_type=tx_type,
            payment_method=payment_method if payment_method else None,
            raw_status=tx_status,
        )

    def _determine_type(
        self, counterparty: str, description: str, direction: str
    ) -> TransactionType:
        """判断交易类型"""
        text = f"{counterparty} {description}"

        # 退款
        if "退款" in text or "退回" in text:
            return TransactionType.REFUND

        # 转账/充值/提现
        transfer_keywords = ["充值", "提现", "转账", "零钱", "余额宝"]
        if any(kw in text for kw in transfer_keywords):
            return TransactionType.TRANSFER

        # 根据方向判断收入/支出
        if direction == "收入":
            return TransactionType.INCOME
        elif direction == "支出":
            return TransactionType.EXPENSE

        return TransactionType.UNKNOWN


class AlipayParserV2(AlipayParser):
    """
    支付宝账单解析器（新版格式兼容）

    新版格式可能有不同的列名
    """

    HEADER_SIGNATURES = [
        "交易订单号",
        "商户订单号",
        "创建时间",
        "对方账户",
        "金额",
        "收/付款方式",
        "交易状态",
    ]

    def parse_row(self, row: dict[str, str], line_num: int) -> Transaction | None:
        """解析新版格式"""

        # 映射新旧字段名
        field_mapping = {
            "创建时间": "交易创建时间",
            "对方账户": "交易对方",
            "收/付款方式": "交易方式",
            "交易订单号": "交易号",
            "商户订单号": "商家订单号",
        }

        # 转换字段名
        normalized_row = {}
        for key, value in row.items():
            normalized_key = field_mapping.get(key, key)
            normalized_row[normalized_key] = value

        # 添加金额字段
        if "金额" in row and "金额（元）" not in normalized_row:
            normalized_row["金额（元）"] = row["金额"]

        return super().parse_row(normalized_row, line_num)


class AlipayParserV3(AlipayParser):
    """
    支付宝账单解析器（V3 格式）

    V3 格式表头：交易时间,交易分类,交易对方,商品说明,金额,收/支,收付款方式,交易状态
    """

    HEADER_SIGNATURES = [
        "交易时间",
        "交易分类",
        "交易对方",
        "商品说明",
        "金额",
        "收/支",
        "收付款方式",
        "交易状态",
    ]

    def parse_row(self, row: dict[str, str], line_num: int) -> Transaction | None:
        """解析 V3 格式"""

        # 映射 V3 字段名到 V1 字段名
        field_mapping = {
            "交易时间": "交易创建时间",
            "商品说明": "商品名称",
            "收付款方式": "交易方式",
            "交易分类": "备注",
        }

        # 转换字段名
        normalized_row = {}
        for key, value in row.items():
            normalized_key = field_mapping.get(key, key)
            normalized_row[normalized_key] = value

        # 添加金额字段（V3 使用 "金额"，V1 使用 "金额（元）"）
        if "金额" in row and "金额（元）" not in normalized_row:
            normalized_row["金额（元）"] = row["金额"]

        return super().parse_row(normalized_row, line_num)
