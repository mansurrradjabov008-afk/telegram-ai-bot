"""
Tests for Backup Rotation and Integrity Manager (Step 1.10).
Tested strictly on tmp_path; never touches production data/ directory.
"""

import sqlite3
import time
from pathlib import Path
import pytest
from services.backup_manager import (
    create_safe_backup,
    plan_backup_rotation,
    execute_backup_rotation,
)


def test_create_safe_backup_and_integrity(tmp_path):
    """create_safe_backup uses Connection.backup() and verifies integrity."""
    db_file = tmp_path / "live.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO test_data (val) VALUES ('hello');")
    conn.commit()
    conn.close()

    backup_dir = tmp_path / "backups"
    ok, b_path, res = create_safe_backup(db_file, target_dir=backup_dir)

    assert ok is True
    assert b_path.exists()
    assert res == "ok"


def test_backup_rotation_plan_and_dry_run(tmp_path):
    """plan_backup_rotation keeps newest keep_count and identifies older ones."""
    backup_dir = tmp_path / "backup_store"
    backup_dir.mkdir()

    # Create 8 dummy backup files with staggered timestamps
    created_files = []
    for i in range(8):
        f = backup_dir / f"butler_backup_20261010_00000{i}.db"
        f.write_text("test")
        created_files.append(f)
        time.sleep(0.01)

    kept, to_delete = plan_backup_rotation(backup_dir, keep_count=5)

    assert len(kept) == 5
    assert len(to_delete) == 3
    # Kept should be the newest 5
    assert kept == created_files[-5:]
    assert to_delete == created_files[:3]

    # Dry run deletes nothing
    dry_res = execute_backup_rotation(backup_dir, keep_count=5, dry_run=True)
    assert len(dry_res) == 3
    for f in created_files:
        assert f.exists()

    # Real run in temp folder only
    del_res = execute_backup_rotation(backup_dir, keep_count=5, dry_run=False)
    assert len(del_res) == 3
    for f in created_files[:3]:
        assert not f.exists()
    for f in created_files[-5:]:
        assert f.exists()
