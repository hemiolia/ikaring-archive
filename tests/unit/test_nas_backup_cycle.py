#!/usr/bin/env python3
"""Unit tests for NAS backup cycle coordinator (scripts/nas_backup_cycle.py).

Tests safety invariants and edge cases using temporary directories and subprocess mocks:
- Successful full backup cycle, roundtrip verification, and secret-free cloud receipt
- GDrive remote listing failure aborts before upload
- Rejection of existing destination filenames on remote
- Manifest integrity validation and tampering rejection
- Encrypted file hash mismatch rejection
- Roundtrip SHA256 mismatch triggers cleanup of only the uploading temporary object
- Non-blocking flock concurrency rejection
"""

import fcntl
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import nas_backup_cycle


class TestNasBackupCycle(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)

        self.db_path = self.root / "archive.sqlite3"
        self.db_path.write_bytes(b"SQLITE3_DUMMY_DATABASE_CONTENT")

        self.passphrase_file = self.root / "passphrase.txt"
        self.passphrase_file.write_text("SUPER_SECRET_GPG_PASSPHRASE\n", encoding="utf-8")

        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.remote_dir = "gdrive_origin:ikaring-backups"

        # Prepare synthetic backup artifacts
        self.raw_path = self.backup_dir / "archive_20260925_120000_uuid123.sqlite3"
        self.raw_data = b"SYNTHETIC_RAW_SQLITE_CONTENT"
        self.raw_path.write_bytes(self.raw_data)

        self.enc_path = self.backup_dir / "archive_20260925_120000_uuid123.sqlite3.zst.gpg"
        self.enc_data = b"SYNTHETIC_ENCRYPTED_ZST_GPG_CONTENT_ABCDEF"
        self.enc_path.write_bytes(self.enc_data)

        self.enc_sha256 = hashlib.sha256(self.enc_data).hexdigest()
        self.enc_md5 = hashlib.md5(self.enc_data).hexdigest()
        self.enc_bytes = len(self.enc_data)

        self.manifest_path = self.backup_dir / "archive_20260925_120000_uuid123.sqlite3.manifest.json"
        self.manifest_dict = {
            "timestamp": "2026-09-25T12:00:00Z",
            "raw_snapshot": {
                "basename": self.raw_path.name,
                "bytes": len(self.raw_data),
                "sha256": hashlib.sha256(self.raw_data).hexdigest(),
                "quick_check": "ok",
            },
            "encrypted_snapshot": {
                "basename": self.enc_path.name,
                "bytes": self.enc_bytes,
                "sha256": self.enc_sha256,
                "compression": "zstd",
                "cipher": "AES256",
            },
            "verification": {
                "sha256_match": True,
                "quick_check": "ok",
            },
        }
        self.manifest_path.write_text(json.dumps(self.manifest_dict, indent=2), encoding="utf-8")
        self.manifest_bytes = self.manifest_path.stat().st_size
        self.manifest_md5 = hashlib.md5(self.manifest_path.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _default_subprocess_run(self, cmd, *args, **kwargs):
        """Mock dispatcher for subprocess.run handling backup script and rclone commands."""
        cmd_str = [str(c) for c in cmd]
        first = cmd_str[0]

        # Backup creation script
        if "nas_create_verified_backup.sh" in first:
            payload = {
                "status": "ok",
                "raw_path": str(self.raw_path),
                "encrypted_path": str(self.enc_path),
                "manifest_path": str(self.manifest_path),
            }
            return type("Result", (), {"returncode": 0, "stdout": json.dumps(payload) + "\n", "stderr": ""})()

        # rclone commands
        if first == "rclone":
            subcmd = cmd_str[1]
            if subcmd == "lsf":
                return type("Result", (), {"returncode": 0, "stdout": "existing_old_backup.sqlite3\n", "stderr": ""})()
            elif subcmd == "copyto":
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            elif subcmd == "size":
                target = cmd_str[3]
                if self.enc_path.name in target:
                    return type("Result", (), {"returncode": 0, "stdout": json.dumps({"count": 1, "bytes": self.enc_bytes}), "stderr": ""})()
                elif self.manifest_path.name in target:
                    return type("Result", (), {"returncode": 0, "stdout": json.dumps({"count": 1, "bytes": self.manifest_bytes}), "stderr": ""})()
                return type("Result", (), {"returncode": 0, "stdout": json.dumps({"count": 1, "bytes": 0}), "stderr": ""})()
            elif subcmd == "md5sum":
                target = cmd_str[2]
                if self.enc_path.name in target:
                    return type("Result", (), {"returncode": 0, "stdout": f"{self.enc_md5}  {target}\n", "stderr": ""})()
                elif self.manifest_path.name in target:
                    return type("Result", (), {"returncode": 0, "stdout": f"{self.manifest_md5}  {target}\n", "stderr": ""})()
                return type("Result", (), {"returncode": 0, "stdout": f"dummy_md5  {target}\n", "stderr": ""})()
            elif subcmd == "moveto":
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            elif subcmd == "deletefile":
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        raise NotImplementedError(f"Unhandled mock command: {cmd_str}")

    def _default_subprocess_popen(self, cmd, *args, **kwargs):
        """Mock dispatcher for subprocess.Popen handling rclone cat."""
        cmd_str = [str(c) for c in cmd]
        if cmd_str[:2] == ["rclone", "cat"]:
            mock_proc = MagicMock()
            mock_proc.stdout = io.BytesIO(self.enc_data)
            mock_proc.wait.return_value = 0
            return mock_proc
        raise NotImplementedError(f"Unhandled mock popen command: {cmd_str}")

    def test_backup_cycle_success(self):
        """Standard success path: full backup, upload, verification and receipt creation."""
        executed_cmds = []

        def spy_run(cmd, *args, **kwargs):
            executed_cmds.append([str(c) for c in cmd])
            return self._default_subprocess_run(cmd, *args, **kwargs)

        with patch.object(nas_backup_cycle.subprocess, "run", side_effect=spy_run), \
             patch.object(nas_backup_cycle.subprocess, "Popen", side_effect=self._default_subprocess_popen):

            ret = nas_backup_cycle.main([
                "--db", str(self.db_path),
                "--backup-dir", str(self.backup_dir),
                "--passphrase-file", str(self.passphrase_file),
                "--remote", self.remote_dir,
            ])
            self.assertEqual(ret, 0)

        # Check receipt existence and content
        receipts_dir = self.backup_dir / "cloud-receipts"
        receipt_file = receipts_dir / self.manifest_path.name
        self.assertTrue(receipt_file.is_file())

        receipt_data = json.loads(receipt_file.read_text(encoding="utf-8"))
        self.assertEqual(receipt_data["status"], "ok")
        self.assertEqual(receipt_data["encrypted_snapshot"]["bytes"], self.enc_bytes)
        self.assertEqual(receipt_data["encrypted_snapshot"]["sha256"], self.enc_sha256)
        self.assertEqual(receipt_data["encrypted_snapshot"]["md5"], self.enc_md5)
        self.assertEqual(receipt_data["encrypted_snapshot"]["remote"], f"{self.remote_dir}/{self.enc_path.name}")
        self.assertEqual(receipt_data["manifest"]["remote"], f"{self.remote_dir}/{self.manifest_path.name}")
        self.assertTrue(receipt_data["verification"]["roundtrip_sha256_verified"])

        # Invariant: No secrets or passphrase in receipt or arguments
        receipt_text = receipt_file.read_text(encoding="utf-8")
        self.assertNotIn("SUPER_SECRET_GPG_PASSPHRASE", receipt_text)
        self.assertNotIn("account", receipt_text.lower())

        # Verify command sequence
        copy_cmds = [cmd for cmd in executed_cmds if cmd[:2] == ["rclone", "copyto"]]
        moveto_cmds = [cmd for cmd in executed_cmds if cmd[:2] == ["rclone", "moveto"]]
        self.assertEqual(len(copy_cmds), 2)
        self.assertEqual(len(moveto_cmds), 2)
        self.assertIn("--immutable", copy_cmds[0])
        self.assertIn("--immutable", moveto_cmds[0])

    def test_listing_failure_aborts(self):
        """If rclone lsf fails, the cycle must abort immediately without attempting uploads."""
        def failed_lsf(cmd, *args, **kwargs):
            if cmd[:2] == ["rclone", "lsf"]:
                return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "network error"})()
            return self._default_subprocess_run(cmd, *args, **kwargs)

        with patch.object(nas_backup_cycle.subprocess, "run", side_effect=failed_lsf), \
             patch.object(nas_backup_cycle.subprocess, "Popen", side_effect=self._default_subprocess_popen):

            ret = nas_backup_cycle.main([
                "--db", str(self.db_path),
                "--backup-dir", str(self.backup_dir),
                "--passphrase-file", str(self.passphrase_file),
                "--remote", self.remote_dir,
            ])
            self.assertEqual(ret, 1)

        # Ensure no cloud receipt was created
        receipt_file = self.backup_dir / "cloud-receipts" / self.manifest_path.name
        self.assertFalse(receipt_file.exists())

    def test_duplicate_remote_name_rejected(self):
        """If remote listing already contains the target filename, abort immediately."""
        def duplicate_lsf(cmd, *args, **kwargs):
            if cmd[:2] == ["rclone", "lsf"]:
                # Remote already has the encrypted snapshot
                return type("Result", (), {"returncode": 0, "stdout": f"{self.enc_path.name}\n", "stderr": ""})()
            return self._default_subprocess_run(cmd, *args, **kwargs)

        with patch.object(nas_backup_cycle.subprocess, "run", side_effect=duplicate_lsf), \
             patch.object(nas_backup_cycle.subprocess, "Popen", side_effect=self._default_subprocess_popen):

            ret = nas_backup_cycle.main([
                "--db", str(self.db_path),
                "--backup-dir", str(self.backup_dir),
                "--passphrase-file", str(self.passphrase_file),
                "--remote", self.remote_dir,
            ])
            self.assertEqual(ret, 1)

    def test_manifest_tampering_rejected(self):
        """If manifest verification fields or checks are invalid, abort before uploading."""
        invalid_manifests = [
            # sha256_match not True
            {**self.manifest_dict, "verification": {"sha256_match": False, "quick_check": "ok"}},
            # verification quick_check not ok
            {**self.manifest_dict, "verification": {"sha256_match": True, "quick_check": "corrupt"}},
            # raw_snapshot quick_check not ok
            {**self.manifest_dict, "raw_snapshot": {**self.manifest_dict["raw_snapshot"], "quick_check": "corrupt"}},
            # encrypted basename mismatch
            {**self.manifest_dict, "encrypted_snapshot": {**self.manifest_dict["encrypted_snapshot"], "basename": "wrong.gpg"}},
        ]

        for bad_manifest in invalid_manifests:
            with self.subTest(bad_manifest=bad_manifest):
                self.manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")
                with patch.object(nas_backup_cycle.subprocess, "run", side_effect=self._default_subprocess_run), \
                     patch.object(nas_backup_cycle.subprocess, "Popen", side_effect=self._default_subprocess_popen):

                    ret = nas_backup_cycle.main([
                        "--db", str(self.db_path),
                        "--backup-dir", str(self.backup_dir),
                        "--passphrase-file", str(self.passphrase_file),
                        "--remote", self.remote_dir,
                    ])
                    self.assertEqual(ret, 1)

    def test_enc_hash_mismatch_with_manifest_rejected(self):
        """If local encrypted file does not match manifest SHA256, abort before upload."""
        bad_manifest = {
            **self.manifest_dict,
            "encrypted_snapshot": {**self.manifest_dict["encrypted_snapshot"], "sha256": "0000000000000000000000000000000000000000000000000000000000000000"},
        }
        self.manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")

        with patch.object(nas_backup_cycle.subprocess, "run", side_effect=self._default_subprocess_run), \
             patch.object(nas_backup_cycle.subprocess, "Popen", side_effect=self._default_subprocess_popen):

            ret = nas_backup_cycle.main([
                "--db", str(self.db_path),
                "--backup-dir", str(self.backup_dir),
                "--passphrase-file", str(self.passphrase_file),
                "--remote", self.remote_dir,
            ])
            self.assertEqual(ret, 1)

    def test_roundtrip_sha256_mismatch_cleans_only_uploading(self):
        """If rclone cat stream does not match source SHA256, delete uploading temp and preserve local raw/enc."""
        deleted_files = []

        def tracking_run(cmd, *args, **kwargs):
            if cmd[:2] == ["rclone", "deletefile"]:
                deleted_files.append(str(cmd[2]))
            return self._default_subprocess_run(cmd, *args, **kwargs)

        def corrupted_cat_popen(cmd, *args, **kwargs):
            cmd_str = [str(c) for c in cmd]
            if cmd_str[:2] == ["rclone", "cat"]:
                mock_proc = MagicMock()
                # Return corrupted content during stream
                mock_proc.stdout = io.BytesIO(b"CORRUPTED_STREAM_DATA")
                mock_proc.wait.return_value = 0
                return mock_proc
            return self._default_subprocess_popen(cmd, *args, **kwargs)

        with patch.object(nas_backup_cycle.subprocess, "run", side_effect=tracking_run), \
             patch.object(nas_backup_cycle.subprocess, "Popen", side_effect=corrupted_cat_popen):

            ret = nas_backup_cycle.main([
                "--db", str(self.db_path),
                "--backup-dir", str(self.backup_dir),
                "--passphrase-file", str(self.passphrase_file),
                "--remote", self.remote_dir,
            ])
            self.assertEqual(ret, 1)

        # Invariant: uploading temporary was cleaned up
        self.assertEqual(len(deleted_files), 1)
        self.assertTrue(".uploading-" in deleted_files[0])

        # Invariant: local raw, encrypted, and manifest files are kept intact
        self.assertTrue(self.raw_path.is_file())
        self.assertTrue(self.enc_path.is_file())
        self.assertTrue(self.manifest_path.is_file())

    def test_flock_concurrency_rejection(self):
        """If another instance holds the non-blocking flock, duplicate invocation is rejected."""
        lock_file = self.backup_dir / ".nas_backup_cycle.lock"
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            with patch.object(nas_backup_cycle.subprocess, "run", side_effect=self._default_subprocess_run), \
                 patch.object(nas_backup_cycle.subprocess, "Popen", side_effect=self._default_subprocess_popen):

                ret = nas_backup_cycle.main([
                    "--db", str(self.db_path),
                    "--backup-dir", str(self.backup_dir),
                    "--passphrase-file", str(self.passphrase_file),
                    "--remote", self.remote_dir,
                ])
                self.assertEqual(ret, 1)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_missing_db_or_passphrase_fails(self):
        """CLI validator rejects non-existent database or passphrase file."""
        nonexistent = self.root / "missing.file"
        ret_db = nas_backup_cycle.main([
            "--db", str(nonexistent),
            "--backup-dir", str(self.backup_dir),
            "--passphrase-file", str(self.passphrase_file),
            "--remote", self.remote_dir,
        ])
        self.assertEqual(ret_db, 1)

        ret_pass = nas_backup_cycle.main([
            "--db", str(self.db_path),
            "--backup-dir", str(self.backup_dir),
            "--passphrase-file", str(nonexistent),
            "--remote", self.remote_dir,
        ])
        self.assertEqual(ret_pass, 1)


if __name__ == "__main__":
    unittest.main()
