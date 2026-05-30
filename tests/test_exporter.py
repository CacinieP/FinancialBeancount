"""
Tests for beancount_dedup.exporters.beancount — BeancountExporter.
"""

import os
import tempfile
from datetime import datetime
from decimal import Decimal

from beancount_dedup.models import DedupStatus, Platform, Transaction

# ── export_transaction format ─────────────────────────────────────────────


class TestExportTransaction:
    def test_contains_date(self, exporter, sample_expense_alipay):
        out = exporter.export_transaction(sample_expense_alipay)
        assert "2024-01-15" in out

    def test_contains_flag(self, exporter, sample_expense_alipay):
        out = exporter.export_transaction(sample_expense_alipay)
        assert " * " in out

    def test_contains_payee(self, exporter, sample_expense_alipay):
        out = exporter.export_transaction(sample_expense_alipay)
        assert "星巴克" in out

    def test_contains_narration(self, exporter, sample_expense_alipay):
        out = exporter.export_transaction(sample_expense_alipay)
        assert "拿铁咖啡" in out

    def test_contains_accounts(self, exporter, sample_expense_alipay):
        out = exporter.export_transaction(sample_expense_alipay)
        assert "Assets:" in out
        assert "Expenses:" in out

    def test_contains_currency(self, exporter, sample_expense_alipay):
        out = exporter.export_transaction(sample_expense_alipay)
        assert "CNY" in out

    def test_income_format(self, exporter, sample_income_alipay):
        out = exporter.export_transaction(sample_income_alipay)
        assert "Assets:" in out
        assert "Income:" in out or "Expenses:" in out

    def test_review_flag_is_exclamation(self, exporter):
        tx = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-50"),
            counterparty="测试",
            description="测试",
            status=DedupStatus.REVIEW,
        )
        out = exporter.export_transaction(tx)
        assert " ! " in out

    def test_without_meta(self, exporter, sample_expense_alipay):
        out = exporter.export_transaction(sample_expense_alipay, include_meta=False)
        assert 'id: "' not in out


# ── Valid beancount syntax ────────────────────────────────────────────────


class TestBeancountSyntax:
    def test_first_line_starts_with_date(self, exporter, sample_expense_alipay):
        out = exporter.export_transaction(sample_expense_alipay)
        first_line = out.split("\n")[0]
        assert first_line.startswith("2024-")

    def test_postings_indented(self, exporter, sample_expense_alipay):
        out = exporter.export_transaction(sample_expense_alipay)
        for line in out.split("\n"):
            if "Assets:" in line or "Expenses:" in line or "Income:" in line:
                assert line.startswith("  "), f"Account line not indented: {line!r}"


# ── export_duplicate_report ───────────────────────────────────────────────


class TestExportDuplicateReport:
    def test_report_has_comment_markers(self, exporter):
        txs = [
            Transaction(
                platform=Platform.ALIPAY,
                datetime=datetime(2024, 1, 15, 14, 0, 0),
                amount=Decimal("-100"),
                counterparty="星巴克",
                status=DedupStatus.UNIQUE,
            ),
            Transaction(
                platform=Platform.BANK,
                datetime=datetime(2024, 1, 15, 14, 0, 0),
                amount=Decimal("-100"),
                counterparty="支付宝-星巴克",
                status=DedupStatus.DUPLICATE,
                duplicate_of="abc",
            ),
        ]
        out = exporter.export_duplicate_report(txs)
        assert "; " in out
        assert "重复交易" in out or "总交易数" in out

    def test_report_to_file(self, exporter):
        txs = [
            Transaction(
                platform=Platform.ALIPAY,
                datetime=datetime(2024, 1, 15, 14, 0, 0),
                amount=Decimal("-100"),
                counterparty="星巴克",
                status=DedupStatus.UNIQUE,
            )
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".beancount", delete=False, encoding="utf-8"
        ) as f:
            path = f.name
        try:
            exporter.export_duplicate_report(txs, output_path=path)
            assert os.path.exists(path)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert len(content) > 0
        finally:
            os.unlink(path)


# ── _sanitize_narration ───────────────────────────────────────────────────


class TestSanitizeNarration:
    def test_removes_quotes(self, exporter):
        result = exporter._sanitize_narration('He said "hello"')
        assert '"' not in result

    def test_truncates_long_text(self, exporter):
        result = exporter._sanitize_narration("x" * 100)
        assert len(result) <= 50

    def test_empty_returns_unknown(self, exporter):
        assert exporter._sanitize_narration("") == "Unknown"
        assert exporter._sanitize_narration(None) == "Unknown"


# ── _determine_asset_account ──────────────────────────────────────────────


class TestExporterAssetAccount:
    def test_alipay(self, exporter):
        tx = Transaction(platform=Platform.ALIPAY)
        assert "Alipay" in exporter._determine_asset_account(tx)

    def test_wechat(self, exporter):
        tx = Transaction(platform=Platform.WECHAT)
        assert "WeChat" in exporter._determine_asset_account(tx)

    def test_bank(self, exporter):
        tx = Transaction(platform=Platform.BANK)
        acct = exporter._determine_asset_account(tx)
        assert "Bank" in acct or "Checking" in acct


# ── _format_amount ────────────────────────────────────────────────────────


class TestFormatAmount:
    def test_positive(self, exporter):
        assert exporter._format_amount(Decimal("100.5")) == "100.50 CNY"

    def test_negative(self, exporter):
        assert exporter._format_amount(Decimal("-33.33")) == "-33.33 CNY"

    def test_zero(self, exporter):
        assert exporter._format_amount(Decimal("0")) == "0.00 CNY"


# ── Full export (file output) ─────────────────────────────────────────────


class TestFullExport:
    def test_export_creates_file(self, exporter, sample_expense_alipay):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".beancount", delete=False, encoding="utf-8"
        ) as f:
            path = f.name
        try:
            exporter.export([sample_expense_alipay], output_path=path)
            assert os.path.exists(path)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert "2024-01-15" in content
            assert "; Beancount" in content or "open" in content
        finally:
            os.unlink(path)
