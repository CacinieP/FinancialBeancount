"""
Tests for beancount_dedup.account_classifier — BeancountAccountClassifier.
"""

import pytest

from beancount_dedup.account_classifier import (
    BeancountAccountClassifier,
    AccountType,
    AssetCategory,
    ExpenseCategory,
    IncomeCategory,
)


# ── Enum smoke tests ──────────────────────────────────────────────────────


class TestEnums:
    def test_account_type_values(self):
        assert AccountType.ASSETS.value == "Assets"
        assert AccountType.INCOME.value == "Income"
        assert AccountType.EXPENSES.value == "Expenses"

    def test_expense_category_values(self):
        assert ExpenseCategory.FOOD.value == "Food"
        assert ExpenseCategory.TRANSPORT.value == "Transport"
        assert ExpenseCategory.SHOPPING.value == "Shopping"

    def test_income_category_values(self):
        assert IncomeCategory.SALARY.value == "Salary"
        assert IncomeCategory.REFUND.value == "Refund"


# ── classify_transaction ──────────────────────────────────────────────────


class TestClassifyTransaction:
    def test_expense_food(self, classifier):
        result = classifier.classify_transaction(
            "星巴克", "拿铁咖啡", tx_type="expense", platform="alipay"
        )
        assert result["asset_account"] == "Assets:Current:Digital:Alipay"
        assert "Food" in result["opposing_account"]

    def test_expense_transport(self, classifier):
        result = classifier.classify_transaction(
            "滴滴出行", "打车", tx_type="expense", platform="wechat"
        )
        assert result["asset_account"] == "Assets:Current:Digital:WeChat"
        assert "Transport" in result["opposing_account"]

    def test_expense_shopping(self, classifier):
        result = classifier.classify_transaction(
            "京东", "购物", tx_type="expense", platform="alipay"
        )
        assert "Shopping" in result["opposing_account"]

    def test_expense_entertainment(self, classifier):
        result = classifier.classify_transaction(
            "腾讯视频", "会员", tx_type="expense", platform="wechat"
        )
        assert "Entertainment" in result["opposing_account"]

    def test_income_salary(self, classifier):
        result = classifier.classify_transaction(
            "公司名称", "工资", tx_type="income", platform="bank"
        )
        assert "Salary" in result["opposing_account"]

    def test_income_refund(self, classifier):
        result = classifier.classify_transaction(
            "京东", "退款", tx_type="income", platform="alipay"
        )
        assert "Refund" in result["opposing_account"]

    def test_result_has_tags(self, classifier):
        result = classifier.classify_transaction(
            "星巴克", "咖啡", tx_type="expense", platform="alipay"
        )
        assert isinstance(result["tags"], list)

    def test_result_has_category(self, classifier):
        result = classifier.classify_transaction(
            "星巴克", "咖啡", tx_type="expense", platform="alipay"
        )
        assert "category" in result
        assert isinstance(result["category"], str)


# ── _determine_asset_account ──────────────────────────────────────────────


class TestDetermineAssetAccount:
    @pytest.mark.parametrize(
        "platform, expected",
        [
            ("alipay", "Assets:Current:Digital:Alipay"),
            ("wechat", "Assets:Current:Digital:WeChat"),
            ("bank", "Assets:Current:Bank:Checking"),
        ],
    )
    def test_platform_accounts(self, classifier, platform, expected):
        assert classifier.determine_asset_account(platform) == expected

    def test_bank_with_known_bank_name(self, classifier):
        result = classifier.determine_asset_account("bank", "招商银行尾号8888")
        assert result == "Assets:Current:Bank:CMB"

    def test_unknown_platform_returns_cash(self, classifier):
        assert classifier.determine_asset_account("cashapp") == "Assets:Current:Cash"

    @pytest.mark.parametrize(
        "counterparty, code",
        [
            ("招商银行", "CMB"),
            ("工行", "ICBC"),
            ("建行", "CCB"),
            ("农行", "ABC"),
            ("中行", "BOC"),
        ],
    )
    def test_bank_code_mapping(self, classifier, counterparty, code):
        result = classifier.determine_asset_account("bank", counterparty)
        assert result == f"Assets:Current:Bank:{code}"


# ── _classify_expense ─────────────────────────────────────────────────────


class TestClassifyExpense:
    def test_merchant_override_starbucks(self, classifier):
        result = classifier.classify_transaction(
            "星巴克", "咖啡", tx_type="expense", platform="alipay"
        )
        assert result["opposing_account"] == "Expenses:Food:Restaurant"

    def test_merchant_override_mcdonalds(self, classifier):
        result = classifier.classify_transaction(
            "麦当劳", "套餐", tx_type="expense", platform="alipay"
        )
        assert result["opposing_account"] == "Expenses:Food:Restaurant"

    def test_merchant_override_jd(self, classifier):
        result = classifier.classify_transaction(
            "京东", "电子产品", tx_type="expense", platform="alipay"
        )
        assert result["opposing_account"] == "Expenses:Shopping:Online"

    def test_unknown_expense(self, classifier):
        result = classifier.classify_transaction(
            "未知名商户", "未知商品", tx_type="expense", platform="alipay"
        )
        assert result["opposing_account"] == "Expenses:Unknown"


# ── _generate_tags ────────────────────────────────────────────────────────


class TestGenerateTags:
    def test_alipay_tag(self, classifier):
        result = classifier.classify_transaction(
            "支付宝商户", "消费", tx_type="expense", platform="alipay"
        )
        assert "alipay" in result["tags"]

    def test_delivery_tag(self, classifier):
        result = classifier.classify_transaction(
            "美团", "外卖", tx_type="expense", platform="wechat"
        )
        assert "delivery" in result["tags"]

    def test_red_packet_tag(self, classifier):
        result = classifier.classify_transaction(
            "某人", "红包", tx_type="income", platform="wechat"
        )
        assert "red_packet" in result["tags"]

    def test_topup_tag(self, classifier):
        result = classifier.classify_transaction(
            "充值中心", "充值", tx_type="expense", platform="alipay"
        )
        assert "topup" in result["tags"]
