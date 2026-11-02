"""Native beangulp/Fava importer for the supported statement CSV formats.

Install the ``beangulp`` extra before importing this optional module. The
adapter reads one CSV at a time and returns standard Beancount Transactions;
account declarations remain in the user's ledger. It never starts AI or loads
a ledger. beangulp/Fava can mark duplicates against their existing entries.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from beancount.core import data
    from beancount.core.amount import Amount
    from beangulp import Importer
except ImportError as exc:
    raise ImportError(
        "Install financial-beancount[beangulp] to use the beangulp/Fava importer"
    ) from exc

from .account_classifier import BeancountAccountClassifier
from .config import AccountConfig, AppConfig
from .deduplicator import DeduplicationEngine
from .models import DedupStatus, TransactionType
from .parsers.strict import build_strict_parser

if TYPE_CHECKING:
    from datetime import date

    from .models import Transaction


def _stable_id(transaction: Transaction) -> str:
    # Parser transaction IDs may be random. Source data gives reproducible
    # metadata across imports, without inventing a bank reference number.
    content = json.dumps(
        {
            "platform": transaction.platform.value,
            "datetime": transaction.datetime.isoformat(),
            "amount": str(transaction.amount),
            "counterparty": transaction.counterparty,
            "description": transaction.description,
            "raw_data": {str(key): value for key, value in transaction.raw_data.items()},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return "financialbeancount-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]


class FinancialBeancountImporter(Importer):
    """Import recognized Alipay, WeChat Pay, UnionPay and bank statement CSVs.

    ``config`` accepts either AppConfig or AccountConfig. Account rules,
    operating currency and metadata settings are used; AI is always disabled.
    Register one instance in the beangulp/Fava ``CONFIG`` list. Inherited
    ``name`` is the standard beangulp property identifying this importer.
    """

    def __init__(self, config: AppConfig | AccountConfig | None = None):
        if isinstance(config, AccountConfig):
            config = AppConfig(accounts=config)
        if config is not None and not isinstance(config, AppConfig):
            raise TypeError("config must be AppConfig or AccountConfig")
        self.config = config or AppConfig()
        if not re.fullmatch(r"[A-Z][A-Z0-9'._-]*", self.config.operating_currency):
            raise ValueError("Invalid operating currency")
        self.classifier = BeancountAccountClassifier(self.config.accounts)

    def _transactions(self, filepath: str) -> list[Transaction]:
        path = Path(filepath)
        if path.suffix.lower() != ".csv" or not path.is_file():
            raise ValueError("This importer accepts recognized statement CSV files only")
        result = build_strict_parser().parse(str(path))
        if result.errors:
            raise ValueError("Statement CSV could not be parsed completely: " + result.errors[0])
        if not result.transactions:
            raise ValueError("No supported statement transactions were found in this CSV")
        return result.transactions

    def identify(self, filepath: str) -> bool:
        """Match a supported, parseable statement; unrelated CSV files stay free."""
        try:
            self._transactions(filepath)
        except (OSError, ValueError):
            return False
        return True

    def account(self, filepath: str) -> str:
        """Return the platform wallet used to archive this statement document."""
        transactions = self._transactions(filepath)
        return self.classifier.determine_asset_account(transactions[0].platform.value)

    def date(self, filepath: str) -> date:
        """Archive the document under its last successfully parsed transaction."""
        return max(tx.datetime.date() for tx in self._transactions(filepath))

    @staticmethod
    def cmp(entry1: data.Directive, entry2: data.Directive) -> bool:
        """Match our exact source identity and unchanged financial content only.

        beangulp's default comparator allows dates and amounts to differ. A
        duplicate marker can suppress an entry from normal import output, so
        similarity is insufficient. Older or hand-written entries without our
        content ID remain available for manual reconciliation.
        """
        if not isinstance(entry1, data.Transaction) or not isinstance(entry2, data.Transaction):
            return False
        source_id = entry1.meta.get("id")
        if not isinstance(source_id, str) or not re.fullmatch(
            r"financialbeancount-[0-9a-f]{24}", source_id
        ):
            return False
        if (
            source_id != entry2.meta.get("id")
            or entry1.date != entry2.date
            or entry1.payee != entry2.payee
            or entry1.narration != entry2.narration
        ):
            return False
        # Posting metadata and ordering can change on a printer/parser round
        # trip. Account, exact amount/currency, cost and price must still agree.
        return Counter((p.account, p.units, p.cost, p.price) for p in entry1.postings) == Counter(
            (p.account, p.units, p.cost, p.price) for p in entry2.postings
        )

    def deduplicate(self, entries: data.Entries, existing: data.Entries) -> None:
        """Mark proven repeats without heuristic matching or changing existing."""
        by_id: dict[str, list[data.Transaction]] = defaultdict(list)
        for target in existing:
            if isinstance(target, data.Transaction):
                identifier = target.meta.get("id")
                if isinstance(identifier, str):
                    by_id[identifier].append(target)
        for entry in entries:
            if not isinstance(entry, data.Transaction):
                continue
            identifier = entry.meta.get("id")
            if not isinstance(identifier, str):
                continue
            for target in by_id.get(identifier, []):
                if target is not entry and self.cmp(entry, target):
                    entry.meta["__duplicate__"] = target
                    break

    def extract(self, filepath: str, existing: data.Entries | None = None) -> data.Entries:
        """Return transactions only; beangulp handles existing-ledger duplicates.

        Existing entries are passed through the native framework's separate
        ``deduplicate`` method. No existing ledger is opened or evaluated here.
        """
        engine = DeduplicationEngine()
        engine.add_transactions(self._transactions(filepath))
        transactions = engine.get_unique_transactions()
        entries: data.Entries = []
        for tx in sorted(transactions, key=lambda transaction: transaction.datetime):
            direction = "expense" if tx.is_expense else "income"
            classification = self.classifier.classify_transaction(
                tx.counterparty, tx.description, direction, tx.platform.value
            )
            is_transfer = (
                tx.tx_type == TransactionType.TRANSFER
                or tx.status == DedupStatus.INTERNAL_TRANSFER
                or engine.is_internal_transfer(tx)
            )
            if is_transfer:
                classification["opposing_account"] = "Equity:Transfers:Unassigned"
            # Use data objects rather than interpolation into ledger syntax:
            # Beancount's own printer escapes quotes, newlines and backslashes.
            metadata = data.new_metadata(str(Path(filepath)), 0)
            if self.config.include_metadata:
                metadata.update(
                    {
                        "id": _stable_id(tx),
                        "platform": tx.platform.value,
                        "status": tx.status.value,
                        "source-file": Path(filepath).name,
                        "transaction-time": tx.time_str,
                        "__source__": json.dumps(tx.raw_data, ensure_ascii=False, default=str),
                    }
                )
                if tx.payment_method:
                    metadata["method"] = tx.payment_method
                if tx.match_level:
                    metadata["match-level"] = tx.match_level
                if is_transfer:
                    metadata["review-reason"] = "Choose the other account for this transfer"
            flag = (
                "!"
                if is_transfer
                or tx.status != DedupStatus.UNIQUE
                or tx.tx_type in {TransactionType.TRANSFER, TransactionType.REFUND}
                else "*"
            )
            tags = frozenset(tag for tag in tx.tags if re.fullmatch(r"[\w/-]+", tag))
            entries.append(
                data.Transaction(
                    metadata,
                    tx.datetime.date(),
                    flag,
                    tx.counterparty or None,
                    tx.description or "Unknown",
                    tags,
                    frozenset(),
                    [
                        data.Posting(
                            classification["asset_account"],
                            Amount(tx.amount, self.config.operating_currency),
                            None,
                            None,
                            None,
                            None,
                        ),
                        data.Posting(
                            classification["opposing_account"],
                            Amount(-tx.amount, self.config.operating_currency),
                            None,
                            None,
                            None,
                            None,
                        ),
                    ],
                )
            )
        return entries
