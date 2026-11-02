"""
Command-line interface for Beancount Dedup Tool.

Provides the ``beancount-dedup`` console script entry point.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

from . import AutoConverter, DeduplicationEngine
from .account_classifier import BeancountAccountClassifier
from .config import AppConfig, load_config
from .exporters.beancount import BeancountExporter
from .models import Transaction
from .parsers.strict import build_strict_parser
from .state import StateStore

logger = logging.getLogger(__name__)

INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )


def _ensure_output_dir(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _protect_paths(targets: list[Path], sources: set[Path]) -> None:
    """Refuse source replacement and collisions between generated artifacts."""
    for index, target in enumerate(targets):
        others = sources | {p.resolve() for p in targets[:index]}
        if target.resolve() in others or (
            target.exists() and any(p.exists() and target.samefile(p) for p in others)
        ):
            raise ValueError("Output paths must be distinct and must not overwrite input files")


def process_input_folder(
    input_dir: Path | None = None,
    output_dir: Path | None = None,
    incremental: bool = False,
    config: AppConfig | None = None,
    *,
    ai_classify: bool = False,
    ledger_path: str | None = None,
    date_window_days: int = 0,
    statement_files: list[Path] | None = None,
    output_path: Path | None = None,
    fragment: bool = False,
) -> None:
    """
    Automatically process all statement files in the input folder.

    Supported formats: CSV, XLSX, XLS, PDF.
    Supported platforms: Alipay, WeChat Pay, bank cards, UnionPay.

    When *incremental* is True, loads a previous state file so that
    already-processed files (unchanged mtime) and transactions whose
    L1 fingerprint was seen before are skipped.
    """
    config = config or AppConfig()
    # Validate account configuration before reading or writing user files.
    classifier = BeancountAccountClassifier(config.accounts)
    if ai_classify:
        from .ai import validate_ai_config

        validate_ai_config(config.ai)
    ledger_sources: set[Path] = set()
    if ledger_path:
        from .beancount_io import import_beancount

        ledger_sources.update(import_beancount(ledger_path).source_files)
    input_dir = input_dir or Path(config.input_dir)
    output_dir = output_dir or Path(config.output_dir)
    _ensure_output_dir(output_dir)

    # ── Incremental state ─────────────────────────────────────────────
    state: StateStore | None = None
    if incremental:
        state = StateStore(path=output_dir / ".dedup_state.json")
        state.load()
        logger.info(
            "Incremental mode: %d previously processed files, %d fingerprints",
            state.file_count,
            state.fingerprint_count,
        )

    converted_dir = output_dir / "converted"
    converted_dir.mkdir(parents=True, exist_ok=True)

    if statement_files is None and not input_dir.exists():
        logger.error("input folder does not exist: %s", input_dir)
        logger.info("Please create the folder and place statement files in it.")
        return

    supported_extensions = [".csv", ".xlsx", ".xls", ".pdf"]
    input_files: list[Path] = list(statement_files or [])
    if statement_files is None:
        for ext in supported_extensions:
            input_files.extend(input_dir.glob(f"*{ext}"))

    if not input_files:
        logger.error("No statement files found in %s", input_dir)
        logger.info("Supported formats: %s", ", ".join(supported_extensions))
        return

    # Filter out already-processed files when in incremental mode.
    if incremental and state is not None:
        skipped = [f for f in input_files if state.is_file_processed(f)]
        input_files = [f for f in input_files if not state.is_file_processed(f)]
        if skipped:
            logger.info(
                "Skipping %d already-processed file(s): %s",
                len(skipped),
                ", ".join(f.name for f in skipped),
            )

    csv_files = [f for f in input_files if f.suffix.lower() == ".csv"]
    convert_files = [f for f in input_files if f.suffix.lower() != ".csv"]

    logger.info("=" * 60)
    logger.info("File scan results")
    logger.info("=" * 60)
    logger.info("  CSV files: %d", len(csv_files))
    logger.info("  Need conversion: %d", len(convert_files))
    logger.info("")

    # Step 1: convert non-CSV files
    conversion_results: list[tuple[Path, object]] = []
    if convert_files:
        logger.info("=" * 60)
        logger.info("Step 1/3: Format conversion")
        logger.info("=" * 60)

        converter = AutoConverter()
        for filepath in convert_files:
            ext = filepath.suffix.lower()
            logger.info("\n[%s] %s", ext.upper(), filepath.name)

            key = hashlib.sha256(str(filepath.resolve()).encode()).hexdigest()[:12]
            output_csv = str(converted_dir / f"{filepath.stem}-{key}.csv")
            _protect_paths([Path(output_csv)], {p.resolve() for p in input_files} | ledger_sources)
            conv_result = converter.convert(str(filepath), output_path=output_csv)
            conversion_results.append((filepath, conv_result))

            if conv_result.success:
                logger.info(
                    "  [OK] Conversion succeeded: %d rows -> %s",
                    conv_result.rows_converted,
                    Path(output_csv).name,
                )
            else:
                logger.error("  [FAIL] Conversion failed")
                for err in conv_result.errors:
                    logger.error("    - %s", err)

        success_count = sum(1 for _, r in conversion_results if r.success)
        logger.info("\nConversion done: %d/%d succeeded", success_count, len(convert_files))

    # Step 2: parse all CSV files
    logger.info("\n" + "=" * 60)
    logger.info("Step 2/3: Parse statements")
    logger.info("=" * 60)

    auto_parser = build_strict_parser()

    all_transactions: list[Transaction] = []

    for filepath in csv_files:
        logger.info("\n[CSV] %s", filepath.name)
        result = auto_parser.parse(str(filepath))
        logger.info("  %s", result)
        if result.errors:
            raise ValueError(f"Could not safely import {filepath}: " + "; ".join(result.errors))
        all_transactions.extend(result.transactions)

    if convert_files:
        for filepath, conv_result in conversion_results:
            if conv_result.success:
                csv_path = conv_result.output_path
                csv_name = Path(csv_path).name
                logger.info("\n[CSV] %s (from %s)", csv_name, filepath.name)
                result = auto_parser.parse(csv_path)
                logger.info("  %s", result)
                if result.errors:
                    raise ValueError(
                        f"Could not safely import {filepath}: " + "; ".join(result.errors)
                    )
                all_transactions.extend(result.transactions)

    if not all_transactions:
        logger.error("\nNo transactions were parsed successfully.")
        return

    # Step 3: deduplicate and export
    logger.info("\n" + "=" * 60)
    logger.info("Step 3/3: Deduplication")
    logger.info("=" * 60)
    logger.info("\n%d transactions to process...", len(all_transactions))

    engine = DeduplicationEngine()

    if incremental and state is not None:
        seen = state.get_seen_fingerprints()
        engine.add_transactions_incremental(all_transactions, seen)
        # The set `seen` was mutated in-place with new fingerprints.
    else:
        engine.add_transactions(all_transactions)

    report = engine.generate_report()
    logger.info("\n%s", report)

    unique_txs = engine.get_unique_transactions()
    if ledger_path:
        from .reconciliation import reconcile_transactions

        reconciliation = reconcile_transactions(
            unique_txs,
            ledger_path,
            classifier=classifier,
            currency=config.operating_currency,
            date_window_days=date_window_days,
        ).to_dict()
        logger.info(
            "Ledger reconciliation: %s — %s", reconciliation["status"], reconciliation["counts"]
        )

    suggestions = None
    if ai_classify:
        from .ai import suggest_classifications

        suggestions = suggest_classifications(unique_txs, classifier, config.ai)
        logger.info(
            "Local model classification report: %s", output_dir / "classification_report.json"
        )
    exporter = BeancountExporter(
        accounts=config.accounts,
        suggestions=suggestions,
        currency=config.operating_currency,
    )

    output_file = str(output_path or output_dir / "output.beancount")
    source_paths = {p.resolve() for p in input_files} | ledger_sources
    if ledger_path:
        source_paths.add(Path(ledger_path).resolve())
        source_paths.update(Path(p).resolve() for p in reconciliation["scope"]["source_files"])
    targets = [Path(output_file), output_dir / "duplicate_report.beancount"]
    if ledger_path:
        targets.append(output_dir / "reconciliation_report.json")
    if suggestions is not None:
        targets.append(output_dir / "classification_report.json")
    _protect_paths(targets, source_paths)
    if ledger_path:
        (output_dir / "reconciliation_report.json").write_text(
            json.dumps(reconciliation, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if suggestions is not None:
        (output_dir / "classification_report.json").write_text(
            json.dumps(
                {
                    "model": config.ai.model,
                    "review_required": True,
                    "suggestions": [s.to_dict() for s in suggestions],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    exporter.export(
        unique_txs,
        output_path=output_file,
        include_meta=config.include_metadata,
        include_open_entries=not fragment,
    )
    logger.info("\nDeduplicated transactions exported to: %s", output_file)

    report_file = str(output_dir / "duplicate_report.beancount")
    exporter.export_duplicate_report(engine.processed, output_path=report_file)
    logger.info("Duplicate report exported to: %s", report_file)

    # ── Persist state ──────────────────────────────────────────────────
    if incremental and state is not None:
        processed_files = csv_files + [fp for fp, cr in conversion_results if cr.success]
        new_fps = engine.get_l1_fingerprints()
        for fp in processed_files:
            state.mark_processed(fp, new_fps)
        state.save()
        logger.info("Incremental state saved (%d fingerprints total)", state.fingerprint_count)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``beancount-dedup``."""
    parser = argparse.ArgumentParser(
        prog="beancount-dedup",
        description="Beancount multi-platform statement deduplication tool",
    )
    parser.add_argument("--alipay", help="Alipay statement file path")
    parser.add_argument("--wechat", help="WeChat Pay statement file path")
    parser.add_argument("--bank", help="Bank statement file path")
    parser.add_argument("--unionpay", help="UnionPay (云闪付) statement file path")
    parser.add_argument(
        "--beancount", help="Import a native Beancount ledger and export a validated new file"
    )
    parser.add_argument(
        "--fragment",
        action="store_true",
        help="Export transactions without open directives, for inclusion in an existing ledger",
    )
    parser.add_argument("-o", "--output", help="Output file path")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--demo", action="store_true", help="Run demo with sample data")
    parser.add_argument(
        "--ai-classify",
        action="store_true",
        help="Suggest unmatched categories with a local model; review ! entries before import",
    )
    parser.add_argument(
        "--model-file",
        help="Local GGUF file; automatically starts/stops llama-server and enables AI classification",
    )
    parser.add_argument("--ledger", help="Existing Beancount ledger to reconcile (read-only)")
    parser.add_argument(
        "--date-window-days",
        type=int,
        default=0,
        help="Reconciliation date tolerance (default: same day)",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Skip already-processed files and transactions (state persisted in output/.dedup_state.json)",
    )

    parser.add_argument(
        "--config",
        help="YAML config file (default: search ./beancount-dedup.yaml, "
        "./beancount-dedup.yml, ./.beancount-dedup.yaml, ~/.beancount-dedup/config.yaml)",
    )

    args = parser.parse_args(argv)
    _setup_logging(verbose=args.verbose)

    if args.config and not Path(args.config).exists():
        parser.error(f"config file not found: {args.config}")
    try:
        config = load_config(args.config)
    except (ValueError, TypeError) as exc:
        parser.error(f"invalid configuration: {exc}")
    if args.date_window_days < 0:
        parser.error("--date-window-days must be non-negative")
    if args.demo and (args.ai_classify or args.model_file or args.ledger):
        parser.error("--demo cannot be combined with model or ledger processing")
    for filepath in (
        args.alipay,
        args.wechat,
        args.bank,
        args.unionpay,
        args.beancount,
        args.ledger,
        args.model_file,
    ):
        if filepath and not Path(filepath).is_file():
            parser.error(f"file not found: {filepath}")

    if args.beancount:
        if any(
            (
                args.alipay,
                args.wechat,
                args.bank,
                args.unionpay,
                args.ledger,
                args.ai_classify,
                args.model_file,
                args.incremental,
                args.demo,
                args.fragment,
            )
        ):
            parser.error(
                "--beancount round-trip is a separate workflow; use --ledger to reconcile statements"
            )
        from .beancount_io import export_beancount, import_beancount

        try:
            target = (
                Path(args.output) if args.output else Path(config.output_dir) / "imported.beancount"
            )
            ledger = import_beancount(args.beancount)
            export_beancount(ledger, target)
            logger.info("Native Beancount ledger exported to: %s", target)
        except (ValueError, OSError, RuntimeError) as exc:
            parser.exit(2, f"beancount-dedup: {exc}\n")
    elif args.demo:
        logger.info("Running demo with sample data...")
        # Import demo functions from example_usage for backward compatibility
        from example_usage import (  # type: ignore[import-not-found]
            demo_basic_usage,
            demo_continuous_same_amount,
            demo_export_beancount,
            demo_internal_transfer,
            demo_l3_fuzzy_match,
        )

        demo_basic_usage()
        demo_l3_fuzzy_match()
        demo_continuous_same_amount()
        demo_internal_transfer()
        demo_export_beancount()
    else:
        files = [Path(f) for f in (args.alipay, args.wechat, args.bank, args.unionpay) if f]
        output_path = Path(args.output) if args.output else None
        kwargs = {
            "incremental": args.incremental,
            "ai_classify": args.ai_classify or bool(args.model_file),
            "ledger_path": args.ledger,
            "date_window_days": args.date_window_days,
            "statement_files": files or None,
            "output_path": output_path,
            "output_dir": output_path.parent if output_path else None,
            "fragment": args.fragment,
        }
        try:
            if args.model_file:
                from dataclasses import replace

                from .model_runtime import managed_local_model

                with managed_local_model(args.model_file, config.ai) as local_config:
                    process_input_folder(config=replace(config, ai=local_config), **kwargs)
            else:
                process_input_folder(config=config, **kwargs)
        except (ValueError, OSError, RuntimeError) as exc:
            parser.exit(2, f"beancount-dedup: {exc}\n")


if __name__ == "__main__":
    main()
