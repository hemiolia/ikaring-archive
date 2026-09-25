#!/usr/bin/env python3
"""Switch a stopped SQLite database to a verified, immutable snapshot.

The caller must stop every writer and keep it stopped until this command exits.
The lock only coordinates callers that use the same lock path. No file is deleted.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
from pathlib import Path

CHUNK = 1024 * 1024
SIDECARS = ("", "-wal", "-shm", "-journal")


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def regular_or_absent(path, required=False):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise ValueError(f"Required file is missing: {path}")
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"File must be regular and must not be a symlink: {path}")
    return info


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"File changed type: {path}")
        while True:
            block = os.read(fd, CHUNK)
            if not block:
                break
            digest.update(block)
            size += len(block)
    finally:
        os.close(fd)
    return size, digest.hexdigest()


def quick_check_immutable(path):
    # immutable=1 ensures SQLite cannot create or alter sidecars on the source.
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA cache_size=-65536")
        rows = connection.execute("PRAGMA quick_check").fetchall()
        if rows != [("ok",)]:
            raise ValueError(f"SQLite quick_check failed: {rows[:4]}")
    finally:
        connection.close()


def verify_snapshot(path, expected_bytes, expected_sha256):
    before = regular_or_absent(path, required=True)
    for suffix in SIDECARS[1:]:
        if os.path.lexists(str(path) + suffix):
            raise ValueError(f"Snapshot is not standalone: {path}{suffix}")
    if before.st_size != expected_bytes:
        raise ValueError("Snapshot size does not match expected bytes")
    actual = digest_file(path)
    if actual != (expected_bytes, expected_sha256):
        raise ValueError("Snapshot SHA-256 or byte count does not match")
    quick_check_immutable(path)
    after = regular_or_absent(path, required=True)
    if signature(before) != signature(after):
        raise ValueError("Snapshot changed during verification")
    return before


def copy_stage(source, destination, expected_bytes, expected_sha256):
    src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        src_before = os.fstat(src)
        if not stat.S_ISREG(src_before.st_mode):
            raise ValueError("Snapshot changed type before staging")
        dst = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            digest = hashlib.sha256()
            count = 0
            while True:
                block = os.read(src, CHUNK)
                if not block:
                    break
                digest.update(block)
                count += len(block)
                view = memoryview(block)
                while view:
                    view = view[os.write(dst, view):]
            os.fsync(dst)
        finally:
            os.close(dst)
        if signature(src_before) != signature(os.fstat(src)):
            raise ValueError("Snapshot changed during staging")
        if (count, digest.hexdigest()) != (expected_bytes, expected_sha256):
            raise ValueError("Staged copy differs from expected snapshot")
    finally:
        os.close(src)
    sync_dir(destination.parent)
    if digest_file(destination) != (expected_bytes, expected_sha256):
        raise ValueError("Staged copy failed independent hash verification")


class Journal:
    def __init__(self, directory, base):
        self.directory = directory
        self.base = base
        self.number = 0

    def record(self, event, **details):
        self.number += 1
        path = self.directory / f"{self.number:04d}-{event}.json"
        payload = {"event": event, "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "database": str(self.base), **details}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_dir(self.directory)


def move_and_sync(source, destination):
    if os.path.lexists(destination):
        raise FileExistsError(f"Refusing to replace existing file: {destination}")
    os.replace(source, destination)
    sync_dir(source.parent)
    if destination.parent != source.parent:
        sync_dir(destination.parent)


def unresolved_runs(database):
    prefix = f".{database.name}.cutover-"
    for path in database.parent.glob(prefix + "*"):
        if not path.is_dir() or path.is_symlink():
            raise ValueError(f"Unsafe cutover record: {path}")
        records = sorted(path.glob("[0-9][0-9][0-9][0-9]-*.json"))
        if not records or not (records[-1].name.endswith("-committed.json") or
                               records[-1].name.endswith("-rollback_complete.json")):
            raise ValueError(f"Earlier cutover needs inspection before retry: {path}")


def rollback(database, run_dir, retired, journal, old_signatures):
    receipt_errors = []

    def note(event, **details):
        try:
            journal.record(event, **details)
        except Exception as error:
            # Restoring the old files takes priority over writing diagnostics.
            receipt_errors.append(f"{event}: {error}")

    note("rollback_begin")
    failed_new = run_dir / "failed-new"
    failed_new.mkdir(mode=0o700)
    sync_dir(run_dir)
    for suffix in SIDECARS:
        current = Path(str(database) + suffix)
        old = retired / current.name
        if not os.path.lexists(old):
            if old_signatures[suffix] is None and os.path.lexists(current):
                regular_or_absent(current, required=True)
                note("save_failed_new_intent", name=current.name)
                move_and_sync(current, failed_new / current.name)
                note("saved_failed_new", name=current.name)
            elif old_signatures[suffix] is not None and not os.path.lexists(current):
                raise RuntimeError(f"Original file is missing during rollback: {current}")
            continue
        if os.path.lexists(current):
            regular_or_absent(current, required=True)
            note("save_failed_new_intent", name=current.name)
            move_and_sync(current, failed_new / current.name)
            note("saved_failed_new", name=current.name)
        note("restore_old_intent", name=current.name)
        move_and_sync(old, current)
        note("restored_old", name=current.name)
    note("rollback_complete")
    if receipt_errors:
        raise RuntimeError("DB files restored but rollback receipts failed: " + "; ".join(receipt_errors))


def cutover(snapshot, database, lock_path, expected_bytes, expected_sha256):
    snapshot = Path(os.path.abspath(snapshot))
    database = Path(os.path.abspath(database))
    lock_path = Path(os.path.abspath(lock_path))
    if snapshot == database or lock_path in (snapshot, database):
        raise ValueError("Snapshot, database and lock must be distinct paths")
    if not database.parent.is_dir() or database.parent.is_symlink():
        raise ValueError("Database parent must be an existing real directory")
    if not lock_path.parent.is_dir() or lock_path.parent.is_symlink():
        raise ValueError("Lock parent must be an existing real directory")
    regular_or_absent(lock_path)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError("Lock is not a regular file")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"Cutover lock is busy: {lock_path}") from error
        unresolved_runs(database)
        old_signatures = {}
        for suffix in SIDECARS:
            info = regular_or_absent(Path(str(database) + suffix), required=(suffix == ""))
            old_signatures[suffix] = signature(info) if info else None
        original = verify_snapshot(snapshot, expected_bytes, expected_sha256)
        run_dir = database.parent / f".{database.name}.cutover-{uuid.uuid4().hex}"
        run_dir.mkdir(mode=0o700)
        sync_dir(database.parent)
        retired = run_dir / "retired"
        retired.mkdir(mode=0o700)
        sync_dir(run_dir)
        journal = Journal(run_dir, database)
        stage = run_dir / "staged.sqlite3"
        try:
            journal.record("started", snapshot=str(snapshot), lock=str(lock_path),
                           expected_bytes=expected_bytes, expected_sha256=expected_sha256)
            copy_stage(snapshot, stage, expected_bytes, expected_sha256)
            if signature(original) != signature(regular_or_absent(snapshot, required=True)):
                raise ValueError("Snapshot changed after staging")
            journal.record("stage_verified", stage=str(stage))
            # Recheck the complete destination set immediately before the first move.
            for suffix in SIDECARS:
                info = regular_or_absent(Path(str(database) + suffix), required=(suffix == ""))
                if (signature(info) if info else None) != old_signatures[suffix]:
                    raise ValueError(f"Destination changed during verification: {database}{suffix}")
            journal.record("retire_begin")
            for suffix in SIDECARS:
                current = Path(str(database) + suffix)
                if not os.path.lexists(current):
                    continue
                journal.record("retire_intent", name=current.name)
                move_and_sync(current, retired / current.name)
                journal.record("retired", name=current.name)
            journal.record("retire_complete")
            for suffix in SIDECARS:
                if os.path.lexists(str(database) + suffix):
                    raise ValueError("Unexpected DB file appeared after retirement")
            journal.record("promote_intent")
            move_and_sync(stage, database)
            journal.record("promoted")
            if digest_file(database) != (expected_bytes, expected_sha256):
                raise ValueError("Canonical DB differs after promotion")
            for suffix in SIDECARS[1:]:
                if os.path.lexists(str(database) + suffix):
                    raise ValueError("Unexpected DB sidecar appeared after promotion")
            journal.record("committed", retired=str(retired), canonical_sha256=expected_sha256)
            return run_dir
        except Exception as error:
            try:
                rollback(database, run_dir, retired, journal, old_signatures)
            except Exception as rollback_error:
                raise RuntimeError(f"Cutover failed: {error}; rollback incomplete: {rollback_error}; inspect {run_dir}") from error
            raise RuntimeError(f"Cutover failed and original DB restored; inspect {run_dir}: {error}") from error
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--expected-bytes", type=int, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    if args.expected_bytes <= 0 or not re.fullmatch(r"[0-9a-fA-F]{64}", args.expected_sha256):
        parser.error("Expected bytes must be positive and SHA-256 must be 64 hex digits")
    try:
        receipt = cutover(args.snapshot, args.database, args.lock,
                          args.expected_bytes, args.expected_sha256.lower())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "committed", "receipt_dir": str(receipt)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
