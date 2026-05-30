"""
微信支付账单解析器

微信CSV格式（示例）：
交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注

微信2024+格式（示例）：
交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注,是否为专场交易
"""

import csv
import logging
from io import StringIO
from pathlib import Path

from ..models import Platform, Transaction, TransactionType
from .base import BaseParser, ParseResult

logger = logging.getLogger(__name__)

# Common BOM and invisible Unicode codepoints that may appear at the
# start of exported CSV files.  Kept as a tuple of codepoint integers
# so that no literal control characters end up in source.
_BOM_CODEPOINTS: tuple[int, ...] = (
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE (canonical UTF-8 BOM)
    0xFFFE,  # Byte-order mark (reversed)
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x200E,  # LEFT-TO-RIGHT MARK
    0x200F,  # RIGHT-TO-LEFT MARK
    0x202A,  # LEFT-TO-RIGHT EMBEDDING
    0x202B,  # RIGHT-TO-LEFT EMBEDDING
    0x202C,  # POP DIRECTIONAL FORMATTING
    0x202D,  # LEFT-TO-RIGHT OVERRIDE
    0x202E,  # RIGHT-TO-LEFT OVERRIDE
    0x2060,  # WORD JOINER
    0x2061,  # FUNCTION APPLICATION
    0x2062,  # INVISIBLE TIMES
    0x2063,  # INVISIBLE SEPARATOR
    0x2064,  # INVISIBLE PLUS
    0x2066,  # LEFT-TO-RIGHT ISOLATE
    0x2067,  # RIGHT-TO-LEFT ISOLATE
    0x2068,  # FIRST STRONG ISOLATE
    0x2069,  # POP DIRECTIONAL ISOLATE
    0xFE00,  # VARIATION SELECTOR-1
    0xFE01,  # VARIATION SELECTOR-2
    0xFE02,  # VARIATION SELECTOR-3
    0xFE03,  # VARIATION SELECTOR-4
    0xFE04,  # VARIATION SELECTOR-5
    0xFE05,  # VARIATION SELECTOR-6
    0xFE06,  # VARIATION SELECTOR-7
    0xFE07,  # VARIATION SELECTOR-8
    0xFE08,  # VARIATION SELECTOR-9
    0xFE09,  # VARIATION SELECTOR-10
    0xFE0A,  # VARIATION SELECTOR-11
    0xFE0B,  # VARIATION SELECTOR-12
    0xFE0C,  # VARIATION SELECTOR-13
    0xFE0D,  # VARIATION SELECTOR-14
    0xFE0E,  # VARIATION SELECTOR-15
    0xFE0F,  # VARIATION SELECTOR-16
    0xFEFF,  # BOM (duplicate for completeness)
    0xFE18,  # PRESENTATION FORM FOR VERTICAL RIGHT WHITE LENTICULAR BRACKET
    0xFFF9,  # INTERLINEAR ANNOTATION ANCHOR
    0xFFFA,  # INTERLINEAR ANNOTATION SEPARATOR
    0xFFFB,  # INTERLINEAR ANNOTATION TERMINATOR
    0xFFFC,  # OBJECT REPLACEMENT CHARACTER
    0xFFFD,  # REPLACEMENT CHARACTER
    0x00AD,  # SOFT HYPHEN
    0x00A0,  # NO-BREAK SPACE
)


def _strip_bom(text: str) -> str:
    """
    去除字符串开头所有 BOM 及类似的不可见控制字符。

    不仅处理标准 UTF-8 BOM (\\ufeff)，还处理 UTF-16/32 BOM 残留、
    方向控制符、零宽字符等可能出现在 CSV 导出文件中的隐形字符。
    """
    start = 0
    while start < len(text) and ord(text[start]) in _BOM_CODEPOINTS:
        start += 1
    return text[start:]


class WechatParser(BaseParser):
    """微信支付账单解析器"""

    # 微信CSV表头特征
    HEADER_SIGNATURES = ["交易时间", "交易类型", "交易对方", "收/支", "金额", "金额(元)"]

    # 所有已知格式的表头特征集合。
    # 每个条目是 (版本名, 必须存在的判别字段列表, 用于宽松匹配的签名列表)。
    # 判别字段（required）全部存在时才认定该版本；签名用于日志和兜底。
    HEADER_REGISTRY: list[tuple[str, list[str], list[str]]] = [
        (
            "wechat_2024",
            ["是否为专场交易"],
            [
                "交易时间",
                "交易类型",
                "交易对方",
                "商品",
                "收/支",
                "金额(元)",
                "支付方式",
                "当前状态",
                "交易单号",
                "商户单号",
                "备注",
                "是否为专场交易",
            ],
        ),
        (
            "wechat_2023",
            ["交易时间", "交易类型", "交易对方", "金额(元)", "交易单号"],
            [
                "交易时间",
                "交易类型",
                "交易对方",
                "商品",
                "收/支",
                "金额(元)",
                "支付方式",
                "当前状态",
                "交易单号",
                "商户单号",
                "备注",
            ],
        ),
    ]

    def __init__(self):
        super().__init__(Platform.WECHAT)
        self._format_version: str | None = None

    @property
    def format_version(self) -> str | None:
        """返回检测到的格式版本标识（如 'wechat_2023', 'wechat_2024'）"""
        return self._format_version

    def detect_format(self, headers: list[str]) -> bool:
        """
        检测是否为微信支付账单格式

        按优先级依次匹配 HEADER_REGISTRY 中所有已知格式。
        先检查判别字段（required）是否全部存在，再确认签名匹配过半。
        记录匹配到的版本标识并输出日志。
        """
        # 清除表头中可能的 BOM 残留
        header_set = set(_strip_bom(h) for h in headers if h is not None)

        for version_name, required, signatures in self.HEADER_REGISTRY:
            # 所有判别字段必须存在
            if not all(sig in header_set for sig in required):
                continue
            # 签名匹配过半（用于日志和二次确认）
            matched = sum(1 for sig in signatures if sig in header_set)
            if matched >= len(signatures) // 2:
                self._format_version = version_name
                logger.info(
                    "WeChat format detected as %s (matched %d/%d signatures)",
                    version_name,
                    matched,
                    len(signatures),
                )
                return True

        # 兜底：回退到原始 HEADER_SIGNATURES 检测
        matched = sum(1 for sig in self.HEADER_SIGNATURES if sig in header_set)
        if matched >= len(self.HEADER_SIGNATURES) // 2:
            self._format_version = "wechat_2023"
            logger.info("WeChat format detected as wechat_2023 (fallback)")
            return True

        return False

    def parse(self, filepath: str, source_file: str = ""):
        """
        解析微信账单 CSV 文件

        微信账单格式特殊，前面有很多标题行，需要先找到真正的表头
        """
        result = ParseResult()

        try:
            # 先读取原始行来找到表头位置
            for encoding in self.ENCODINGS:
                try:
                    with open(filepath, encoding=encoding, newline="") as f:
                        # 读取所有原始行
                        raw_lines = f.readlines()
                        break
                except UnicodeDecodeError:
                    continue
            else:
                raise ValueError(f"无法以支持的编码读取文件: {filepath}")

            result.total_rows = len(raw_lines)

            if not raw_lines:
                result.errors.append("CSV文件为空")
                return result

            # 微信账单需要找到真正的表头行
            header_line_idx = -1
            for i, line in enumerate(raw_lines):
                # 查找包含核心表头字段的行
                if any(sig in line for sig in ["交易时间", "交易类型"]):
                    header_line_idx = i
                    break

            if header_line_idx == -1:
                result.errors.append("未找到表头行，文件格式不匹配")
                return result

            # 使用 csv.DictReader 读取，但跳过表头之前的行
            # 创建一个StringIO，只包含从表头行开始的内容
            csv_content = "".join(raw_lines[header_line_idx:])

            # 健壮地去除所有 BOM 及类似字符
            csv_content = _strip_bom(csv_content)

            reader = csv.DictReader(StringIO(csv_content))
            rows = list(reader)

            # 清除表头字段名中的 BOM 残留
            if rows:
                cleaned_rows = []
                for row in rows:
                    cleaned_row = {_strip_bom(k): v for k, v in row.items() if k is not None}
                    cleaned_rows.append(cleaned_row)
                rows = cleaned_rows

            # 检测格式
            headers = list(rows[0].keys()) if rows else []
            if not self.detect_format(headers):
                result.errors.append(f"文件格式不匹配，表头: {headers}")
                return result

            # 解析每一行数据
            for i, row in enumerate(rows):
                line_num = header_line_idx + i + 2  # +2 因为第一行是表头

                try:
                    tx = self.parse_row(row, line_num)
                    if tx:
                        tx.source_file = source_file or Path(filepath).name
                        result.transactions.append(tx)
                        result.parsed_rows += 1
                except Exception as e:
                    result.errors.append(f"第{line_num}行解析错误: {e!s}")

        except Exception as e:
            result.errors.append(f"文件读取错误: {e!s}")

        return result

    # 所有已知的微信交易状态
    HANDLED_STATUSES = {
        "支付成功",
        "已存入零钱",
        "已到账",
        "退款成功",
        "交易成功",
        "对方已退还",
        "已全额退款",
        "已退款",
    }

    def parse_row(self, row: dict[str, str], line_num: int) -> Transaction | None:
        """解析单行数据"""

        # 获取交易状态
        tx_status = row.get("当前状态", "").strip()

        # 跳过不在已处理状态列表中的交易（空状态允许通过）
        if tx_status and tx_status not in self.HANDLED_STATUSES:
            return None

        # 解析金额和方向
        amount_str = row.get("金额(元)", row.get("金额", "0"))
        direction = row.get("收/支", "")

        # 微信金额格式如 "¥35.00" 或 "35.00"
        amount = self._clean_amount(amount_str)

        # 根据收支方向调整金额符号
        if direction == "支出":
            amount = -abs(amount)
        elif direction == "收入":
            amount = abs(amount)
        elif direction in {"/", ""}:
            # 可能是转账或其他类型，根据金额符号判断
            amount = -abs(amount) if amount < 0 else abs(amount)
        else:
            return None

        # 解析时间
        time_str = row.get("交易时间", "")
        if not time_str:
            return None

        try:
            dt = self._parse_datetime(time_str)
        except ValueError:
            return None

        # 获取交易信息
        counterparty = row.get("交易对方", "").strip()
        description = row.get("商品", "").strip()
        if not description:
            description = row.get("交易类型", "").strip()

        payment_method = row.get("支付方式", "").strip()

        # 判断交易类型
        tx_type = self._determine_type(counterparty, description, row.get("交易类型", ""))

        return Transaction(
            platform=Platform.WECHAT,
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
        self, counterparty: str, description: str, tx_type_str: str
    ) -> TransactionType:
        """判断交易类型"""
        text = f"{counterparty} {description} {tx_type_str}"

        # 退款
        if any(kw in text for kw in ["退款", "退还", "退款入账"]):
            return TransactionType.REFUND

        # 转账/充值/提现
        transfer_keywords = ["充值", "提现", "零钱通", "零钱", "转账", "信用卡还款"]
        if any(kw in text for kw in transfer_keywords):
            return TransactionType.TRANSFER

        # 二维码收付款
        if "二维码收款" in text:
            return TransactionType.INCOME
        if "扫二维码付款" in text:
            return TransactionType.EXPENSE

        return TransactionType.UNKNOWN
