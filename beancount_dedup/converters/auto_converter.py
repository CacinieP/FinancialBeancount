"""
自动转换器 - 自动检测文件格式并选择合适的转换器
"""

from pathlib import Path
from typing import List, Dict, Any, Optional

from .base import BaseConverter, ConversionResult
from .pdf_converter import PDFConverter
from .xlsx_converter import XLSXConverter


class AutoConverter:
    """
    自动文件格式转换器

    自动检测输入文件格式并使用相应的转换器将其转换为 CSV
    """

    def __init__(self):
        """初始化自动转换器，注册所有支持的转换器"""
        self.converters: List[BaseConverter] = [
            XLSXConverter(),
            PDFConverter(),
        ]

    def convert(
        self,
        input_path: str,
        output_path: Optional[str] = None,
        **kwargs
    ) -> ConversionResult:
        """
        自动检测并转换文件为 CSV

        Args:
            input_path: 输入文件路径
            output_path: 输出 CSV 文件路径（可选）
            **kwargs: 传递给具体转换器的额外参数

        Returns:
            ConversionResult 转换结果
        """
        # 验证输入文件
        if not Path(input_path).exists():
            result = ConversionResult()
            result.errors.append(f"输入文件不存在: {input_path}")
            return result

        # 查找合适的转换器
        for converter in self.converters:
            if converter.can_convert(input_path):
                return converter.convert(input_path, output_path, **kwargs)

        # 未找到支持的转换器
        result = ConversionResult()
        result.errors.append(f"不支持的文件格式: {Path(input_path).suffix}")
        result.errors.append(
            f"支持的格式: {self.get_supported_extensions()}"
        )
        return result

    def get_supported_extensions(self) -> List[str]:
        """
        获取所有支持的文件扩展名

        Returns:
            支持的扩展名列表
        """
        extensions = []
        for converter in self.converters:
            extensions.extend(converter.supported_extensions)
        return sorted(set(extensions))

    def register(self, converter: BaseConverter):
        """
        注册自定义转换器

        Args:
            converter: 转换器实例
        """
        self.converters.insert(0, converter)  # 插入到前面，优先使用

    def is_supported(self, filepath: str) -> bool:
        """
        检查文件是否支持转换

        Args:
            filepath: 文件路径

        Returns:
            是否支持转换
        """
        for converter in self.converters:
            if converter.can_convert(filepath):
                return True
        return False


def convert_to_csv(
    input_path: str,
    output_path: Optional[str] = None,
    **kwargs
) -> ConversionResult:
    """
    便捷函数：将文件转换为 CSV

    Args:
        input_path: 输入文件路径
        output_path: 输出 CSV 文件路径（可选）
        **kwargs: 传递给转换器的额外参数

    Returns:
        ConversionResult 转换结果

    Example:
        >>> result = convert_to_csv("statement.pdf")
        >>> if result.success:
        ...     print(f"转换成功: {result.output_path}")
        ...     print(f"转换了 {result.rows_converted} 行")
    """
    converter = AutoConverter()
    return converter.convert(input_path, output_path, **kwargs)
