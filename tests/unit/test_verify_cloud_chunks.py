"""Cloud readback through a fake rclone and real gpg/zstd subprocesses."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

from scripts import backup_chunks


@unittest.skipUnless(shutil.which('gpg') and shutil.which('zstd'), 'gpg and zstd required')
class VerifyCloudChunksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = self.root / 'key'
        self.key.write_text('artificial-test-passphrase-only')
        self.key.chmod(0o600)
        self.db = self.root / 'artificial.sqlite3'
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE sample (value TEXT)')
        con.executemany('INSERT INTO sample VALUES (?)', [(f'row-{i}',) for i in range(100)])
        con.commit()
        self.assertEqual(con.execute('PRAGMA quick_check').fetchall(), [('ok',)])
        con.close()
        compressed = subprocess.run(['zstd', '-q', '-c', str(self.db)],
                                    check=True, capture_output=True).stdout
        self.cipher = self.root / 'artificial.sqlite3.zst.gpg'
        subprocess.run(['gpg', '--batch', '--yes', '--no-tty', '--pinentry-mode', 'loopback',
                        '--passphrase-file', str(self.key), '--symmetric', '--cipher-algo',
                        'AES256', '--compress-algo', 'none', '--output', str(self.cipher)],
                       input=compressed, check=True, stderr=subprocess.DEVNULL)
        raw = self.db.read_bytes()
        encrypted = self.cipher.read_bytes()
        self.source_manifest = self.root / 'artificial.manifest.json'
        self.source_manifest.write_text(json.dumps({
            'timestamp': '2026-09-25T00:00:00+00:00',
            'raw_snapshot': {'basename': self.db.name, 'bytes': len(raw),
                             'sha256': hashlib.sha256(raw).hexdigest(), 'quick_check': 'ok'},
            'encrypted_snapshot': {'basename': self.cipher.name, 'bytes': len(encrypted),
                                   'sha256': hashlib.sha256(encrypted).hexdigest(),
                                   'compression': 'zstd', 'cipher': 'AES256'},
            'verification': {'sha256_match': True, 'quick_check': 'ok'},
        }))
        result = backup_chunks.split(self.cipher, self.source_manifest,
                                     self.root / 'bundles', chunk_size=100)
        self.bundle = Path(result['bundle_dir'])
        self.manifest = Path(result['chunk_manifest'])
        self.parts = json.loads(self.manifest.read_text())['parts']
        self.assertGreater(len(self.parts), 1)
        self.remote = self.root / 'fake-remote'
        self.remote.mkdir()
        for part in self.parts:
            shutil.copy2(self.bundle / part['basename'], self.remote / part['basename'])
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        fake_rclone = fake_bin / 'rclone'
        fake_rclone.write_text(
            '#!' + sys.executable + '\n'
            'import os, pathlib, sys\n'
            'if len(sys.argv) != 3 or sys.argv[1] != "cat": sys.exit(64)\n'
            'name = sys.argv[2].rsplit("/", 1)[-1]\n'
            'path = pathlib.Path(os.environ["FAKE_RCLONE_ROOT"]) / name\n'
            'if not path.is_file(): sys.exit(3)\n'
            'sys.stdout.buffer.write(path.read_bytes())\n'
            'sys.stdout.buffer.flush()\n'
            'if name == os.environ.get("FAKE_RCLONE_FAIL_AFTER_DATA"): sys.exit(17)\n'
        )
        fake_rclone.chmod(fake_rclone.stat().st_mode | stat.S_IXUSR)
        self.env = os.environ.copy()
        self.env['PATH'] = str(fake_bin) + os.pathsep + self.env['PATH']
        self.env['FAKE_RCLONE_ROOT'] = str(self.remote)

    def run_verify(self, key=None, env=None):
        return subprocess.run([
            sys.executable, str(Path(__file__).resolve().parents[2] / 'scripts/verify_cloud_chunks.py'),
            '--remote', 'gdrive_origin:イカリング3アーカイブ/backups',
            '--manifest', str(self.manifest), '--passphrase-file', str(key or self.key),
        ], env=env or self.env, capture_output=True, text=True)

    def test_full_cloud_roundtrip_receipt(self):
        before = {path.relative_to(self.root) for path in self.root.rglob('*')}
        proc = self.run_verify()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(before, {path.relative_to(self.root) for path in self.root.rglob('*')})
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual(receipt['ciphertext']['sha256'], hashlib.sha256(self.cipher.read_bytes()).hexdigest())
        self.assertEqual(receipt['raw_snapshot']['sha256'], hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(len(receipt['parts']), len(self.parts))
        self.assertTrue(all(part['remote'].endswith('/' + expected['basename'])
                            for part, expected in zip(receipt['parts'], self.parts)))
        self.assertEqual(receipt['raw_snapshot']['quick_check_source'], 'encrypted_manifest')
        self.assertFalse(receipt['verification']['sqlite_quick_check_performed'])
        self.assertNotIn('artificial-test-passphrase-only', proc.stdout)

    def test_missing_tampered_and_nonzero_rclone_are_failures_without_receipt(self):
        first = self.remote / self.parts[0]['basename']
        original = first.read_bytes()
        first.unlink()
        missing = self.run_verify()
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(missing.stdout, '')
        first.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        tampered = self.run_verify()
        self.assertNotEqual(tampered.returncode, 0)
        self.assertEqual(tampered.stdout, '')
        first.write_bytes(original)
        env = self.env.copy()
        env['FAKE_RCLONE_FAIL_AFTER_DATA'] = self.parts[0]['basename']
        failed = self.run_verify(env=env)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, '')
        self.assertNotIn('artificial-test-passphrase-only', missing.stderr + tampered.stderr + failed.stderr)

    def test_wrong_key_and_modified_source_manifest_fail(self):
        wrong_key = self.root / 'wrong-key'
        wrong_key.write_text('wrong artificial key')
        wrong_key.chmod(0o600)
        wrong = self.run_verify(key=wrong_key)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertEqual(wrong.stdout, '')
        self.assertNotIn('wrong artificial key', wrong.stderr)
        copied = self.bundle / self.source_manifest.name
        copied.write_bytes(copied.read_bytes() + b' ')
        changed = self.run_verify()
        self.assertNotEqual(changed.returncode, 0)
        self.assertEqual(changed.stdout, '')


if __name__ == '__main__':
    unittest.main()
