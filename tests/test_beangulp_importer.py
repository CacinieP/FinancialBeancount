"""Exercise the native optional beangulp protocol and Beancount serialization."""

# Check the optional extra before importing modules that require it.
# ruff: noqa: E402

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

beangulp = pytest.importorskip("beangulp")

from beancount import loader
from beancount.core import data
from beancount.parser import parser, printer
from beancount_dedup.beangulp_importer import FinancialBeancountImporter
from beancount_dedup.config import AccountConfig, AppConfig
from click.testing import CliRunner

from tests.conftest import ALIPAY_CSV_DATA, BANK_CSV_DATA, WECHAT_CSV_DATA

CLEAN_ALIPAY_CSV_DATA = "\n".join(ALIPAY_CSV_DATA.splitlines()[:3]) + "\n"


@pytest.fixture
def statement(tmp_path):
    path = tmp_path / "alipay.csv"
    path.write_text(CLEAN_ALIPAY_CSV_DATA, encoding="utf-8")
    return path


def _serialize(entries):
    output = io.StringIO()
    printer.print_entries(entries, file=output)
    return output.getvalue()


def test_native_protocol_and_archive_information(statement):
    importer = FinancialBeancountImporter()
    assert isinstance(importer, beangulp.Importer)
    assert importer.name == "beancount_dedup.beangulp_importer.FinancialBeancountImporter"
    assert importer.identify(str(statement)) is True
    assert importer.account(str(statement)) == "Assets:Current:Digital:Alipay"
    assert importer.date(str(statement)) == date(2024, 1, 15)
    assert importer.filename(str(statement)) is None


@pytest.mark.parametrize("content", ["name,amount\nbook,12.00\n", "", "x,y\n1,2\n"])
def test_identify_does_not_claim_unrelated_or_empty_csvs(tmp_path, content):
    path = tmp_path / "other.csv"
    path.write_text(content, encoding="utf-8")
    importer = FinancialBeancountImporter()
    assert importer.identify(str(path)) is False
    with pytest.raises(ValueError):
        importer.extract(str(path), [])


def test_identify_rejects_non_csv_and_missing_files(tmp_path):
    importer = FinancialBeancountImporter()
    path = tmp_path / "statement.xlsx"
    path.write_text(ALIPAY_CSV_DATA, encoding="utf-8")
    assert importer.identify(str(path)) is False
    assert importer.identify(str(tmp_path / "missing.csv")) is False


@pytest.mark.parametrize(
    "content,wallet",
    [
        (WECHAT_CSV_DATA, "Assets:Current:Digital:WeChat"),
        (BANK_CSV_DATA, "Assets:Current:Bank:Checking"),
    ],
)
def test_other_recognized_platforms(tmp_path, content, wallet):
    path = tmp_path / "statement.csv"
    path.write_text(content, encoding="utf-8")
    importer = FinancialBeancountImporter()
    assert importer.identify(str(path)) is True
    assert importer.account(str(path)) == wallet
    assert importer.extract(str(path), [])


def test_transactions_are_balanced_standard_entries_without_open(statement):
    importer = FinancialBeancountImporter()
    entries = importer.extract(str(statement), [])
    assert len(entries) == 2
    assert all(isinstance(entry, data.Transaction) for entry in entries)
    assert all(sum(p.units.number for p in e.postings) == 0 for e in entries)
    assert entries[0].postings[0].units.number == Decimal("-100.00")
    assert entries[0].meta["platform"] == "alipay"
    assert entries[0].meta["source-file"] == "alipay.csv"
    assert entries[0].meta["transaction-time"] == "14:00:00"
    assert "星巴克" in entries[0].meta["__source__"]
    parsed, errors, _ = parser.parse_string(_serialize(entries))
    assert not errors
    assert len(parsed) == len(entries)
    assert all(isinstance(entry, data.Transaction) for entry in parsed)


def test_custom_accounts_currency_and_metadata_settings(statement):
    accounts = AccountConfig(
        platform_wallets={"alipay": "Assets:Wallet:Ali"},
        merchant_overrides={"星巴克": "Expenses:Coffee"},
    )
    importer = FinancialBeancountImporter(AppConfig(accounts=accounts, operating_currency="USD"))
    assert importer.account(str(statement)) == "Assets:Wallet:Ali"
    first = importer.extract(str(statement), [])[0]
    assert [p.account for p in first.postings] == ["Assets:Wallet:Ali", "Expenses:Coffee"]
    assert {p.units.currency for p in first.postings} == {"USD"}
    assert FinancialBeancountImporter(accounts).account(str(statement)) == "Assets:Wallet:Ali"
    minimal = FinancialBeancountImporter(AppConfig(include_metadata=False))
    assert set(minimal.extract(str(statement), [])[0].meta) == {"filename", "lineno"}


def test_serialization_preserves_literal_payee_and_metadata_without_directive_injection(tmp_path):
    rows = list(csv.reader(io.StringIO(ALIPAY_CSV_DATA)))
    malicious = 'Store"\\\n2020-01-01 open Assets:Injected CNY\n'
    rows[1][7] = malicious
    rows[1][16] = malicious
    output = io.StringIO()
    csv.writer(output).writerows(rows[:2])
    path = tmp_path / "quote.csv"
    path.write_text(output.getvalue(), encoding="utf-8")
    original = FinancialBeancountImporter().extract(str(path), [])
    entries, errors, _ = parser.parse_string(_serialize(original))
    assert not errors
    assert len(entries) == 1
    assert isinstance(entries[0], data.Transaction)
    assert entries[0].payee == original[0].payee
    assert entries[0].meta["method"] == original[0].meta["method"]
    assert not any(isinstance(entry, data.Open) for entry in entries)


def test_stable_metadata_and_existing_entries_are_not_mutated(statement):
    importer = FinancialBeancountImporter()
    existing = importer.extract(str(statement), [])
    before = _serialize(existing)
    again = importer.extract(str(statement), existing)
    assert _serialize(again) == before
    importer.deduplicate(again, existing)
    assert all("__duplicate__" in entry.meta for entry in again)
    assert _serialize(existing) == before


def test_transfer_with_known_direction_uses_review_equity_without_income_or_expense(tmp_path):
    rows = list(csv.reader(io.StringIO(ALIPAY_CSV_DATA)))
    output = io.StringIO()
    csv.writer(output).writerows([rows[0], rows[3]])
    path = tmp_path / "transfer.csv"
    path.write_text(output.getvalue(), encoding="utf-8")
    entries = FinancialBeancountImporter().extract(str(path), [])
    assert len(entries) == 1
    assert entries[0].flag == "!"
    assert [p.account for p in entries[0].postings] == [
        "Assets:Current:Digital:Alipay",
        "Equity:Transfers:Unassigned",
    ]
    assert entries[0].postings[0].units.number == Decimal("1000.00")
    assert entries[0].postings[1].units.number == Decimal("-1000.00")


@pytest.mark.parametrize(
    "content,direction_index,direction",
    [(ALIPAY_CSV_DATA, 10, "不计收支"), (WECHAT_CSV_DATA, 4, "/")],
)
def test_ambiguous_transfer_direction_rejects_whole_file(
    tmp_path, content, direction_index, direction
):
    rows = list(csv.reader(io.StringIO(content)))
    rows[1][direction_index] = direction
    output = io.StringIO()
    csv.writer(output).writerows(rows[:3])
    path = tmp_path / "ambiguous.csv"
    path.write_text(output.getvalue(), encoding="utf-8")
    importer = FinancialBeancountImporter()
    assert importer.identify(str(path)) is False
    with pytest.raises(ValueError, match="could not be parsed completely"):
        importer.extract(str(path), [])


@pytest.mark.parametrize("column,bad_value", [(2, "bad-date"), (9, "bad-amount"), (10, "?")])
def test_malformed_successful_row_cannot_be_silently_omitted(tmp_path, column, bad_value):
    rows = list(csv.reader(io.StringIO(CLEAN_ALIPAY_CSV_DATA)))
    rows[1][column] = bad_value
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    path = tmp_path / "incomplete.csv"
    path.write_text(output.getvalue(), encoding="utf-8")
    importer = FinancialBeancountImporter()
    assert importer.identify(str(path)) is False
    with pytest.raises(ValueError, match="could not be parsed completely"):
        importer.extract(str(path), [])


def test_cancelled_alipay_order_is_not_recorded_as_consumption(tmp_path):
    rows = list(csv.reader(io.StringIO(CLEAN_ALIPAY_CSV_DATA)))
    rows[1][11] = "交易关闭"
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    path = tmp_path / "closed.csv"
    path.write_text(output.getvalue(), encoding="utf-8")
    entries = FinancialBeancountImporter().extract(str(path), [])
    assert len(entries) == 1
    assert entries[0].payee == "麦当劳"


def test_near_date_and_amount_different_consumption_is_not_suppressed(statement):
    importer = FinancialBeancountImporter()
    yesterday = importer.extract(str(statement), [])[0]
    # Reproduce the default beangulp false positive: the same accounts, date
    # within two days, amount differing by less than 5%, and a different payee.
    today = yesterday._replace(
        meta={"id": "financialbeancount-" + "a" * 24},
        date=date(2024, 1, 16),
        payee="麦当劳",
        narration="午餐套餐",
        postings=[
            p._replace(units=p.units._replace(number=p.units.number * Decimal("1.02")))
            for p in yesterday.postings
        ],
    )
    importer.deduplicate([today], [yesterday])
    assert "__duplicate__" not in today.meta
    assert importer.cmp(today, yesterday) is False


@pytest.mark.parametrize("metadata", [{}, {"id": "old-importer-id"}])
def test_existing_without_our_content_id_is_never_automatically_suppressed(statement, metadata):
    importer = FinancialBeancountImporter()
    extracted = importer.extract(str(statement), [])
    existing = [entry._replace(meta=dict(metadata)) for entry in extracted]
    importer.deduplicate(extracted, existing)
    assert all("__duplicate__" not in entry.meta for entry in extracted)
    assert all(importer.cmp(a, b) is False for a, b in zip(extracted, existing))


@pytest.mark.parametrize("field", ["date", "payee", "narration", "amount", "account", "currency"])
def test_copied_id_with_changed_transaction_stays_for_review(statement, field):
    importer = FinancialBeancountImporter()
    original = importer.extract(str(statement), [])[0]
    changed = original._replace(meta=dict(original.meta))
    if field == "date":
        changed = changed._replace(date=date(2024, 1, 16))
    elif field in {"payee", "narration"}:
        changed = changed._replace(**{field: "Different value"})
    elif field == "account":
        changed = changed._replace(
            postings=[p._replace(account="Assets:Different") for p in changed.postings]
        )
    else:
        units = {"number": Decimal("-101")} if field == "amount" else {"currency": "USD"}
        changed = changed._replace(
            postings=[
                changed.postings[0]._replace(units=changed.postings[0].units._replace(**units)),
                changed.postings[1],
            ]
        )
    importer.deduplicate([changed], [original])
    assert "__duplicate__" not in changed.meta


def test_exact_repeat_survives_parser_metadata_and_preserves_existing_order(statement):
    importer = FinancialBeancountImporter()
    extracted = importer.extract(str(statement), [])
    existing, errors, _ = parser.parse_string(_serialize(extracted))
    assert not errors
    existing.reverse()
    before = _serialize(existing)
    importer.deduplicate(extracted, existing)
    assert all("__duplicate__" in entry.meta for entry in extracted)
    assert _serialize(existing) == before


def test_importer_runs_without_ai_or_ledger_loading(statement, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("An importer must not start AI or load a ledger")

    import socket

    from beancount_dedup import ai

    monkeypatch.setattr(loader, "load_file", forbidden)
    monkeypatch.setattr(loader, "load_string", forbidden)
    monkeypatch.setattr(ai.LocalModelClient, "suggest", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert FinancialBeancountImporter().extract(str(statement), [])


def test_real_beangulp_driver_can_identify_and_extract(statement):
    importer = FinancialBeancountImporter()
    cli = beangulp.Ingest([importer]).cli
    runner = CliRunner()
    identified = runner.invoke(cli, ["identify", str(statement)])
    assert identified.exit_code == 0, identified.output
    assert importer.name in identified.output
    extracted = runner.invoke(cli, ["extract", str(statement)])
    assert extracted.exit_code == 0, extracted.output
    entries, errors, _ = parser.parse_string(extracted.stdout)
    assert not errors
    assert len(entries) == 2
    assert all(isinstance(entry, data.Transaction) for entry in entries)


def test_imported_entries_fit_an_existing_include_ledger(statement, tmp_path):
    entries = FinancialBeancountImporter().extract(str(statement), [])
    imported = tmp_path / "imported.beancount"
    imported.write_text(_serialize(entries), encoding="utf-8")
    accounts = sorted({posting.account for entry in entries for posting in entry.postings})
    main = tmp_path / "main.beancount"
    main.write_text(
        "\n".join(f"2024-01-01 open {account} CNY" for account in accounts)
        + '\ninclude "imported.beancount"\n',
        encoding="utf-8",
    )
    loaded, errors, _ = loader.load_file(str(main))
    assert not errors
    assert sum(isinstance(entry, data.Transaction) for entry in loaded) == 2
    assert sum(isinstance(entry, data.Open) for entry in loaded) == len(accounts)
