#!/usr/bin/env python3
"""Read back cloud backup parts and verify the decrypted snapshot as a stream.

Only ``rclone cat`` is used on the remote. No ciphertext or raw database copy is
created locally. The source manifest's SQLite quick_check is provenance, not a
fresh SQLite check performed by this command.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_chunks

READ_SIZE = 1024 * 1024
MAX_CHUNK_MANIFEST = 16 * 1024 * 1024
MAX_ENCRYPTED_MANIFEST = 1024 * 1024


class VerificationError(RuntimeError):
    pass


def _regular_file(path, limit):
    if path.is_symlink():
        raise VerificationError('LOCAL_SYMLINK_REFUSED')
    try:
        info = path.stat()
    except OSError as exc:
        raise VerificationError('LOCAL_FILE_UNAVAILABLE') from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise VerificationError('LOCAL_FILE_INVALID')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_checked(path, limit):
    before = _regular_file(path, limit)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            data = stream.read(limit + 1)
            info = os.fstat(stream.fileno())
            opened = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except OSError as exc:
        raise VerificationError('LOCAL_FILE_UNAVAILABLE') from exc
    if len(data) > limit or opened != before or _regular_file(path, limit) != before:
        raise VerificationError('LOCAL_FILE_CHANGED')
    return data


def _validate_keyfile(path):
    info = _regular_file(path, MAX_ENCRYPTED_MANIFEST)
    if info[2] == 0 or path.stat().st_mode & 0o077:
        raise VerificationError('PASSPHRASE_FILE_INVALID')


def _remote_path(remote, basename):
    if not re.fullmatch(r'[A-Za-z0-9_-]+:.+', remote) or remote.endswith('/'):
        raise VerificationError('REMOTE_PATH_INVALID')
    backup_chunks.safe_basename(basename)
    return remote + '/' + basename


def _stop_process(process):
    if process is None:
        return
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()


def _load_manifest(chunk_manifest):
    if chunk_manifest.parent.is_symlink():
        raise VerificationError('BUNDLE_SYMLINK_REFUSED')
    raw = _read_checked(chunk_manifest, MAX_CHUNK_MANIFEST)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise VerificationError('CHUNK_MANIFEST_INVALID_JSON') from exc
    try:
        cipher, reference, parts = backup_chunks.validate_chunk_manifest(data)
    except backup_chunks.ChunkError as exc:
        raise VerificationError('CHUNK_MANIFEST_INVALID') from exc
    source_path = chunk_manifest.parent / reference['basename']
    source = _read_checked(source_path, MAX_ENCRYPTED_MANIFEST)
    if len(source) != reference['bytes'] or hashlib.sha256(source).hexdigest() != reference['sha256']:
        raise VerificationError('ENCRYPTED_MANIFEST_HASH_MISMATCH')
    try:
        source_data = backup_chunks.load_encrypted_manifest(
            source, cipher['basename'], cipher['bytes'], cipher['sha256'])
    except backup_chunks.ChunkError as exc:
        raise VerificationError('ENCRYPTED_MANIFEST_INVALID') from exc
    return data, raw, source_data, cipher, parts


def verify(remote, chunk_manifest, passphrase_file):
    chunk_manifest = Path(chunk_manifest).absolute()
    passphrase_file = Path(passphrase_file).absolute()
    _validate_keyfile(passphrase_file)
    data, manifest_bytes, source_data, cipher_expected, parts = _load_manifest(chunk_manifest)
    if not all(shutil.which(tool) for tool in ('rclone', 'gpg', 'zstd')):
        raise VerificationError('REQUIRED_TOOL_MISSING')
    # Validate every path before starting any cloud reads.
    remote_parts = [_remote_path(remote, part['basename']) for part in parts]
    expected_raw = source_data['raw_snapshot']
    raw_digest = hashlib.sha256()
    raw_size = 0
    raw_error = []
    gpg = None
    zstd = None
    current_rclone = None
    completed = False

    def read_raw():
        nonlocal raw_size
        try:
            while chunk := zstd.stdout.read(READ_SIZE):
                raw_size += len(chunk)
                if raw_size > expected_raw['bytes']:
                    raise VerificationError('RAW_SIZE_EXCEEDED')
                raw_digest.update(chunk)
        except BaseException as exc:
            raw_error.append(exc)
            _stop_process(gpg)
            _stop_process(zstd)

    reader = None
    observed_parts = []
    cipher_digest = hashlib.sha256()
    cipher_size = 0
    try:
        gpg = subprocess.Popen(
            ['gpg', '--batch', '--yes', '--no-tty', '--pinentry-mode', 'loopback',
             '--passphrase-file', str(passphrase_file), '--decrypt'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        zstd = subprocess.Popen(['zstd', '-d', '-c'], stdin=gpg.stdout,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        gpg.stdout.close()
        reader = threading.Thread(target=read_raw, daemon=True)
        reader.start()
        for part, remote_part in zip(parts, remote_parts):
            current_rclone = subprocess.Popen(['rclone', 'cat', remote_part],
                                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            part_digest = hashlib.sha256()
            part_size = 0
            try:
                while chunk := current_rclone.stdout.read(READ_SIZE):
                    part_size += len(chunk)
                    cipher_size += len(chunk)
                    if part_size > part['bytes'] or cipher_size > cipher_expected['bytes']:
                        raise VerificationError('CLOUD_PART_SIZE_EXCEEDED')
                    part_digest.update(chunk)
                    cipher_digest.update(chunk)
                    gpg.stdin.write(chunk)
                current_rclone.stdout.close()
                if current_rclone.wait() != 0:
                    raise VerificationError('RCLONE_CAT_FAILED')
            finally:
                _stop_process(current_rclone)
                current_rclone.stdout.close()
                current_rclone = None
            if part_size != part['bytes'] or part_digest.hexdigest() != part['sha256']:
                raise VerificationError('CLOUD_PART_HASH_MISMATCH')
            observed_parts.append({'remote': remote_part, 'bytes': part_size,
                                   'sha256': part_digest.hexdigest()})
        gpg.stdin.close()
        reader.join()
        zstd.stdout.close()
        gpg_status = gpg.wait()
        zstd_status = zstd.wait()
        if raw_error:
            raise VerificationError('RAW_STREAM_FAILED') from raw_error[0]
        if gpg_status or zstd_status:
            raise VerificationError('DECRYPTION_PIPELINE_FAILED')
        if cipher_size != cipher_expected['bytes'] or cipher_digest.hexdigest() != cipher_expected['sha256']:
            raise VerificationError('CIPHERTEXT_HASH_MISMATCH')
        if raw_size != expected_raw['bytes'] or raw_digest.hexdigest() != expected_raw['sha256']:
            raise VerificationError('RAW_HASH_MISMATCH')
        completed = True
    except (BrokenPipeError, OSError) as exc:
        raise VerificationError('CLOUD_STREAM_FAILED') from exc
    finally:
        if current_rclone is not None:
            _stop_process(current_rclone)
        if gpg is not None and gpg.stdin and not gpg.stdin.closed:
            try:
                gpg.stdin.close()
            except BrokenPipeError:
                pass
        _stop_process(gpg)
        _stop_process(zstd)
        if reader is not None:
            reader.join(timeout=5)
        if not completed and zstd is not None and zstd.stdout:
            zstd.stdout.close()

    return {
        'status': 'verified',
        'verified_at_utc': datetime.now(timezone.utc).isoformat(),
        'remote': remote,
        'chunk_manifest': {'basename': chunk_manifest.name, 'bytes': len(manifest_bytes),
                           'sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                           'source': 'local_chunk_manifest'},
        'encrypted_manifest': {**data['encrypted_manifest'], 'source': 'local_copied_manifest'},
        'parts': observed_parts,
        'ciphertext': {'bytes': cipher_size, 'sha256': cipher_digest.hexdigest()},
        'raw_snapshot': {'bytes': raw_size, 'sha256': raw_digest.hexdigest(),
                         'quick_check': 'ok', 'quick_check_source': 'encrypted_manifest'},
        'verification': {'cloud_part_sha256': True, 'cloud_ciphertext_sha256': True,
                         'decrypted_raw_sha256': True, 'sqlite_quick_check_performed': False},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote', required=True, help='Read-only rclone remote and folder')
    parser.add_argument('--manifest', required=True, type=Path, help='Local chunk manifest')
    parser.add_argument('--passphrase-file', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = verify(args.remote, args.manifest, args.passphrase_file)
    except (VerificationError, backup_chunks.ChunkError) as exc:
        print('Cloud verification failed: ' + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print('Cloud verification failed: LOCAL_OR_PROCESS_ERROR', file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
