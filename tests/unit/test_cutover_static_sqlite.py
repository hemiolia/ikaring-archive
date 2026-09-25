import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import cutover_static_sqlite as cutover


def make_db(path, value):
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE record (value TEXT)")
        connection.execute("INSERT INTO record VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def value_of(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT value FROM record").fetchone()[0]
    finally:
        connection.close()


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cutover-artificial-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.snapshot = root / "source.sqlite3"
        self.database = root / "archive.sqlite3"
        self.lock = root / "archive.lock"
        make_db(self.snapshot, "new")
        make_db(self.database, "old")
        data = self.snapshot.read_bytes()
        self.expected_bytes = len(data)
        self.expected_sha256 = hashlib.sha256(data).hexdigest()

    def run_cutover(self):
        return cutover.cutover(self.snapshot, self.database, self.lock,
                               self.expected_bytes, self.expected_sha256)

    def test_success_preserves_old_set_and_source(self):
        source_before = self.snapshot.read_bytes()
        old_before = self.database.read_bytes()
        sidecars = {}
        for suffix in ("-wal", "-shm", "-journal"):
            sidecars[suffix] = ("old" + suffix).encode()
            Path(str(self.database) + suffix).write_bytes(sidecars[suffix])
        receipt = self.run_cutover()
        self.assertEqual(self.snapshot.read_bytes(), source_before)
        self.assertEqual(self.database.read_bytes(), source_before)
        self.assertEqual((receipt / "retired" / self.database.name).read_bytes(), old_before)
        for suffix, data in sidecars.items():
            self.assertEqual((receipt / "retired" / (self.database.name + suffix)).read_bytes(), data)
            self.assertFalse(Path(str(self.database) + suffix).exists())
        events = [json.loads(p.read_text())["event"] for p in sorted(receipt.glob("*.json"))]
        self.assertEqual(events[-1], "committed")
        self.assertIn("promote_intent", events)

    def test_wrong_hash_and_corrupt_sqlite_leave_destination_untouched(self):
        old = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            cutover.cutover(self.snapshot, self.database, self.lock,
                            self.expected_bytes, "0" * 64)
        self.assertEqual(self.database.read_bytes(), old)
        self.snapshot.write_bytes(b"X" * self.expected_bytes)
        expected = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        with self.assertRaises(sqlite3.DatabaseError):
            cutover.cutover(self.snapshot, self.database, self.lock,
                            self.expected_bytes, expected)
        self.assertEqual(self.database.read_bytes(), old)

    def test_symlinks_and_busy_lock_are_rejected(self):
        old = self.database.read_bytes()
        other = self.database.parent / "other.sqlite3"
        self.snapshot.rename(other)
        self.snapshot.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.run_cutover()
        self.snapshot.unlink()
        other.rename(self.snapshot)
        Path(str(self.database) + "-wal").symlink_to(self.snapshot)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.run_cutover()
        Path(str(self.database) + "-wal").unlink()
        lock_fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "busy"):
                self.run_cutover()
        finally:
            os.close(lock_fd)
        self.assertEqual(self.database.read_bytes(), old)

    def test_failure_after_promotion_restores_old_and_preserves_new(self):
        old = self.database.read_bytes()
        original_digest = cutover.digest_file

        def fail_canonical(path):
            if Path(path) == self.database:
                raise OSError("injected canonical digest failure")
            return original_digest(path)

        with patch.object(cutover, "digest_file", side_effect=fail_canonical):
            with self.assertRaisesRegex(RuntimeError, "original DB restored"):
                self.run_cutover()
        self.assertEqual(self.database.read_bytes(), old)
        receipts = list(self.database.parent.glob(".archive.sqlite3.cutover-*"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual((receipts[0] / "failed-new" / self.database.name).read_bytes(),
                         self.snapshot.read_bytes())
        self.assertTrue(list(receipts[0].glob("*-rollback_complete.json")))

    def test_partial_retirement_rolls_back_all_moved_files(self):
        old = self.database.read_bytes()
        wal = Path(str(self.database) + "-wal")
        wal.write_bytes(b"old wal")
        original_move = cutover.move_and_sync

        def fail_wal(source, destination):
            if Path(source) == wal:
                raise OSError("injected retirement failure")
            return original_move(source, destination)

        with patch.object(cutover, "move_and_sync", side_effect=fail_wal):
            with self.assertRaisesRegex(RuntimeError, "original DB restored"):
                self.run_cutover()
        self.assertEqual(self.database.read_bytes(), old)
        self.assertEqual(wal.read_bytes(), b"old wal")

    def test_unfinished_prior_run_blocks_a_new_cutover(self):
        old = self.database.read_bytes()
        record_dir = self.database.parent / ".archive.sqlite3.cutover-interrupted"
        record_dir.mkdir()
        (record_dir / "0001-started.json").write_text('{"event":"started"}')
        with self.assertRaisesRegex(ValueError, "needs inspection"):
            self.run_cutover()
        self.assertEqual(self.database.read_bytes(), old)

    def test_rollback_restores_old_even_when_receipt_write_fails(self):
        old = self.database.read_bytes()
        original_digest = cutover.digest_file
        original_record = cutover.Journal.record

        def fail_canonical(path):
            if Path(path) == self.database:
                raise OSError("injected canonical digest failure")
            return original_digest(path)

        def fail_rollback_begin(journal, event, **details):
            if event == "rollback_begin":
                raise OSError("injected receipt failure")
            return original_record(journal, event, **details)

        with patch.object(cutover, "digest_file", side_effect=fail_canonical), \
             patch.object(cutover.Journal, "record", fail_rollback_begin):
            with self.assertRaisesRegex(RuntimeError, "DB files restored"):
                self.run_cutover()
        self.assertEqual(self.database.read_bytes(), old)

    def test_unexpected_new_sidecar_is_preserved_outside_restored_set(self):
        old = self.database.read_bytes()
        original_digest = cutover.digest_file
        unexpected = Path(str(self.database) + "-wal")

        def fail_canonical(path):
            if Path(path) == self.database:
                unexpected.write_bytes(b"unexpected new wal")
            return original_digest(path)

        with patch.object(cutover, "digest_file", side_effect=fail_canonical):
            with self.assertRaisesRegex(RuntimeError, "original DB restored"):
                self.run_cutover()
        self.assertEqual(self.database.read_bytes(), old)
        self.assertFalse(unexpected.exists())
        receipt = next(self.database.parent.glob(".archive.sqlite3.cutover-*"))
        self.assertEqual((receipt / "failed-new" / unexpected.name).read_bytes(),
                         b"unexpected new wal")


if __name__ == "__main__":
    unittest.main()
