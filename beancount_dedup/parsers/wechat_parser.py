"""
微信支付账单解析器

微信CSV格式（示例）：
交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
"""

from typing import Dict, List, Optional
from decimal import Decimal
from io import StringIO
from pathlib import Path

from .base import BaseParser, ParseResult
from ..models import Transaction, Platform, TransactionType


class WechatParser(BaseParser):
    """微信支付账单解析器"""

    # 微信CSV表头特征
    HEADER_SIGNATURES = [
        "交易时间", "交易类型", "交易对方", "收/支", "金额", "金额(元)"
    ]

    def __init__(self):
        super().__init__(Platform.WECHAT)

    def detect_format(self, headers: List[str]) -> bool:
        """检测是否为微信支付账单格式"""
        header_set = set(h for h in headers if h is not None)
        # 检查核心签名字段（至少匹配大部分）
        matched = sum(1 for sig in self.HEADER_SIGNATURES if sig in header_set)
        # 至少匹配一半以上才认为是微信格式
        return matched >= len(self.HEADER_SIGNATURES) // 2

    def parse(self, filepath: str, source_file: str = ""):
        """
        解析微信账单 CSV 文件

        微信账单格式特殊，前面有很多标题行，需要先找到真正的表头
        """
        import csv

        result = ParseResult()

        try:
            # 先读取原始行来找到表头位置
            for encoding in self.ENCODINGS:
                try:
                    with open(filepath, "r", encoding=encoding, newline="") as f:
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
                result.errors.append(f"未找到表头行，文件格式不匹配")
                return result

            # 使用 csv.DictReader 读取，但跳过表头之前的行
            # 创建一个StringIO，只包含从表头行开始的内容
            from io import StringIO
            csv_content = "".join(raw_lines[header_line_idx:])

            # 处理可能的 BOM
            if csv_content.startswith('\ufeff'):
                csv_content = csv_content[1:]

            reader = csv.DictReader(StringIO(csv_content))
            rows = list(reader)

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
                    result.errors.append(f"第{line_num}行解析错误: {str(e)}")

        except Exception as e:
            result.errors.append(f"文件读取错误: {str(e)}")

        return result
    
    def parse_row(self, row: Dict[str, str], line_num: int) -> Optional[Transaction]:
        """解析单行数据"""
        
        # 获取交易状态
        tx_status = row.get("当前状态", "").strip()
        
        # 跳过未完成的交易
        if tx_status and tx_status not in ["支付成功", "已存入零钱", "已到账", "退款成功"]:
            # 但如果没有状态字段，继续处理
            if tx_status in ["对方已退还", "已全额退款", "已退款"]:
                pass  # 这是退款，可以处理
            elif tx_status not in ["", "交易成功"]:
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
        elif direction == "/" or direction == "":
            # 可能是转账或其他类型，根据金额符号判断
            if amount < 0:
                amount = -abs(amount)
            else:
                amount = abs(amount)
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
        )
    
    def _determine_type(self, counterparty: str, description: str, tx_type_str: str) -> TransactionType:
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
