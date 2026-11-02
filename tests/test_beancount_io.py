"""Native ledger roundtrips preserve accounting structure and source files."""

import pytest
from beancount_dedup.beancount_io import BeancountIOError, export_beancount, import_beancount

pytest.importorskip("beancount")
from beancount.core import data, number


@pytest.fixture
def rich_ledger(tmp_path):
    source = tmp_path / "source"
    child = source / "parts"
    receipt = source / "receipts" / 'receipt "copy".pdf'
    child.mkdir(parents=True)
    receipt.parent.mkdir()
    receipt.write_bytes(b"synthetic receipt")
    main = source / "main.beancount"
    main.write_text(
        """option "title" "My \\"quoted\\" ledger"
option "operating_currency" "CNY"
option "documents" "receipts"
option "booking_method" "FIFO"
option "inferred_tolerance_default" "CNY:0.00001"
include "parts/*.beancount"
2024-01-01 commodity CNY
  name: "Chinese yuan"
2024-01-01 commodity ACME
2024-01-01 open Assets:Cash CNY
2024-01-01 open Assets:Savings CNY
2024-01-01 open Assets:Broker ACME
2024-01-01 open Equity:Opening CNY
2024-01-01 open Expenses:Food CNY
2024-01-01 open Expenses:Fees CNY
2024-01-01 pad Assets:Cash Equity:Opening
2024-01-02 balance Assets:Cash 1000 CNY
2024-01-03 * "Merchant" "Split payment" #food ^receipt-1
  receipt: "user metadata"
  reviewed: TRUE
  Assets:Cash -10.123 CNY
    bank-ref: "reference"
  Expenses:Food 10.10 CNY
  Expenses:Fees 0.023 CNY
2024-01-04 * "Internal transfer"
  Assets:Cash -100 CNY
  Assets:Savings 100 CNY
2024-01-05 * "Buy stock"
  Assets:Broker 2 ACME {50 CNY, 2024-01-05, "lot-a"} @ 50 CNY
  Assets:Cash -100 CNY
2024-01-06 price ACME 51.12345 CNY
2024-01-07 note Assets:Cash "A \\"quoted\\" note" #noted ^note-link
  comment: "still here"
2024-01-08 event "location" "A \\"quoted\\" city"
2024-01-09 document Assets:Cash "receipts/receipt \\"copy\\".pdf" #receipt ^document-link
2024-01-10 query "Cash query" "SELECT account WHERE account ~ \\"Assets\\""
2024-01-11 custom "budget" Expenses:Food "Some \\"quoted\\" text" 10.123 CNY 2024-12-31 TRUE 2.75
  user-meta: "custom metadata"
2024-01-31 close Expenses:Fees
""",
        encoding="utf-8",
    )
    included = child / "extra.beancount"
    included.write_text(
        """option "operating_currency" "USD"
2024-02-01 * "Included entry" #included ^from-child
  Assets:Cash -1 CNY
  Expenses:Food
""",
        encoding="utf-8",
    )
    return main, included, receipt


def without_location(entry):
    meta = {key: value for key, value in entry.meta.items() if key not in {"filename", "lineno"}}
    if isinstance(entry, data.Transaction):
        postings = [
            posting._replace(
                meta={
                    key: value
                    for key, value in (posting.meta or {}).items()
                    if key not in {"filename", "lineno"}
                }
            )
            for posting in entry.postings
        ]
        return entry._replace(meta=meta, postings=postings)
    return entry._replace(meta=meta)


def test_native_roundtrip_preserves_directives_split_cost_price_metadata_and_options(
    rich_ledger, tmp_path
):
    main, included, receipt = rich_ledger
    original = {path: path.read_bytes() for path in (main, included)}
    ledger = import_beancount(main)
    target = tmp_path / "new" / "export.beancount"
    text = export_beancount(ledger, target)
    restored = import_beancount(target)
    assert [without_location(entry) for entry in restored.entries] == [
        without_location(entry) for entry in ledger.entries
    ]
    assert len([entry for entry in restored.entries if isinstance(entry, data.Pad)]) == 1
    assert len([entry for entry in restored.entries if isinstance(entry, data.Balance)]) == 1
    assert {type(entry).__name__ for entry in restored.entries} >= {
        "Open",
        "Close",
        "Commodity",
        "Transaction",
        "Balance",
        "Pad",
        "Price",
        "Note",
        "Event",
        "Document",
        "Query",
        "Custom",
    }
    split = next(
        entry for entry in restored.entries if getattr(entry, "narration", None) == "Split payment"
    )
    assert len(split.postings) == 3
    assert split.tags == frozenset({"food"})
    assert split.links == frozenset({"receipt-1"})
    assert split.meta["receipt"] == "user metadata"
    assert split.postings[0].meta["bank-ref"] == "reference"
    stock = next(
        entry for entry in restored.entries if getattr(entry, "narration", None) == "Buy stock"
    )
    assert (
        stock.postings[0].cost
        == next(
            entry for entry in ledger.entries if getattr(entry, "narration", None) == "Buy stock"
        )
        .postings[0]
        .cost
    )
    assert stock.postings[0].price.number.as_tuple().exponent == 0
    assert next(
        entry for entry in restored.entries if isinstance(entry, data.Document)
    ).filename == str(receipt)
    assert restored.options["title"] == 'My "quoted" ledger'
    assert restored.options["operating_currency"] == ["CNY", "USD"]
    assert restored.options["documents"] == [str(receipt.parent)]
    assert restored.options["booking_method"].name == "FIFO"
    assert (
        restored.options["inferred_tolerance_default"]
        == ledger.options["inferred_tolerance_default"]
    )
    # Includes are consolidated and amounts missing in original postings remain
    # omitted; neither the pad-generated transaction nor interpolation is saved.
    assert 'include "' not in text
    assert "(Padding inserted for Balance" not in text
    last = restored.entries[-1]
    assert isinstance(last, data.Transaction)
    assert last.postings[-1].units is number.MISSING
    assert target.read_text(encoding="utf-8") == text
    assert {path: path.read_bytes() for path in original} == original


@pytest.mark.parametrize("which", ["main", "include", "symlink", "hardlink", "existing"])
def test_export_never_overwrites_sources_or_existing_files(rich_ledger, tmp_path, which):
    main, included, _ = rich_ledger
    target = {"main": main, "include": included}.get(which, tmp_path / "target.beancount")
    if which == "symlink":
        target.symlink_to(included)
    elif which == "hardlink":
        target.hardlink_to(included)
    elif which == "existing":
        target.write_text("existing output", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(BeancountIOError, match=r"overwrite|already exists"):
        export_beancount(import_beancount(main), target)
    assert target.read_bytes() == before


@pytest.mark.parametrize("plugin_in_include", [False, True])
def test_plugins_in_any_source_are_rejected_without_import_or_loader_cache(
    tmp_path, monkeypatch, plugin_in_include
):
    marker = tmp_path / "executed"
    (tmp_path / "dangerous_plugin.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    main = tmp_path / "main.beancount"
    if plugin_in_include:
        main.write_text('include "child.beancount"\n', encoding="utf-8")
        plugin_source = tmp_path / "child.beancount"
    else:
        plugin_source = main
    plugin_source.write_text('plugin "dangerous_plugin"\n', encoding="utf-8")
    from beancount import loader

    monkeypatch.setattr(
        loader, "load_file", lambda *_args, **_kwargs: pytest.fail("unsafe loader invoked")
    )
    with pytest.raises(BeancountIOError, match="plugins"):
        import_beancount(main)
    assert not marker.exists()
    assert not list(tmp_path.glob("*.picklecache"))


@pytest.mark.parametrize(
    "source",
    [
        'include "missing.beancount"\n',
        'include "input.beancount"\n',
        "not valid beancount\n",
        '2024-01-01 open Assets:Cash CNY\n2024-01-01 * "Unbalanced"\n  Assets:Cash 1 CNY\n',
        "2024-01-01 open Assets:Cash CNY\n2024-01-02 balance Assets:Cash 1 CNY\n",
    ],
)
def test_bad_includes_parse_errors_and_accounting_errors_fail_before_export(tmp_path, source):
    path = tmp_path / "input.beancount"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(BeancountIOError):
        import_beancount(path)


def test_unprintable_metadata_cannot_silently_disappear(rich_ledger, tmp_path):
    ledger = import_beancount(rich_ledger[0])
    ledger.entries[0].meta["nested-user-value"] = {"important": "value"}
    target = tmp_path / "export.beancount"
    with pytest.raises(BeancountIOError, match=r"metadata|preserve"):
        export_beancount(ledger, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".beancount-export-*"))


def test_total_cost_total_price_and_numeric_metadata_keep_decimal_scale(tmp_path):
    source = tmp_path / "input.beancount"
    source.write_text(
        """option "operating_currency" "USD"
2024-01-01 open Assets:Cash USD
2024-01-01 open Assets:Stock TEST
2024-01-01 open Expenses:Fees USD
2024-01-02 ! "Total cost and price" #trading ^trade-ref
  decimal-meta: 0.0100
  amount-meta: 2.3000 USD
  date-meta: 2024-01-02
  empty-meta:
  Assets:Stock 2 TEST {{100.0000 USD, "lot-\\"a"}} @@ 100.0000 USD
  ! Assets:Cash -100.0000 USD
""",
        encoding="utf-8",
    )
    ledger = import_beancount(source)
    exported = tmp_path / "export.beancount"
    export_beancount(ledger, exported)
    restored = import_beancount(exported)
    left = ledger.entries[-1]
    right = restored.entries[-1]
    assert without_location(left) == without_location(right)
    assert right.meta["decimal-meta"].as_tuple() == left.meta["decimal-meta"].as_tuple()
    assert right.meta["amount-meta"].number.as_tuple() == left.meta["amount-meta"].number.as_tuple()
    assert (
        right.postings[0].cost.number_total.as_tuple()
        == left.postings[0].cost.number_total.as_tuple()
    )
    assert right.postings[0].price.number.as_tuple() == left.postings[0].price.number.as_tuple()
    assert right.postings[1].flag == "!"


def test_custom_account_roots_and_options_survive_native_io(tmp_path):
    source = tmp_path / "input.beancount"
    source.write_text(
        """option "name_assets" "Actifs"
option "name_expenses" "Depenses"
option "use_precise_interpolation" "TRUE"
2024-01-01 open Actifs:Cash EUR
2024-01-01 open Depenses:Food EUR
2024-01-02 * "Expense"
  Actifs:Cash -1.00 EUR
  Depenses:Food
""",
        encoding="utf-8",
    )
    target = tmp_path / "export.beancount"
    export_beancount(import_beancount(source), target)
    restored = import_beancount(target)
    assert restored.options["name_assets"] == "Actifs"
    assert restored.options["name_expenses"] == "Depenses"
    assert restored.options["use_precise_interpolation"] is True
    assert restored.entries[-1].postings[0].account == "Actifs:Cash"


def test_missing_source_fails_cleanly(tmp_path):
    with pytest.raises(BeancountIOError, match="not found"):
        import_beancount(tmp_path / "missing.beancount")
