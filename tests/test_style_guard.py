"""
Safety guard test and autouse fixture for Production Database Protection (Rule E7).
Guarantees that test and scratch runners are strictly forbidden from pointing to
the production database (data/butler.db).
"""

import sqlite3
from pathlib import Path
import pytest
import aiosqlite
from config import settings

_PROD_DB_PATH = (settings.BASE_DIR / "data" / "butler.db").resolve()
_ORIG_SQLITE_CONNECT = sqlite3.connect


def guarded_sqlite_connect(database, *args, **kwargs):
    """
    Patched sqlite3.connect that prevents any connection to production database.
    """
    try:
        resolved = Path(database).resolve()
        if resolved == _PROD_DB_PATH:
            raise RuntimeError(
                f"SAFETY GUARD ACTIVATED: Connection to production database '{_PROD_DB_PATH}' is strictly forbidden during tests!"
            )
    except (TypeError, ValueError):
        pass
    return _ORIG_SQLITE_CONNECT(database, *args, **kwargs)


@pytest.fixture(autouse=True, scope="session")
def enforce_production_db_guard():
    """
    Session-level autouse fixture to patch sqlite3.connect and safeguard production DB.
    """
    sqlite3.connect = guarded_sqlite_connect
    yield
    sqlite3.connect = _ORIG_SQLITE_CONNECT


def test_style_guard_blocks_sync_production_db():
    """Verify synchronous sqlite3.connect to production DB raises RuntimeError."""
    with pytest.raises(RuntimeError, match="SAFETY GUARD ACTIVATED"):
        sqlite3.connect(_PROD_DB_PATH)


@pytest.mark.asyncio
async def test_style_guard_blocks_async_production_db():
    """Verify asynchronous aiosqlite.connect to production DB raises RuntimeError."""
    with pytest.raises(RuntimeError, match="SAFETY GUARD ACTIVATED"):
        async with aiosqlite.connect(_PROD_DB_PATH):
            pass


def test_style_guard_permits_temp_db(tmp_path: Path):
    """Verify connections to temporary isolated database files succeed."""
    temp_db_path = tmp_path / "safe_temp.db"
    conn = sqlite3.connect(temp_db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE test (id INTEGER PRIMARY KEY);")
        cursor.execute("INSERT INTO test (id) VALUES (1);")
        conn.commit()
        res = cursor.execute("SELECT id FROM test;").fetchone()
        assert res[0] == 1
    finally:
        conn.close()
