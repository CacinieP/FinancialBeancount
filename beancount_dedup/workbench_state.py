"""Local, persistent review state shared by the graphical workbench endpoints."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import json
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

from .account_classifier import BeancountAccountClassifier, is_valid_account
from .ai import suggest_classifications
from .beancount_io import BeancountLedger, _ExactFormatter, export_beancount, import_beancount
from .config import AccountConfig, AppConfig
from .converters.auto_converter import AutoConverter
from .deduplicator import DeduplicationEngine
from .models import DedupStatus, Platform, Transaction, TransactionType
from .parsers.strict import build_strict_parser
from .reconciliation import reconcile_transactions

_TRANSFER_ACCOUNT = "Equity:Transfers:Unassigned"
_UPLOAD_LIMIT = 100 * 1024 * 1024


class _WalletClassifier(BeancountAccountClassifier):
    """Use the user's explicit platform mapping rather than guessing bank names."""

    def determine_asset_account(self, platform: str, counterparty: str = "") -> str:
        return self.platform_wallets.get(
            platform, super().determine_asset_account(platform, counterparty)
        )


def _relative_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("上传文件名必须是安全的相对路径")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or any(part in {"..", "."} or ":" in part for part in path.parts)
        or not path.parts
    ):
        raise ValueError("上传文件名不能包含绝对路径或上级目录")
    return str(path)


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("账簿只能引用本次上传目录内的文件")
    return resolved


def _serialize_tx(tx: Transaction) -> dict[str, Any]:
    value = asdict(tx)
    value.update(
        amount=str(tx.amount),
        datetime=tx.datetime.isoformat(),
        platform=tx.platform.value,
        tx_type=tx.tx_type.value,
        status=tx.status.value,
        tags=sorted(tx.tags),
    )
    return value


def _deserialize_tx(value: dict[str, Any]) -> Transaction:
    value = dict(value)
    value.update(
        amount=Decimal(value["amount"]),
        datetime=datetime.fromisoformat(value["datetime"]),
        platform=Platform(value["platform"]),
        tx_type=TransactionType(value["tx_type"]),
        status=DedupStatus(value["status"]),
        tags=set(value.get("tags", [])),
    )
    return Transaction(**value)


def _is_transfer(tx: Transaction) -> bool:
    return tx.tx_type == TransactionType.TRANSFER or tx.status == DedupStatus.INTERNAL_TRANSFER


class WorkbenchState:
    """Review immutable transaction facts, with explicit local-only persistence."""

    def __init__(self, workspace: Path, config: AppConfig | None = None):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.config = config if config is not None else AppConfig()
        if not re.fullmatch(r"[A-Z][A-Z0-9'._-]*", self.config.operating_currency):
            raise ValueError("无效的记账币种")
        self._lock = threading.RLock()
        self._inputs: dict[str, dict[str, Any]] = {}
        self._transactions: list[Transaction] = []
        self._reviews: dict[str, dict[str, Any]] = {}
        self._duplicates = 0
        self._ledger: BeancountLedger | None = None
        self._ledger_root: Path | None = None
        self._ledger_name: str | None = None
        self._ledger_dir: Path | None = None
        self._report: dict[str, Any] | None = None
        self._is_demo = False
        self._restore()

    def _classifier(self, accounts: AccountConfig | None = None) -> _WalletClassifier:
        return _WalletClassifier(accounts if accounts is not None else self.config.accounts)

    def _session_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "is_demo": self._is_demo,
            "accounts": asdict(self.config.accounts),
            "currency": self.config.operating_currency,
            "inputs": {
                name: {
                    "path": item["path"],
                    "transactions": [_serialize_tx(tx) for tx in item["transactions"]],
                }
                for name, item in self._inputs.items()
            },
            "reviews": self._reviews,
            "reconciliation_window": self._report["scope"]["date_window_days"]
            if self._report
            else None,
            "ledger": {
                "root": str(self._ledger_root.relative_to(self.workspace)),
                "directory": str(self._ledger_dir.relative_to(self.workspace)),
                "name": self._ledger_name,
            }
            if self._ledger_root and self._ledger_dir
            else None,
        }

    def _save(self) -> None:
        text = json.dumps(self._session_payload(), ensure_ascii=False, indent=2, allow_nan=False)
        temporary = self.workspace / f".session-{uuid.uuid4().hex}.json"
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self.workspace / "session.json")
        finally:
            temporary.unlink(missing_ok=True)

    def _commit(
        self, *, accounts: AccountConfig | None = None, currency: str | None = None, **updates: Any
    ) -> None:
        old = {key: getattr(self, key) for key in updates}
        old_accounts = self.config.accounts
        old_currency = self.config.operating_currency
        try:
            for key, value in updates.items():
                setattr(self, key, value)
            if accounts is not None:
                self.config.accounts = accounts
            if currency is not None:
                self.config.operating_currency = currency
            self._save()
        except Exception:
            for key, value in old.items():
                setattr(self, key, value)
            self.config.accounts = old_accounts
            self.config.operating_currency = old_currency
            raise

    def _restore(self) -> None:
        path = self.workspace / "session.json"
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != 1:
                raise ValueError("不支持的工作区存档版本")
            accounts = AccountConfig(**payload["accounts"])
            self._classifier(accounts)
            currency = payload["currency"]
            if not re.fullmatch(r"[A-Z][A-Z0-9'._-]*", currency):
                raise ValueError("存档币种无效")
            inputs = {}
            for name, item in payload["inputs"].items():
                _relative_name(name)
                _inside(self.workspace / item["path"], self.workspace)
                inputs[name] = {
                    "path": item["path"],
                    "transactions": [_deserialize_tx(tx) for tx in item["transactions"]],
                }
            transactions, reviews, duplicates = self._process(inputs, accounts)
            for tx in transactions:
                saved = payload.get("reviews", {}).get(tx.id)
                if saved:
                    if not is_valid_account(saved.get("account")):
                        raise ValueError("存档含无效账户")
                    reviews[tx.id] = saved
            ledger = payload.get("ledger")
            if ledger:
                directory = _inside(self.workspace / ledger["directory"], self.workspace)
                root = _inside(self.workspace / ledger["root"], directory)
                self._check_ledger_upload(root, directory)
                self._ledger = import_beancount(root)
                self._ledger_root, self._ledger_dir, self._ledger_name = (
                    root,
                    directory,
                    ledger["name"],
                )
            self.config.accounts = accounts
            self.config.operating_currency = currency
            self._inputs, self._transactions, self._reviews = inputs, transactions, reviews
            self._duplicates, self._is_demo = duplicates, bool(payload.get("is_demo"))
            # Reconciliation is intentionally rerun against the restored ledger;
            # stale matching decisions must never hide exported transactions.
            self._report = None
            if (
                self._ledger_root
                and transactions
                and payload.get("reconciliation_window") is not None
            ):
                self._report = self._reconciliation_report(payload["reconciliation_window"])

        except Exception as exc:
            raise ValueError(f"无法恢复本地工作区：{exc}") from exc

    def _stage(self, files: list[dict[str, str]], category: str) -> tuple[Path, dict[str, Path]]:
        if not isinstance(files, list) or not files:
            raise ValueError("请选择要导入的文件")
        root = self.workspace / "uploads" / category / uuid.uuid4().hex
        staged = {}
        total = 0
        try:
            for item in files:
                if not isinstance(item, dict):
                    raise ValueError("上传文件格式无效")
                name = _relative_name(item.get("name", ""))
                if name in staged:
                    raise ValueError(f"上传包含重复文件名：{name}")
                try:
                    content = base64.b64decode(item.get("content", ""), validate=True)
                except (ValueError, TypeError, binascii.Error) as exc:
                    raise ValueError(f"文件内容不是有效的Base64：{name}") from exc
                total += len(content)
                if total > _UPLOAD_LIMIT:
                    raise ValueError("单次上传不能超过100MB")
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                staged[name] = path
            return root, staged
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    def _read_statements(self, staged: dict[str, Path]) -> dict[str, dict[str, Any]]:
        inputs = {}
        for name, path in staged.items():
            if path.suffix.lower() not in {".csv", ".xlsx", ".xls", ".pdf"}:
                raise ValueError(f"不支持的账单格式：{name}")
            parsed_path = path
            if path.suffix.lower() != ".csv":
                converted = path.parent / f".converted-{uuid.uuid4().hex}.csv"
                conversion = AutoConverter().convert(str(path), str(converted))
                if not conversion.success:
                    raise ValueError(f"账单转换失败：{name}：{'；'.join(conversion.errors)}")
                parsed_path = Path(conversion.output_path)
            result = build_strict_parser().parse(str(parsed_path))
            if result.errors:
                raise ValueError(f"账单导入失败：{name}：{'；'.join(result.errors)}")
            for index, tx in enumerate(result.transactions):
                tx.source_file = name
                identity = f"{name}\0{index}\0{tx.datetime.isoformat()}\0{tx.amount}\0{tx.counterparty}\0{tx.description}"
                tx.id = hashlib.sha256(identity.encode()).hexdigest()[:20]
            inputs[name] = {
                "path": str(path.relative_to(self.workspace)),
                "transactions": result.transactions,
            }
        return inputs

    def _initial_review(self, tx: Transaction, classifier: _WalletClassifier) -> dict[str, Any]:
        direction = "expense" if tx.is_expense else "income"
        if _is_transfer(tx):
            account, source, reason = (
                _TRANSFER_ACCOUNT,
                "transfer",
                "请选择实际转入/转出的资产或负债账户后确认",
            )
        else:
            account = classifier.classify_transaction(
                tx.counterparty, tx.description, direction, tx.platform.value
            )["opposing_account"]
            matched = classifier.has_rule_match(tx.counterparty, tx.description, direction)
            source, reason = (
                ("rule", "规则命中，等待确认")
                if matched
                else ("fallback", "未命中规则，等待分类或手动选择")
            )
            if tx.tx_type == TransactionType.REFUND:
                reason = "退款需核实原交易和对方账户后确认"
        return {
            "account": account,
            "classification_source": source,
            "review_status": "pending",
            "suggested_account": None,
            "confidence": None,
            "reason": reason,
        }

    def _process(
        self,
        inputs: dict[str, dict[str, Any]],
        accounts: AccountConfig,
        *,
        preserve_reviews: bool = True,
    ) -> tuple[list[Transaction], dict[str, dict[str, Any]], int]:
        all_txs = copy.deepcopy([tx for item in inputs.values() for tx in item["transactions"]])
        engine = DeduplicationEngine()
        for tx, result in zip(all_txs, engine.add_transactions(all_txs)):
            # The legacy engine does not assign INTERNAL_TRANSFER to the model.
            if result.status == DedupStatus.INTERNAL_TRANSFER:
                tx.status = result.status
        transactions = [tx for tx in all_txs if tx.status != DedupStatus.DUPLICATE]
        classifier = self._classifier(accounts)
        reviews = {}
        for tx in transactions:
            reviews[tx.id] = copy.deepcopy(
                (self._reviews.get(tx.id) if preserve_reviews else None)
                or self._initial_review(tx, classifier)
            )
        return transactions, reviews, sum(tx.status == DedupStatus.DUPLICATE for tx in all_txs)

    def _accounts_for_real_import(self) -> AccountConfig:
        """Keep user settings, but remove the demo-only synthetic override."""
        if not self._is_demo:
            return self.config.accounts
        overrides = dict(self.config.accounts.merchant_overrides)
        if overrides.get("示例·需自选") == "Expenses:Unknown":
            overrides.pop("示例·需自选")
        return replace(self.config.accounts, merchant_overrides=overrides)

    def import_statements(self, files: list[dict[str, str]]) -> None:
        with self._lock:
            root, staged = self._stage(files, "statements")
            try:
                inputs = {
                    **({} if self._is_demo else self._inputs),
                    **self._read_statements(staged),
                }
                accounts = self._accounts_for_real_import()
                transactions, reviews, duplicates = self._process(
                    inputs,
                    accounts,
                    preserve_reviews=not self._is_demo,
                )
                clear_demo_ledger: dict[str, Any] = (
                    {
                        "_ledger": None,
                        "_ledger_root": None,
                        "_ledger_dir": None,
                        "_ledger_name": None,
                    }
                    if self._is_demo
                    else {}
                )
                self._commit(
                    accounts=accounts,
                    **clear_demo_ledger,
                    _inputs=inputs,
                    _transactions=transactions,
                    _reviews=reviews,
                    _duplicates=duplicates,
                    _report=None,
                    _is_demo=False,
                )
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise

    def _check_ledger_upload(self, main: Path, directory: Path) -> None:
        import glob

        from beancount.core import data
        from beancount.parser import parser

        pending = [_inside(main, directory)]
        visited = set()
        while pending:
            path = pending.pop(0)
            if path in visited:
                raise ValueError("账簿include重复或循环引用")
            visited.add(path)
            entries, errors, options = parser.parse_file(
                str(path), report_filename=str(path), encoding="utf-8"
            )
            if errors:
                raise ValueError(
                    "账簿存在语法错误：" + "；".join(error.message for error in errors)
                )
            if options.get("plugin"):
                raise ValueError("工作台不执行账簿插件，请先导出不含插件的账簿")
            for entry in entries:
                if isinstance(entry, data.Document):
                    _inside(Path(entry.filename), directory)
            for document_dir in options.get("documents", []):
                if Path(document_dir).is_absolute():
                    raise ValueError("documents目录必须是上传目录内的相对路径")
                _inside(path.parent / document_dir, directory)
            for pattern in options["include"]:
                _relative_name(pattern)
                search = _inside(path.parent / pattern, directory)
                matches = sorted(glob.glob(str(search), recursive=True))
                if not matches:
                    raise ValueError(f"上传中缺少include文件：{pattern}")
                pending.extend(_inside(Path(match), directory) for match in matches)

    def import_ledger(self, files: list[dict[str, str]], main: str | None = None) -> None:
        with self._lock:
            directory, staged = self._stage(files, "ledger")
            try:
                if main is not None:
                    name = _relative_name(main)
                    if name not in staged:
                        raise ValueError("指定的账簿主文件不在本次上传中")
                else:
                    candidates = [
                        name
                        for name in staged
                        if Path(name).suffix.lower() in {".beancount", ".bean"}
                    ]
                    main_names = [
                        name for name in candidates if Path(name).name == "main.beancount"
                    ]
                    if len(main_names) == 1:
                        name = main_names[0]
                    elif len(candidates) == 1:
                        name = candidates[0]
                    else:
                        raise ValueError("多个账簿文件时请指定主文件（例如main.beancount）")
                root = staged[name]
                self._check_ledger_upload(root, directory)
                ledger = import_beancount(root)
                clear_demo_statements: dict[str, Any] = (
                    {
                        "_inputs": {},
                        "_transactions": [],
                        "_reviews": {},
                        "_duplicates": 0,
                    }
                    if self._is_demo
                    else {}
                )
                self._commit(
                    accounts=self._accounts_for_real_import(),
                    **clear_demo_statements,
                    _ledger=ledger,
                    _ledger_root=root,
                    _ledger_dir=directory,
                    _ledger_name=name,
                    _report=None,
                    _is_demo=False,
                )
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise

    def _ledger_accounts(self) -> list[str]:
        if not self._ledger:
            return []
        from beancount.core import data

        return sorted(
            {entry.account for entry in self._ledger.entries if isinstance(entry, data.Open)}
        )

    def _matched_ids(self) -> set[str]:
        return (
            {entry["statement"]["id"] for entry in self._report["matched"]}
            if self._report
            else set()
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            classifier = self._classifier()
            matched = self._matched_ids()
            statement_only = (
                {entry["id"] for entry in self._report["statement_only"]} if self._report else set()
            )
            ambiguous = (
                {
                    entry["id"]
                    for group in self._report["ambiguous"]
                    for entry in group["statements"]
                }
                if self._report
                else set()
            )
            rows = []
            for tx in self._transactions:
                status = (
                    "matched"
                    if tx.id in matched
                    else "ambiguous"
                    if tx.id in ambiguous
                    else "statement_only"
                    if tx.id in statement_only
                    else "not_checked"
                )
                rows.append(
                    {
                        "id": tx.id,
                        "date": tx.date_str,
                        "time": tx.time_str,
                        "counterparty": tx.counterparty,
                        "description": tx.description,
                        "amount": str(tx.amount),
                        "currency": self.config.operating_currency,
                        "platform": tx.platform.value,
                        "tx_type": tx.tx_type.value,
                        "dedup_status": tx.status.value,
                        "asset_account": classifier.determine_asset_account(
                            tx.platform.value, tx.counterparty
                        ),
                        "source_file": tx.source_file,
                        "reconciliation_status": status,
                        **copy.deepcopy(self._reviews[tx.id]),
                    }
                )
            ledger_accounts = self._ledger_accounts()
            available = (
                set(ledger_accounts)
                | set(classifier.platform_wallets.values())
                | {_TRANSFER_ACCOUNT}
            )
            available.update(classifier.candidate_accounts("expense"))
            available.update(classifier.candidate_accounts("income"))
            report = copy.deepcopy(self._report)
            differences = (
                sum(
                    report["counts"].get(key, 0)
                    for key in ("statement_only", "ledger_only", "ambiguous_groups")
                )
                if report
                else 0
            )
            return {
                "workspace": {
                    "name": "本地工作区"
                    if self.workspace.name in {"data", "workbench-data"}
                    else self.workspace.name,
                    "currency": self.config.operating_currency,
                    "has_statements": bool(self._inputs),
                    "has_ledger": self._ledger is not None,
                    "is_demo": self._is_demo,
                },
                "is_demo": self._is_demo,
                "counts": {
                    "transactions": len(rows),
                    "pending": sum(row["review_status"] == "pending" for row in rows),
                    "confirmed": sum(row["review_status"] == "confirmed" for row in rows),
                    "suggested": sum(row["suggested_account"] is not None for row in rows),
                    "matched": len(matched),
                    "differences": differences,
                    "duplicates": self._duplicates,
                },
                "transactions": rows,
                "accounts": {
                    "wallets": dict(classifier.platform_wallets),
                    "available": sorted(available),
                    "rules": {
                        "mode": self.config.accounts.rules_mode,
                        "merchant_overrides": dict(self.config.accounts.merchant_overrides),
                        "expense_rules": dict(self.config.accounts.expense_rules),
                        "income_rules": dict(self.config.accounts.income_rules),
                    },
                },
                "files": [
                    {"name": name, "count": len(item["transactions"])}
                    for name, item in self._inputs.items()
                ],
                "ledger": {
                    "loaded": self._ledger is not None,
                    "filename": self._ledger_name,
                    "entry_count": len(self._ledger.entries) if self._ledger else 0,
                    "transaction_count": sum(
                        type(entry).__name__ == "Transaction" for entry in self._ledger.entries
                    )
                    if self._ledger
                    else 0,
                    "accounts": ledger_accounts,
                    "source_files": [
                        str(path.relative_to(self._ledger_dir))
                        for path in self._ledger.source_files
                    ]
                    if self._ledger and self._ledger_dir
                    else [],
                    "report": report,
                },
            }

    def classify(self) -> None:
        with self._lock:
            eligible = [
                tx
                for tx in self._transactions
                if self._reviews[tx.id]["review_status"] == "pending"
                and self._reviews[tx.id]["classification_source"] == "fallback"
                and tx.tx_type != TransactionType.REFUND
                and not _is_transfer(tx)
            ]
            if not eligible:
                return
            reviews = copy.deepcopy(self._reviews)
            settings = replace(self.config.ai)
            if self._ledger:
                settings.allowed_accounts = [
                    account
                    for account in self._ledger_accounts()
                    if account.startswith(("Expenses:", "Income:")) and is_valid_account(account)
                ]
                if not settings.allowed_accounts:
                    for tx in eligible:
                        reviews[tx.id]["reason"] = (
                            "导入账簿没有可供模型选择的收支Open账户，请先配置账户"
                        )
                    self._commit(_reviews=reviews)
                    return
            explanations = {
                "unavailable": "本地模型服务不可用，保留原分类",
                "request_limit": "本轮已达到模型请求上限",
                "invalid_response": "模型返回无效，保留原分类",
                "no_candidates": "该方向没有可供模型选择的账户",
                "needs_review": "模型置信度不足，需要手动复核",
            }
            for suggestion in suggest_classifications(eligible, self._classifier(), settings):
                review = reviews[suggestion.transaction_id]
                review.update(
                    suggested_account=suggestion.suggested_account,
                    confidence=suggestion.confidence,
                    reason=suggestion.reason or explanations.get(suggestion.status, "等待人工确认"),
                )
                if suggestion.status == "suggested" and suggestion.suggested_account:
                    review.update(
                        account=suggestion.suggested_account, classification_source="model"
                    )
            self._commit(_reviews=reviews)

    def _reconciliation_report(self, date_window_days: int) -> dict[str, Any]:
        if self._ledger_root is None:
            raise ValueError("请先导入现有Beancount账簿")
        report = reconcile_transactions(
            self._transactions,
            self._ledger_root,
            self._classifier(),
            self.config.operating_currency,
            date_window_days,
        ).to_dict()
        matched = []
        for candidate in report["matched"]:
            statement = candidate["statement"]
            posting = candidate["ledger_posting"]
            counterparty = re.sub(r"\s+", "", statement.get("counterparty", "")).casefold()
            payee = re.sub(r"\s+", "", posting.get("payee", "")).casefold()
            if counterparty and payee and counterparty == payee:
                matched.append(candidate)
            else:
                report["ambiguous"].append(
                    {
                        "reason": "金额日期相同但交易对方不同，需人工核对",
                        "statements": [statement],
                        "ledger_postings": [posting],
                    }
                )
        report["matched"] = matched
        report["counts"]["matched"] = len(matched)
        report["counts"]["ambiguous_groups"] = len(report["ambiguous"])
        if report["scope"]["accounts"]:
            report["status"] = (
                "differences"
                if any(report[key] for key in ("statement_only", "ledger_only", "ambiguous"))
                else "matched"
            )
        return report

    def reconcile(self, date_window_days: int = 0) -> None:
        with self._lock:
            if not self._ledger_root:
                raise ValueError("请先导入现有Beancount账簿")
            if not self._transactions:
                raise ValueError("请先导入需要核对的账单")
            report = self._reconciliation_report(date_window_days)
            self._commit(_report=report)

    def update_transactions(
        self, ids: list[str], account: str | None = None, confirm: bool = True
    ) -> None:
        with self._lock:
            if (
                not isinstance(ids, list)
                or not ids
                or any(not isinstance(item, str) for item in ids)
            ):
                raise ValueError("请选择至少一笔交易")
            if not isinstance(confirm, bool):
                raise ValueError("确认状态必须为布尔值")
            if any(tx_id not in self._reviews for tx_id in ids):
                raise ValueError("交易ID不存在，未修改任何交易")
            if account is not None and not is_valid_account(account):
                raise ValueError("账户名不符合Beancount语法")
            reviews = copy.deepcopy(self._reviews)
            by_id = {tx.id: tx for tx in self._transactions}
            classifier = self._classifier()
            for tx_id in set(ids):
                tx, review = by_id[tx_id], reviews[tx_id]
                if account is not None:
                    if account == classifier.determine_asset_account(
                        tx.platform.value, tx.counterparty
                    ):
                        raise ValueError("对方账户不能与本笔交易的资金账户相同")
                    review.update(
                        account=account,
                        classification_source="manual",
                        suggested_account=None,
                        confidence=None,
                        reason="手动选择账户",
                    )
                if confirm and review["account"] == classifier.determine_asset_account(
                    tx.platform.value, tx.counterparty
                ):
                    raise ValueError("对方账户不能与本笔交易的资金账户相同")
                if (
                    confirm
                    and _is_transfer(tx)
                    and not review["account"].startswith(("Assets:", "Liabilities:"))
                ):
                    raise ValueError("转账必须选择实际的资产或负债对方账户后才能确认")
                review["review_status"] = "confirmed" if confirm else "pending"
                if confirm:
                    review["reason"] = "已由你确认"
            self._commit(_reviews=reviews)

    def update_accounts(self, wallets: dict[str, str]) -> None:
        with self._lock:
            if not isinstance(wallets, dict) or any(
                key not in {platform.value for platform in Platform} for key in wallets
            ):
                raise ValueError("无效的平台钱包配置")
            accounts = replace(
                self.config.accounts,
                platform_wallets={**self.config.accounts.platform_wallets, **wallets},
            )
            self._classifier(accounts)
            reviews = copy.deepcopy(self._reviews)
            for tx in self._transactions:
                if tx.platform.value in wallets:
                    reviews[tx.id]["review_status"] = "pending"
                    reviews[tx.id]["reason"] = "资金账户已更改，请重新确认"
            self._commit(accounts=accounts, _reviews=reviews, _report=None)

    def update_rules(
        self,
        mode: str = "append",
        merchant_overrides: dict[str, str] | None = None,
        expense_rules: dict[str, str] | None = None,
        income_rules: dict[str, str] | None = None,
    ) -> None:
        with self._lock:
            accounts = replace(
                self.config.accounts,
                rules_mode=mode,
                merchant_overrides=dict(merchant_overrides or {}),
                expense_rules=dict(expense_rules or {}),
                income_rules=dict(income_rules or {}),
            )
            classifier = self._classifier(accounts)
            reviews = copy.deepcopy(self._reviews)
            for tx in self._transactions:
                review = reviews[tx.id]
                if (
                    review["review_status"] == "pending"
                    and review["classification_source"] != "manual"
                ):
                    reviews[tx.id] = self._initial_review(tx, classifier)
            self._commit(accounts=accounts, _reviews=reviews)

    def _statement_entries(self, transactions: list[Transaction]) -> list[Any]:
        from beancount.core import amount, data

        entries = []
        classifier = self._classifier()
        currency = self.config.operating_currency
        for index, tx in enumerate(transactions):
            review = self._reviews[tx.id]
            opposing = review["account"]
            asset = classifier.determine_asset_account(tx.platform.value, tx.counterparty)
            if not is_valid_account(opposing) or not is_valid_account(asset):
                raise ValueError("导出含无效账户")
            meta = data.new_metadata(
                "<workbench>",
                index + 1,
                {
                    "id": tx.id,
                    "source_file": tx.source_file,
                    "platform": tx.platform.value,
                    "classification": review["classification_source"],
                    "review_status": review["review_status"],
                },
            )
            if tx.raw_status:
                meta["raw_status"] = tx.raw_status
            if review["confidence"] is not None:
                meta["confidence"] = Decimal(str(review["confidence"]))
            entries.append(
                data.Transaction(
                    meta,
                    tx.datetime.date(),
                    "*" if review["review_status"] == "confirmed" else "!",
                    tx.counterparty or None,
                    tx.description,
                    frozenset(tx.tags),
                    frozenset(),
                    [
                        data.Posting(
                            asset,
                            amount.Amount(tx.amount, currency),
                            None,
                            None,
                            None,
                            data.new_metadata("<workbench>", index + 1),
                        ),
                        data.Posting(
                            opposing,
                            amount.Amount(-tx.amount, currency),
                            None,
                            None,
                            None,
                            data.new_metadata("<workbench>", index + 1),
                        ),
                    ],
                )
            )
        return entries

    def export(
        self, kind: str = "standalone", only_confirmed: bool = False, exclude_matched: bool = False
    ) -> dict[str, Any]:
        with self._lock:
            if kind not in {"standalone", "fragment", "native"}:
                raise ValueError("不支持的导出类型")
            if not isinstance(only_confirmed, bool) or not isinstance(exclude_matched, bool):
                raise ValueError("导出筛选参数必须为布尔值")
            from beancount.core import data
            from beancount.parser import parser, printer

            warnings = []
            if self._is_demo:
                warnings.append("这是合成示例数据，请勿导入真实账簿")
            with tempfile.TemporaryDirectory(
                prefix="workbench-export-", dir=self.workspace
            ) as directory:
                target = Path(directory) / "export.beancount"
                if kind == "native":
                    if not self._ledger:
                        raise ValueError("请先导入现有Beancount账簿")
                    content = export_beancount(self._ledger, target)
                    count = sum(
                        isinstance(entry, data.Transaction) for entry in self._ledger.entries
                    )
                    warnings.append("原生导出仅保留已导入账簿，不会合并新账单交易")
                else:
                    matched = self._matched_ids() if exclude_matched else set()
                    transactions = [
                        tx
                        for tx in self._transactions
                        if tx.id not in matched
                        and (
                            not only_confirmed
                            or self._reviews[tx.id]["review_status"] == "confirmed"
                        )
                    ]
                    entries = self._statement_entries(transactions)
                    earliest = min(
                        (tx.datetime.date() for tx in transactions), default=date(1970, 1, 1)
                    )
                    names = sorted(
                        {posting.account for entry in entries for posting in entry.postings}
                    )
                    opens = [
                        data.Open(
                            data.new_metadata("<workbench>", 0),
                            earliest,
                            name,
                            [self.config.operating_currency],
                            None,
                        )
                        for name in names
                    ]
                    _, _, options = parser.parse_string("")
                    options.update(
                        title="Financial Beancount Workbench",
                        operating_currency=[self.config.operating_currency],
                    )
                    content = export_beancount(
                        BeancountLedger(opens + entries, options, ()), target
                    )
                    if kind == "fragment":
                        output = io.StringIO()
                        entry_printer = printer.EntryPrinter()
                        entry_printer.dformat = entry_printer.dformat_max = _ExactFormatter()
                        for entry in entries:
                            output.write(entry_printer(entry) + "\n")
                        content = output.getvalue()
                    count = len(transactions)
                    pending = sum(
                        self._reviews[tx.id]["review_status"] == "pending" for tx in transactions
                    )
                    if pending:
                        warnings.append(f"包含{pending}笔待确认交易，以!标记导出")
                    if matched:
                        warnings.append(f"已排除{len(matched)}笔与现有账簿匹配的交易")
            return {
                "content": content,
                "filename": f"financial-beancount-{kind}.beancount",
                "transaction_count": count,
                "warnings": warnings,
            }

    def reset(self) -> None:
        """Clear the active review session; retain all uploaded/source copies.

        A custom workspace can already contain unrelated files. Physical cleanup
        must be a separate, explicitly scoped action, never a recursive reset.
        """
        with self._lock:
            self._commit(
                _inputs={},
                _transactions=[],
                _reviews={},
                _duplicates=0,
                _ledger=None,
                _ledger_root=None,
                _ledger_dir=None,
                _ledger_name=None,
                _report=None,
                _is_demo=False,
            )

    def load_demo(self) -> None:
        with self._lock:
            accounts = AccountConfig(merchant_overrides={"示例·需自选": "Expenses:Unknown"})
            definitions = [
                ("示例·拾页", "科学读物一本", "-58.00", TransactionType.EXPENSE),
                ("示例·小巷", "午间套餐一份", "-32.50", TransactionType.EXPENSE),
                ("示例·轨道", "轨道出行单程票", "-6.00", TransactionType.EXPENSE),
                ("示例·某雇主", "本月劳动报酬", "12000.00", TransactionType.INCOME),
                ("星巴克", "咖啡饮品", "-25.00", TransactionType.EXPENSE),
                ("示例·需自选", "个人用途待选择", "-88.00", TransactionType.EXPENSE),
                ("示例·本人", "转账到银行卡", "-200.00", TransactionType.TRANSFER),
                ("示例·商户退回", "退款入账", "15.00", TransactionType.REFUND),
            ]
            transactions = [
                Transaction(
                    id=f"demo-{index + 1}",
                    platform=Platform.ALIPAY,
                    datetime=datetime(2024, 1, 15 + index, 12),
                    amount=Decimal(number),
                    counterparty=merchant,
                    description=description,
                    tx_type=kind,
                    source_file="合成示例账单.csv",
                    raw_status="支付成功",
                )
                for index, (merchant, description, number, kind) in enumerate(definitions)
            ]
            root, staged = self._stage(
                [
                    {
                        "name": "main.beancount",
                        "content": base64.b64encode(
                            """option "title" "合成示例账簿"
option "operating_currency" "CNY"
2024-01-01 open Assets:Current:Digital:Alipay CNY
2024-01-01 open Assets:Current:Bank:Checking CNY
2024-01-01 open Expenses:Education:Books CNY
2024-01-01 open Expenses:Food:Restaurant CNY
2024-01-01 open Expenses:Transport:Public CNY
2024-01-01 open Income:Salary:Gross CNY
2024-01-15 * "示例·拾页" "已在账簿中登记"
  Assets:Current:Digital:Alipay -58.00 CNY
  Expenses:Education:Books 58.00 CNY
2024-01-16 * "示例·账簿独有" "账单没有的记录"
  Assets:Current:Digital:Alipay -7.25 CNY
  Expenses:Food:Restaurant 7.25 CNY
""".encode()
                        ).decode(),
                    }
                ],
                "ledger",
            )
            try:
                demo_path = root / "synthetic-statements.json"
                demo_path.write_text(
                    json.dumps([_serialize_tx(tx) for tx in transactions], ensure_ascii=False),
                    encoding="utf-8",
                )
                inputs = {
                    "合成示例账单.csv": {
                        "path": str(demo_path.relative_to(self.workspace)),
                        "transactions": transactions,
                    }
                }
                txs, reviews, duplicates = self._process(inputs, accounts, preserve_reviews=False)
                ledger = import_beancount(staged["main.beancount"])
                self._commit(
                    accounts=accounts,
                    currency="CNY",
                    _inputs=inputs,
                    _transactions=txs,
                    _reviews=reviews,
                    _duplicates=duplicates,
                    _ledger=ledger,
                    _ledger_root=staged["main.beancount"],
                    _ledger_dir=root,
                    _ledger_name="main.beancount",
                    _report=None,
                    _is_demo=True,
                )
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise
