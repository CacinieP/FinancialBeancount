"""
Beancount 账户分类系统

基于 Beancount 官方最佳实践的账户分类体系
参考：https://github.com/beancount/beancount
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AccountConfig


_DEFAULT_RULES = json.loads(
    files("beancount_dedup").joinpath("default_account_rules.json").read_text(encoding="utf-8")
)


def is_valid_account(account: object) -> bool:
    """Validate account names without requiring the optional Beancount package.

    Each component starts with an uppercase ASCII letter or a Unicode letter;
    remaining characters are letters, numbers or hyphens. Whitespace, quotes
    and newlines cannot become Beancount directives through configuration.
    """
    if not isinstance(account, str):
        return False
    parts = account.split(":")
    if len(parts) < 2 or parts[0] not in {"Assets", "Liabilities", "Equity", "Income", "Expenses"}:
        return False
    for part in parts[1:]:
        if not part or not part[0].isalpha():
            return False
        if part[0].isascii() and not part[0].isupper():
            return False
        if not all(char.isalnum() or char == "-" for char in part):
            return False
    return True


def _validate_rules(rules: dict[str, str], roots: tuple[str, ...]) -> None:
    for keyword, account in rules.items():
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("Classification rule keywords must be non-empty strings")
        if not is_valid_account(account) or account.split(":", 1)[0] not in roots:
            raise ValueError(f"Invalid classification account for keyword {keyword!r}")


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
        "unionpay": "Assets:Current:Digital:UnionPay",
    }

    # Compatibility views of the packaged default rules. Instances copy these
    # mappings so user overrides never mutate the defaults.
    EXPENSE_CATEGORIES = {
        ExpenseCategory(key): dict(value)
        for key, value in _DEFAULT_RULES["expense_categories"].items()
    }
    INCOME_CATEGORIES = {
        IncomeCategory(key): dict(value)
        for key, value in _DEFAULT_RULES["income_categories"].items()
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

    MERCHANT_ACCOUNT_OVERRIDES = dict(_DEFAULT_RULES["merchant_overrides"])

    def __init__(self, accounts: AccountConfig | None = None):
        """
        初始化分类器

        Args:
            accounts: 用户账户配置（通常来自 YAML 的 accounts 段）。
                platform_wallets / bank_codes 覆盖内置值；
                merchant_overrides 优先于内置商户和关键词规则。
        """
        self.platform_wallets = dict(self.PLATFORM_WALLET_ACCOUNTS)
        self.bank_codes = dict(self.BANK_CODE_MAPPING)
        self.user_overrides: dict[str, str] = {}
        self.expense_rules: dict[str, str] = {}
        self.income_rules: dict[str, str] = {}
        self.rules_mode = "append"
        if accounts is not None:
            self.rules_mode = accounts.rules_mode
            if self.rules_mode not in {"append", "replace"}:
                raise ValueError("accounts.rules_mode must be 'append' or 'replace'")
            self.platform_wallets.update(accounts.platform_wallets)
            self.bank_codes.update(accounts.bank_codes)
            self.user_overrides = dict(accounts.merchant_overrides)
            self.expense_rules = dict(accounts.expense_rules)
            self.income_rules = dict(accounts.income_rules)

        _validate_rules(self.user_overrides, ("Expenses", "Income"))
        _validate_rules(self.expense_rules, ("Expenses",))
        _validate_rules(self.income_rules, ("Income",))
        for account in self.platform_wallets.values():
            if not is_valid_account(account) or account.split(":", 1)[0] not in {
                "Assets",
                "Liabilities",
            }:
                raise ValueError(
                    "Platform wallet accounts must be valid asset or liability accounts"
                )
        for code in self.bank_codes.values():
            if (
                not isinstance(code, str)
                or ":" in code
                or not is_valid_account(f"Assets:Bank:{code}")
            ):
                raise ValueError("Bank codes must be a valid single account component")

        use_defaults = self.rules_mode == "append"
        self.merchant_rules = dict(self.MERCHANT_ACCOUNT_OVERRIDES) if use_defaults else {}
        self.expense_categories = (
            {key: dict(value) for key, value in self.EXPENSE_CATEGORIES.items()}
            if use_defaults
            else {}
        )
        self.income_categories = (
            {key: dict(value) for key, value in self.INCOME_CATEGORIES.items()}
            if use_defaults
            else {}
        )

    @staticmethod
    def is_fallback(account: str) -> bool:
        """Whether classification did not find a configured or built-in rule."""
        return account in {"Expenses:Unknown", "Income:Other", "Income:Unknown"}

    def candidate_accounts(self, tx_type: str) -> list[str]:
        """Return accounts in the active account tree for model suggestions."""
        if tx_type not in {"expense", "income"}:
            raise ValueError("tx_type must be 'expense' or 'income'")
        root = "Expenses" if tx_type == "expense" else "Income"
        accounts = {"Expenses:Unknown" if tx_type == "expense" else "Income:Other"}
        accounts.update(
            account for account in self.user_overrides.values() if account.startswith(f"{root}:")
        )
        rules = self.expense_rules if tx_type == "expense" else self.income_rules
        accounts.update(rules.values())
        categories = self.expense_categories if tx_type == "expense" else self.income_categories
        accounts.update(account for values in categories.values() for account in values.values())
        if tx_type == "expense":
            accounts.update(self.merchant_rules.values())
        return sorted(accounts)

    def has_rule_match(self, counterparty: str, description: str, tx_type: str) -> bool:
        """Distinguish an explicit fallback-account rule from no matching rule."""
        if tx_type not in {"expense", "income"}:
            raise ValueError("tx_type must be 'expense' or 'income'")
        text = f"{counterparty} {description}".lower()
        root = "Expenses" if tx_type == "expense" else "Income"
        if self._match_user_override(text, root):
            return True
        rules = self.expense_rules if tx_type == "expense" else self.income_rules
        if self._match_rules(text, rules):
            return True
        if tx_type == "expense" and self._match_rules(text, self.merchant_rules):
            return True
        categories = self.expense_categories if tx_type == "expense" else self.income_categories
        return any(self._match_rules(text, values) for values in categories.values())

    @staticmethod
    def _match_rules(text: str, rules: dict[str, str]) -> str | None:
        for keyword, account in rules.items():
            if keyword.lower() in text:
                return account
        return None

    def _match_user_override(self, text: str, root: str) -> str | None:
        for keyword, account in self.user_overrides.items():
            if account.startswith(f"{root}:") and keyword.lower() in text:
                return account
        return None

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
        if platform in ("alipay", "wechat", "unionpay"):
            return self.platform_wallets[platform]
        elif platform == "bank":
            # 尝试从 counterparty 中提取银行名称
            for bank_name, code in self.bank_codes.items():
                if bank_name in counterparty:
                    return f"Assets:Current:Bank:{code}"
            return self.platform_wallets["bank"]

        return "Assets:Current:Cash"

    def _classify_expense(self, counterparty: str, description: str, text: str) -> str:
        """分类支出交易"""
        user_account = self._match_user_override(text, "Expenses")
        if user_account:
            return user_account

        custom_account = self._match_rules(text, self.expense_rules)
        if custom_account:
            return custom_account

        # 1. 检查特殊商户映射
        for merchant, account in self.merchant_rules.items():
            if merchant.lower() in text:
                return account

        # 2. 检查按类别的关键词
        for keywords in self.expense_categories.values():
            for keyword, account in keywords.items():
                if keyword in text:
                    return account

        return "Expenses:Unknown"

    def _classify_income(self, counterparty: str, description: str, text: str) -> str:
        """分类收入交易"""
        user_account = self._match_user_override(text, "Income")
        if user_account:
            return user_account

        custom_account = self._match_rules(text, self.income_rules)
        if custom_account:
            return custom_account

        # The packaged order preserves refund, salary and investment priority.
        for keywords in self.income_categories.values():
            account = self._match_rules(text, keywords)
            if account:
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
