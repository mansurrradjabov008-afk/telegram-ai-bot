"""
Tests for Database Isolation Guard (Step 1.1).
Guarantees that test and scratch runners are strictly forbidden from pointing to
the production database (data/butler.db).
"""

import os
import tempfile
from pathlib import Path
import pytest
from config import settings
from database.isolation_guard import ensure_not_production_db


def test_isolation_guard_refuses_production_db():
    """Guard must raise RuntimeError when pointing to production data/butler.db."""
    prod_path = (settings.BASE_DIR / "data" / "butler.db").resolve()
    with pytest.raises(RuntimeError, match="SAFETY GUARD ACTIVATED"):
        ensure_not_production_db(prod_path, caller_name="TestRunner")


def test_isolation_guard_permits_temp_db(tmp_path):
    """Guard must succeed when pointing to a temporary test database."""
    temp_db = tmp_path / "test_isolated.db"
    # Should not raise
    ensure_not_production_db(temp_db, caller_name="TestRunner")


@pytest.mark.asyncio
async def test_shadow_check_refuses_production_db():
    """scratch/shadow_check.py must refuse to run if directed at production DB."""
    from scratch.shadow_check import run_shadow_check
    prod_path = (settings.BASE_DIR / "data" / "butler.db").resolve()
    with pytest.raises(RuntimeError, match="SAFETY GUARD ACTIVATED"):
        await run_shadow_check(custom_db_path=prod_path)
