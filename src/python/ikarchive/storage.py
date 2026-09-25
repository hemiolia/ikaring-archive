"""Storage health and backup space checks."""

from pathlib import Path
import shutil
import sqlite3
from typing import Any, Dict, Union

LOW_SPACE_BYTES = 2 * 1024**3
CRITICAL_SPACE_BYTES = 512 * 1024**2


def storage_health(path: Union[str, Path]) -> Dict[str, Any]:
    target = Path(path)
    if target.is_file():
        target = target.parent
    while not target.exists():
        parent = target.parent
        if parent == target:
            break
        target = parent

    usage = shutil.disk_usage(target)
    free = usage.free
    total = usage.total

    if free < CRITICAL_SPACE_BYTES:
        state = "critical"
    elif free < LOW_SPACE_BYTES:
        state = "low"
    else:
        state = "normal"

    return {
        "state": state,
        "free_bytes": free,
        "total_bytes": total,
        "low_space_bytes": LOW_SPACE_BYTES,
        "critical_space_bytes": CRITICAL_SPACE_BYTES,
    }


def backup_required_bytes(connection: sqlite3.Connection) -> int:
    cursor = connection.cursor()
    cursor.execute("PRAGMA page_count")
    page_count = cursor.fetchone()[0]
    cursor.execute("PRAGMA page_size")
    page_size = cursor.fetchone()[0]
    return int(page_count * page_size + 64 * 1024**2)


def ensure_backup_space(connection: sqlite3.Connection, destination: Union[str, Path]) -> int:
    dest_path = Path(destination)
    if dest_path.exists():
        raise ValueError("BACKUP_DESTINATION_EXISTS")

    required = backup_required_bytes(connection)
    health = storage_health(dest_path)
    free = health["free_bytes"]

    if free - required < CRITICAL_SPACE_BYTES:
        raise RuntimeError("BACKUP_INSUFFICIENT_SPACE")

    return required
