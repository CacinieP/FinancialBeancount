"""
Tests for beancount_dedup.state — StateStore and incremental processing.
"""

import json
from datetime import datetime
from decimal import Decimal

from beancount_dedup.deduplicator import DeduplicationEngine
from beancount_dedup.models import Platform, Transaction
from beancount_dedup.state import StateStore

# ── StateStore save/load roundtrip ──────────────────────────────────────


class TestStateStoreRoundtrip:
    def test_save_load_roundtrip(self, tmp_path):
        state_file = tmp_path / "state.json"
        store = StateStore(path=state_file)
        store.mark_processed("/some/file.csv", {"abc123", "def456"})
        store.save()

        # Load into a fresh instance
        loaded = StateStore(path=state_file)
        loaded.load()

        assert loaded.get_seen_fingerprints() == {"abc123", "def456"}
        assert loaded.file_count == 1
        assert loaded.fingerprint_count == 2
        assert loaded.last_run is not None

    def test_load_missing_file_starts_fresh(self, tmp_path):
        store = StateStore(path=tmp_path / "nonexistent.json")
        store.load()
        assert store.file_count == 0
        assert store.fingerprint_count == 0
        assert store.get_seen_fingerprints() == set()

    def test_load_corrupt_json_starts_fresh(self, tmp_path):
        state_file = tmp_path / "bad.json"
        state_file.write_text("{{{{not valid json", encoding="utf-8")

        store = StateStore(path=state_file)
        store.load()
        assert store.file_count == 0
        assert store.fingerprint_count == 0

    def test_save_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "deep" / "dir" / "state.json"
        store = StateStore(path=nested)
        store.mark_processed("f.csv", {"fp1"})
        store.save()
        assert nested.exists()

    def test_json_structure(self, tmp_path):
        state_file = tmp_path / "state.json"
        store = StateStore(path=state_file)
        store.mark_processed("a.csv", {"x"})
        store.save()

        with open(state_file, encoding="utf-8") as fh:
            data = json.load(fh)

        assert data["version"] == 1
        assert "last_run" in data
        assert "a.csv" in data["files"]
        assert "x" in data["seen_fingerprints"]


# ── is_file_processed ───────────────────────────────────────────────────


class TestIsFileProcessed:
    def test_unprocessed_file(self, tmp_path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("hello", encoding="utf-8")

        store = StateStore(path=tmp_path / "state.json")
        assert store.is_file_processed(csv_file) is False

    def test_processed_file_same_mtime(self, tmp_path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("hello", encoding="utf-8")
        mtime = csv_file.stat().st_mtime

        store = StateStore(path=tmp_path / "state.json")
        # Manually inject the file entry with matching mtime.
        store._files[str(csv_file)] = mtime

        assert store.is_file_processed(csv_file) is True

    def test_processed_file_changed_mtime(self, tmp_path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("hello", encoding="utf-8")

        store = StateStore(path=tmp_path / "state.json")
        # Inject a stale mtime.
        store._files[str(csv_file)] = 0.0

        assert store.is_file_processed(csv_file) is False

    def test_nonexistent_file(self, tmp_path):
        store = StateStore(path=tmp_path / "state.json")
        assert store.is_file_processed("/no/such/file.csv") is False

    def test_roundtrip_preserves_file_tracking(self, tmp_path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("hello", encoding="utf-8")
        state_file = tmp_path / "state.json"

        # Save state.
        store = StateStore(path=state_file)
        store.mark_processed(csv_file, {"fp1"})
        store.save()

        # Reload and verify the file is considered processed.
        loaded = StateStore(path=state_file)
        loaded.load()
        assert loaded.is_file_processed(csv_file) is True


# ── Incremental dedup: skipping already-seen fingerprints ───────────────


class TestIncrementalDedup:
    def test_skips_seen_fingerprints(self):
        engine = DeduplicationEngine()
        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="星巴克",
            description="咖啡",
        )
        Transaction(
            platform=Platform.BANK,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="支付宝-星巴克",
            description="快捷支付",
        )

        # First run: process tx1 normally.
        engine.add_transactions([tx1])
        seen = engine.get_l1_fingerprints()
        assert len(seen) >= 1

        # Second run: using a fresh engine with the seen fingerprints.
        engine2 = DeduplicationEngine()
        # Create a new tx identical to tx1.
        tx1_dup = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="星巴克",
            description="咖啡",
        )
        tx_new = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 16, 10, 0, 0),
            amount=Decimal("-50.00"),
            counterparty="肯德基",
            description="午餐",
        )
        results = engine2.add_transactions_incremental(
            [tx1_dup, tx_new], seen_fingerprints=set(seen)
        )

        # tx1_dup should be skipped; only tx_new should produce a result.
        processed_ids = [r.transaction.id for r in results]
        assert tx1_dup.id not in processed_ids
        assert tx_new.id in processed_ids
        assert len(results) == 1

    def test_seen_set_grows_with_new_transactions(self):
        engine = DeduplicationEngine()
        seen: set[str] = set()

        tx1 = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="星巴克",
            description="咖啡",
        )
        engine.add_transactions_incremental([tx1], seen)
        assert len(seen) >= 1

        tx2 = Transaction(
            platform=Platform.WECHAT,
            datetime=datetime(2024, 1, 16, 12, 0, 0),
            amount=Decimal("-35.00"),
            counterparty="滴滴出行",
            description="打车",
        )
        engine.add_transactions_incremental([tx2], seen)
        assert len(seen) >= 2

    def test_add_transactions_incremental_empty_seen(self):
        engine = DeduplicationEngine()
        seen: set[str] = set()

        tx = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="星巴克",
            description="咖啡",
        )
        results = engine.add_transactions_incremental([tx], seen)
        assert len(results) == 1
        assert results[0].transaction is tx

    def test_full_incremental_cycle(self, tmp_path):
        """Simulate two consecutive incremental runs with StateStore."""
        state_file = tmp_path / "state.json"

        # ── Run 1 ─────────────────────────────────────────────────────
        engine1 = DeduplicationEngine()
        state1 = StateStore(path=state_file)
        state1.load()  # fresh

        tx_a = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="星巴克",
            description="咖啡",
        )
        seen1 = state1.get_seen_fingerprints()
        engine1.add_transactions_incremental([tx_a], seen1)

        # Persist.
        fps1 = engine1.get_l1_fingerprints()
        state1.mark_processed("input/alipay.csv", fps1)
        state1.save()

        # ── Run 2 ─────────────────────────────────────────────────────
        engine2 = DeduplicationEngine()
        state2 = StateStore(path=state_file)
        state2.load()

        assert state2.fingerprint_count >= 1

        # Same transaction as run 1 — should be skipped.
        tx_a_again = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="星巴克",
            description="咖啡",
        )
        # New transaction.
        tx_b = Transaction(
            platform=Platform.WECHAT,
            datetime=datetime(2024, 1, 16, 12, 0, 0),
            amount=Decimal("-35.00"),
            counterparty="滴滴出行",
            description="打车",
        )

        seen2 = state2.get_seen_fingerprints()
        results2 = engine2.add_transactions_incremental([tx_a_again, tx_b], seen2)

        # Only tx_b should have been processed.
        assert len(results2) == 1
        assert results2[0].transaction.counterparty == "滴滴出行"


# ── get_l1_fingerprints ─────────────────────────────────────────────────


class TestGetL1Fingerprints:
    def test_returns_fingerprints_from_index(self):
        engine = DeduplicationEngine()
        tx = Transaction(
            platform=Platform.ALIPAY,
            datetime=datetime(2024, 1, 15, 14, 0, 0),
            amount=Decimal("-100.00"),
            counterparty="星巴克",
            description="咖啡",
        )
        engine.add_transaction(tx)

        fps = engine.get_l1_fingerprints()
        assert isinstance(fps, set)
        assert len(fps) >= 1

    def test_empty_engine_returns_empty_set(self):
        engine = DeduplicationEngine()
        assert engine.get_l1_fingerprints() == set()
