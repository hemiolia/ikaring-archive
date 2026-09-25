import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts import encrypt_static_snapshot as snapshot
from scripts import verified_backup_support as backup_support

@unittest.skipUnless(shutil.which('zstd') and shutil.which('gpg'),'zstd and gpg required')
class EncryptStaticSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='snapshot-test-')
        self.root=Path(self.tmp.name)
        self.db=self.root/'static.sqlite3'
        connection=sqlite3.connect(self.db)
        connection.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, payload BLOB)')
        connection.execute('INSERT INTO records(payload) VALUES(?)',(os.urandom(1024*1024),))
        connection.commit();connection.close()
        self.raw_hash=hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.output=self.root/'encrypted';self.output.mkdir()
        self.key=self.root/'passphrase'
        fd=os.open(self.key,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as file:file.write(b'artificial-test-passphrase-only\n')
        self.gpg_home=self.root/'gnupg';self.gpg_home.mkdir(mode=0o700)
        self.env=patch.dict(os.environ,{'GNUPGHOME':str(self.gpg_home)})
        self.env.start()
    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def assert_no_artifacts(self):
        self.assertEqual(list(self.output.iterdir()),[])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),self.raw_hash)

    def test_real_gpg_zstd_roundtrip_and_compatible_manifest(self):
        result=snapshot.make_snapshot(self.db,self.output,self.key)
        self.assertEqual(result['status'],'ok')
        encrypted=Path(result['encrypted_path'])
        manifest_path=Path(result['manifest_path'])
        self.assertTrue(encrypted.is_file() and manifest_path.is_file())
        self.assertEqual(encrypted.stat().st_mode & 0o777,0o600)
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['raw_snapshot'],{
            'basename':self.db.name,'bytes':self.db.stat().st_size,
            'sha256':self.raw_hash,'quick_check':'ok'})
        self.assertEqual(manifest['encrypted_snapshot']['basename'],encrypted.name)
        self.assertEqual(manifest['encrypted_snapshot']['compression'],'zstd')
        self.assertEqual(manifest['encrypted_snapshot']['cipher'],'AES256')
        ciphertext=encrypted.read_bytes()
        self.assertEqual(manifest['encrypted_snapshot']['sha256'],hashlib.sha256(ciphertext).hexdigest())
        self.assertEqual(manifest['encrypted_snapshot']['md5'],hashlib.md5(ciphertext).hexdigest())
        self.assertEqual(manifest['verification'],{'sha256_match':True,'quick_check':'ok'})
        decrypted=subprocess.run([shutil.which('gpg'),'--batch','--no-tty','--pinentry-mode','loopback',
            '--passphrase-file',str(self.key),'--decrypt',str(encrypted)],capture_output=True)
        self.assertEqual(decrypted.returncode,0)
        restored=subprocess.run([shutil.which('zstd'),'-d','-c'],input=decrypted.stdout,capture_output=True)
        self.assertEqual(restored.returncode,0)
        self.assertEqual(hashlib.sha256(restored.stdout).hexdigest(),self.raw_hash)
        self.assertEqual(len(restored.stdout),self.db.stat().st_size)
        self.assertNotIn(b'artificial-test-passphrase-only',manifest_path.read_bytes())

    def test_tampered_ciphertext_fails_decrypt_verification(self):
        result=snapshot.make_snapshot(self.db,self.output,self.key)
        original=Path(result['encrypted_path']).read_bytes()
        tampered=self.root/'tampered.gpg'
        tampered.write_bytes(original[:-32]+bytes(x^0x55 for x in original[-32:]))
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.decrypt_hash(tampered,self.key,snapshot.SpaceMonitor(self.output),
                shutil.which('zstd'),shutil.which('gpg'))
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),self.raw_hash)

    def test_failed_pipe_keeps_source_and_publishes_nothing(self):
        failing=self.root/'failing-zstd'
        failing.write_text('#!/bin/sh\nexit 19\n',encoding='utf-8')
        failing.chmod(0o700)
        real_gpg=shutil.which('gpg')
        with patch.object(snapshot.shutil,'which',side_effect=lambda name:str(failing) if name=='zstd' else real_gpg):
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.make_snapshot(self.db,self.output,self.key)
        self.assert_no_artifacts()

    def test_initial_and_running_low_space_fail_without_final_files(self):
        with patch.object(snapshot,'free_bytes',return_value=800*snapshot.MIB):
            with self.assertRaisesRegex(snapshot.SnapshotError,'OUTPUT_SPACE_LOW'):
                snapshot.make_snapshot(self.db,self.output,self.key)
        self.assert_no_artifacts()
        calls=0
        def declining(_directory):
            nonlocal calls
            calls+=1
            return 2*snapshot.GIB if calls<3 else 100*snapshot.MIB
        with patch.object(snapshot,'free_bytes',side_effect=declining):
            with self.assertRaisesRegex(snapshot.SnapshotError,'OUTPUT_SPACE_LOW_DURING_PROCESSING'):
                snapshot.make_snapshot(self.db,self.output,self.key)
        self.assert_no_artifacts()

    def test_sidecar_or_symlink_is_not_a_static_snapshot(self):
        sidecar=Path(str(self.db)+'-wal')
        sidecar.touch()
        with self.assertRaisesRegex(snapshot.SnapshotError,'SOURCE_NOT_STANDALONE'):
            snapshot.make_snapshot(self.db,self.output,self.key)
        sidecar.unlink()
        link=self.root/'db-link.sqlite3'
        link.symlink_to(self.db)
        with self.assertRaisesRegex(snapshot.SnapshotError,'SOURCE_SYMLINK_REFUSED'):
            snapshot.make_snapshot(link,self.output,self.key)
        self.assert_no_artifacts()

    def test_quick_check_cache_is_64_mib_and_connection_local(self):
        real_connect=sqlite3.connect
        observed=[]
        class ObservedConnection(sqlite3.Connection):
            def execute(self,statement,*args,**kwargs):
                if statement.strip().lower().startswith('pragma quick_check'):
                    observed.append(super().execute('PRAGMA cache_size').fetchone()[0])
                return super().execute(statement,*args,**kwargs)
        def instrumented_connect(*args,**kwargs):
            kwargs['factory']=ObservedConnection
            return real_connect(*args,**kwargs)
        def new_connection_cache():
            connection=real_connect(self.db)
            try:return connection.execute('PRAGMA cache_size').fetchone()[0]
            finally:connection.close()
        before=new_connection_cache()
        backup=self.root/'backup.sqlite3'
        with patch.object(snapshot.sqlite3,'connect',side_effect=instrumented_connect):
            snapshot.quick_check_readonly(self.db)
            self.assertEqual(backup_support.cmd_quick_check(argparse.Namespace(path=self.db)),0)
            self.assertEqual(backup_support.cmd_backup_db(argparse.Namespace(src_path=self.db,dst_path=backup)),0)
        after=new_connection_cache()
        self.assertEqual(observed,[-65536,-65536,-65536])
        self.assertEqual(after,before)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),self.raw_hash)

if __name__=='__main__':unittest.main()
