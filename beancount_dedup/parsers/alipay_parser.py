"""
支付宝账单解析器

支付宝CSV格式（示例）：
交易号,商家订单号,交易创建时间,付款时间,最近修改时间,交易来源地,类型,交易对方,商品名称,金额（元）,收/支,交易状态,服务费（元）,成功退款（元）,备注,资金状态,交易方式
"""

from ..models import Platform, Transaction, TransactionType
from .base import BaseParser


class AlipayParser(BaseParser):
    """支付宝账单解析器"""

    # 支付宝CSV表头特征
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

    def __init__(self):
        super().__init__(Platform.ALIPAY)

    def detect_format(self, headers: list[str]) -> bool:
        """检测是否为支付宝账单格式"""
        header_set = set(h for h in headers if h is not None)
        # 检查核心签名字段（至少匹配大部分）
        matched = sum(1 for sig in self.HEADER_SIGNATURES if sig in header_set)
        # 至少匹配一半以上才认为是支付宝格式
        return matched >= len(self.HEADER_SIGNATURES) // 2

    def parse_row(self, row: dict[str, str], line_num: int) -> Transaction | None:
        """解析单行数据"""

        # 获取交易状态
        tx_status = row.get("交易状态", "").strip()

        # 跳过未完成的交易
        if tx_status not in ["交易成功", "退款成功", "已全额退款", "支付成功"]:
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
