"""
Beancount output validation.

Optionally validates generated .beancount files using the ``beancount`` parser.
Requires ``beancount>=3.0`` to be installed (optional dependency).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of validating a Beancount file."""

    filepath: str
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    num_entries: int = 0


def validate_beancount_file(filepath: str | Path) -> ValidationResult:
    """
    Validate a .beancount file using the Beancount parser.

    Requires ``beancount>=3.0`` to be installed.
    Returns a ValidationResult indicating success/failure.
    """
    filepath = Path(filepath)
    result = ValidationResult(filepath=str(filepath))

    if not filepath.exists():
        result.valid = False
        result.errors.append(f"File does not exist: {filepath}")
        return result

    try:
        from beancount import loader
    except ImportError:
        result.warnings.append(
            "beancount package not installed — skipping validation. "
            "Install with: pip install beancount"
        )
        return result

    try:
        entries, errors, _options = loader.load_file(str(filepath))
        result.num_entries = len(entries)

        if errors:
            result.valid = False
            for error in errors:
                msg = f"{error.source.filename}:{error.source.line} — {error.message}"
                result.errors.append(msg)
                logger.debug("Beancount validation error: %s", msg)
        else:
            logger.debug(
                "Beancount validation passed: %d entries in %s",
                result.num_entries,
                filepath,
            )

    except Exception as exc:
        result.valid = False
        result.errors.append(f"Failed to parse file: {exc}")

    return result


def validate_beancount_text(text: str) -> ValidationResult:
    """
    Validate Beancount text content using the Beancount parser.

    Requires ``beancount>=3.0`` to be installed.
    """
    result = ValidationResult(filepath="<string>")

    try:
        from beancount import loader
    except ImportError:
        result.warnings.append("beancount package not installed — skipping validation.")
        return result

    try:
        # beancount.loader.load_string is not available in all versions
        # Use load_file with a temporary file as fallback
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".beancount", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name

        try:
            entries, errors, _options = loader.load_file(tmp_path)
            result.num_entries = len(entries)

            if errors:
                result.valid = False
                for error in errors:
                    msg = f"{error.source.filename}:{error.source.line} — {error.message}"
                    result.errors.append(msg)
        finally:
            os.unlink(tmp_path)

    except Exception as exc:
        result.valid = False
        result.errors.append(f"Failed to parse content: {exc}")

    return result
