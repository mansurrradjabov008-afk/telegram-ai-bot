"""
Backup Rotation and Integrity Manager (Step 1.10).
Uses Python sqlite3 Connection.backup() plus PRAGMA integrity_check.
Implements safe rotation with BACKUP_KEEP (default 5).
Implemented and unit tested on isolated databases; NOT executed on production.
"""

import os
import sqlite3
import datetime
from pathlib import Path
from typing import List, Tuple, Optional
from config import settings


def create_safe_backup(
    source_db_path: Path | str,
    target_dir: Optional[Path | str] = None,
) -> Tuple[bool, Path, str]:
    """
    Creates a transactionally safe online backup of source SQLite database
    using sqlite3 Connection.backup() and runs PRAGMA integrity_check.
    Returns (success: bool, backup_path: Path, integrity_result: str).
    """
    src_p = Path(source_db_path).resolve()
    if not src_p.exists():
        return False, Path(), "source_not_found"

    if target_dir is None:
        target_d = src_p.parent
    else:
        target_d = Path(target_dir).resolve()
        target_d.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = target_d / f"butler_backup_{timestamp}.db"

    src_conn = sqlite3.connect(f"file:{src_p}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(backup_file)

    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    # Integrity verification
    check_conn = sqlite3.connect(f"file:{backup_file}?mode=ro", uri=True)
    try:
        cur = check_conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        res = cur.fetchall()
        integrity_ok = bool(res and res[0][0] == "ok")
        return integrity_ok, backup_file, str(res[0][0] if res else "empty")
    finally:
        check_conn.close()


def get_backup_files(backup_dir: Path | str) -> List[Path]:
    """Lists all .db backup files in directory sorted by modification time (oldest first)."""
    b_dir = Path(backup_dir).resolve()
    if not b_dir.exists():
        return []

    backups = []
    for f in b_dir.glob("butler*backup*"):
        if f.is_file() and not f.name.endswith("-wal") and not f.name.endswith("-shm"):
            backups.append(f)

    backups.sort(key=lambda p: p.stat().st_mtime)
    return backups


def plan_backup_rotation(
    backup_dir: Path | str,
    keep_count: int = 5,
) -> Tuple[List[Path], List[Path]]:
    """
    Computes which backup files to keep and which to delete based on BACKUP_KEEP.
    Returns (kept_backups: List[Path], candidates_to_delete: List[Path]).
    Does NOT delete any files; returns plan only.
    """
    all_backups = get_backup_files(backup_dir)
    if len(all_backups) <= keep_count:
        return all_backups, []

    to_delete = all_backups[:-keep_count]
    to_keep = all_backups[-keep_count:]
    return to_keep, to_delete


def execute_backup_rotation(
    backup_dir: Path | str,
    keep_count: int = 5,
    dry_run: bool = True,
) -> List[Path]:
    """
    Executes backup rotation. When dry_run is True, deletes nothing.
    Returns list of deleted paths (or candidate paths in dry_run).
    """
    to_keep, to_delete = plan_backup_rotation(backup_dir, keep_count)
    if dry_run:
        return to_delete

    deleted = []
    for f in to_delete:
        try:
            f.unlink()
            deleted.append(f)
            # Remove associated -wal or -shm files if present
            wal_f = f.with_name(f"{f.name}-wal")
            shm_f = f.with_name(f"{f.name}-shm")
            if wal_f.exists():
                wal_f.unlink()
            if shm_f.exists():
                shm_f.unlink()
        except Exception:
            pass

    return deleted
