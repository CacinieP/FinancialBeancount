"""
文件格式转换器基类
"""

import csv
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ConversionResult:
    """转换结果"""

    success: bool = False
    output_path: str = ""
    rows_converted: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __str__(self):
        if self.success:
            return (
                f"ConversionResult(success=True, "
                f"output={self.output_path}, "
                f"rows={self.rows_converted})"
            )
        return f"ConversionResult(success=False, errors={self.errors})"


class BaseConverter(ABC):
    """
    文件格式转换器基类

    子类需要实现：
    - supported_extensions: 支持的文件扩展名列表
    - convert: 执行转换操作
    """

    @property
    @abstractmethod
    def supported_extensions(self) -> list[str]:
        """支持的文件扩展名（小写，带点）"""

    def can_convert(self, filepath: str) -> bool:
        """
        检查文件是否支持转换

        Args:
            filepath: 文件路径

        Returns:
            是否支持转换
        """
        ext = Path(filepath).suffix.lower()
        return ext in self.supported_extensions

    @abstractmethod
    def convert(
        self, input_path: str, output_path: str | None = None, **kwargs
    ) -> ConversionResult:
        """
        执行转换操作

        Args:
            input_path: 输入文件路径
            output_path: 输出CSV文件路径（可选，默认自动生成）
            **kwargs: 额外参数

        Returns:
            ConversionResult 转换结果
        """

    def _generate_output_path(self, input_path: str) -> str:
        """
        生成输出文件路径

        默认在输入文件同目录下生成同名.csv文件
        """
        input_path_obj = Path(input_path)
        output_path = input_path_obj.with_suffix(".csv")
        return str(output_path)

    def _write_csv(
        self, rows: list[dict[str, Any]], output_path: str, encoding: str = "utf-8-sig"
    ) -> int:
        """
        将数据写入CSV文件

        Args:
            rows: 要写入的数据行
            output_path: 输出文件路径
            encoding: 文件编码

        Returns:
            写入的行数
        """
        if not rows:
            return 0

        with open(output_path, "w", encoding=encoding, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        return len(rows)

    def _normalize_headers(self, headers: list[str]) -> list[str]:
        """
        归一化表头

        处理空格、特殊字符等
        """
        normalized = []
        for h in headers:
            if h is None:
                normalized.append("")
            else:
                # 去除首尾空格和特殊字符
                normalized.append(str(h).strip())
        return normalized
