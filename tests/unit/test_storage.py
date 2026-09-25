"""Unit tests for ikarchive.storage."""

import collections
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import sys

# Ensure src/python is on sys.path
SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "python"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ikarchive.storage import (
    CRITICAL_SPACE_BYTES,
    LOW_SPACE_BYTES,
    backup_required_bytes,
    ensure_backup_space,
    storage_health,
)

Usage = collections.namedtuple("Usage", ["total", "used", "free"])


class StorageHealthTest(unittest.TestCase):
    def test_threshold_critical_below(self):
        with patch("shutil.disk_usage", return_value=Usage(10 * 1024**3, 0, CRITICAL_SPACE_BYTES - 1)):
            health = storage_health("/some/path")
            self.assertEqual(health["state"], "critical")
            self.assertEqual(health["free_bytes"], CRITICAL_SPACE_BYTES - 1)
            self.assertEqual(health["total_bytes"], 10 * 1024**3)
            self.assertEqual(health["low_space_bytes"], LOW_SPACE_BYTES)
            self.assertEqual(health["critical_space_bytes"], CRITICAL_SPACE_BYTES)

    def test_threshold_critical_boundary(self):
        with patch("shutil.disk_usage", return_value=Usage(10 * 1024**3, 0, CRITICAL_SPACE_BYTES)):
            health = storage_health("/some/path")
            self.assertEqual(health["state"], "low")

    def test_threshold_low_below(self):
        with patch("shutil.disk_usage", return_value=Usage(10 * 1024**3, 0, LOW_SPACE_BYTES - 1)):
            health = storage_health("/some/path")
            self.assertEqual(health["state"], "low")

    def test_threshold_low_boundary(self):
        with patch("shutil.disk_usage", return_value=Usage(10 * 1024**3, 0, LOW_SPACE_BYTES)):
            health = storage_health("/some/path")
            self.assertEqual(health["state"], "normal")

    def test_threshold_normal_above(self):
        with patch("shutil.disk_usage", return_value=Usage(10 * 1024**3, 0, LOW_SPACE_BYTES + 1)):
            health = storage_health("/some/path")
            self.assertEqual(health["state"], "normal")

    def test_existing_file_uses_parent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "test.db"
            file_path.touch()
            with patch("shutil.disk_usage", return_value=Usage(10**9, 0, 10**9)) as mock_usage:
                storage_health(file_path)
                mock_usage.assert_called_once_with(Path(tmp_dir))

    def test_nonexistent_ancestor_resolution(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            deep_path = Path(tmp_dir) / "level1" / "level2" / "target.db"
            with patch("shutil.disk_usage", return_value=Usage(10**9, 0, 10**9)) as mock_usage:
                storage_health(deep_path)
                mock_usage.assert_called_once_with(Path(tmp_dir))

    def test_exceptions_not_swallowed(self):
        with patch("shutil.disk_usage", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                storage_health("/some/path")


class BackupRequiredBytesTest(unittest.TestCase):
    def test_real_sqlite_calculation(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data TEXT)")
            for i in range(100):
                cursor.execute("INSERT INTO t (data) VALUES (?)", ("x" * 500,))
            conn.commit()

            page_count = cursor.execute("PRAGMA page_count").fetchone()[0]
            page_size = cursor.execute("PRAGMA page_size").fetchone()[0]
            expected = int(page_count * page_size + 64 * 1024**2)

            res = backup_required_bytes(conn)
            self.assertIsInstance(res, int)
            self.assertEqual(res, expected)
        finally:
            conn.close()


class EnsureBackupSpaceTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_reject_existing_destination(self):
        with tempfile.NamedTemporaryFile() as tmp:
            with self.assertRaises(ValueError) as ctx:
                ensure_backup_space(self.conn, tmp.name)
            self.assertEqual(str(ctx.exception), "BACKUP_DESTINATION_EXISTS")

    def test_reject_existing_directory_destination(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(ValueError) as ctx:
                ensure_backup_space(self.conn, tmp_dir)
            self.assertEqual(str(ctx.exception), "BACKUP_DESTINATION_EXISTS")

    def test_insufficient_space_raises_and_creates_no_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "nested" / "backup.db"
            required = backup_required_bytes(self.conn)
            # free - required < CRITICAL_SPACE_BYTES
            mock_free = required + CRITICAL_SPACE_BYTES - 1

            with patch("shutil.disk_usage", return_value=Usage(10 * 1024**3, 0, mock_free)):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_backup_space(self.conn, dest)
                self.assertEqual(str(ctx.exception), "BACKUP_INSUFFICIENT_SPACE")

            # Check that destination and parent were NOT created
            self.assertFalse(dest.exists())
            self.assertFalse((Path(tmp_dir) / "nested").exists())

    def test_sufficient_space_returns_required_and_creates_no_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "nested" / "backup.db"
            required = backup_required_bytes(self.conn)
            # free - required == CRITICAL_SPACE_BYTES (sufficient)
            mock_free = required + CRITICAL_SPACE_BYTES

            with patch("shutil.disk_usage", return_value=Usage(10 * 1024**3, 0, mock_free)):
                returned = ensure_backup_space(self.conn, dest)
                self.assertEqual(returned, required)

            # Check that destination and parent were NOT created
            self.assertFalse(dest.exists())
            self.assertFalse((Path(tmp_dir) / "nested").exists())


if __name__ == "__main__":
    unittest.main()
