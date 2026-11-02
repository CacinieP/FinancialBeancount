"""End-to-end CLI checks for local suggestions and ledger interoperability."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

import pytest
from beancount_dedup.ai import ClassificationSuggestion
from beancount_dedup.cli import main
from beancount_dedup.config import AccountConfig
from beancount_dedup.exporters.beancount import BeancountExporter
from beancount_dedup.models import Platform, Transaction

pytest.importorskip("beancount")
from beancount import loader

CSV = (
    "交易号,商家订单号,交易创建时间,付款时间,最近修改时间,交易来源地,类型,交易对方,商品名称,"
    "金额（元）,收/支,交易状态,服务费（元）,成功退款（元）,备注,资金状态,交易方式\n"
    "123,,2024-01-15 14:00:00,2024-01-15 14:00:00,2024-01-15 14:00:00,APP,其他,"
    "晨光书店,Python教材,68.00,支出,支付成功,0.00,,,余额,余额支付\n"
)


def setup_statement(tmp_path):
    path = tmp_path / "statement.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


def test_ai_cli_generates_valid_review_draft_and_report(tmp_path, monkeypatch):
    statement = setup_statement(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "beancount_dedup.ai.LocalModelClient.suggest",
        lambda *a: {
            "account": "Expenses:Education:Books",
            "confidence": 0.95,
            "reason": "教材",
        },
    )
    main(["--alipay", str(statement), "--ai-classify", "-o", "out/draft.beancount"])
    text = (tmp_path / "out/draft.beancount").read_text()
    assert '2024-01-15 ! "晨光书店"' in text
    assert "Expenses:Education:Books" in text
    assert "local_model_review_required" in text
    _, errors, _ = loader.load_file(str(tmp_path / "out/draft.beancount"))
    assert not errors
    report = (tmp_path / "out/classification_report.json").read_text()
    assert '"status": "suggested"' in report
    assert '"rule_account": "Expenses:Unknown"' in report


def test_explicit_files_honor_config_and_fragment(tmp_path, monkeypatch):
    statement = setup_statement(tmp_path)
    config = tmp_path / "custom.yaml"
    config.write_text("accounts:\n  merchant_overrides:\n    书店: Expenses:Books\n")
    monkeypatch.chdir(tmp_path)
    main(["--alipay", str(statement), "--config", str(config), "--fragment"])
    text = (tmp_path / "output/output.beancount").read_text()
    assert "Expenses:Books" in text
    assert " open " not in text


def test_model_file_auto_starts_service_with_effective_config(tmp_path, monkeypatch):
    statement = setup_statement(tmp_path)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    calls = []

    @contextmanager
    def managed(path, config):
        calls.append("start")
        try:
            yield replace(config, base_url="http://127.0.0.1:18747/v1")
        finally:
            calls.append("stop")

    def suggest(txs, classifier, config):
        assert config.base_url == "http://127.0.0.1:18747/v1"
        return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("beancount_dedup.model_runtime.managed_local_model", managed)
    monkeypatch.setattr("beancount_dedup.ai.suggest_classifications", suggest)
    main(["--alipay", str(statement), "--model-file", str(model)])
    assert calls == ["start", "stop"]


def test_cli_reconciles_without_writing_original_ledger(tmp_path, monkeypatch):
    statement = setup_statement(tmp_path)
    ledger = tmp_path / "main.beancount"
    original = (
        "2024-01-01 open Assets:Current:Digital:Alipay CNY\n"
        "2024-01-01 open Expenses:Books CNY\n"
        '2024-01-15 * "书店" "教材"\n'
        "  Assets:Current:Digital:Alipay -68 CNY\n  Expenses:Books 68 CNY\n"
    )
    ledger.write_text(original)
    monkeypatch.chdir(tmp_path)
    main(["--alipay", str(statement), "--ledger", str(ledger)])
    assert ledger.read_text() == original
    report = (tmp_path / "output/reconciliation_report.json").read_text()
    assert '"status": "matched"' in report


def test_cli_refuses_to_overwrite_ledger(tmp_path, monkeypatch):
    statement = setup_statement(tmp_path)
    ledger = tmp_path / "main.beancount"
    original = "2024-01-01 open Assets:Current:Digital:Alipay CNY\n"
    ledger.write_text(original)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--alipay", str(statement), "--ledger", str(ledger), "-o", str(ledger)])
    assert exc.value.code == 2
    assert ledger.read_text() == original


def test_exporter_never_applies_low_confidence_suggestion():
    tx = Transaction(id="tx", platform=Platform.ALIPAY, amount="-68", counterparty="书店")
    suggestion = ClassificationSuggestion(
        "tx", "Expenses:Unknown", "needs_review", "Expenses:Books", 0.2
    )
    text = BeancountExporter(suggestions=[suggestion]).export_transaction(tx)
    assert "Expenses:Unknown" in text
    assert "Expenses:Books" not in text
    assert " ! " in text


def test_legacy_exporter_uses_same_wallet_defaults_and_overrides():
    tx = Transaction(platform=Platform.ALIPAY, amount="-1")
    default = BeancountExporter(use_classifier=False).export_transaction(tx)
    custom = BeancountExporter(
        use_classifier=False, accounts=AccountConfig(platform_wallets={"alipay": "Assets:Alipay"})
    ).export_transaction(tx)
    assert "Assets:Current:Digital:Alipay" in default
    assert "Assets:Alipay" in custom


def test_historical_export_has_accounts_open_before_transactions():
    tx = Transaction(platform=Platform.ALIPAY, amount="-1", datetime=datetime(2020, 1, 1))
    text = BeancountExporter().export([tx])
    _, errors, _ = loader.load_string(text)
    assert not errors


def test_fragment_can_be_included_in_existing_ledger(tmp_path):
    tx = Transaction(platform=Platform.ALIPAY, amount="-1", datetime=datetime(2024, 1, 15))
    BeancountExporter().export([tx], str(tmp_path / "import.beancount"), include_open_entries=False)
    main_ledger = tmp_path / "main.beancount"
    main_ledger.write_text(
        'include "import.beancount"\n2024-01-01 open Assets:Current:Digital:Alipay CNY\n'
        "2024-01-01 open Expenses:Unknown CNY\n"
    )
    _, errors, _ = loader.load_file(str(main_ledger))
    assert not errors


def test_cli_refuses_output_colliding_with_its_report(tmp_path, monkeypatch):
    statement = setup_statement(tmp_path)
    target = tmp_path / "duplicate_report.beancount"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--alipay", str(statement), "-o", str(target)])
    assert exc.value.code == 2
    assert not target.exists()


def test_native_cli_roundtrip(tmp_path, monkeypatch):
    source = tmp_path / "main.beancount"
    source.write_text('option "operating_currency" "CNY"\n2024-01-01 open Assets:Cash CNY\n')
    target = tmp_path / "export.beancount"
    monkeypatch.chdir(tmp_path)
    main(["--beancount", str(source), "-o", str(target)])
    assert 'option "operating_currency" "CNY"' in target.read_text()
    assert "open Assets:Cash" in target.read_text()
    assert source.exists()


def test_cli_rejects_bad_successful_row_without_partial_export(tmp_path, monkeypatch):
    statement = setup_statement(tmp_path)
    header, row = CSV.splitlines()
    bad = row.replace("2024-01-15 14:00:00", "bad-date")
    statement.write_text(header + "\n" + row + "\n" + bad + "\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--alipay", str(statement)])
    assert exc.value.code == 2
    assert not (tmp_path / "output/output.beancount").exists()


def test_cli_does_not_book_cancelled_order(tmp_path, monkeypatch):
    statement = setup_statement(tmp_path)
    header, row = CSV.splitlines()
    cancelled = row.replace("支付成功", "交易关闭").replace("晨光书店", "取消商户")
    statement.write_text(header + "\n" + row + "\n" + cancelled + "\n")
    monkeypatch.chdir(tmp_path)
    main(["--alipay", str(statement)])
    output = (tmp_path / "output/output.beancount").read_text()
    assert "晨光书店" in output
    assert "取消商户" not in output
