"""
文件格式转换器模块

支持将 PDF、XLSX 等格式转换为 CSV，以便后续解析处理。
"""

from .base import BaseConverter, ConversionResult
from .pdf_converter import PDFConverter
from .xlsx_converter import XLSXConverter
from .auto_converter import AutoConverter, convert_to_csv

__all__ = [
    "BaseConverter",
    "ConversionResult",
    "PDFConverter",
    "XLSXConverter",
    "AutoConverter",
    "convert_to_csv",
]
