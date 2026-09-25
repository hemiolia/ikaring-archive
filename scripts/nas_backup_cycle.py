#!/usr/bin/env python3
"""NAS Backup Cycle Coordinator.

Executes a verified local SQLite backup inside a container, verifies the
generated artifacts, and coordinates the immutable upload to Google Drive
via rclone with roundtrip SHA256 integrity check and atomic cloud receipts.
"""

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Set, Tuple


def setup_environment() -> None:
    """Prepares required environment variables and working directories."""
    gnupg_home = pathlib.Path(os.environ.get("GNUPGHOME", "/tmp/gnupg"))
    gnupg_home.mkdir(parents=True, exist_ok=True)
    os.chmod(gnupg_home, 0o700)
    os.environ["GNUPGHOME"] = str(gnupg_home)

    if "RCLONE_CONFIG" not in os.environ:
        os.environ["RCLONE_CONFIG"] = "/secrets/rclone.conf"


def acquire_nonblocking_lock(backup_dir: pathlib.Path) -> int:
    """Acquires an exclusive, non-blocking lock to reject duplicate runs.

    Returns the open file descriptor which must be kept open until process exit.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    lock_file = backup_dir / ".nas_backup_cycle.lock"
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        os.close(fd)
        raise RuntimeError(f"Another backup cycle is already running: {e}")
    return fd


def compute_file_hashes(path: pathlib.Path) -> Tuple[int, str, str]:
    """Computes file size, SHA256 and MD5 hashes in 1MB chunks."""
    h_sha = hashlib.sha256()
    h_md5 = hashlib.md5()
    total_bytes = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h_sha.update(chunk)
            h_md5.update(chunk)
            total_bytes += len(chunk)
    return total_bytes, h_sha.hexdigest(), h_md5.hexdigest()


def compute_stream_sha256(stream) -> str:
    """Computes SHA256 hash by reading binary stream in 1MB chunks."""
    h_sha = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1048576), b""):
        h_sha.update(chunk)
    return h_sha.hexdigest()


def rclone_join(remote_dir: str, name: str) -> str:
    """Joins a remote directory specification and a target file basename."""
    if remote_dir.endswith(":"):
        return f"{remote_dir}{name}"
    return f"{remote_dir.rstrip('/')}/{name}"


def locate_backup_script() -> pathlib.Path:
    """Locates the nas_create_verified_backup.sh helper script."""
    self_dir = pathlib.Path(__file__).resolve().parent
    local_script = self_dir / "nas_create_verified_backup.sh"
    if local_script.is_file():
        return local_script

    system_script = pathlib.Path("/app/scripts/nas_create_verified_backup.sh")
    if system_script.is_file():
        return system_script

    raise FileNotFoundError("nas_create_verified_backup.sh not found in script dir or /app/scripts")


def run_create_backup(
    db_path: pathlib.Path, backup_dir: pathlib.Path, passphrase_file: pathlib.Path
) -> Dict[str, str]:
    """Executes nas_create_verified_backup.sh and parses its final JSON result."""
    script_path = locate_backup_script()
    cmd = [
        str(script_path),
        str(db_path.resolve()),
        str(backup_dir.resolve()),
        str(passphrase_file.resolve()),
    ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Backup creation script failed with exit code {proc.returncode}")

    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("Backup creation script produced empty stdout")

    last_line = lines[-1]
    try:
        data = json.loads(last_line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse JSON output from backup script: {e}; raw line: {last_line}")

    if not isinstance(data, dict) or data.get("status") != "ok":
        raise RuntimeError(f"Backup creation script returned non-ok status: {data}")

    for key in ("raw_path", "encrypted_path", "manifest_path"):
        if key not in data or not data[key]:
            raise RuntimeError(f"Backup creation result missing key '{key}': {data}")

    return data


def verify_backup_artifacts(
    backup_data: Dict[str, str]
) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path, Dict[str, any], Dict[str, any]]:
    """Verifies that backup artifacts exist, match the manifest, and hashes agree."""
    raw_path = pathlib.Path(backup_data["raw_path"]).resolve()
    enc_path = pathlib.Path(backup_data["encrypted_path"]).resolve()
    manifest_path = pathlib.Path(backup_data["manifest_path"]).resolve()

    if not raw_path.is_file():
        raise FileNotFoundError(f"Raw snapshot not found: {raw_path}")
    if not enc_path.is_file():
        raise FileNotFoundError(f"Encrypted snapshot not found: {enc_path}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"Manifest is not valid JSON: {e}")

    verification = manifest_data.get("verification")
    if not isinstance(verification, dict):
        raise ValueError("Manifest missing valid 'verification' section")
    if verification.get("sha256_match") is not True:
        raise ValueError("Manifest verification.sha256_match is not true")
    if verification.get("quick_check") != "ok":
        raise ValueError(f"Manifest verification.quick_check is not 'ok': {verification.get('quick_check')}")

    raw_snap = manifest_data.get("raw_snapshot")
    if not isinstance(raw_snap, dict) or raw_snap.get("quick_check") != "ok":
        raise ValueError(f"Manifest raw_snapshot quick_check is not 'ok': {raw_snap}")

    enc_snap = manifest_data.get("encrypted_snapshot")
    if not isinstance(enc_snap, dict):
        raise ValueError("Manifest missing valid 'encrypted_snapshot' section")

    if enc_snap.get("basename") != enc_path.name:
        raise ValueError(
            f"Encrypted basename mismatch: manifest '{enc_snap.get('basename')}' vs actual '{enc_path.name}'"
        )

    expected_bytes = int(enc_snap.get("bytes", -1))
    expected_sha256 = enc_snap.get("sha256")

    enc_bytes, enc_sha256, enc_md5 = compute_file_hashes(enc_path)
    if enc_bytes != expected_bytes:
        raise ValueError(
            f"Encrypted file size mismatch: manifest {expected_bytes} vs actual {enc_bytes}"
        )
    if enc_sha256 != expected_sha256:
        raise ValueError(
            f"Encrypted file sha256 mismatch: manifest '{expected_sha256}' vs actual '{enc_sha256}'"
        )

    man_bytes, man_sha256, man_md5 = compute_file_hashes(manifest_path)

    enc_hashes = {"bytes": enc_bytes, "sha256": enc_sha256, "md5": enc_md5}
    manifest_hashes = {"bytes": man_bytes, "sha256": man_sha256, "md5": man_md5}

    return raw_path, enc_path, manifest_path, enc_hashes, manifest_hashes


def check_remote_listing(remote_dir: str, enc_name: str, manifest_name: str) -> None:
    """Inspects remote directory listing and rejects existing destinations.

    Fails the cycle immediately if listing fails, rather than treating failure
    as non-existence.
    """
    proc = subprocess.run(["rclone", "lsf", remote_dir], stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to list remote directory: {remote_dir} (exit code {proc.returncode})")

    existing_files: Set[str] = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    if enc_name in existing_files:
        raise FileExistsError(f"Remote encrypted target already exists: {rclone_join(remote_dir, enc_name)}")
    if manifest_name in existing_files:
        raise FileExistsError(f"Remote manifest target already exists: {rclone_join(remote_dir, manifest_name)}")


def get_remote_file_size(remote_path: str) -> int:
    """Queries file size of a remote object using rclone size --json."""
    proc = subprocess.run(
        ["rclone", "size", "--json", remote_path],
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rclone size failed on {remote_path} (exit code {proc.returncode})")
    try:
        data = json.loads(proc.stdout)
        return int(data["bytes"])
    except Exception as e:
        raise RuntimeError(f"Failed to parse rclone size output for {remote_path}: {e}")


def get_remote_file_md5(remote_path: str) -> str:
    """Queries MD5 hash of a remote object using rclone md5sum."""
    proc = subprocess.run(
        ["rclone", "md5sum", remote_path],
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rclone md5sum failed on {remote_path} (exit code {proc.returncode})")
    tokens = proc.stdout.strip().split()
    if not tokens:
        raise RuntimeError(f"Empty rclone md5sum output for {remote_path}")
    return tokens[0]


def upload_and_verify_objects(
    remote_dir: str,
    enc_path: pathlib.Path,
    manifest_path: pathlib.Path,
    enc_hashes: Dict[str, any],
    manifest_hashes: Dict[str, any],
) -> Tuple[str, str]:
    """Uploads encrypted archive and manifest with interim verification.

    - Uploads encrypted object to unique temporary .uploading-<UUID> destination
    - Verifies size and MD5
    - Streams back content via rclone cat and compares SHA256 roundtrip
    - Promotes encrypted object using rclone moveto --immutable
    - Uploads manifest to temporary destination, verifies size and MD5, and promotes
    - Automatically cleans up only temporary uploading objects on failure, preserving
      any existing objects or raw backup files.
    """
    upload_uuid = uuid.uuid4().hex
    enc_uploading_remote = rclone_join(remote_dir, f"{enc_path.name}.uploading-{upload_uuid}")
    enc_final_remote = rclone_join(remote_dir, enc_path.name)
    manifest_uploading_remote = rclone_join(remote_dir, f"{manifest_path.name}.uploading-{upload_uuid}")
    manifest_final_remote = rclone_join(remote_dir, manifest_path.name)

    created_remotes: List[str] = []

    try:
        # 1. Upload encrypted file to temporary uploading destination
        subprocess.run(
            ["rclone", "copyto", str(enc_path), enc_uploading_remote, "--immutable"],
            check=True,
            stderr=sys.stderr,
        )
        created_remotes.append(enc_uploading_remote)

        # 2. Verify temporary encrypted object size and MD5
        remote_enc_size = get_remote_file_size(enc_uploading_remote)
        if remote_enc_size != enc_hashes["bytes"]:
            raise ValueError(
                f"Remote uploading encrypted size mismatch: remote {remote_enc_size} vs local {enc_hashes['bytes']}"
            )

        remote_enc_md5 = get_remote_file_md5(enc_uploading_remote)
        if remote_enc_md5 != enc_hashes["md5"]:
            raise ValueError(
                f"Remote uploading encrypted MD5 mismatch: remote {remote_enc_md5} vs local {enc_hashes['md5']}"
            )

        # 3. Read back full content via rclone cat and perform roundtrip SHA256 comparison
        cat_proc = subprocess.Popen(
            ["rclone", "cat", enc_uploading_remote],
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
        roundtrip_sha256 = compute_stream_sha256(cat_proc.stdout)
        cat_rc = cat_proc.wait()
        if cat_rc != 0:
            raise RuntimeError(f"rclone cat failed with exit code {cat_rc}")

        if roundtrip_sha256 != enc_hashes["sha256"]:
            raise ValueError(
                f"Remote roundtrip SHA256 mismatch: read {roundtrip_sha256} vs source {enc_hashes['sha256']}"
            )

        # 4. Finalize encrypted file with immutable move
        subprocess.run(
            ["rclone", "moveto", "--immutable", enc_uploading_remote, enc_final_remote],
            check=True,
            stderr=sys.stderr,
        )
        created_remotes.remove(enc_uploading_remote)

        # 5. Upload manifest file to temporary uploading destination
        subprocess.run(
            ["rclone", "copyto", str(manifest_path), manifest_uploading_remote, "--immutable"],
            check=True,
            stderr=sys.stderr,
        )
        created_remotes.append(manifest_uploading_remote)

        # 6. Verify temporary manifest object size and MD5
        remote_man_size = get_remote_file_size(manifest_uploading_remote)
        if remote_man_size != manifest_hashes["bytes"]:
            raise ValueError(
                f"Remote uploading manifest size mismatch: remote {remote_man_size} vs local {manifest_hashes['bytes']}"
            )

        remote_man_md5 = get_remote_file_md5(manifest_uploading_remote)
        if remote_man_md5 != manifest_hashes["md5"]:
            raise ValueError(
                f"Remote uploading manifest MD5 mismatch: remote {remote_man_md5} vs local {manifest_hashes['md5']}"
            )

        # 7. Finalize manifest file with immutable move
        subprocess.run(
            ["rclone", "moveto", "--immutable", manifest_uploading_remote, manifest_final_remote],
            check=True,
            stderr=sys.stderr,
        )
        created_remotes.remove(manifest_uploading_remote)

        return enc_final_remote, manifest_final_remote

    except Exception:
        # Failure cleanup: only clean up uploading objects from this invocation.
        # Never touch finalized objects or local backups.
        for rem in created_remotes:
            try:
                subprocess.run(["rclone", "deletefile", rem], stderr=sys.stderr)
            except Exception:
                pass
        raise


def write_cloud_receipt(
    backup_dir: pathlib.Path,
    raw_path: pathlib.Path,
    enc_path: pathlib.Path,
    manifest_path: pathlib.Path,
    enc_final_remote: str,
    manifest_final_remote: str,
    enc_hashes: Dict[str, any],
    manifest_hashes: Dict[str, any],
) -> pathlib.Path:
    """Writes a verified completion receipt using fsync and atomic rename.

    Contains no secrets, passwords, tokens, or account IDs.
    """
    receipts_dir = backup_dir / "cloud-receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    receipt_name = manifest_path.name if manifest_path.name.endswith(".json") else f"{manifest_path.name}.json"
    final_receipt_path = receipts_dir / receipt_name
    tmp_receipt_path = receipts_dir / f".tmp_receipt_{uuid.uuid4().hex}"

    receipt_data = {
        "status": "ok",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "raw_snapshot": {
            "basename": raw_path.name,
        },
        "encrypted_snapshot": {
            "basename": enc_path.name,
            "bytes": enc_hashes["bytes"],
            "sha256": enc_hashes["sha256"],
            "md5": enc_hashes["md5"],
            "remote": enc_final_remote,
        },
        "manifest": {
            "basename": manifest_path.name,
            "bytes": manifest_hashes["bytes"],
            "sha256": manifest_hashes["sha256"],
            "md5": manifest_hashes["md5"],
            "remote": manifest_final_remote,
        },
        "verification": {
            "remote_listing_checked": True,
            "upload_size_verified": True,
            "upload_md5_verified": True,
            "roundtrip_sha256_verified": True,
        },
    }

    try:
        with open(tmp_receipt_path, "w", encoding="utf-8") as f:
            json.dump(receipt_data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_receipt_path, final_receipt_path)

        try:
            dir_fd = os.open(str(receipts_dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass

        return final_receipt_path
    finally:
        if tmp_receipt_path.is_file():
            try:
                tmp_receipt_path.unlink(missing_ok=True)
            except Exception:
                pass


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="NAS Backup Cycle Coordinator")
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument("--backup-dir", required=True, help="Local backup target directory")
    parser.add_argument("--passphrase-file", required=True, help="Path to passphrase file")
    parser.add_argument("--remote", required=True, help="Rclone remote directory (e.g. gdrive:ikaring-backups)")

    args = parser.parse_args(argv)

    db_path = pathlib.Path(args.db).resolve()
    backup_dir = pathlib.Path(args.backup_dir).resolve()
    passphrase_file = pathlib.Path(args.passphrase_file).resolve()
    remote_dir = args.remote.strip()

    if not db_path.is_file():
        sys.stderr.write(f"Error: Database file does not exist: {db_path}\n")
        return 1

    if not passphrase_file.is_file():
        sys.stderr.write(f"Error: Passphrase file does not exist: {passphrase_file}\n")
        return 1

    if not remote_dir:
        sys.stderr.write("Error: Remote directory must not be empty\n")
        return 1

    setup_environment()

    # Reject duplicate concurrent runs using non-blocking flock
    try:
        lock_fd = acquire_nonblocking_lock(backup_dir)
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    try:
        # Step 1: Create verified backup snapshot
        backup_result = run_create_backup(db_path, backup_dir, passphrase_file)

        # Step 2: Verify local artifacts and manifest consistency
        raw_path, enc_path, manifest_path, enc_hashes, manifest_hashes = verify_backup_artifacts(backup_result)

        # Step 3: Remote listing check to ensure remote directory is reachable and destination does not exist
        check_remote_listing(remote_dir, enc_path.name, manifest_path.name)

        # Step 4 & 5: Upload encrypted file and manifest with verification and roundtrip check
        enc_remote, manifest_remote = upload_and_verify_objects(
            remote_dir, enc_path, manifest_path, enc_hashes, manifest_hashes
        )

        # Step 6: Atomic receipt generation
        receipt_path = write_cloud_receipt(
            backup_dir,
            raw_path,
            enc_path,
            manifest_path,
            enc_remote,
            manifest_remote,
            enc_hashes,
            manifest_hashes,
        )

        result_summary = {
            "status": "ok",
            "receipt": str(receipt_path),
            "remote_encrypted": enc_remote,
            "remote_manifest": manifest_remote,
            "bytes": enc_hashes["bytes"],
            "sha256": enc_hashes["sha256"],
        }
        sys.stdout.write(json.dumps(result_summary, indent=2) + "\n")
        return 0

    except Exception as e:
        sys.stderr.write(f"Error: Backup cycle failed: {e}\n")
        return 1
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
