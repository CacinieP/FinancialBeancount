"""Conservative matching against real Beancount parsing and validation."""

import builtins
import json
from datetime import datetime
from decimal import Decimal

import pytest
from beancount_dedup.account_classifier import BeancountAccountClassifier
from beancount_dedup.config import AccountConfig
from beancount_dedup.models import Platform, Transaction, TransactionType
from beancount_dedup.reconciliation import ReconciliationError, reconcile_transactions

pytest.importorskip("beancount")

ASSET = "Assets:Current:Digital:Alipay"
WECHAT = "Assets:Current:Digital:WeChat"
HEADER = f"""2024-01-01 open {ASSET} CNY, USD
2024-01-01 open {WECHAT} CNY
2024-01-01 open Expenses:Unknown CNY, USD
2024-01-01 open Income:Other CNY
"""


def _tx(amount="-30.00", day=15, identifier="statement-1", platform=Platform.ALIPAY):
    return Transaction(
        id=identifier,
        platform=platform,
        datetime=datetime(2024, 1, day, 12),
        amount=Decimal(amount),
        counterparty="Store",
        description="Goods",
        source_file="statement.csv",
        tx_type=TransactionType.EXPENSE if Decimal(amount) < 0 else TransactionType.INCOME,
    )


def _entry(amount="-30.00", day=15, account=ASSET, currency="CNY"):
    opposing = "Income:Other" if Decimal(amount) > 0 else "Expenses:Unknown"
    return (
        f'2024-01-{day:02} * "Store" "Goods"\n'
        f"  {account} {amount} {currency}\n"
        f"  {opposing} {-Decimal(amount)} {currency}\n"
    )


def _ledger(tmp_path, text):
    ledger = tmp_path / "main.beancount"
    ledger.write_text(HEADER + text, encoding="utf-8")
    return ledger


def test_exact_signed_match_is_read_only_and_json_ready(tmp_path):
    ledger = _ledger(tmp_path, _entry() + _entry("42.15", day=16))
    original = ledger.read_bytes()
    report = reconcile_transactions([_tx(), _tx("42.15", day=16, identifier="income")], ledger)
    result = json.loads(json.dumps(report.to_dict()))
    assert result["status"] == "matched"
    assert result["counts"]["matched"] == 2
    assert Decimal(result["totals"]["statement_minus_ledger"]) == 0
    assert result["matched"][0]["statement"]["id"] == "statement-1"
    assert result["matched"][0]["ledger_posting"]["filename"] == str(ledger)
    assert result["matched"][0]["ledger_posting"]["line"] > 0
    assert ledger.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["main.beancount"]


def test_includes_are_loaded(tmp_path):
    child = tmp_path / "january.beancount"
    child.write_text(_entry(), encoding="utf-8")
    ledger = _ledger(tmp_path, 'include "january.beancount"\n')
    report = reconcile_transactions([_tx()], ledger)
    assert len(report.matched) == 1
    assert report.matched[0]["ledger_posting"]["filename"] == str(child)


def test_mismatched_sign_and_exact_decimal_difference(tmp_path):
    ledger = _ledger(tmp_path, _entry("30.00"))
    report = reconcile_transactions([_tx("-30.01")], ledger)
    assert not report.matched
    assert len(report.statement_only) == len(report.ledger_only) == 1
    assert Decimal(report.totals["statement_minus_ledger"]) == Decimal("-60.01")


def test_duplicate_ledger_amounts_are_ambiguous(tmp_path):
    ledger = _ledger(tmp_path, _entry() + _entry())
    report = reconcile_transactions([_tx()], ledger)
    assert not report.matched
    assert not report.statement_only
    assert not report.ledger_only
    assert len(report.ambiguous) == 1
    assert len(report.ambiguous[0]["ledger_postings"]) == 2


def test_one_posting_cannot_match_two_statements(tmp_path):
    ledger = _ledger(tmp_path, _entry())
    report = reconcile_transactions([_tx(), _tx(identifier="statement-2")], ledger)
    assert not report.matched
    assert len(report.ambiguous) == 1
    assert len(report.ambiguous[0]["statements"]) == 2
    assert len(report.ambiguous[0]["ledger_postings"]) == 1


def test_date_window_is_explicit(tmp_path):
    ledger = _ledger(tmp_path, _entry(day=16))
    exact = reconcile_transactions([_tx()], ledger)
    assert len(exact.statement_only) == 1
    assert not exact.ledger_only  # outside the exact input date coverage
    widened = reconcile_transactions([_tx()], ledger, date_window_days=1)
    assert len(widened.matched) == 1
    assert widened.scope["date_ranges"][ASSET] == {"start": "2024-01-14", "end": "2024-01-16"}


def test_scope_filters_currency_account_and_dates(tmp_path):
    ledger = _ledger(
        tmp_path,
        _entry()
        + _entry("-20.00", currency="USD")
        + _entry("-15.00", account=WECHAT)
        + _entry("-10.00", day=14)
        + _entry("-50.00", day=16),
    )
    report = reconcile_transactions([_tx()], ledger)
    assert report.to_dict()["status"] == "matched"
    assert len(report.matched) == 1
    assert Decimal(report.totals["ledger"]) == Decimal("-30")
    assert report.scope["accounts"] == [ASSET]


def test_scope_is_per_account(tmp_path):
    ledger = _ledger(
        tmp_path,
        _entry(day=15) + _entry("-20.00", day=20, account=WECHAT) + _entry(day=20),
    )
    report = reconcile_transactions(
        [_tx(), _tx("-20.00", day=20, platform=Platform.WECHAT, identifier="wechat")], ledger
    )
    assert len(report.matched) == 2
    assert not report.ledger_only
    assert report.scope["date_ranges"][ASSET]["end"] == "2024-01-15"


def test_user_account_configuration_reaches_matcher(tmp_path):
    custom = "Assets:Wallet"
    ledger = tmp_path / "main.beancount"
    ledger.write_text(
        f"2024-01-01 open {custom} CNY\n2024-01-01 open Expenses:Unknown CNY\n"
        + _entry(account=custom),
        encoding="utf-8",
    )
    classifier = BeancountAccountClassifier(AccountConfig(platform_wallets={"alipay": custom}))
    report = reconcile_transactions([_tx()], ledger, classifier=classifier)
    assert len(report.matched) == 1
    assert report.scope["accounts"] == [custom]


@pytest.mark.parametrize(
    "content", ["not a valid ledger\n", _entry().replace("30.00 CNY", "29.00 CNY", 1)]
)
def test_parser_and_unbalanced_ledger_fail_loudly(tmp_path, content):
    ledger = _ledger(tmp_path, content)
    with pytest.raises(ReconciliationError, match="Invalid ledger"):
        reconcile_transactions([_tx()], ledger)


def test_missing_include_fails_loudly(tmp_path):
    ledger = _ledger(tmp_path, 'include "missing.beancount"\n')
    with pytest.raises(ReconciliationError, match="does not match any files"):
        reconcile_transactions([_tx()], ledger)


def test_user_plugin_is_rejected_without_import(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, 'plugin "should_never_be_imported"\n' + _entry())
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "should_never_be_imported"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(ReconciliationError, match="User ledger plugins"):
        reconcile_transactions([_tx()], ledger)


def test_missing_beancount_dependency_fails_loudly(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, _entry())
    original_import = builtins.__import__

    def missing_import(name, *args, **kwargs):
        if name == "beancount":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_import)
    with pytest.raises(ReconciliationError, match=r"financial-beancount\[validate\]"):
        reconcile_transactions([_tx()], ledger)


@pytest.mark.parametrize("window", [-1, 0.5, True])
def test_invalid_date_window_is_rejected(tmp_path, window):
    ledger = _ledger(tmp_path, _entry())
    with pytest.raises(ReconciliationError, match="non-negative integer"):
        reconcile_transactions([_tx()], ledger, date_window_days=window)


def test_refund_type_and_original_transaction_are_unchanged(tmp_path):
    ledger = _ledger(tmp_path, _entry("30.00"))
    tx = _tx("30.00")
    tx.tx_type = TransactionType.REFUND
    before = tx.to_dict()
    assert len(reconcile_transactions([tx], ledger).matched) == 1
    assert tx.to_dict() == before


def test_empty_input_does_not_claim_success(tmp_path):
    ledger = _ledger(tmp_path, _entry())
    report = reconcile_transactions([], ledger)
    assert report.to_dict()["status"] == "no_statement_transactions"
    assert not report.ledger_only
    assert report.scope["accounts"] == []


def test_failed_balance_assertion_is_reported(tmp_path):
    ledger = _ledger(tmp_path, _entry() + f"2024-01-16 balance {ASSET} -29 CNY\n")
    with pytest.raises(ReconciliationError, match="Balance failed"):
        reconcile_transactions([_tx()], ledger)
