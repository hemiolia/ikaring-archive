"""Exercise NAS transport through a fake ssh that runs the remote shell command."""

import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import sync_gdrive, sync_nas


class TestNasTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.local = root / "local"
        self.remote = root / "remote's space"
        self.download = root / "download"
        self.bin = root / "bin"
        for path in (self.local, self.remote, self.download, self.bin):
            path.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text('#!/bin/sh\n[ "$1" = "fake-host" ] || exit 64\nshift\nexec /bin/sh -c "$*"\n')
        ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
        self.env = patch.dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_real_push_pull_and_remote_scan_with_shell_metacharacters(self):
        rel = "exports/quote's; $(exit 73) report.txt"
        source = self.local / rel
        source.parent.mkdir()
        source.write_bytes(b"new data\x00with bytes")
        old = self.remote / rel
        old.parent.mkdir()
        old.write_bytes(b"old data")

        sync_nas.push_file(source, rel, "fake-host", str(self.remote), True, self.local)
        self.assertEqual(old.read_bytes(), source.read_bytes())
        conflicts = list((self.remote / ".sync-conflicts").glob("*/" + rel))
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].read_bytes(), b"old data")
        self.assertEqual(sync_nas.scan_remote("fake-host", str(self.remote))[rel]["size"], source.stat().st_size)

        sync_nas.pull_file("fake-host", str(self.remote), rel, self.download / rel, self.download)
        self.assertEqual((self.download / rel).read_bytes(), source.read_bytes())

    def test_filename_starting_with_dash_roundtrips(self):
        rel = "exports/-leading.txt"
        source = self.local / rel
        source.parent.mkdir()
        source.write_bytes(b"dash name")
        sync_nas.push_file(source, rel, "fake-host", str(self.remote), False, self.local)
        sync_nas.pull_file("fake-host", str(self.remote), rel, self.download / rel, self.download)
        self.assertEqual((self.download / rel).read_bytes(), b"dash name")

    def test_valid_tar_with_nonzero_upstream_never_commits(self):
        rel = "exports/report.txt"
        source = self.local / rel
        source.parent.mkdir()
        source.write_bytes(b"new")
        target = self.remote / rel
        target.parent.mkdir()
        target.write_bytes(b"old")
        real_tar = shutil.which("tar", path=os.environ["PATH"].split(":", 1)[1])
        fake_tar = self.bin / "tar"
        fake_tar.write_text(
            '#!/bin/sh\n' +
            'case "$1" in\n'
            '  --no-mac-metadata) ' + real_tar + ' "$@"; exit 7;;\n'
            '  *) exec ' + real_tar + ' "$@";;\n'
            'esac\n'
        )
        fake_tar.chmod(fake_tar.stat().st_mode | stat.S_IXUSR)

        with self.assertRaisesRegex(RuntimeError, "tar=7"):
            sync_nas.push_file(source, rel, "fake-host", str(self.remote), True, self.local)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(self.remote.glob(".tmp_sync_stage_*")), [])
        self.assertFalse((self.remote / ".sync-conflicts").exists())

    def test_remote_symlink_rejected_before_any_outside_write(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.remote / "exports").symlink_to(outside, target_is_directory=True)
        source = self.local / "exports/report.txt"
        source.parent.mkdir()
        source.write_bytes(b"new")
        with self.assertRaisesRegex(RuntimeError, "Symlink rejected"):
            sync_nas.push_file(source, "exports/report.txt", "fake-host", str(self.remote), False, self.local)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(self.remote.glob(".tmp_sync_stage_*")), [])

    def test_conflict_archive_symlink_rejected_before_outside_write(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.remote / ".sync-conflicts").symlink_to(outside, target_is_directory=True)
        rel = "exports/report.txt"
        source = self.local / rel
        source.parent.mkdir()
        source.write_bytes(b"new")
        target = self.remote / rel
        target.parent.mkdir()
        target.write_bytes(b"old")
        with self.assertRaisesRegex(RuntimeError, "Symlink rejected"):
            sync_nas.push_file(source, rel, "fake-host", str(self.remote), True, self.local)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(outside.iterdir()), [])

    def test_pull_rejects_remote_symlink_without_replacing_local(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "report.txt").write_bytes(b"outside")
        (self.remote / "exports").symlink_to(outside, target_is_directory=True)
        target = self.download / "exports/report.txt"
        target.parent.mkdir()
        target.write_bytes(b"old")
        with self.assertRaisesRegex(RuntimeError, "Symlink rejected"):
            sync_nas.pull_file("fake-host", str(self.remote), "exports/report.txt", target, self.download)
        self.assertEqual(target.read_bytes(), b"old")

    def test_storage_marker_excluded_in_both_directions_but_other_config_kept(self):
        for root in (self.local, self.remote):
            config = root / "config"
            config.mkdir()
            (config / "storage-location.json").write_text('{"backend":"nas"}')
            (config / "other.json").write_text("{}")
        for ignore in (sync_nas.should_ignore, sync_gdrive.should_ignore):
            self.assertTrue(ignore("config/storage-location.json"))
            self.assertFalse(ignore("config/other.json"))
        for scan in (sync_nas.scan_local, sync_gdrive.scan_dir):
            for root in (self.local, self.remote):
                found = scan(root)
                self.assertNotIn("config/storage-location.json", found)
                self.assertIn("config/other.json", found)
        remote_found = sync_nas.scan_remote("fake-host", str(self.remote))
        self.assertNotIn("config/storage-location.json", remote_found)
        self.assertIn("config/other.json", remote_found)
        for planner in (sync_nas.plan_sync, sync_gdrive.plan_sync):
            to_push, to_pull = planner(sync_nas.scan_local(self.local), remote_found)
            self.assertEqual(to_push, [])
            self.assertEqual(to_pull, [])


if __name__ == "__main__":
    unittest.main()
