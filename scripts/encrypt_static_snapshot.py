#!/usr/bin/env python3
"""Encrypt a stopped, standalone SQLite snapshot without another raw DB copy.

The source must already be a static snapshot. This command never checkpoints,
copies, modifies, or removes it; a concurrent writer makes the result invalid.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

GIB=1024**3
MIB=1024**2
MIN_START_FREE=GIB
MIN_RUNNING_FREE=512*MIB
CHUNK=1024*1024

class SnapshotError(RuntimeError):
    pass

def free_bytes(directory):
    return shutil.disk_usage(directory).free

def ensure_space(directory,minimum):
    if free_bytes(directory)<minimum:
        raise SnapshotError('OUTPUT_SPACE_LOW')

def source_identity(source):
    if source.is_symlink():raise SnapshotError('SOURCE_SYMLINK_REFUSED')
    info=source.stat()
    if not stat.S_ISREG(info.st_mode):raise SnapshotError('SOURCE_NOT_REGULAR')
    for suffix in ('-wal','-shm','-journal'):
        sidecar=Path(str(source)+suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise SnapshotError('SOURCE_NOT_STANDALONE: '+sidecar.name)
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def ensure_unchanged(source,identity):
    if source_identity(source)!=identity:raise SnapshotError('SOURCE_CHANGED_DURING_SNAPSHOT')

def quick_check_readonly(source):
    # immutable=1 keeps this inspection from creating WAL/SHM sidecars.
    connection=sqlite3.connect(source.as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA cache_size=-65536')
        rows=connection.execute('PRAGMA quick_check').fetchall()
        if rows!=[('ok',)]:raise SnapshotError('SOURCE_QUICK_CHECK_FAILED')
    finally:
        connection.close()

def passphrase_keyfile(path):
    if path.is_symlink():raise SnapshotError('PASSPHRASE_SYMLINK_REFUSED')
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size==0:
        raise SnapshotError('PASSPHRASE_FILE_INVALID')
    if info.st_mode & 0o077:
        raise SnapshotError('PASSPHRASE_FILE_PERMISSIONS')

class SpaceMonitor:
    def __init__(self,directory):
        self.directory=directory
        self.stop=threading.Event()
        self.low=threading.Event()
        self.processes=[]
        self.thread=None
    def start(self):
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.thread.start()
    def _run(self):
        while not self.stop.wait(0.2):
            try:available=free_bytes(self.directory)
            except OSError:available=0
            if available<MIN_RUNNING_FREE:
                self.low.set()
                for process in list(self.processes):
                    if process.poll() is None:
                        try:process.terminate()
                        except ProcessLookupError:pass
                return
    def check(self):
        if self.low.is_set() or free_bytes(self.directory)<MIN_RUNNING_FREE:
            raise SnapshotError('OUTPUT_SPACE_LOW_DURING_PROCESSING')
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=2)

def stop_processes(*processes):
    for process in processes:
        if process is not None and process.poll() is None:
            try:process.terminate()
            except ProcessLookupError:pass
    for process in processes:
        if process is None:continue
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill();process.wait()

def encrypt_stream(source,identity,encrypted_temp,keyfile,monitor,zstd_exe,gpg_exe):
    raw_sha=hashlib.sha256()
    raw_bytes=0
    zstd=None
    gpg=None
    try:
        zstd=subprocess.Popen([zstd_exe,'-3','-T2','-c'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        gpg=subprocess.Popen([gpg_exe,'--batch','--yes','--no-tty','--pinentry-mode','loopback',
            '--passphrase-file',str(keyfile),'--symmetric','--cipher-algo','AES256',
            '--compress-algo','none','--output',str(encrypted_temp)],
            stdin=zstd.stdout,stderr=subprocess.DEVNULL)
        zstd.stdout.close()
        monitor.processes=[zstd,gpg]
        monitor.start()
        flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
        fd=os.open(source,flags)
        with os.fdopen(fd,'rb') as raw:
            info=os.fstat(raw.fileno())
            if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=identity:
                raise SnapshotError('SOURCE_CHANGED_DURING_SNAPSHOT')
            while chunk:=raw.read(CHUNK):
                monitor.check()
                raw_sha.update(chunk)
                raw_bytes+=len(chunk)
                zstd.stdin.write(chunk)
                monitor.check()
        zstd.stdin.close()
        zstd_status=zstd.wait()
        gpg_status=gpg.wait()
        if zstd_status or gpg_status:
            raise SnapshotError('ENCRYPTION_PIPELINE_FAILED')
        monitor.check()
    except (BrokenPipeError,OSError) as exc:
        if monitor.low.is_set():raise SnapshotError('OUTPUT_SPACE_LOW_DURING_PROCESSING') from exc
        raise SnapshotError('ENCRYPTION_PIPELINE_FAILED') from exc
    finally:
        if zstd is not None and zstd.stdin and not zstd.stdin.closed:
            try:zstd.stdin.close()
            except BrokenPipeError:pass
        stop_processes(zstd,gpg)
        monitor.close()
    return raw_bytes,raw_sha.hexdigest()

def decrypt_hash(encrypted_temp,keyfile,monitor,zstd_exe,gpg_exe):
    restored_sha=hashlib.sha256()
    restored_bytes=0
    gpg=None
    zstd=None
    try:
        gpg=subprocess.Popen([gpg_exe,'--batch','--yes','--no-tty','--pinentry-mode','loopback',
            '--passphrase-file',str(keyfile),'--decrypt',str(encrypted_temp)],
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        zstd=subprocess.Popen([zstd_exe,'-d','-c'],stdin=gpg.stdout,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        gpg.stdout.close()
        monitor.processes=[gpg,zstd]
        monitor.start()
        while chunk:=zstd.stdout.read(CHUNK):
            monitor.check()
            restored_sha.update(chunk)
            restored_bytes+=len(chunk)
        zstd.stdout.close()
        zstd_status=zstd.wait()
        gpg_status=gpg.wait()
        if zstd_status or gpg_status:
            raise SnapshotError('DECRYPTION_PIPELINE_FAILED')
        monitor.check()
    finally:
        stop_processes(gpg,zstd)
        monitor.close()
    return restored_bytes,restored_sha.hexdigest()

def file_hashes(path):
    sha=hashlib.sha256()
    md5=hashlib.md5()
    count=0
    with path.open('rb') as stream:
        while chunk:=stream.read(CHUNK):
            sha.update(chunk);md5.update(chunk);count+=len(chunk)
        os.fsync(stream.fileno())
    return {'bytes':count,'sha256':sha.hexdigest(),'md5':md5.hexdigest()}

def write_manifest_temp(directory,data):
    fd,name=tempfile.mkstemp(prefix='.tmp_manifest_',dir=directory)
    path=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            json.dump(data,output,ensure_ascii=False,indent=2)
            output.write('\n')
            output.flush();os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path

def fsync_directory(directory):
    fd=os.open(directory,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def make_snapshot(source,output_dir,keyfile):
    source=Path(source).absolute()
    output_dir=Path(output_dir).absolute()
    keyfile=Path(keyfile).absolute()
    identity=source_identity(source)
    passphrase_keyfile(keyfile)
    if output_dir.is_symlink():raise SnapshotError('OUTPUT_SYMLINK_REFUSED')
    output_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    ensure_space(output_dir,MIN_START_FREE)
    quick_check_readonly(source)
    ensure_unchanged(source,identity)
    zstd_exe=shutil.which('zstd')
    gpg_exe=shutil.which('gpg')
    if not zstd_exe or not gpg_exe:raise SnapshotError('ZSTD_OR_GPG_MISSING')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    unique=uuid.uuid4().hex
    prefix=f'{source.name}.{stamp}.{unique}'
    enc_final=output_dir/(prefix+'.zst.gpg')
    manifest_final=output_dir/(prefix+'.manifest.json')
    fd,enc_name=tempfile.mkstemp(prefix='.tmp_enc_',dir=output_dir)
    os.close(fd)
    enc_temp=Path(enc_name)
    manifest_temp=None
    enc_published=False
    try:
        raw_bytes,raw_sha=encrypt_stream(source,identity,enc_temp,keyfile,SpaceMonitor(output_dir),zstd_exe,gpg_exe)
        ensure_unchanged(source,identity)
        if raw_bytes!=identity[2]:raise SnapshotError('SOURCE_SIZE_CHANGED')
        ensure_space(output_dir,MIN_RUNNING_FREE)
        enc_info=file_hashes(enc_temp)
        restored_bytes,restored_sha=decrypt_hash(enc_temp,keyfile,SpaceMonitor(output_dir),zstd_exe,gpg_exe)
        if (restored_bytes,restored_sha)!=(raw_bytes,raw_sha):
            raise SnapshotError('DECRYPTED_HASH_OR_SIZE_MISMATCH')
        ensure_unchanged(source,identity)
        ensure_space(output_dir,MIN_RUNNING_FREE)
        manifest={
            'timestamp':datetime.now(timezone.utc).isoformat(),
            'raw_snapshot':{'basename':source.name,'bytes':raw_bytes,'sha256':raw_sha,'quick_check':'ok'},
            'encrypted_snapshot':{'basename':enc_final.name,**enc_info,'compression':'zstd','cipher':'AES256'},
            'verification':{'sha256_match':True,'quick_check':'ok'},
        }
        manifest_temp=write_manifest_temp(output_dir,manifest)
        ensure_unchanged(source,identity)
        ensure_space(output_dir,MIN_RUNNING_FREE)
        # Each hard link is atomic and refuses to overwrite. The manifest is the
        # final completion marker; an orphan encrypted file is never success.
        os.link(enc_temp,enc_final)
        enc_published=True
        os.link(manifest_temp,manifest_final)
        fsync_directory(output_dir)
        return {'status':'ok','encrypted_path':str(enc_final),'manifest_path':str(manifest_final)}
    except BaseException as exc:
        if enc_published and not manifest_final.exists():
            raise SnapshotError('MANIFEST_NOT_PUBLISHED; orphan encrypted file: '+str(enc_final)) from exc
        raise
    finally:
        enc_temp.unlink(missing_ok=True)
        if manifest_temp is not None:manifest_temp.unlink(missing_ok=True)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path)
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--passphrase-file',required=True,type=Path)
    args=parser.parse_args(argv)
    try:result=make_snapshot(args.db,args.output_dir,args.passphrase_file)
    except (OSError,sqlite3.Error,SnapshotError) as exc:
        print('Snapshot failed: '+str(exc),file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':sys.exit(main())
