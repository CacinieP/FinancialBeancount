"""
Beancount 账户分类系统

基于 Beancount 官方最佳实践的账户分类体系
参考：https://github.com/beancount/beancount
"""

from dataclasses import dataclass
from enum import Enum


class AccountType(Enum):
    """五大核心账户类型"""

    ASSETS = "Assets"  # 资产
    LIABILITIES = "Liabilities"  # 资产
    EQUITY = "Equity"  # 权益
    INCOME = "Income"  # 收入
    EXPENSES = "Expenses"  # 支出


class AssetCategory(Enum):
    """资产二级分类"""

    CURRENT = "Current"  # 流动资产
    FIXED = "Fixed"  # 固定资产
    INVESTMENTS = "Investments"  # 投资资产
    RECEIVABLES = "Receivables"  # 应收账款
    PREPAID = "Prepaid"  # 预付款


class ExpenseCategory(Enum):
    """支出二级分类"""

    FOOD = "Food"  # 餐饮
    HOUSING = "Housing"  # 住房
    TRANSPORT = "Transport"  # 交通
    SHOPPING = "Shopping"  # 购物
    ENTERTAINMENT = "Entertainment"  # 娱乐
    MEDICAL = "Medical"  # 医疗
    EDUCATION = "Education"  # 教育
    COMMUNICATION = "Communication"  # 通信
    FINANCIAL = "Financial"  # 金融
    PERSONAL = "Personal"  # 个人
    TRANSFER = "Transfer"  # 转账
    UNKNOWN = "Unknown"  # 未知


class IncomeCategory(Enum):
    """收入二级分类"""

    SALARY = "Salary"  # 工资
    BONUS = "Bonus"  # 奖金
    INVESTMENT = "Investment"  # 投资
    REFUND = "Refund"  # 退款
    GIFT = "Gift"  # 礼物
    OTHER = "Other"  # 其他


@dataclass
class AccountMapping:
    """
    Beancount 账户映射配置
    """

    platform: str  # 平台名称
    account_type: AccountType  # 账户类型
    category: str  # 二级分类
    subcategory: str  # 三级分类（可选）
    account_template: str  # 账户模板，可用 {counterpart} 等占位符

    def get_account(self, counterparty: str = "", description: str = "") -> str:
        """生成完整的账户路径"""
        return self.account_template.format(counterpart=counterparty)


class BeancountAccountClassifier:
    """
    Beancount 账户分类器

    根据交易智能分类到符合 Beancount 最佳实践的账户
    """

    # 平台数字钱包账户映射
    PLATFORM_WALLET_ACCOUNTS = {
        "alipay": "Assets:Current:Digital:Alipay",
        "wechat": "Assets:Current:Digital:WeChat",
        "bank": "Assets:Current:Bank:Checking",
    }

    # 支出关键词映射（更细致的分类）
    EXPENSE_CATEGORIES = {
        # 餐饮 - 二级和三级分类
        ExpenseCategory.FOOD: {
            "restaurant": "Expenses:Food:Restaurant",
            "grocery": "Expenses:Food:Grocery",
            "supermarket": "Expenses:Food:Grocery",
            "coffee": "Expenses:Food:Restaurant",
            "tea": "Expenses:Food:Restaurant",
            "bakery": "Expenses:Food:Grocery",
            "convenience": "Expenses:Food:Grocery",
            "fruit": "Expenses:Food:Grocery",
            "starbucks": "Expenses:Food:Restaurant",
            "mcdonalds": "Expenses:Food:Restaurant",
            "kfc": "Expenses:Food:Restaurant",
            "subway": "Expenses:Food:Restaurant",
            "肯德基": "Expenses:Food:Restaurant",
            "麦当劳": "Expenses:Food:Restaurant",
            "星巴克": "Expenses:Food:Restaurant",
        },
        # 住房
        ExpenseCategory.HOUSING: {
            "rent": "Expenses:Housing:Rent",
            "utilities": "Expenses:Housing:Utilities",
            "water": "Expenses:Housing:Utilities",
            "electric": "Expenses:Housing:Utilities",
            "gas": "Expenses:Housing:Utilities",
            "internet": "Expenses:Housing:Utilities",
            "property": "Expenses:Housing:Maintenance",
            "物业": "Expenses:Housing:Maintenance",
            "宽带": "Expenses:Housing:Utilities",
        },
        # 交通
        ExpenseCategory.TRANSPORT: {
            "地铁": "Expenses:Transport:Public",
            "subway": "Expenses:Transport:Public",
            "公交": "Expenses:Transport:Public",
            "bus": "Expenses:Transport:Public",
            "taxi": "Expenses:Transport:Private",
            "滴滴": "Expenses:Transport:Private",
            "出租车": "Expenses:Transport:Private",
            "parking": "Expenses:Transport:Private",
            "gas": "Expenses:Transport:Private",
            "fuel": "Expenses:Transport:Private",
            "加油": "Expenses:Transport:Private",
            "高速": "Expenses:Transport:Private",
            "铁路": "Expenses:Transport:Public",
        },
        # 购物
        ExpenseCategory.SHOPPING: {
            "jd": "Expenses:Shopping:Online",
            "京东": "Expenses:Shopping:Online",
            "taobao": "Expenses:Shopping:Online",
            "淘宝": "Expenses:Shopping:Online",
            "tmall": "Expenses:Shopping:Online",
            "天猫": "Expenses:Shopping:Online",
            "pinduoduo": "Expenses:Shopping:Online",
            "拼多多": "Expenses:Shopping:Online",
            "supermarket": "Expenses:Shopping:Daily",
            "超市": "Expenses:Shopping:Daily",
            "便利店": "Expenses:Shopping:Daily",
            "7-eleven": "Expenses:Shopping:Daily",
            "familymart": "Expenses:Shopping:Daily",
            "walmart": "Expenses:Shopping:Daily",
            "快团团": "Expenses:Shopping:Daily",
            "美团": "Expenses:Food:Restaurant",  # 美团通常是餐饮
        },
        # 娱乐
        ExpenseCategory.ENTERTAINMENT: {
            "movie": "Expenses:Entertainment:Movies",
            "电影": "Expenses:Entertainment:Movies",
            "cinema": "Expenses:Entertainment:Movies",
            "theater": "Expenses:Entertainment:Live",
            "game": "Expenses:Entertainment:Digital",
            "游戏": "Expenses:Entertainment:Digital",
            "qq": "Expenses:Entertainment:Digital",
            "tencent": "Expenses:Entertainment:Digital",
            "netflix": "Expenses:Entertainment:Subscription",
            "iqiyi": "Expenses:Entertainment:Subscription",
            "youku": "Expenses:Entertainment:Subscription",
            "腾讯视频": "Expenses:Entertainment:Subscription",
        },
        # 医疗
        ExpenseCategory.MEDICAL: {
            "hospital": "Expenses:Medical:Hospitals",
            "pharmacy": "Expenses:Medical:Pharmacy",
            "药店": "Expenses:Medical:Pharmacy",
            "clinic": "Expenses:Medical:Clinic",
            "保险": "Expenses:Medical:Insurance",
            "体检": "Expenses:Medical:Checkup",
        },
        # 教育
        ExpenseCategory.EDUCATION: {
            "course": "Expenses:Education:Tuition",
            "培训": "Expenses:Education:Tuition",
            "书籍": "Expenses:Education:Books",
            "电子书": "Expenses:Education:Books",
            "kindle": "Expenses:Education:Books",
            "学校": "Expenses:Education:Tuition",
            "辅导班": "Expenses:Education:Tutoring",
            "datawhale": "Expenses:Education:Online",
        },
        # 通信
        ExpenseCategory.COMMUNICATION: {
            "mobile": "Expenses:Communication:Phone",
            "phone": "Expenses:Communication:Phone",
            "话费": "Expenses:Communication:Phone",
            "流量": "Expenses:Communication:Data",
            "充值": "Expenses:Communication:Phone",
        },
        # 金融
        ExpenseCategory.FINANCIAL: {
            "fee": "Expenses:Financial:Fee",
            "手续费": "Expenses:Financial:Fee",
            "interest": "Expenses:Financial:Interest",
            "保险": "Expenses:Financial:Insurance",
            "理财": "Expenses:Financial:Investment",
        },
    }

    # 收入关键词映射
    INCOME_CATEGORIES = {
        IncomeCategory.SALARY: {
            "工资": "Income:Salary:Gross",
            "薪资": "Income:Salary:Gross",
            "salary": "Income:Salary:Gross",
            "奖金": "Income:Salary:Bonus",
            "年终奖": "Income:Salary:Bonus",
            "绩效": "Income:Salary:Bonus",
        },
        IncomeCategory.INVESTMENT: {
            "分红": "Income:Investment:Dividend",
            "dividend": "Income:Investment:Dividend",
            "利息": "Income:Investment:Interest",
            "理财": "Income:Investment:Interest",
            "基金": "Income:Investment:Fund",
            "股票": "Income:Investment:CapitalGains",
        },
        IncomeCategory.REFUND: {
            "退款": "Income:Refund:Shopping",
            "退回": "Income:Refund:Shopping",
            "refund": "Income:Refund:Shopping",
        },
    }

    # 银行名称映射
    BANK_CODE_MAPPING = {
        "招商": "CMB",
        "工行": "ICBC",
        "建行": "CCB",
        "农行": "ABC",
        "中行": "BOC",
        "交通": "BCM",
        "邮储": "PSBC",
        "中信": "CITIC",
        "光大": "CEB",
        "华夏": "HXB",
        "民生": "CMBC",
        "广发": "GDB",
        "平安": "PAB",
        "浦发": "SPDB",
        "兴业": "CIB",
        "浙商": "ZSB",
    }

    # 特殊商户映射（手动定义）
    MERCHANT_ACCOUNT_OVERRIDES = {
        # 餐饮连锁
        "星巴克": "Expenses:Food:Restaurant",
        "Starbucks": "Expenses:Food:Restaurant",
        "麦当劳": "Expenses:Food:Restaurant",
        "McDonald's": "Expenses:Food:Restaurant",
        "肯德基": "Expenses:Food:Restaurant",
        "KFC": "Expenses:Food:Restaurant",
        "必胜客": "Expenses:Food:Restaurant",
        " Pizza Hut": "Expenses:Food:Restaurant",
        "汉堡王": "Expenses:Food:Restaurant",
        "华莱士": "Expenses:Food:Restaurant",
        # 咖啡
        "瑞幸": "Expenses:Food:Coffee",
        "luckin": "Expenses:Food:Coffee",
        "Costa": "Expenses:Food:Coffee",
        "漫咖啡": "Expenses:Food:Coffee",
        # 便利店
        "7-Eleven": "Expenses:Shopping:Daily",
        "FamilyMart": "Expenses:Shopping:Daily",
        "全家": "Expenses:Shopping:Daily",
        "罗森": "Expenses:Shopping:Daily",
        "LAWSON": "Expenses:Shopping:Daily",
        "喜士多": "Expenses:Shopping:Daily",
        # 电商
        "京东": "Expenses:Shopping:Online",
        "JD": "Expenses:Shopping:Online",
        "淘宝": "Expenses:Shopping:Online",
        "天猫": "Expenses:Shopping:Online",
        "拼多多": "Expenses:Shopping:Online",
        "唯品会": "Expenses:Shopping:Online",
        # 数字服务
        "美团": "Expenses:Food:Delivery",  # 美团外卖
        "饿了么": "Expenses:Food:Delivery",
        "滴滴": "Expenses:Transport:Private",
        "快滴": "Expenses:Transport:Private",
        "高德": "Expenses:Transport:Private",
        # 数字娱乐
        "腾讯视频": "Expenses:Entertainment:Subscription",
        "爱奇艺": "Expenses:Entertainment:Subscription",
        "Netflix": "Expenses:Entertainment:Subscription",
        "Spotify": "Expenses:Entertainment:Subscription",
        # 教育
        "得到": "Expenses:Education:Books",
        "知乎": "Expenses:Education:Online",
        "Bilibili": "Expenses:Entertainment:Subscription",
    }

    def __init__(self):
        """初始化分类器"""

    def classify_transaction(
        self, counterparty: str, description: str, tx_type: str = "", platform: str = ""
    ) -> dict[str, str]:
        """
        对交易进行智能分类

        Returns:
            {
                "asset_account": "资产账户",
                "opposing_account": "对方账户（支出/收入）",
                "category": "分类",
                "tags": ["标签列表"]
            }
        """
        text = f"{counterparty} {description}".lower()

        # 1. 确定资产账户
        asset_account = self._determine_asset_account(platform, counterparty)

        # 2. 确定对方账户
        if tx_type == "expense" or (not tx_type and self._is_likely_expense(text)):
            opposing_account = self._classify_expense(counterparty, description, text)
        elif tx_type == "income" or self._is_likely_income(text):
            opposing_account = self._classify_income(counterparty, description, text)
        else:
            opposing_account = (
                "Expenses:Unknown" if self._is_likely_expense(text) else "Income:Unknown"
            )

        # 3. 生成标签
        tags = self._generate_tags(counterparty, description, text)

        return {
            "asset_account": asset_account,
            "opposing_account": opposing_account,
            "category": self._get_category_from_account(opposing_account),
            "tags": tags,
        }

    def _determine_asset_account(self, platform: str, counterparty: str = "") -> str:
        """确定资产账户"""
        platform = platform.lower()

        # 直接返回平台对应的账户
        if platform == "alipay":
            return self.PLATFORM_WALLET_ACCOUNTS["alipay"]
        elif platform == "wechat":
            return self.PLATFORM_WALLET_ACCOUNTS["wechat"]
        elif platform == "bank":
            # 尝试从 counterparty 中提取银行名称
            for bank_name, code in self.BANK_CODE_MAPPING.items():
                if bank_name in counterparty:
                    return f"Assets:Current:Bank:{code}"
            return self.PLATFORM_WALLET_ACCOUNTS["bank"]

        return "Assets:Current:Cash"

    def _classify_expense(self, counterparty: str, description: str, text: str) -> str:
        """分类支出交易"""
        # 1. 检查特殊商户映射
        for merchant, account in self.MERCHANT_ACCOUNT_OVERRIDES.items():
            if merchant.lower() in text:
                return account

        # 2. 检查按类别的关键词
        for keywords in self.EXPENSE_CATEGORIES.values():
            for keyword, account in keywords.items():
                if keyword in text:
                    return account

        return "Expenses:Unknown"

    def _classify_income(self, counterparty: str, description: str, text: str) -> str:
        """分类收入交易"""
        # 1. 检查退款
        if any(kw in text for kw in ["退款", "退回", "refund"]):
            return self.INCOME_CATEGORIES[IncomeCategory.REFUND]["退款"]

        # 2. 检查工资
        for keyword, account in self.INCOME_CATEGORIES[IncomeCategory.SALARY].items():
            if keyword in text:
                return account

        # 3. 检查投资
        for keyword, account in self.INCOME_CATEGORIES[IncomeCategory.INVESTMENT].items():
            if keyword in text:
                return account

        return "Income:Other"

    def _is_likely_expense(self, text: str) -> bool:
        """判断是否为支出"""
        expense_keywords = [
            "购买",
            "消费",
            "支付",
            "支出",
            "买",
            "餐厅",
            "超市",
            "地铁",
            "公交",
            "打车",
            "外卖",
            "美团",
            "饿了么",
        ]
        return any(kw in text for kw in expense_keywords)

    def _is_likely_income(self, text: str) -> bool:
        """判断是否为收入"""
        income_keywords = [
            "工资",
            "薪资",
            "奖金",
            "分红",
            "利息",
            "退款",
            "转入",
            "收入",
            "进账",
            "存入",
            "充值",
            "工资",
        ]
        return any(kw in text for kw in income_keywords)

    def _generate_tags(self, counterparty: str, description: str, text: str) -> list[str]:
        """生成交易标签"""
        tags = []

        # 添加平台标签
        if "支付宝" in counterparty or "alipay" in text:
            tags.append("alipay")
        if "微信" in counterparty or "wechat" in text:
            tags.append("wechat")
        if "银行" in counterparty or "bank" in text:
            tags.append("bank")

        # 添加场景标签
        if "外卖" in text or "配送" in text:
            tags.append("delivery")
        if "快递" in text or "配送" in text:
            tags.append("delivery")
        if "红包" in text:
            tags.append("red_packet")
        if "充值" in text:
            tags.append("topup")

        return tags

    def determine_asset_account(self, platform: str, counterparty: str = "") -> str:
        """Public API for determining the asset account for a transaction."""
        return self._determine_asset_account(platform, counterparty)

    def _get_category_from_account(self, account: str) -> str:
        """从账户路径提取分类"""
        parts = account.split(":")
        if len(parts) >= 2:
            return parts[1]
        return "Unknown"


# 预定义的账户模板
ACCOUNT_TEMPLATES = {
    # 按年份组织
    "yearly": """
# 推荐的文件组织（按年份）
main.beancount              # 主文件
├── accounts.beancount       # 账户定义
├── 2024/
│   ├── 2024-01.beancount  # 1月交易
│   ├── 2024-02.beancount  # 2月交易
│   └── ...
└── 2025/
    ├── 2025-01.beancount
    └── ...
""",
    # 按账户类型组织
    "by_account": """
# 推荐的文件组织（按账户类型）
main.beancount
├── accounts.beancount       # 所有账户定义
├── assets.beancount        # 资产账户交易
├── liabilities.beancount   # 负债账户交易
├── expenses.beancount     # 支出账户交易
└── income.beancount       # 收入账户交易
""",
    # 按平台组织
    "by_platform": """
# 推荐的文件组织（按平台）
main.beancount
├── accounts.beancount
├── alipay.beancount       # 支付宝交易
├── wechat.beancount       # 微信交易
└── bank.beancount         # 银行交易
""",
}
