"""Read-only, deterministic statement-to-ledger reconciliation.

Only postings in the input accounts' date ranges and requested currency are
compared. A match requires the same account, signed Decimal amount, and date
(or explicitly configured date window). Ambiguous candidates are never claimed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .account_classifier import BeancountAccountClassifier
from .models import Transaction


class ReconciliationError(ValueError):
    """The ledger or inputs cannot be reconciled reliably."""


@dataclass
class ReconciliationReport:
    """JSON-ready report; amounts are strings to preserve decimal precision."""

    ledger_path: str
    scope: dict[str, Any]
    totals: dict[str, Any]
    matched: list[dict[str, Any]] = field(default_factory=list)
    statement_only: list[dict[str, Any]] = field(default_factory=list)
    ledger_only: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        report = asdict(self)
        if not self.scope["accounts"]:
            report["status"] = "no_statement_transactions"
        else:
            report["status"] = (
                "differences"
                if self.statement_only or self.ledger_only or self.ambiguous
                else "matched"
            )
        report["counts"] = {
            "matched": len(self.matched),
            "statement_only": len(self.statement_only),
            "ledger_only": len(self.ledger_only),
            "ambiguous_groups": len(self.ambiguous),
        }
        return report


def _error_text(error: Any) -> str:
    source = getattr(error, "source", None) or {}
    return f"{source.get('filename', '<ledger>')}:{source.get('lineno', 0)}: {error.message}"


def _load_ledger(ledger_path: Path) -> list[Any]:
    """Load includes and validate without user plugins or pickle-cache access.

    Beancount's public load_file executes arbitrary user plugins and uses a
    pickle cache. Its recursive parser is isolated here so reconciliation can
    remain read-only. Fail explicitly if that interface changes.
    """
    try:
        from beancount import loader
        from beancount.core import data
        from beancount.ops import balance, pad, validation
        from beancount.parser import booking
    except ImportError as exc:
        raise ReconciliationError(
            "Ledger reconciliation requires Beancount. Install financial-beancount[validate]."
        ) from exc

    if not ledger_path.is_file():
        raise ReconciliationError(f"Ledger file not found: {ledger_path}")
    parse_recursive = getattr(loader, "_parse_recursive", None)
    if parse_recursive is None:
        raise ReconciliationError("Unsupported Beancount loader; recursive parsing is unavailable.")
    try:
        entries, errors, options = parse_recursive([(str(ledger_path), True)], None, "utf-8")
        if errors:
            raise ReconciliationError("Invalid ledger:\n" + "\n".join(map(_error_text, errors)))
        if options.get("plugin"):
            raise ReconciliationError(
                "User ledger plugins are not executed during reconciliation. "
                "Export a materialized ledger without plugin directives first."
            )
        entries.sort(key=data.entry_sortkey)
        entries, errors = booking.book(entries, options)
        # Only Beancount's own pad and balance passes run; user plugin imports
        # and loader cache reads/writes are deliberately excluded.
        if options.get("plugin_processing_mode", "default") != "raw":
            entries, pad_errors = pad.pad(entries, options)
            errors.extend(pad_errors)
            entries, balance_errors = balance.check(entries, options)
            errors.extend(balance_errors)
        errors.extend(validation.validate(entries, options))
        if errors:
            raise ReconciliationError("Invalid ledger:\n" + "\n".join(map(_error_text, errors)))
        return entries
    except ReconciliationError:
        raise
    except Exception as exc:
        raise ReconciliationError(f"Could not load ledger: {exc}") from exc


def _statement_record(tx: Transaction, account: str, currency: str) -> dict[str, Any]:
    return {
        "id": tx.id,
        "date": tx.date_str,
        "account": account,
        "amount": str(tx.amount),
        "currency": currency,
        "counterparty": tx.counterparty,
        "description": tx.description,
        "source_file": tx.source_file,
    }


def _ledger_records(
    entries: list[Any], ranges: dict[str, tuple[date, date]], currency: str
) -> list[dict[str, Any]]:
    from beancount.core import data

    records = []
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        for index, posting in enumerate(entry.postings):
            interval = ranges.get(posting.account)
            if interval is None or not interval[0] <= entry.date <= interval[1]:
                continue
            if posting.units is None or posting.units.currency != currency:
                continue
            meta = {**entry.meta, **(posting.meta or {})}
            records.append(
                {
                    "id": f"{meta['filename']}:{meta['lineno']}:{index}",
                    "date": entry.date.isoformat(),
                    "account": posting.account,
                    "amount": str(posting.units.number),
                    "currency": posting.units.currency,
                    "payee": entry.payee or "",
                    "narration": entry.narration,
                    "filename": meta["filename"],
                    "line": meta["lineno"],
                    "posting_index": index,
                }
            )
    return records


def _candidate_groups(
    statements: list[dict[str, Any]], ledgers: list[dict[str, Any]], window: int
) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    by_amount: dict[tuple[str, Decimal], list[int]] = defaultdict(list)
    for index, posting in enumerate(ledgers):
        by_amount[(posting["account"], Decimal(posting["amount"]))].append(index)
    forward: dict[int, set[int]] = defaultdict(set)
    reverse: dict[int, set[int]] = defaultdict(set)
    for index, statement in enumerate(statements):
        key = (statement["account"], Decimal(statement["amount"]))
        statement_date = date.fromisoformat(statement["date"])
        for candidate in by_amount[key]:
            ledger_date = date.fromisoformat(ledgers[candidate]["date"])
            if abs((statement_date - ledger_date).days) <= window:
                forward[index].add(candidate)
                reverse[candidate].add(index)
    return forward, reverse


def _ambiguous_groups(
    statements: list[dict[str, Any]],
    ledgers: list[dict[str, Any]],
    forward: dict[int, set[int]],
    reverse: dict[int, set[int]],
    matched: set[int],
) -> list[dict[str, Any]]:
    groups = []
    visited = set(matched)
    for start in sorted(forward):
        if start in visited:
            continue
        pending = [start]
        statement_ids: set[int] = set()
        ledger_ids: set[int] = set()
        while pending:
            index = pending.pop()
            if index in statement_ids:
                continue
            statement_ids.add(index)
            for candidate in forward[index]:
                ledger_ids.add(candidate)
                pending.extend(reverse[candidate] - statement_ids)
        visited.update(statement_ids)
        groups.append(
            {
                "reason": "Candidates are shared or multiple postings match; manual review required.",
                "statements": [statements[i] for i in sorted(statement_ids)],
                "ledger_postings": [ledgers[i] for i in sorted(ledger_ids)],
            }
        )
    return groups


def reconcile_transactions(
    transactions: list[Transaction],
    ledger_path: str | Path,
    classifier: BeancountAccountClassifier | None = None,
    currency: str = "CNY",
    date_window_days: int = 0,
) -> ReconciliationReport:
    """Compare deduplicated statements with existing ledger asset postings.

    Date coverage is inferred separately for each input asset account. The
    report does not establish full ledger completeness or opening balances.
    Classification, transaction amounts, statuses, and ledger files are never
    changed. Both sides of an automatic match must have exactly one candidate.
    """
    if isinstance(date_window_days, bool) or not isinstance(date_window_days, int):
        raise ReconciliationError("date_window_days must be a non-negative integer.")
    if date_window_days < 0:
        raise ReconciliationError("date_window_days must be a non-negative integer.")
    if not isinstance(currency, str) or not currency:
        raise ReconciliationError("currency must be a non-empty currency code.")
    classifier = classifier or BeancountAccountClassifier()
    ledger_path = Path(ledger_path).expanduser().resolve()
    entries = _load_ledger(ledger_path)
    statements = []
    ranges: dict[str, tuple[date, date]] = {}
    for tx in transactions:
        if not tx.amount.is_finite():
            raise ReconciliationError(f"Non-finite statement amount: {tx.id}")
        account = classifier.determine_asset_account(tx.platform.value, tx.counterparty)
        statements.append(_statement_record(tx, account, currency))
        lower = tx.datetime.date() - timedelta(days=date_window_days)
        upper = tx.datetime.date() + timedelta(days=date_window_days)
        previous = ranges.get(account, (lower, upper))
        ranges[account] = (min(previous[0], lower), max(previous[1], upper))
    ledgers = _ledger_records(entries, ranges, currency)
    forward, reverse = _candidate_groups(statements, ledgers, date_window_days)
    matched_ids: set[int] = set()
    matched = []
    for index, candidates in sorted(forward.items()):
        if len(candidates) != 1:
            continue
        candidate = next(iter(candidates))
        if len(reverse[candidate]) == 1:
            matched_ids.add(index)
            matched.append({"statement": statements[index], "ledger_posting": ledgers[candidate]})

    by_account = {}
    for account in sorted(ranges):
        statement_sum = sum(
            (Decimal(s["amount"]) for s in statements if s["account"] == account), Decimal(0)
        )
        ledger_sum = sum(
            (Decimal(s["amount"]) for s in ledgers if s["account"] == account), Decimal(0)
        )
        by_account[account] = {
            "statement": str(statement_sum),
            "ledger": str(ledger_sum),
            "statement_minus_ledger": str(statement_sum - ledger_sum),
        }
    statement_total = sum((Decimal(s["amount"]) for s in statements), Decimal(0))
    ledger_total = sum((Decimal(s["amount"]) for s in ledgers), Decimal(0))
    return ReconciliationReport(
        ledger_path=str(ledger_path),
        scope={
            "currency": currency,
            "accounts": sorted(ranges),
            "date_ranges": {
                account: {"start": interval[0].isoformat(), "end": interval[1].isoformat()}
                for account, interval in sorted(ranges.items())
            },
            "date_window_days": date_window_days,
            "coverage": "Input account date ranges only; no opening-balance verification.",
            "user_plugins_executed": False,
            "source_files": sorted(
                {
                    str(Path(e.meta["filename"]).resolve())
                    for e in entries
                    if e.meta and e.meta.get("filename")
                }
            ),
        },
        totals={
            "currency": currency,
            "statement": str(statement_total),
            "ledger": str(ledger_total),
            "statement_minus_ledger": str(statement_total - ledger_total),
            "by_account": by_account,
        },
        matched=matched,
        statement_only=[s for i, s in enumerate(statements) if not forward.get(i)],
        ledger_only=[s for i, s in enumerate(ledgers) if not reverse.get(i)],
        ambiguous=_ambiguous_groups(statements, ledgers, forward, reverse, matched_ids),
    )
