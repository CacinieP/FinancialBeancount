"""Configurable account trees and validation before export/model prompting."""

import pytest
from beancount_dedup.account_classifier import BeancountAccountClassifier, is_valid_account
from beancount_dedup.config import AccountConfig


def classify(classifier, merchant, description="", direction="expense"):
    return classifier.classify_transaction(
        merchant, description, tx_type=direction, platform="alipay"
    )["opposing_account"]


def test_append_keeps_defaults_and_prioritizes_user_rules():
    classifier = BeancountAccountClassifier(
        AccountConfig(
            expense_rules={"星巴克": "Expenses:Custom:Coffee"},
            income_rules={"工资": "Income:Custom:Employment"},
        )
    )
    assert classify(classifier, "星巴克") == "Expenses:Custom:Coffee"
    assert classify(classifier, "麦当劳") == "Expenses:Food:Restaurant"
    assert classify(classifier, "某公司", "工资", "income") == "Income:Custom:Employment"
    assert classify(classifier, "某公司", "基金", "income") == "Income:Investment:Fund"


def test_merchant_overrides_take_priority_over_direction_rules():
    classifier = BeancountAccountClassifier(
        AccountConfig(
            merchant_overrides={"商户": "Expenses:Custom:Override"},
            expense_rules={"商户": "Expenses:Custom:Keyword"},
        )
    )
    assert classify(classifier, "商户") == "Expenses:Custom:Override"


def test_replace_uses_only_supplied_rules_in_both_directions():
    classifier = BeancountAccountClassifier(
        AccountConfig(
            rules_mode="replace",
            expense_rules={"饮品": "Expenses:个人:饮食"},
            income_rules={"雇主": "Income:Custom:Employer"},
            merchant_overrides={"专属": "Expenses:Custom:Shop"},
        )
    )
    assert classify(classifier, "星巴克", "饮品") == "Expenses:个人:饮食"
    assert classify(classifier, "专属商店") == "Expenses:Custom:Shop"
    assert classify(classifier, "星巴克") == "Expenses:Unknown"
    for description in ["退款", "工资", "分红", "基金"]:
        assert classify(classifier, "未知商家", description, "income") == "Income:Other"
    assert classify(classifier, "雇主", "工资", "income") == "Income:Custom:Employer"


def test_replacement_candidates_cannot_leak_default_account_tree():
    classifier = BeancountAccountClassifier(
        AccountConfig(
            rules_mode="replace",
            expense_rules={"coffee": "Expenses:Mine:Coffee"},
            income_rules={"salary": "Income:Mine:Salary"},
        )
    )
    assert classifier.candidate_accounts("expense") == ["Expenses:Mine:Coffee", "Expenses:Unknown"]
    assert classifier.candidate_accounts("income") == ["Income:Mine:Salary", "Income:Other"]
    assert classifier.is_fallback("Expenses:Unknown")
    assert classifier.is_fallback("Income:Other")
    assert not classifier.is_fallback("Expenses:Mine:Coffee")
    with pytest.raises(ValueError, match="tx_type"):
        classifier.candidate_accounts("transfer")


def test_active_rules_and_candidates_do_not_share_instance_state():
    classifier = BeancountAccountClassifier(AccountConfig(expense_rules={"acme": "Expenses:Mine"}))
    classifier.merchant_rules.clear()
    classifier.expense_categories.clear()
    classifier.expense_rules["new"] = "Expenses:New"
    fresh = BeancountAccountClassifier()
    assert classify(fresh, "星巴克") == "Expenses:Food:Restaurant"
    assert "Expenses:Mine" not in fresh.candidate_accounts("expense")
    assert "Expenses:New" not in fresh.candidate_accounts("expense")


@pytest.mark.parametrize(
    "settings",
    [
        {"rules_mode": "merge"},
        {"expense_rules": {"shop": "Income:Salary"}},
        {"income_rules": {"shop": "Expenses:Food"}},
        {"merchant_overrides": {"shop": "Assets:Bank"}},
        {"expense_rules": {"": "Expenses:Food"}},
        {"expense_rules": {"shop": "Expenses:Food\n2024-01-01 open Assets:Injected"}},
        {"expense_rules": {"shop": 'Expenses:Food"'}},
        {"expense_rules": {"shop": "Expenses:lowercase"}},
        {"expense_rules": {"shop": "Expenses:Food Stuff"}},
        {"platform_wallets": {"alipay": "Assets:Cash\n"}},
        {"bank_codes": {"bank": "CMB:Injected"}},
    ],
)
def test_invalid_mode_and_account_injection_are_rejected(settings):
    with pytest.raises(ValueError):
        BeancountAccountClassifier(AccountConfig(**settings))


@pytest.mark.parametrize("account", ["Expenses:Food", "Income:工资", "Assets:Bank:CMB-1234"])
def test_account_validator_accepts_exportable_names(account):
    assert is_valid_account(account)


def test_refund_precedence_is_preserved_after_moving_defaults():
    classifier = BeancountAccountClassifier()
    assert classify(classifier, "商家", "工资退款", "income") == "Income:Refund:Shopping"


def test_explicit_fallback_account_rules_remain_distinguishable_from_no_match():
    classifier = BeancountAccountClassifier(
        AccountConfig(
            rules_mode="replace",
            expense_rules={"private": "Expenses:Unknown"},
            merchant_overrides={"other": "Income:Other"},
        )
    )
    assert classifier.has_rule_match("private", "", "expense")
    assert classifier.has_rule_match("other", "", "income")
    assert not classifier.has_rule_match("unmatched", "", "expense")
    assert not classifier.has_rule_match("unmatched", "", "income")
