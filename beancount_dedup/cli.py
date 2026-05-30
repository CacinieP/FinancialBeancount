"""
Command-line interface for Beancount Dedup Tool.

Provides the ``beancount-dedup`` console script entry point.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import AutoConverter, DeduplicationEngine
from .exporters.beancount import BeancountExporter
from .models import Transaction
from .parsers.alipay_parser import AlipayParser, AlipayParserV2, AlipayParserV3
from .parsers.bank_parser import BankParser, CMBParser, ICBCParser
from .parsers.base import AutoParser
from .parsers.unionpay_parser import UnionPayParser
from .parsers.wechat_parser import WechatParser
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


def process_input_folder(
    input_dir: Path | None = None,
    output_dir: Path | None = None,
    incremental: bool = False,
) -> None:
    """
    Automatically process all statement files in the input folder.

    Supported formats: CSV, XLSX, XLS, PDF.
    Supported platforms: Alipay, WeChat Pay, bank cards, UnionPay.

    When *incremental* is True, loads a previous state file so that
    already-processed files (unchanged mtime) and transactions whose
    L1 fingerprint was seen before are skipped.
    """
    input_dir = input_dir or INPUT_DIR
    output_dir = output_dir or OUTPUT_DIR
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

    if not input_dir.exists():
        logger.error("input folder does not exist: %s", input_dir)
        logger.info("Please create the folder and place statement files in it.")
        return

    supported_extensions = [".csv", ".xlsx", ".xls", ".pdf"]
    input_files: list[Path] = []
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

            output_csv = str(converted_dir / f"{filepath.stem}.csv")
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

    auto_parser = AutoParser()
    auto_parser.register(AlipayParser())
    auto_parser.register(AlipayParserV2())
    auto_parser.register(AlipayParserV3())
    auto_parser.register(WechatParser())
    auto_parser.register(CMBParser())
    auto_parser.register(ICBCParser())
    auto_parser.register(UnionPayParser())
    auto_parser.register(BankParser())

    all_transactions: list[Transaction] = []

    for filepath in csv_files:
        logger.info("\n[CSV] %s", filepath.name)
        result = auto_parser.parse(str(filepath))
        logger.info("  %s", result)
        all_transactions.extend(result.transactions)

    if convert_files:
        for filepath, conv_result in conversion_results:
            if conv_result.success:
                csv_path = conv_result.output_path
                csv_name = Path(csv_path).name
                logger.info("\n[CSV] %s (from %s)", csv_name, filepath.name)
                result = auto_parser.parse(csv_path)
                logger.info("  %s", result)
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
    exporter = BeancountExporter()

    output_file = str(output_dir / "output.beancount")
    exporter.export(unique_txs, output_path=output_file)
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
    parser.add_argument("-o", "--output", help="Output file path")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--demo", action="store_true", help="Run demo with sample data")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Skip already-processed files and transactions (state persisted in output/.dedup_state.json)",
    )

    args = parser.parse_args(argv)
    _setup_logging(verbose=args.verbose)

    if args.demo:
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
    elif args.alipay or args.wechat or args.bank or args.unionpay:
        from example_usage import parse_real_files  # type: ignore[import-not-found]

        parse_real_files(
            args.alipay, args.wechat, args.bank, args.output, unionpay_file=args.unionpay
        )
    else:
        logger.info("Auto-processing files from input/ folder")
        logger.info("Use --help for more options\n")
        process_input_folder(incremental=args.incremental)


if __name__ == "__main__":
    main()
