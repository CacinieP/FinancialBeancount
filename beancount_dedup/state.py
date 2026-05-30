"""
Incremental processing state persistence.

Saves/loads processed transaction fingerprints (L1 hashes) to/from a JSON file
so that subsequent runs can skip already-processed files and transactions.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default state file location (relative to project root)
DEFAULT_STATE_PATH = Path("output") / ".dedup_state.json"


class StateStore:
    """
    Persists incremental-processing state across runs.

    Tracks:
    - Processed file paths with their mtimes (to detect changed files).
    - Seen L1 fingerprints (the primary dedup key).
    - Last run timestamp.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_STATE_PATH
        # Internal data structures — populated by load() or fresh state.
        self._files: dict[str, float] = {}  # filepath -> mtime
        self._seen_fingerprints: set[str] = set()
        self._last_run: str | None = None  # ISO-8601

    # ── Persistence ────────────────────────────────────────────────────────

    def save(self) -> None:
        """Write current state to disk as JSON."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {
            "version": 1,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "files": self._files,
            "seen_fingerprints": sorted(self._seen_fingerprints),
        }

        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

        # Atomic-ish rename (Windows: replace needs to target existing file)
        if self.path.exists():
            os.replace(tmp_path, self.path)
        else:
            os.rename(tmp_path, self.path)

        logger.debug("State saved to %s (%d fingerprints)", self.path, len(self._seen_fingerprints))

    def load(self) -> None:
        """Load state from disk.  If the file is missing or corrupt, starts fresh."""
        if not self.path.exists():
            logger.debug("State file %s not found; starting fresh", self.path)
            self._files = {}
            self._seen_fingerprints = set()
            self._last_run = None
            return

        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load state from %s: %s; starting fresh", self.path, exc)
            self._files = {}
            self._seen_fingerprints = set()
            self._last_run = None
            return

        self._files = data.get("files", {})
        self._seen_fingerprints = set(data.get("seen_fingerprints", []))
        self._last_run = data.get("last_run")

        logger.info(
            "Loaded state from %s: %d files, %d fingerprints, last run %s",
            self.path,
            len(self._files),
            len(self._seen_fingerprints),
            self._last_run or "never",
        )

    # ── Queries ────────────────────────────────────────────────────────────

    def is_file_processed(self, filepath: str | Path) -> bool:
        """
        Return True if *filepath* was processed in a previous run and its
        mtime has not changed since.
        """
        filepath_str = str(filepath)
        try:
            current_mtime = Path(filepath).stat().st_mtime
        except OSError:
            return False

        stored_mtime = self._files.get(filepath_str)
        return stored_mtime is not None and stored_mtime == current_mtime

    def get_seen_fingerprints(self) -> set[str]:
        """Return the set of all L1 fingerprints seen in previous runs."""
        return set(self._seen_fingerprints)  # return a copy

    # ── Mutations ──────────────────────────────────────────────────────────

    def mark_processed(self, filepath: str | Path, fingerprints: set[str]) -> None:
        """
        Record that *filepath* has been fully processed and associate the
        given L1 *fingerprints* with it.
        """
        filepath_str = str(filepath)
        try:
            mtime = Path(filepath).stat().st_mtime
        except OSError:
            mtime = 0.0

        self._files[filepath_str] = mtime
        self._seen_fingerprints.update(fingerprints)

    @property
    def last_run(self) -> str | None:
        return self._last_run

    @property
    def file_count(self) -> int:
        return len(self._files)

    @property
    def fingerprint_count(self) -> int:
        return len(self._seen_fingerprints)
