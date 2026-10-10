"""
Unit and integration tests for Writing Style Storage (CRUD and Schema).
"""

from datetime import datetime, timedelta
from pathlib import Path
import pytest
import pytest_asyncio

from database.connection import init_db
from database.crud import DatabaseManager


@pytest_asyncio.fixture
async def temp_style_db(tmp_path: Path):
    """Provides a fresh isolated temporary SQLite database for style storage testing."""
    temp_path = tmp_path / "test_style_storage.db"
    await init_db(temp_path)
    db_mgr = DatabaseManager(temp_path)
    await db_mgr.init_style_schema()
    return db_mgr, temp_path


@pytest.mark.asyncio
async def test_style_schema_init_idempotent(temp_style_db):
    db_mgr, _ = temp_style_db
    # Second call should not raise
    await db_mgr.init_style_schema()


@pytest.mark.asyncio
async def test_insert_and_get_style_samples(temp_style_db):
    db_mgr, _ = temp_style_db
    now_str = datetime.now().isoformat()

    sample_id = await db_mgr.insert_style_sample(
        contact_user_id=1001,
        tg_message_id=501,
        text_redacted="Salom, ertaga ko'rishamiz.",
        char_len=26,
        token_len=4,
        lang="uz-lat",
        register="sen",
        sent_at=now_str,
        source="import",
    )
    assert sample_id is not None

    samples = await db_mgr.get_style_samples(contact_user_id=1001)
    assert len(samples) == 1
    assert samples[0]["text_redacted"] == "Salom, ertaga ko'rishamiz."
    assert samples[0]["source"] == "import"

    cnt = await db_mgr.count_style_samples(contact_user_id=1001, lang="uz-lat")
    assert cnt == 1


@pytest.mark.asyncio
async def test_style_notes_crud(temp_style_db):
    db_mgr, _ = temp_style_db

    await db_mgr.upsert_style_note("global", "Doim qisqa va samimiy yozing.")
    note = await db_mgr.get_style_note("global")
    assert note == "Doim qisqa va samimiy yozing."

    # Update note
    await db_mgr.upsert_style_note("global", "Yangi eslatma.")
    note_up = await db_mgr.get_style_note("global")
    assert note_up == "Yangi eslatma."

    all_notes = await db_mgr.get_all_style_notes()
    assert all_notes["global"] == "Yangi eslatma."

    deleted = await db_mgr.delete_style_note("global")
    assert deleted is True
    assert await db_mgr.get_style_note("global") is None


@pytest.mark.asyncio
async def test_style_profiles_crud(temp_style_db):
    db_mgr, _ = temp_style_db

    await db_mgr.upsert_style_profile(
        scope="global",
        lang="uz-lat",
        stats_json='{"avg_len": 45}',
        phrases_json='["salom"]',
        sample_count=100,
    )

    prof = await db_mgr.get_style_profile("global", "uz-lat")
    assert prof is not None
    assert prof["sample_count"] == 100
    assert prof["stats_json"] == '{"avg_len": 45}'

    all_profs = await db_mgr.list_style_profiles()
    assert len(all_profs) == 1


@pytest.mark.asyncio
async def test_delete_and_forget_lifecycle(temp_style_db):
    db_mgr, _ = temp_style_db
    now_str = datetime.now().isoformat()

    await db_mgr.insert_style_sample(
        contact_user_id=2001,
        tg_message_id=601,
        text_redacted="Xabar 1",
        char_len=7,
        token_len=2,
        lang="uz-lat",
        register="siz",
        sent_at=now_str,
        source="import",
    )
    await db_mgr.insert_style_sample(
        contact_user_id=2002,
        tg_message_id=602,
        text_redacted="Xabar 2",
        char_len=7,
        token_len=2,
        lang="uz-lat",
        register="siz",
        sent_at=now_str,
        source="live",
    )
    await db_mgr.upsert_style_note("contact:2001", "Maxsus uslub")
    await db_mgr.upsert_style_profile("contact:2001", "uz-lat", "{}", "[]", 1)

    # Per-contact cleanup
    del_s = await db_mgr.delete_style_samples_for_contact(2001)
    del_p = await db_mgr.delete_style_profiles_for_contact(2001)
    assert del_s == 1
    assert del_p == 1
    assert await db_mgr.count_style_samples(contact_user_id=2001) == 0
    assert await db_mgr.count_style_samples(contact_user_id=2002) == 1

    # Full forget
    res = await db_mgr.delete_all_style_data()
    assert res["samples"] == 1
    assert await db_mgr.count_style_samples() == 0
