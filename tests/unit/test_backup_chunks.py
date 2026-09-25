import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts import backup_chunks

class BackupChunksTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='backup-chunks-test-')
        self.root=Path(self.tmp.name)
        self.cipher=self.root/'archive.unique.sqlite3.zst.gpg'
        self.data=b'ciphertext-unique-'+os.urandom(71)
        self.cipher.write_bytes(self.data)
        self.enc_manifest=self.root/'archive.unique.sqlite3.manifest.json'
        self.enc_manifest.write_text(json.dumps({
            'timestamp':'2026-09-25T00:00:00Z',
            'raw_snapshot':{'basename':'archive.sqlite3','bytes':123,'sha256':'0'*64,'quick_check':'ok'},
            'encrypted_snapshot':{'basename':self.cipher.name,'bytes':len(self.data),
                'sha256':hashlib.sha256(self.data).hexdigest(),'compression':'zstd','cipher':'AES256'},
            'verification':{'sha256_match':True,'quick_check':'ok'},
        }),encoding='utf-8')
        self.output=self.root/'parts'
        self.destination=self.root/'restored.gpg'
    def tearDown(self):self.tmp.cleanup()

    def split(self):
        result=backup_chunks.split(self.cipher,self.enc_manifest,self.output,17)
        bundle=Path(result['bundle_dir'])
        manifest=Path(result['chunk_manifest'])
        return bundle,manifest,json.loads(manifest.read_text(encoding='utf-8'))

    def test_split_and_join_verify_every_part_and_full_ciphertext(self):
        bundle,manifest,data=self.split()
        self.assertEqual(manifest.name,self.cipher.name+'.chunks.json')
        self.assertEqual(data['format'],'ikaring-archive-chunks-v1')
        self.assertEqual(data['ciphertext'],{'basename':self.cipher.name,'bytes':len(self.data),
            'sha256':hashlib.sha256(self.data).hexdigest()})
        self.assertEqual(data['encrypted_manifest']['basename'],self.enc_manifest.name)
        self.assertEqual((bundle/self.enc_manifest.name).read_bytes(),self.enc_manifest.read_bytes())
        self.assertEqual(data['parts'][0]['basename'],self.cipher.name+'.part-000000')
        self.assertGreater(len(data['parts']),1)
        for index,part in enumerate(data['parts']):
            content=(bundle/part['basename']).read_bytes()
            self.assertEqual(part['index'],index)
            self.assertEqual(len(content),part['bytes'])
            self.assertEqual(hashlib.sha256(content).hexdigest(),part['sha256'])
            self.assertLessEqual(len(content),17)
        result=backup_chunks.join(manifest,self.destination)
        self.assertEqual(result['sha256'],hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.destination.read_bytes(),self.data)
        self.assertEqual(self.destination.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.cipher.read_bytes(),self.data)

    def test_cli_default_is_64_mib_while_explicit_limit_remains_256_mib(self):
        self.assertEqual(backup_chunks.DEFAULT_CHUNK_SIZE,64*1024*1024)
        self.assertEqual(backup_chunks.MAX_CHUNK_SIZE,256*1024*1024)
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(backup_chunks.main(['split','--ciphertext',str(self.cipher),
                '--encrypted-manifest',str(self.enc_manifest),'--output-dir',str(self.output)]),0)
        result=json.loads(output.getvalue())
        manifest=json.loads(Path(result['chunk_manifest']).read_text(encoding='utf-8'))
        self.assertEqual(manifest['chunk_size'],64*1024*1024)
        self.assertEqual(len(manifest['parts']),1)
        with self.assertRaisesRegex(backup_chunks.ChunkError,'CHUNK_SIZE_OUT_OF_RANGE'):
            backup_chunks.split(self.cipher,self.enc_manifest,self.output,backup_chunks.MAX_CHUNK_SIZE+1)

    def test_missing_or_modified_part_fails_and_preserves_other_parts(self):
        bundle,manifest,data=self.split()
        first=bundle/data['parts'][0]['basename']
        first.unlink()
        with self.assertRaises(FileNotFoundError):backup_chunks.join(manifest,self.destination)
        self.assertFalse(self.destination.exists())
        self.assertTrue((bundle/data['parts'][1]['basename']).is_file())
        first.write_bytes(self.data[:17])
        first.write_bytes(b'X'+first.read_bytes()[1:])
        with self.assertRaisesRegex(backup_chunks.ChunkError,'PART_HASH_MISMATCH'):
            backup_chunks.join(manifest,self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.cipher.read_bytes(),self.data)

    def test_traversal_duplicate_order_and_manifest_reference_are_rejected(self):
        bundle,manifest,data=self.split()
        good=json.loads(manifest.read_text(encoding='utf-8'))
        for variant in ('traversal','order','reference'):
            altered=json.loads(json.dumps(good))
            if variant=='traversal':altered['parts'][0]['basename']='../outside'
            elif variant=='order':altered['parts'][1]['index']=0
            else:altered['encrypted_manifest']['basename']='../outside'
            manifest.write_text(json.dumps(altered),encoding='utf-8')
            with self.subTest(variant=variant),self.assertRaises(backup_chunks.ChunkError):
                backup_chunks.join(manifest,self.destination)
            self.assertFalse(self.destination.exists())
        manifest.write_text(json.dumps(good),encoding='utf-8')
        self.assertEqual((bundle/good['parts'][0]['basename']).read_bytes(),self.data[:17])

    def test_existing_destination_is_never_overwritten(self):
        _bundle,manifest,_data=self.split()
        self.destination.write_bytes(b'existing')
        with self.assertRaisesRegex(backup_chunks.ChunkError,'DESTINATION_EXISTS'):
            backup_chunks.join(manifest,self.destination)
        self.assertEqual(self.destination.read_bytes(),b'existing')

    def test_split_rejects_wrong_reference_and_symlink_source(self):
        bad=json.loads(self.enc_manifest.read_text(encoding='utf-8'))
        bad['encrypted_snapshot']['sha256']='f'*64
        self.enc_manifest.write_text(json.dumps(bad),encoding='utf-8')
        with self.assertRaisesRegex(backup_chunks.ChunkError,'CIPHERTEXT_MISMATCH'):
            backup_chunks.split(self.cipher,self.enc_manifest,self.output,17)
        self.assertEqual(list(self.output.iterdir()),[])
        self.enc_manifest.unlink()
        link=self.root/'cipher-link.gpg'
        link.symlink_to(self.cipher)
        with self.assertRaisesRegex(backup_chunks.ChunkError,'SYMLINK_REFUSED'):
            backup_chunks.split(link,self.root/'missing.manifest.json',self.output,17)
        self.assertEqual(self.cipher.read_bytes(),self.data)

if __name__=='__main__':unittest.main()
