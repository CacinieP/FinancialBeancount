"""
CSV 解析器基类
"""

import csv
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..models import Platform, Transaction

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    """解析结果"""

    transactions: list[Transaction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_rows: int = 0
    parsed_rows: int = 0

    def __str__(self):
        return (
            f"ParseResult(total={self.total_rows}, "
            f"parsed={self.parsed_rows}, errors={len(self.errors)})"
        )


class BaseParser(ABC):
    """
    CSV 解析器基类

    子类需要实现：
    - detect_format: 检测文件格式是否匹配
    - parse_row: 解析单行数据为Transaction
    """

    # 文件编码尝试列表
    ENCODINGS = ["utf-8-sig", "utf-8", "gbk", "gb2312", "gb18030"]

    def __init__(self, platform: Platform):
        self.platform = platform

    def _read_csv_with_encoding(self, filepath: str) -> list[dict[str, str]]:
        """
        尝试多种编码读取CSV
        """
        for encoding in self.ENCODINGS:
            try:
                with open(filepath, encoding=encoding, newline="") as f:
                    # 尝试检测是否有BOM
                    f.read(4)
                    f.seek(0)

                    # 使用csv.DictReader读取
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    return rows
            except UnicodeDecodeError:
                continue
            except (OSError, csv.Error) as e:
                logger.debug("Failed to read %s with encoding %s: %s", filepath, encoding, e)
                continue

        raise ValueError(f"无法以支持的编码读取文件: {filepath}")

    def _clean_amount(self, amount_str: str) -> Decimal:
        """
        清洗金额字符串

        处理各种格式：
        - "¥100.00" -> 100.00
        - "-50.5" -> -50.5
        - "+1,234.56" -> 1234.56
        """
        if not amount_str:
            return Decimal("0")

        # 移除货币符号、千分位分隔符、空格
        cleaned = re.sub(r"[¥￥$,\s]", "", amount_str.strip())

        # 处理正负号
        if cleaned.startswith("+"):
            cleaned = cleaned[1:]

        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return Decimal("0")

    def _parse_datetime(self, date_str: str, time_str: str = "") -> datetime:
        """
        解析日期时间

        支持多种格式：
        - 2024-01-15 14:30:00
        - 2024/01/15 14:30
        - 20240115
        """
        date_formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y%m%d %H%M%S",
            "%Y%m%d",
            "%Y-%m-%d",
            "%Y/%m/%d",
        ]

        combined = f"{date_str} {time_str}".strip()

        for fmt in date_formats:
            try:
                return datetime.strptime(combined if time_str else date_str, fmt)
            except ValueError:
                continue

        # 如果都失败，尝试自动解析
        try:
            from dateutil import parser

            return parser.parse(combined if time_str else date_str)
        except (ValueError, TypeError, ImportError) as err:
            raise ValueError(f"无法解析日期时间: {date_str} {time_str}") from err

    @abstractmethod
    def detect_format(self, headers: list[str]) -> bool:
        """
        根据CSV表头检测是否支持此格式

        Args:
            headers: CSV文件的第一行（表头）

        Returns:
            是否支持此格式
        """

    @abstractmethod
    def parse_row(self, row: dict[str, str], line_num: int) -> Transaction | None:
        """
        解析单行数据

        Args:
            row: CSV行数据（字典格式）
            line_num: 行号（用于错误报告）

        Returns:
            Transaction对象，如果解析失败返回None
        """

    def parse(self, filepath: str, source_file: str = "") -> ParseResult:
        """
        解析CSV文件

        Args:
            filepath: CSV文件路径
            source_file: 来源文件名（用于标识）

        Returns:
            ParseResult解析结果
        """
        result = ParseResult()

        try:
            rows = self._read_csv_with_encoding(filepath)
            result.total_rows = len(rows)

            if not rows:
                result.errors.append("CSV文件为空")
                return result

            # 检测格式
            headers = list(rows[0].keys()) if rows else []
            if not self.detect_format(headers):
                result.errors.append(f"文件格式不匹配，表头: {headers}")
                return result

            # 解析每一行
            for i, row in enumerate(rows, start=2):  # 从2开始（跳过表头）
                try:
                    tx = self.parse_row(row, i)
                    if tx:
                        tx.source_file = source_file or Path(filepath).name
                        result.transactions.append(tx)
                        result.parsed_rows += 1
                except Exception as e:
                    result.errors.append(f"第{i}行解析错误: {e!s}")

        except Exception as e:
            result.errors.append(f"文件读取错误: {e!s}")

        return result


class AutoParser:
    """
    自动解析器

    自动检测并选择合适的解析器
    """

    def __init__(self):
        self.parsers: list[BaseParser] = []

    def register(self, parser: BaseParser):
        """注册解析器"""
        self.parsers.append(parser)

    def parse(self, filepath: str) -> ParseResult:
        """
        自动检测并解析文件

        对于微信账单等特殊格式，需要扫描多行来找到真正的表头
        """
        # 首先尝试通过读取前几行检测格式
        headers = None
        parser_to_use = None

        try:
            for encoding in BaseParser.ENCODINGS:
                try:
                    with open(filepath, encoding=encoding, newline="") as f:
                        # 读取前 30 行来检测格式（微信账单表头可能在第 17 行）
                        lines_to_check = []
                        for i, line in enumerate(f):
                            if i >= 30:
                                break
                            lines_to_check.append(line)

                        # 首先尝试第一行作为表头（标准格式）
                        # DictReader 需要至少两行（一行表头，一行数据）
                        if len(lines_to_check) >= 2:
                            try:
                                reader = csv.DictReader(lines_to_check[:2])
                                first_row = next(reader)
                                headers = list(first_row.keys())
                            except StopIteration:
                                pass
                            else:
                                # 检查是否匹配任何解析器
                                for parser in self.parsers:
                                    if parser.detect_format(headers):
                                        parser_to_use = parser
                                        break

                        # 如果没有匹配，尝试在后续行中查找微信格式的表头
                        if not parser_to_use:
                            for i, line in enumerate(lines_to_check):
                                if any(sig in line for sig in ["交易时间", "交易类型"]):
                                    # 找到微信格式的表头行
                                    # 需要至少两行（表头 + 数据）
                                    if i + 1 < len(lines_to_check):
                                        reader = csv.DictReader(lines_to_check[i:])
                                        try:
                                            first_row = next(reader)
                                            headers = list(first_row.keys())
                                        except StopIteration:
                                            continue

                                        # 检查是否是微信格式
                                        for parser in self.parsers:
                                            if parser.__class__.__name__ == "WechatParser":
                                                if parser.detect_format(headers):
                                                    parser_to_use = parser
                                                    break
                                        break

                        if parser_to_use:
                            break

                except UnicodeDecodeError:
                    continue
            else:
                return ParseResult(errors=["无法识别文件编码"])

        except Exception as e:
            return ParseResult(errors=[f"文件读取错误: {e!s}"])

        # 使用找到的解析器进行解析
        if parser_to_use:
            return parser_to_use.parse(filepath)

        # 未找到匹配的解析器
        available = [p.__class__.__name__ for p in self.parsers]
        return ParseResult(
            errors=[
                "未找到匹配的解析器",
                f"文件表头: {headers}",
                f"可用解析器: {available}",
                "请检查文件格式或添加新的解析器",
            ]
        )
