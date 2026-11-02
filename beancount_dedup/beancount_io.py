"""Native Beancount import/export without flattening transactions into statements.

Includes are expanded into one new file. Directives, posting structure, options
and user metadata survive; comments, source locations and formatting do not.
No user plugin, loader cache or document-directory discovery is executed.
"""

from __future__ import annotations

import copy
import glob
import io
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


class BeancountIOError(ValueError):
    """A ledger could not be safely imported or exported without semantic loss."""


@dataclass
class BeancountLedger:
    """Native parsed directives and effective options, retaining every source."""

    entries: list[Any]
    options: dict[str, Any]
    source_files: tuple[Path, ...]


def _beancount_modules():
    try:
        from beancount import loader
        from beancount.core import data
        from beancount.ops import balance, pad, validation
        from beancount.parser import booking, options, parser, printer
    except ImportError as exc:
        raise BeancountIOError(
            "Beancount import/export requires financial-beancount[validate]."
        ) from exc
    return loader, data, balance, pad, validation, booking, options, parser, printer


def _raise_errors(errors: list[Any]) -> None:
    if errors:
        messages = []
        for error in errors:
            source = getattr(error, "source", None) or {}
            messages.append(
                f"{source.get('filename', '<ledger>')}:{source.get('lineno', 0)}: {error.message}"
            )
        raise BeancountIOError("Invalid Beancount ledger:\n" + "\n".join(messages))


def _validate(entries: list[Any], settings: dict[str, Any]) -> None:
    _, data, balance, pad, validation, booking, _, _, _ = _beancount_modules()
    # Booking and built-in passes may attach metadata and interpolate postings.
    # Never expose those transformations as replacements of the raw directives.
    checked = copy.deepcopy(entries)
    checked.sort(key=data.entry_sortkey)
    checked, errors = booking.book(checked, settings)
    if settings.get("plugin_processing_mode", "default") != "raw":
        checked, pad_errors = pad.pad(checked, settings)
        errors.extend(pad_errors)
        checked, balance_errors = balance.check(checked, settings)
        errors.extend(balance_errors)
    errors.extend(validation.validate(checked, settings))
    _raise_errors(errors)


def import_beancount(path: str | Path) -> BeancountLedger:
    """Read and validate a ledger/includes without executing plugins or caches."""
    loader, _, _, _, _, _, _, parser, _ = _beancount_modules()
    root = Path(path).expanduser().resolve()
    pending = [root]
    sources: list[Path] = []
    entries: list[Any] = []
    source_options: list[dict[str, Any]] = []
    while pending:
        source = pending.pop(0)
        if source in sources:
            raise BeancountIOError(f"Duplicate or cyclic Beancount include: {source}")
        if not source.is_file():
            raise BeancountIOError(f"Beancount source file not found: {source}")
        try:
            parsed, errors, settings = parser.parse_file(
                str(source), report_filename=str(source), encoding="utf-8"
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise BeancountIOError(f"Could not read Beancount source: {source}") from exc
        _raise_errors(errors)
        if settings.get("plugin"):
            raise BeancountIOError(
                "User plugins are not executed during Beancount import/export; "
                "export a materialized ledger without plugin directives first."
            )
        sources.append(source)
        entries.extend(parsed)
        source_options.append(settings)
        for pattern in settings["include"]:
            search = Path(pattern)
            if not search.is_absolute():
                search = source.parent / search
            matches = sorted(glob.glob(str(search), recursive=True))
            if not matches:
                raise BeancountIOError(f"Beancount include matches no files: {pattern}")
            pending.extend(Path(match).resolve() for match in matches)
    settings = loader.aggregate_options_map(source_options[0], source_options[1:])
    settings["include"] = [str(source) for source in sources]
    # Document directives are made absolute by the native parser. Directory
    # options need the same treatment before moving the consolidated ledger.
    settings["documents"] = [
        str((root.parent / directory).resolve()) for directory in settings["documents"]
    ]
    _validate(entries, settings)
    return BeancountLedger(entries, settings, tuple(sources))


def _escape(value: str) -> str:
    from beancount.utils.misc_utils import escape_string

    return str(escape_string(value))


def _render_options(settings: dict[str, Any]) -> str:
    _, _, _, _, _, _, options, _, _ = _beancount_modules()
    lines = []
    public_names = {
        descriptor.name for group in options.PUBLIC_OPTION_GROUPS for descriptor in group.options
    }
    for name, descriptor in options.OPTIONS.items():
        if name not in public_names or descriptor.alias:
            continue
        value = settings.get(name, descriptor.default_value)
        if value == descriptor.default_value:
            continue
        values = (
            [f"{key}:{number}" for key, number in value.items()]
            if isinstance(value, dict)
            else value
            if isinstance(value, list)
            else [value]
        )
        for item in values:
            rendered = item
            if isinstance(item, Enum):
                rendered = item.name
            elif isinstance(item, bool):
                rendered = "TRUE" if item else "FALSE"
            lines.append(f'option "{name}" "{_escape(str(rendered))}"')
    return "\n".join(lines)


def _printable_entry(entry: Any) -> Any:
    """Escape strings missing escaping in Beancount 3's directive printer."""
    from beancount.core import data

    if isinstance(entry, data.Transaction):
        postings = [
            posting._replace(cost=posting.cost._replace(label=_escape(posting.cost.label)))
            if posting.cost is not None and posting.cost.label
            else posting
            for posting in entry.postings
        ]
        entry = entry._replace(postings=postings)
    elif isinstance(entry, data.Note):
        entry = entry._replace(comment=_escape(entry.comment))
    elif isinstance(entry, data.Document):
        entry = entry._replace(filename=_escape(entry.filename))
    elif isinstance(entry, data.Event):
        entry = entry._replace(type=_escape(entry.type), description=_escape(entry.description))
    elif isinstance(entry, data.Query):
        entry = entry._replace(name=_escape(entry.name), query_string=_escape(entry.query_string))
    elif isinstance(entry, data.Custom):
        values = [
            value._replace(value=_escape(value.value)) if value.dtype is str else value
            for value in entry.values
        ]
        entry = entry._replace(type=_escape(entry.type), values=values)
    return entry


def _canonical(value: Any) -> Any:
    """Compare all user metadata too, disregarding only parser source location."""
    if isinstance(value, Decimal):
        # Scale affects Beancount's inferred tolerance, so preserve it too.
        return ("Decimal", value.as_tuple())
    if isinstance(value, dict):
        return tuple(
            (key, _canonical(item))
            for key, item in sorted(value.items())
            if key not in {"filename", "lineno"}
        )
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, tuple(_canonical(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return frozenset(_canonical(item) for item in value)
    return value


def _semantic_options(settings: dict[str, Any]) -> dict[str, Any]:
    _, _, _, _, _, _, options, _, _ = _beancount_modules()
    public_names = {
        descriptor.name for group in options.PUBLIC_OPTION_GROUPS for descriptor in group.options
    }
    return {
        name: settings.get(name, descriptor.default_value)
        for name, descriptor in options.OPTIONS.items()
        if name in public_names and not descriptor.alias
    }


class _ExactFormatter:
    @staticmethod
    def format(number: Decimal, currency: str | None = None) -> str:
        """Keep each number's original scale, which controls inferred tolerance."""
        return format(number, "f")


def export_beancount(ledger: BeancountLedger, output_path: str | Path) -> str:
    """Validate and export every native directive to a new, verified file.

    Source files (including symlinks/hardlinks) and existing output files are
    never overwritten. Unsupported printer cases fail before publishing a file.
    """
    _, _, _, _, _, _, _, _, printer = _beancount_modules()
    target = Path(output_path).expanduser().resolve()
    for source in ledger.source_files:
        if target == source or (target.exists() and os.path.samefile(target, source)):
            raise BeancountIOError("Output cannot overwrite any Beancount source or include file.")
    if target.exists():
        raise BeancountIOError(f"Beancount output already exists: {target}")
    _validate(ledger.entries, ledger.options)
    buffer = io.StringIO()
    rendered_options = _render_options(ledger.options)
    if rendered_options:
        buffer.write(rendered_options + "\n\n")
    entry_printer = printer.EntryPrinter(ledger.options["dcontext"])
    # A currency-wide display precision would add/remove trailing zeros and
    # change the inferred tolerance. Preserve every Decimal's own scale.
    entry_printer.dformat = _ExactFormatter()
    entry_printer.dformat_max = _ExactFormatter()
    try:
        for entry in ledger.entries:
            buffer.write(entry_printer(_printable_entry(entry)))
            buffer.write("\n")
    except (ValueError, TypeError, AttributeError) as exc:
        raise BeancountIOError("The Beancount printer cannot preserve this ledger.") from exc
    text = buffer.getvalue()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".beancount-export-",
            suffix=".beancount",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        restored = import_beancount(temporary)
        if _canonical(ledger.entries) != _canonical(restored.entries):
            raise BeancountIOError(
                "Export would change native directives or metadata; no file written."
            )
        if _semantic_options(ledger.options) != _semantic_options(restored.options):
            raise BeancountIOError("Export would change ledger options; no file written.")
        # Atomic create-if-absent protects against a concurrent file appearing.
        # Both paths are in the same directory/filesystem.
        os.link(temporary, target)
    except FileExistsError as exc:
        raise BeancountIOError(f"Beancount output already exists: {target}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return text
