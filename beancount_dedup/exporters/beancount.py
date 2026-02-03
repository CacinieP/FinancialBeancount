"""
Beancount 格式导出器
"""

import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Dict, Optional, Set

from ..models import Transaction, Platform, DedupStatus
from ..account_classifier import BeancountAccountClassifier


class BeancountExporter:
    """
    Beancount 格式导出器
    
    将交易记录导出为 Beancount 复式记账格式
    """
    
    # 默认账户映射
    DEFAULT_ACCOUNT_MAP = {
        # 资产账户
        "alipay": "Assets:Digital:Alipay",
        "wechat": "Assets:Digital:WeChat",
        "bank": "Assets:Bank:Unknown",
        
        # 支出账户（需要根据商户分类细化）
        "expense_unknown": "Expenses:Unknown",
        "expense_food": "Expenses:Food:Dining",
        "expense_transport": "Expenses:Transport",
        "expense_shopping": "Expenses:Shopping",
        "expense_entertainment": "Expenses:Entertainment",
        "expense_medical": "Expenses:Medical",
        "expense_education": "Expenses:Education",
        "expense_housing": "Expenses:Housing",
        "expense_communication": "Expenses:Communication",
        "expense_financial": "Expenses:Financial:Fee",
        
        # 收入账户
        "income_salary": "Income:Salary",
        "income_bonus": "Income:Bonus",
        "income_investment": "Income:Investment",
        "income_refund": "Income:Refund",
        "income_other": "Income:Other",
        
        # 内部转账
        "transfer": "Equity:Transfer",
    }
    
    # 商户关键词到分类的映射
    CATEGORY_KEYWORDS = {
        "food": ["餐厅", "饭店", "美食", "外卖", "肯德基", "麦当劳", "星巴克", 
                "超市", "便利店", "水果", "生鲜", "饿了么", "美团"],
        "transport": ["地铁", "公交", "滴滴", "出租车", "加油", "停车", "高速", "铁路", "航空"],
        "shopping": ["京东", "淘宝", "天猫", "拼多多", "唯品会", "苏宁", "商场", "购物中心"],
        "entertainment": ["电影", "影院", "游戏", "视频", "音乐", "会员", "爱奇艺", "腾讯", "优酷"],
        "medical": ["医院", "药店", "诊所", "体检", "医保"],
        "education": ["学校", "培训", "课程", "教材", "考试", "学费"],
        "housing": ["房租", "物业", "水电", "燃气", "宽带"],
        "communication": ["话费", "流量", "移动", "联通", "电信", "宽带"],
        "financial": ["手续费", "利息", "理财", "基金", "保险"],
    }
    
    def __init__(self, account_map: Optional[Dict[str, str]] = None,
                 use_classifier: bool = True):
        """
        初始化导出器

        Args:
            account_map: 自定义账户映射，覆盖默认值
            use_classifier: 是否使用智能分类器（默认True）
        """
        self.account_map = {**self.DEFAULT_ACCOUNT_MAP}
        if account_map:
            self.account_map.update(account_map)

        # 初始化智能分类器
        self.use_classifier = use_classifier
        if use_classifier:
            self.classifier = BeancountAccountClassifier()
    
    def _sanitize_narration(self, text: str) -> str:
        """
        清理叙述文本，使其符合Beancount要求
        """
        if not text:
            return "Unknown"
        # 移除或替换特殊字符
        text = re.sub(r'["\n\r]', '', text)
        text = text.strip()
        return text[:50] if text else "Unknown"
    
    def _determine_asset_account(self, tx: Transaction) -> str:
        """
        确定资产账户（使用智能分类器）
        """
        if self.use_classifier:
            return self.classifier._determine_asset_account(
                tx.platform.value,
                tx.counterparty
            )

        # 回退到旧逻辑
        if tx.platform == Platform.ALIPAY:
            return self.account_map["alipay"]
        elif tx.platform == Platform.WECHAT:
            return self.account_map["wechat"]
        elif tx.platform == Platform.BANK:
            if tx.payment_method:
                bank_name = self._extract_bank_name(tx.payment_method)
                if bank_name:
                    return f"Assets:Bank:{bank_name}"
            return self.account_map["bank"]
        return "Assets:Unknown"
    
    def _extract_bank_name(self, payment_method: str) -> Optional[str]:
        """
        从支付方式描述中提取银行名称
        """
        bank_keywords = {
            "招商": "CMB", "工行": "ICBC", "建行": "CCB", "农行": "ABC",
            "中行": "BOC", "交通": "BCM", "邮储": "PSBC", "中信": "CITIC",
            "光大": "CEB", "华夏": "HXB", "民生": "CMBC", "广发": "GDB",
            "平安": "PAB", "浦发": "SPDB", "兴业": "CIB", "浙商": "ZSB",
        }
        for keyword, code in bank_keywords.items():
            if keyword in payment_method:
                return code
        return None
    
    def _determine_expense_category(self, tx: Transaction) -> str:
        """
        根据交易对手方和描述确定支出分类（使用智能分类器）
        """
        if self.use_classifier:
            # 使用智能分类器进行分类
            classification = self.classifier.classify_transaction(
                tx.counterparty,
                tx.description,
                tx_type="expense",
                platform=tx.platform.value
            )
            return classification["opposing_account"]

        # 回退到旧逻辑
        text = f"{tx.counterparty} {tx.description}".lower()

        for category, keywords in self.CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    return self.account_map.get(f"expense_{category}", "Expenses:Unknown")

        return self.account_map["expense_unknown"]
    
    def _determine_income_category(self, tx: Transaction) -> str:
        """
        确定收入分类（使用智能分类器）
        """
        if self.use_classifier:
            classification = self.classifier.classify_transaction(
                tx.counterparty,
                tx.description,
                tx_type="income",
                platform=tx.platform.value
            )
            return classification["opposing_account"]

        # 回退到旧逻辑
        text = f"{tx.counterparty} {tx.description}".lower()

        if "退款" in text or "退回" in text or "refund" in text:
            return self.account_map["income_refund"]

        if any(kw in text for kw in ["工资", "薪资", "salary", "奖金", "bonus"]):
            return self.account_map["income_salary"]

        if any(kw in text for kw in ["理财", "基金", "股息", "分红", "利息"]):
            return self.account_map["income_investment"]

        return self.account_map["income_other"]
    
    def _format_amount(self, amount: Decimal, currency: str = "CNY") -> str:
        """
        格式化金额
        """
        # Beancount要求金额精确到两位小数
        return f"{amount:.2f} {currency}"
    
    def export_transaction(self, tx: Transaction, 
                          include_meta: bool = True) -> str:
        """
        导出单笔交易为Beancount格式
        
        示例输出：
        2024-01-15 * "星巴克" "拿铁咖啡"
          id: "tx_abc123"
          platform: "alipay"
          status: "unique"
          Assets:Digital:Alipay    -35.00 CNY
          Expenses:Food:Dining      35.00 CNY
        """
        lines = []
        
        # 日期和标志
        date_str = tx.date_str
        flag = "*" if tx.status == DedupStatus.UNIQUE else "!"
        
        # Payee 和 Narration
        payee = self._sanitize_narration(tx.counterparty)
        narration = self._sanitize_narration(tx.description)
        
        # 首行
        lines.append(f"{date_str} {flag} \"{payee}\" \"{narration}\"")
        
        # 元数据
        if include_meta:
            lines.append(f"  id: \"{tx.id}\"")
            lines.append(f"  platform: \"{tx.platform.value}\"")
            lines.append(f"  status: \"{tx.status.value}\"")
            if tx.payment_method:
                lines.append(f"  method: \"{self._sanitize_narration(tx.payment_method)}\"")
            if tx.duplicate_of:
                lines.append(f"  duplicate_of: \"{tx.duplicate_of}\"")
            if tx.match_level:
                lines.append(f"  match_level: \"{tx.match_level}\"")
        
        # 账户行
        asset_account = self._determine_asset_account(tx)
        amount_abs = tx.amount_abs
        
        if tx.is_expense:
            # 支出：资产减少，费用增加
            expense_account = self._determine_expense_category(tx)
            lines.append(f"  {asset_account:<30} {-amount_abs:>10.2f} CNY")
            lines.append(f"  {expense_account:<30} {amount_abs:>10.2f} CNY")
        elif tx.is_income:
            # 收入：资产增加，收入增加（Beancount中收入为负）
            income_account = self._determine_income_category(tx)
            lines.append(f"  {asset_account:<30} {amount_abs:>10.2f} CNY")
            lines.append(f"  {income_account:<30} {-amount_abs:>10.2f} CNY")
        else:
            # 零金额（不应该发生）
            lines.append(f"  {asset_account:<30} 0.00 CNY")
        
        return "\n".join(lines)
    
    def export(self, transactions: List[Transaction],
               output_path: Optional[str] = None,
               include_meta: bool = True,
               option_entries: Optional[List[str]] = None) -> str:
        """
        批量导出交易为Beancount格式
        
        Args:
            transactions: 交易列表
            output_path: 输出文件路径（可选）
            include_meta: 是否包含元数据
            option_entries: 额外的选项条目（如option "title" "My Ledger"）
        
        Returns:
            Beancount格式的完整文本
        """
        lines = []
        
        # 文件头
        lines.append("; Generated by Beancount Dedup Tool")
        lines.append(f"; Generated at: {datetime.now().isoformat()}")
        lines.append("")
        
        # 选项条目
        if option_entries:
            for entry in option_entries:
                lines.append(entry)
            lines.append("")
        
        # 账户声明
        accounts: Set[str] = set()
        for tx in transactions:
            accounts.add(self._determine_asset_account(tx))
            if tx.is_expense:
                accounts.add(self._determine_expense_category(tx))
            elif tx.is_income:
                accounts.add(self._determine_income_category(tx))
        
        lines.append("; 账户声明")
        for account in sorted(accounts):
            lines.append(f"{datetime.now().year}-01-01 open {account} CNY")
        lines.append("")
        
        # 按日期分组并导出交易
        current_date = None
        for tx in sorted(transactions, key=lambda x: x.datetime):
            if current_date != tx.date_str:
                current_date = tx.date_str
                lines.append(f"\n; === {current_date} ===")
            
            lines.append(self.export_transaction(tx, include_meta))
            lines.append("")
        
        result = "\n".join(lines)
        
        # 写入文件
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(result)
        
        return result
    
    def export_duplicate_report(self, transactions: List[Transaction],
                                 output_path: Optional[str] = None) -> str:
        """
        导出去重报告为Beancount注释格式
        """
        lines = ["; 去重报告", "; " + "=" * 50, ""]
        
        # 统计
        total = len(transactions)
        unique = sum(1 for t in transactions if t.status == DedupStatus.UNIQUE)
        duplicate = sum(1 for t in transactions if t.status == DedupStatus.DUPLICATE)
        review = sum(1 for t in transactions if t.status == DedupStatus.REVIEW)
        
        lines.extend([
            "; 统计",
            f"; 总交易数: {total}",
            f"; 唯一交易: {unique}",
            f"; 重复交易: {duplicate}",
            f"; 待复核: {review}",
            "",
            "; 重复交易详情",
        ])
        
        for tx in transactions:
            if tx.status == DedupStatus.DUPLICATE:
                lines.append(
                    f"; [{tx.platform.value}] {tx.date_str} {tx.amount_abs} "
                    f"{tx.counterparty[:20]} -> duplicate_of: {tx.duplicate_of}"
                )
        
        lines.extend(["", "; 待复核交易", ""])
        
        for tx in transactions:
            if tx.status == DedupStatus.REVIEW:
                lines.append(
                    f"; [{tx.platform.value}] {tx.date_str} {tx.amount_abs} "
                    f"{tx.counterparty[:20]} -> potential_dup: {tx.duplicate_of}"
                )
        
        result = "\n".join(lines)
        
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(result)
        
        return result
