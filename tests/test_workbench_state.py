"""Persisted workbench review/export operations use real local accounting data."""

import base64
from decimal import Decimal

import pytest
from beancount_dedup.ai import ClassificationSuggestion
from beancount_dedup.beancount_io import import_beancount
from beancount_dedup.config import AppConfig
from beancount_dedup.workbench_state import WorkbenchState

WECHAT = """交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
2024-01-15 12:00:00,商户消费,星巴克,拿铁咖啡,支出,30.00,零钱,支付成功,order-1,,
2024-01-16 13:00:00,商户消费,青禾文化,一本科学读物,支出,68.1230,零钱,支付成功,order-2,,
"""
LEDGER = """option "title" "Existing ledger"
option "operating_currency" "CNY"
2024-01-01 open Assets:Current:Digital:WeChat CNY
2024-01-01 open Expenses:Education:Books CNY
2024-01-01 open Expenses:Food:Restaurant CNY
2024-01-15 * "星巴克" "Existing payment"
  Assets:Current:Digital:WeChat -30.00 CNY
  Expenses:Food:Restaurant 30.00 CNY
"""


def upload(name, content):
    if isinstance(content, str):
        content = content.encode()
    return {"name": name, "content": base64.b64encode(content).decode()}


@pytest.fixture
def state(tmp_path):
    return WorkbenchState(tmp_path / "workspace")


def test_real_statement_import_sets_pending_reviews_and_keeps_original_bytes(state):
    state.import_statements([upload("wechat.csv", WECHAT)])
    snapshot = state.snapshot()
    assert snapshot["counts"] == {
        "transactions": 2,
        "pending": 2,
        "confirmed": 0,
        "suggested": 0,
        "matched": 0,
        "differences": 0,
        "duplicates": 0,
    }
    assert [row["classification_source"] for row in snapshot["transactions"]] == [
        "rule",
        "fallback",
    ]
    assert [row["amount"] for row in snapshot["transactions"]] == ["-30.00", "-68.1230"]
    assert snapshot["files"] == [{"name": "wechat.csv", "count": 2}]
    assert next((state.workspace / "uploads").rglob("wechat.csv")).read_text() == WECHAT


def test_ai_uses_ledger_open_accounts_only_and_never_confirms_or_changes_amounts(
    state, monkeypatch
):
    state.import_statements([upload("wechat.csv", WECHAT)])
    state.import_ledger([upload("main.beancount", LEDGER)])
    before = {row["id"]: row["amount"] for row in state.snapshot()["transactions"]}
    captured = []

    def suggest(transactions, _classifier, config):
        captured.extend(transactions)
        assert config.allowed_accounts == ["Expenses:Education:Books", "Expenses:Food:Restaurant"]
        return [
            ClassificationSuggestion(
                tx.id, "Expenses:Unknown", "suggested", "Expenses:Education:Books", 0.94, "科学读物"
            )
            for tx in transactions
        ]

    monkeypatch.setattr("beancount_dedup.workbench_state.suggest_classifications", suggest)
    state.classify()
    snapshot = state.snapshot()
    assert len(captured) == 1
    assert captured[0].counterparty == "青禾文化"
    model = snapshot["transactions"][1]
    assert model["account"] == "Expenses:Education:Books"
    assert model["classification_source"] == "model"
    assert model["review_status"] == "pending"
    assert snapshot["counts"]["suggested"] == 1
    assert {row["id"]: row["amount"] for row in snapshot["transactions"]} == before
    state.update_transactions([model["id"]])
    state.classify()
    assert len(captured) == 1
    assert state.snapshot()["counts"]["confirmed"] == 1


def test_model_service_failure_is_visible_and_preserves_fallback(state, monkeypatch):
    state.import_statements([upload("wechat.csv", WECHAT)])
    monkeypatch.setattr(
        "beancount_dedup.workbench_state.suggest_classifications",
        lambda transactions, _classifier, _config: [
            ClassificationSuggestion(tx.id, "Expenses:Unknown", "unavailable")
            for tx in transactions
        ],
    )
    state.classify()
    row = state.snapshot()["transactions"][1]
    assert row["classification_source"] == "fallback"
    assert row["account"] == "Expenses:Unknown"
    assert "不可用" in row["reason"]


def test_demo_has_eight_explicitly_synthetic_rows_and_exact_reconciliation_scope(state):
    state.load_demo()
    assert state.snapshot()["is_demo"]
    assert state.snapshot()["workspace"]["is_demo"]
    state.reconcile()
    snapshot = state.snapshot()
    assert snapshot["ledger"]["report"]["counts"] == {
        "matched": 1,
        "statement_only": 7,
        "ledger_only": 1,
        "ambiguous_groups": 0,
    }
    assert snapshot["counts"]["transactions"] == 8
    assert snapshot["counts"]["matched"] == 1
    assert sum(row["reconciliation_status"] == "matched" for row in snapshot["transactions"]) == 1
    assert state.export(exclude_matched=True)["transaction_count"] == 7
    assert state.export(exclude_matched=False)["transaction_count"] == 8
    assert state.export()["warnings"][0].startswith("这是合成示例")


def test_demo_ai_skips_rules_explicit_fallback_transfer_refund(state, monkeypatch):
    state.load_demo()
    requested = []

    def suggest(transactions, _classifier, _config):
        requested.extend(tx.id for tx in transactions)
        return [
            ClassificationSuggestion(
                tx.id,
                "Income:Other" if tx.is_income else "Expenses:Unknown",
                "needs_review",
                confidence=0.1,
            )
            for tx in transactions
        ]

    monkeypatch.setattr("beancount_dedup.workbench_state.suggest_classifications", suggest)
    state.classify()
    assert requested == ["demo-1", "demo-2", "demo-3", "demo-4"]
    explicit = state.snapshot()["transactions"][5]
    assert explicit["classification_source"] == "rule"
    assert explicit["account"] == "Expenses:Unknown"


def test_transfer_confirmation_is_atomic_and_requires_distinct_real_asset_account(state):
    state.load_demo()
    before = state.snapshot()
    with pytest.raises(ValueError, match="转账"):
        state.update_transactions(["demo-1", "demo-7"])
    assert state.snapshot() == before
    state.update_transactions(["demo-7"], account="Assets:Current:Bank:Checking")
    row = state.snapshot()["transactions"][6]
    assert row["review_status"] == "confirmed"
    assert row["classification_source"] == "manual"
    state.update_accounts({"alipay": "Assets:Current:Bank:Checking"})
    with pytest.raises(ValueError, match="不能与"):
        state.update_transactions(["demo-7"])


def test_rule_updates_and_reclassification_do_not_override_manual_or_confirmed_choices(
    state, monkeypatch
):
    state.load_demo()
    state.update_transactions(["demo-1"], account="Expenses:Personal:Books", confirm=False)
    state.update_transactions(["demo-2"])
    state.update_rules(
        mode="replace",
        expense_rules={"午间": "Expenses:New:Food"},
        income_rules={"报酬": "Income:New:Salary"},
    )
    rows = {row["id"]: row for row in state.snapshot()["transactions"]}
    assert rows["demo-1"]["account"] == "Expenses:Personal:Books"
    assert rows["demo-2"]["account"] == "Expenses:Unknown"
    assert rows["demo-2"]["review_status"] == "confirmed"
    assert rows["demo-5"]["classification_source"] == "fallback"
    monkeypatch.setattr(
        "beancount_dedup.workbench_state.suggest_classifications", lambda *_args: []
    )
    state.classify()
    assert state.snapshot()["transactions"][0]["account"] == "Expenses:Personal:Books"


def test_statement_export_is_valid_precise_untruncated_and_uses_review_flags(state, tmp_path):
    data = WECHAT.replace("一本科学读物", '"带有“引号”的非常长的描述' + "完整文字" * 30 + '"')
    state.import_statements([upload("wechat.csv", data)])
    rows = state.snapshot()["transactions"]
    state.update_transactions([rows[0]["id"]])
    exported = state.export(exclude_matched=False)
    path = tmp_path / "export.beancount"
    path.write_text(exported["content"], encoding="utf-8")
    ledger = import_beancount(path)
    txs = [entry for entry in ledger.entries if type(entry).__name__ == "Transaction"]
    assert [tx.flag for tx in txs] == ["*", "!"]
    assert txs[1].postings[0].units.number.as_tuple() == Decimal(rows[1]["amount"]).as_tuple()
    assert txs[1].narration == rows[1]["description"]
    assert len(txs[1].narration) > 100
    assert txs[1].meta["id"] == rows[1]["id"]
    assert txs[1].meta["source_file"] == "wechat.csv"
    assert state.export(only_confirmed=True)["transaction_count"] == 1
    fragment = state.export(kind="fragment")["content"]
    assert " open " not in fragment
    assert txs[1].narration in fragment


def test_reconciliation_and_export_use_explicit_wallet_mapping_then_invalidate_on_change(state):
    state.import_statements([upload("wechat.csv", WECHAT)])
    state.import_ledger([upload("main.beancount", LEDGER)])
    state.reconcile()
    assert state.snapshot()["counts"]["matched"] == 1
    assert state.export(exclude_matched=True)["transaction_count"] == 1
    state.update_accounts({"wechat": "Assets:MyWallet"})
    snapshot = state.snapshot()
    assert snapshot["ledger"]["report"] is None
    assert snapshot["counts"]["matched"] == 0
    assert all(row["asset_account"] == "Assets:MyWallet" for row in snapshot["transactions"])
    assert state.export()["transaction_count"] == 2


def test_native_ledger_import_preserves_includes_and_export_does_not_merge_statements(
    state, tmp_path
):
    main = 'option "title" "Native"\ninclude "parts/transactions.beancount"\n'
    state.import_ledger(
        [upload("main.beancount", main), upload("parts/transactions.beancount", LEDGER)]
    )
    state.import_statements([upload("wechat.csv", WECHAT)])
    snapshot = state.snapshot()
    assert snapshot["ledger"]["source_files"] == ["main.beancount", "parts/transactions.beancount"]
    result = state.export(kind="native")
    assert result["transaction_count"] == 1
    path = tmp_path / "native.beancount"
    path.write_text(result["content"], encoding="utf-8")
    restored = import_beancount(path)
    assert restored.options["title"] == "Native"
    assert "Existing payment" in result["content"]
    assert "青禾文化" not in result["content"]
    assert any("不会合并" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    "name", ["../private.csv", "/tmp/private.csv", "folder\\private.csv", "C:/private.csv"]
)
def test_upload_path_escape_is_rejected_without_state_change(state, name):
    state.import_statements([upload("wechat.csv", WECHAT)])
    before = state.snapshot()
    with pytest.raises(ValueError):
        state.import_statements([upload(name, WECHAT)])
    assert state.snapshot() == before


@pytest.mark.parametrize(
    "main",
    [
        'include "/etc/passwd"\n',
        'include "../../outside.beancount"\n',
        'plugin "dangerous.user.plugin"\n',
        '2024-01-01 open Assets:Cash CNY\n2024-01-02 document Assets:Cash "/etc/passwd"\n',
        'option "documents" "/etc"\n',
    ],
)
def test_ledger_external_access_and_plugins_are_rejected_atomically(state, main):
    state.load_demo()
    before = state.snapshot()
    with pytest.raises(ValueError):
        state.import_ledger([upload("main.beancount", main)])
    assert state.snapshot() == before


def test_failed_batch_keeps_existing_session_exactly_and_does_not_import_first_good_file(state):
    state.import_statements([upload("existing.csv", WECHAT)])
    before = state.snapshot()
    saved = (state.workspace / "session.json").read_bytes()
    bad = WECHAT.replace("68.1230", "NaN")
    with pytest.raises(ValueError):
        state.import_statements([upload("good.csv", WECHAT), upload("bad.csv", bad)])
    assert state.snapshot() == before
    assert (state.workspace / "session.json").read_bytes() == saved


def test_ambiguous_ledger_main_is_rejected_instead_of_guessing(state):
    with pytest.raises(ValueError, match="主文件"):
        state.import_ledger([upload("a.beancount", LEDGER), upload("b.beancount", LEDGER)])
    assert not state.snapshot()["workspace"]["has_ledger"]


def test_reimport_same_file_keeps_manual_confirmation_and_does_not_duplicate_transactions(state):
    files = [upload("wechat.csv", WECHAT)]
    state.import_statements(files)
    tx_id = state.snapshot()["transactions"][1]["id"]
    state.update_transactions([tx_id], "Expenses:Education:Books")
    state.import_statements(files)
    snapshot = state.snapshot()
    assert snapshot["counts"]["transactions"] == 2
    assert snapshot["transactions"][1]["id"] == tx_id
    assert snapshot["transactions"][1]["review_status"] == "confirmed"


def test_session_restores_immutable_amounts_review_config_ledger_and_recomputed_match(state):
    state.import_statements([upload("wechat.csv", WECHAT)])
    state.import_ledger([upload("main.beancount", LEDGER)])
    tx_id = state.snapshot()["transactions"][1]["id"]
    state.update_transactions([tx_id], "Expenses:Education:Books")
    state.update_rules(merchant_overrides={"青禾": "Expenses:Education:Books"})
    state.reconcile()
    before = state.snapshot()
    restored = WorkbenchState(state.workspace, AppConfig())
    assert restored.snapshot() == before
    assert restored.export(exclude_matched=True)["transaction_count"] == 1


def test_reset_clears_local_data_but_demo_only_loads_on_explicit_call(state):
    state.load_demo()
    state.reset()
    snapshot = state.snapshot()
    assert not snapshot["is_demo"]
    assert not snapshot["workspace"]["has_statements"]
    assert not snapshot["workspace"]["has_ledger"]
    assert snapshot["transactions"] == []
    assert WorkbenchState(state.workspace).snapshot() == snapshot
    state.import_statements([upload("wechat.csv", WECHAT)])
    state.load_demo()  # The UI explicitly confirms this user-triggered replacement.
    assert state.snapshot()["counts"]["transactions"] == 8
    assert state.snapshot()["workspace"]["is_demo"]


def test_same_date_amount_and_wallet_but_different_payee_is_ambiguous_and_never_excluded(state):
    state.import_statements([upload("wechat.csv", WECHAT)])
    state.import_ledger([upload("main.beancount", LEDGER.replace('"星巴克"', '"完全不同的商家"'))])
    state.reconcile()
    snapshot = state.snapshot()
    assert snapshot["counts"]["matched"] == 0
    assert snapshot["ledger"]["report"]["counts"]["ambiguous_groups"] == 1
    assert snapshot["transactions"][0]["reconciliation_status"] == "ambiguous"
    assert "交易对方不同" in snapshot["ledger"]["report"]["ambiguous"][0]["reason"]
    assert state.export(exclude_matched=True)["transaction_count"] == 2
    restored = WorkbenchState(state.workspace)
    assert restored.snapshot()["counts"]["matched"] == 0
    assert restored.export(exclude_matched=True)["transaction_count"] == 2


def test_matched_payees_require_exact_normalized_equality_and_export_keeps_matches_by_default(
    state,
):
    state.import_statements([upload("wechat.csv", WECHAT)])
    state.import_ledger([upload("main.beancount", LEDGER.replace('"星巴克"', '"星 巴克"'))])
    state.reconcile()
    assert state.snapshot()["counts"]["matched"] == 1
    assert state.export()["transaction_count"] == 2
    assert state.export(exclude_matched=True)["transaction_count"] == 1


def test_reset_never_deletes_preexisting_user_files_or_upload_copies(tmp_path):
    workspace = tmp_path / "custom-workspace"
    original = workspace / "uploads" / "original-user-ledger.beancount"
    original.parent.mkdir(parents=True)
    original.write_text("preexisting user file", encoding="utf-8")
    state = WorkbenchState(workspace)
    state.import_statements([upload("wechat.csv", WECHAT)])
    copied = next((workspace / "uploads").rglob("wechat.csv"))
    before = copied.read_bytes()
    state.reset()
    assert original.read_text(encoding="utf-8") == "preexisting user file"
    assert copied.read_bytes() == before
    assert not state.snapshot()["workspace"]["has_statements"]
    assert WorkbenchState(workspace).snapshot()["transactions"] == []


def test_reset_persistence_failure_keeps_active_session_and_all_files(state, monkeypatch):
    state.import_statements([upload("wechat.csv", WECHAT)])
    before = state.snapshot()
    stored = (state.workspace / "session.json").read_bytes()
    copied = next((state.workspace / "uploads").rglob("wechat.csv"))

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(state, "_save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        state.reset()
    assert state.snapshot() == before
    assert (state.workspace / "session.json").read_bytes() == stored
    assert copied.exists()


def test_import_real_statements_replaces_all_demo_data_atomically(state):
    state.load_demo()
    state.reconcile()
    demo_source = next((state.workspace / "uploads").rglob("main.beancount"))
    state.import_statements([upload("own-wechat.csv", WECHAT)])
    snapshot = state.snapshot()
    assert not snapshot["workspace"]["is_demo"]
    assert snapshot["counts"]["transactions"] == 2
    assert not snapshot["ledger"]["loaded"]
    assert snapshot["ledger"]["report"] is None
    assert snapshot["files"] == [{"name": "own-wechat.csv", "count": 2}]
    assert all(not row["id"].startswith("demo-") for row in snapshot["transactions"])
    assert "示例·需自选" not in snapshot["accounts"]["rules"]["merchant_overrides"]
    exported = state.export()
    assert exported["transaction_count"] == 2
    assert "合成示例" not in exported["content"]
    assert demo_source.exists()  # Only state changes; physical source copies survive.
    assert WorkbenchState(state.workspace).snapshot() == snapshot


def test_import_real_ledger_clears_demo_statements_and_keeps_only_real_ledger(state):
    state.load_demo()
    state.reconcile()
    state.import_ledger([upload("real-main.beancount", LEDGER)])
    snapshot = state.snapshot()
    assert not snapshot["workspace"]["is_demo"]
    assert not snapshot["workspace"]["has_statements"]
    assert snapshot["counts"]["transactions"] == 0
    assert snapshot["counts"]["duplicates"] == 0
    assert snapshot["transactions"] == []
    assert snapshot["files"] == []
    assert snapshot["ledger"]["filename"] == "real-main.beancount"
    assert snapshot["ledger"]["transaction_count"] == 1
    assert snapshot["ledger"]["report"] is None
    assert state.export()["transaction_count"] == 0
    native = state.export(kind="native")
    assert native["transaction_count"] == 1
    assert "合成示例" not in native["content"]
    assert WorkbenchState(state.workspace).snapshot() == snapshot


@pytest.mark.parametrize("kind", ["statements", "ledger"])
def test_failed_real_import_keeps_demo_and_saved_session_unchanged(state, kind):
    state.load_demo()
    state.reconcile()
    before = state.snapshot()
    stored = (state.workspace / "session.json").read_bytes()
    with pytest.raises(ValueError):
        if kind == "statements":
            state.import_statements([upload("bad.csv", WECHAT.replace("68.1230", "NaN"))])
        else:
            state.import_ledger([upload("bad.beancount", 'include "missing.beancount"\n')])
    assert state.snapshot() == before
    assert (state.workspace / "session.json").read_bytes() == stored
    assert state.snapshot()["workspace"]["is_demo"]


def test_non_demo_statement_import_still_appends_other_real_source_files(state):
    state.import_statements([upload("first.csv", WECHAT)])
    second = WECHAT.replace("2024-01-15", "2024-02-15").replace("2024-01-16", "2024-02-16")
    state.import_statements([upload("second.csv", second)])
    assert state.snapshot()["counts"]["transactions"] == 4
    assert [item["name"] for item in state.snapshot()["files"]] == ["first.csv", "second.csv"]
