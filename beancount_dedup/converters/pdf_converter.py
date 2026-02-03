"""
PDF 转换器 - 将 PDF 表格转换为 CSV
"""

import re
from typing import List, Dict, Any, Optional
from pathlib import Path

from .base import BaseConverter, ConversionResult


class PDFConverter(BaseConverter):
    """
    PDF 到 CSV 转换器

    支持从 PDF 文件中提取表格数据并转换为 CSV 格式
    """

    @property
    def supported_extensions(self) -> List[str]:
        return ['.pdf']

    def convert(
        self,
        input_path: str,
        output_path: Optional[str] = None,
        page: Optional[int] = None,
        **kwargs
    ) -> ConversionResult:
        """
        转换 PDF 文件为 CSV

        Args:
            input_path: PDF 文件路径
            output_path: 输出 CSV 文件路径
            page: 指定要转换的页码（None表示全部页面）
            **kwargs: 额外参数
                - encoding: 输出编码（默认 utf-8-sig）
                - password: PDF 密码（如果有）

        Returns:
            ConversionResult 转换结果
        """
        result = ConversionResult()

        # 验证输入文件
        if not Path(input_path).exists():
            result.errors.append(f"输入文件不存在: {input_path}")
            return result

        if not self.can_convert(input_path):
            result.errors.append(f"不支持的文件格式: {input_path}")
            return result

        # 生成输出路径
        if not output_path:
            output_path = self._generate_output_path(input_path)
        result.output_path = output_path

        # 尝试使用不同的PDF库提取表格
        rows = self._extract_tables(input_path, page, result)

        if not rows:
            result.errors.append("未能从 PDF 中提取到表格数据")
            return result

        # 写入 CSV
        try:
            encoding = kwargs.get('encoding', 'utf-8-sig')
            result.rows_converted = self._write_csv(rows, output_path, encoding)
            result.success = True
        except Exception as e:
            result.errors.append(f"写入 CSV 文件失败: {str(e)}")
            return result

        return result

    def _extract_tables(
        self,
        pdf_path: str,
        page: Optional[int],
        result: ConversionResult
    ) -> List[Dict[str, Any]]:
        """
        从 PDF 中提取表格数据

        尝试多种方法按优先级：
        1. pdfplumber - 最准确的表格提取
        2. tabula-py - 基于 Java 的解决方案
        3. PyPDF2 + 正则表达式 - 兜底方案
        """
        # 方法 1: 尝试 pdfplumber
        rows = self._try_pdfplumber(pdf_path, page, result)
        if rows:
            result.metadata['method'] = 'pdfplumber'
            return rows

        # 方法 2: 尝试 tabula-py
        rows = self._try_tabula(pdf_path, page, result)
        if rows:
            result.metadata['method'] = 'tabula'
            return rows

        # 方法 3: 兜底方案 - 使用简单的文本提取
        rows = self._try_text_extraction(pdf_path, page, result)
        if rows:
            result.metadata['method'] = 'text_extraction'
            result.warnings.append("使用文本提取方法，可能不够准确")
            return rows

        result.errors.append("所有 PDF 提取方法均失败")
        result.warnings.append("请确保 PDF 文件包含可提取的表格数据")
        return []

    def _try_pdfplumber(
        self,
        pdf_path: str,
        page: Optional[int],
        result: ConversionResult
    ) -> Optional[List[Dict[str, Any]]]:
        """尝试使用 pdfplumber 提取表格"""
        try:
            import pdfplumber
        except ImportError:
            result.warnings.append("pdfplumber 未安装，跳过此方法")
            return None

        try:
            all_rows = []

            with pdfplumber.open(pdf_path) as pdf:
                pages_to_process = [page - 1] if page else range(len(pdf.pages))

                for page_num in pages_to_process:
                    if page_num < 0 or page_num >= len(pdf.pages):
                        continue

                    pdf_page = pdf.pages[page_num]
                    tables = pdf_page.extract_tables()

                    for table in tables:
                        if not table:
                            continue

                        # 转换为字典列表
                        headers = None
                        for i, row in enumerate(table):
                            # 清理行数据
                            cleaned_row = [
                                str(cell).strip() if cell is not None else ""
                                for cell in row
                            ]

                            if i == 0:
                                # 第一行作为表头
                                headers = self._normalize_headers(cleaned_row)
                            else:
                                # 数据行
                                if headers and len(cleaned_row) == len(headers):
                                    row_dict = dict(zip(headers, cleaned_row))
                                    all_rows.append(row_dict)
                                elif headers:
                                    # 处理列数不匹配的情况
                                    row_dict = dict(zip(
                                        headers,
                                        cleaned_row[:len(headers)]
                                    ))
                                    all_rows.append(row_dict)

            return all_rows

        except Exception as e:
            result.warnings.append(f"pdfplumber 提取失败: {str(e)}")
            return None

    def _try_tabula(
        self,
        pdf_path: str,
        page: Optional[str],
        result: ConversionResult
    ) -> Optional[List[Dict[str, Any]]]:
        """尝试使用 tabula-py 提取表格"""
        try:
            import tabula
        except ImportError:
            result.warnings.append("tabula-py 未安装，跳过此方法")
            return None

        try:
            # tabula 的 pages 参数：None 表示全部，数字表示特定页
            pages = page if page else None

            # 读取所有表格
            tables = tabula.read_pdf(
                pdf_path,
                pages=pages,
                multiple_tables=True,
                encoding='utf-8'
            )

            all_rows = []

            for table in tables:
                if table.empty:
                    continue

                # 转换为字典列表
                headers = self._normalize_headers(table.columns.tolist())
                for _, row in table.iterrows():
                    row_dict = {
                        headers[i]: str(row.iloc[i]).strip()
                        for i in range(len(headers))
                    }
                    all_rows.append(row_dict)

            return all_rows

        except Exception as e:
            result.warnings.append(f"tabula-py 提取失败: {str(e)}")
            return None

    def _try_text_extraction(
        self,
        pdf_path: str,
        page: Optional[int],
        result: ConversionResult
    ) -> Optional[List[Dict[str, Any]]]:
        """
        尝试使用简单文本提取（兜底方案）

        从 PDF 中提取文本并尝试识别表格结构
        """
        try:
            # 尝试 PyPDF2
            try:
                from PyPDF2 import PdfReader
            except ImportError:
                try:
                    from pypdf import PdfReader
                except ImportError:
                    result.warnings.append("PyPDF2/pypdf 未安装")
                    return None

            reader = PdfReader(pdf_path)
            pages_to_process = [page - 1] if page else range(len(reader.pages))

            all_rows = []

            for page_num in pages_to_process:
                if page_num < 0 or page_num >= len(reader.pages):
                    continue

                pdf_page = reader.pages[page_num]
                text = pdf_page.extract_text()

                # 尝试解析文本中的表格
                rows = self._parse_text_as_table(text, result)
                all_rows.extend(rows)

            return all_rows

        except Exception as e:
            result.warnings.append(f"文本提取失败: {str(e)}")
            return None

    def _parse_text_as_table(
        self,
        text: str,
        result: ConversionResult
    ) -> List[Dict[str, Any]]:
        """
        将文本解析为表格

        这是一个简单的启发式方法，尝试从文本中识别表格结构
        """
        lines = text.strip().split('\n')
        if not lines:
            return []

        # 寻找看起来像表头的行（包含常见关键词）
        header_keywords = ['日期', '时间', '交易', '金额', '对方', '商品', '说明']
        header_line_idx = -1

        for i, line in enumerate(lines):
            if any(keyword in line for keyword in header_keywords):
                header_line_idx = i
                break

        if header_line_idx == -1:
            # 没有找到表头，使用第一行
            header_line_idx = 0

        # 解析表头
        headers = self._parse_header_line(lines[header_line_idx])
        headers = self._normalize_headers(headers)

        rows = []

        # 解析数据行
        for line in lines[header_line_idx + 1:]:
            if not line.strip():
                continue

            values = self._parse_data_line(line, len(headers))
            if values and len(values) == len(headers):
                row_dict = dict(zip(headers, values))
                rows.append(row_dict)

        if rows:
            return rows

        # 如果表格解析失败，返回单列数据
        return [{"text": line.strip()} for line in lines if line.strip()]

    def _parse_header_line(self, line: str) -> List[str]:
        """
        解析表头行

        尝试多种分隔符
        """
        # 尝试不同的分隔符
        for sep in ['\t', '|', '  ', ' ']:
            parts = line.split(sep)
            if len(parts) > 1:
                return [p.strip() for p in parts if p.strip()]

        # 如果没有找到分隔符，返回整行作为单个列
        return [line.strip()]

    def _parse_data_line(self, line: str, expected_cols: int) -> Optional[List[str]]:
        """
        解析数据行

        Args:
            line: 数据行文本
            expected_cols: 期望的列数

        Returns:
            解析后的值列表，如果解析失败返回 None
        """
        # 尝试不同的分隔符
        for sep in ['\t', '|', '  ', ' ']:
            parts = line.split(sep)
            if len(parts) == expected_cols:
                return [p.strip() for p in parts]

        # 如果无法匹配，尝试空格分割
        parts = line.split()
        if len(parts) >= expected_cols:
            return parts[:expected_cols]

        return None
