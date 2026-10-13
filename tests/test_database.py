"""
Unit tests for SQLite Database and CRM operations.
"""

import pytest
import pytest_asyncio
from pathlib import Path
import aiosqlite

from database.connection import init_db, get_db
from database.crud import DatabaseManager
from config import settings


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "test_butler.db"
    await init_db(db_file)
    manager = DatabaseManager(db_path=db_file)
    return manager


@pytest.mark.asyncio
async def test_crm_contact_crud(temp_db: DatabaseManager):
    # 1. Upsert contact
    contact = await temp_db.upsert_contact(
        user_id=123456,
        username="bestfriend",
        full_name="Akmal Do'st",
        relationship_type="yaqin_dost",
        custom_instructions="Juda do'stona bo'l, bro deb yoz.",
    )
    assert contact is not None
    assert contact.user_id == 123456
    assert contact.username == "bestfriend"
    assert contact.relationship_type == "yaqin_dost"

    # 2. Get by ID
    by_id = await temp_db.get_contact_by_id(123456)
    assert by_id is not None
    assert by_id.full_name == "Akmal Do'st"

    # 3. Get by username
    by_user = await temp_db.get_contact_by_username("@bestfriend")
    assert by_user is not None
    assert by_user.user_id == 123456

    # 4. Update instructions
    updated = await temp_db.upsert_contact(
        user_id=123456,
        full_name="Akmal Do'st (Yangi)",
        relationship_type="yaqin_dost",
        custom_instructions="Endi o'zbekcha-inglizcha aralashtir.",
    )
    assert updated.full_name == "Akmal Do'st (Yangi)"
    assert "aralashtir" in updated.custom_instructions

    # 5. List contacts
    all_contacts = await temp_db.list_contacts()
    assert len(all_contacts) == 1


@pytest.mark.asyncio
async def test_scheduled_tasks_crud(temp_db: DatabaseManager):
    task = await temp_db.create_scheduled_task(
        target_peer="@ali",
        message_text="Salom Ali, ertaga ko'rishamiz!",
        scheduled_time="2026-10-08T07:00:00+05:00",
    )
    assert task.id is not None
    assert task.status == "pending"

    pending = await temp_db.get_pending_tasks()
    assert len(pending) == 1
    assert pending[0].target_peer == "@ali"

    # Update status
    await temp_db.update_task_status(task.id, "executed")
    remaining = await temp_db.get_pending_tasks()
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_conversation_history(temp_db: DatabaseManager):
    await temp_db.log_message(101, "Ali", "contact", "Salom qalaysan?")
    await temp_db.log_message(101, "AI Butler", "assistant", "Zo'r rahmat, o'zingda nima gap?")

    history = await temp_db.get_recent_history(101, limit=5)
    assert len(history) == 2
    assert history[0].role == "contact"
    assert history[1].role == "assistant"


@pytest.mark.asyncio
async def test_contact_autonomy_modes(temp_db: DatabaseManager):
    """Test contact mode defaults, updates, CHECK constraints, and counts."""
    # 1. New contact defaults to 'draft'
    contact = await temp_db.upsert_contact(user_id=201, full_name="Vali", username="vali")
    assert contact.mode in ("auto", "draft")

    # 2. Update mode by user_id
    ok = await temp_db.update_contact_mode(user_id=201, mode="auto")
    assert ok is True
    updated = await temp_db.get_contact_by_id(201)
    assert updated.mode == "auto"

    # 3. Update mode by username
    ok = await temp_db.update_contact_mode_by_username(username="@vali", mode="mute")
    assert ok is True
    updated = await temp_db.get_contact_by_username("vali")
    assert updated.mode == "mute"

    # 4. Invalid mode raises ValueError
    with pytest.raises(ValueError):
        await temp_db.update_contact_mode(user_id=201, mode="invalid")

    # 5. DB-level CHECK constraint rejects invalid mode on raw SQL
    async with get_db(temp_db.db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute("INSERT INTO contacts (user_id, full_name, mode) VALUES (202, 'Gani', 'invalid')")

    # 6. Mode counts
    counts = await temp_db.get_mode_counts()
    assert counts["mute"] == 1
    assert counts["auto"] == 0
    assert counts["draft"] == 0


@pytest.mark.asyncio
async def test_drafts_lifecycle_and_constraints(temp_db: DatabaseManager):
    """Test draft creation, unique duplicate protection, atomic resolution, and expiration."""
    from datetime import datetime, timedelta

    exp_future = (datetime.now() + timedelta(hours=24)).isoformat()
    exp_past = (datetime.now() - timedelta(hours=1)).isoformat()

    # 1. Create draft
    draft = await temp_db.create_draft(
        source_message_id=9001,
        incoming_text="Qachon uchrashamiz?",
        proposed_reply="Ertaga soat 15:00 da.",
        expires_at=exp_future,
        contact_username="davron_aka",
        contact_user_id=301,
        is_sensitive=True,
        sensitive_reason="meeting_time",
    )
    assert draft is not None
    assert draft.id is not None
    assert draft.status == "pending"
    assert draft.is_sensitive is True
    assert draft.sensitive_reason == "meeting_time"

    # 2. Duplicate draft on same (contact_user_id, source_message_id) is blocked
    dup = await temp_db.create_draft(
        source_message_id=9001,
        incoming_text="Qachon uchrashamiz?",
        proposed_reply="Boshqa taklif.",
        expires_at=exp_future,
        contact_username="davron_aka",
        contact_user_id=301,
    )
    assert dup is None

    # 3. List and count pending drafts
    pending = await temp_db.list_pending_drafts()
    assert len(pending) == 1
    assert pending[0].id == draft.id
    cnt = await temp_db.count_pending_drafts()
    assert cnt == 1

    # 4. Atomic resolution - first press succeeds
    resolved = await temp_db.resolve_draft_atomic(draft.id, "sent")
    assert resolved is True

    # 5. Atomic resolution - second press fails (double-press protection)
    second_press = await temp_db.resolve_draft_atomic(draft.id, "sent")
    assert second_press is False

    # Status is now 'sent'
    updated_draft = await temp_db.get_draft_by_id(draft.id)
    assert updated_draft.status == "sent"

    # 6. Create expired draft and test expire_old_drafts
    exp_draft = await temp_db.create_draft(
        source_message_id=9002,
        incoming_text="Eski xabar",
        proposed_reply="Eski javob",
        expires_at=exp_past,
        contact_user_id=302,
    )
    assert exp_draft is not None
    expired_count = await temp_db.expire_old_drafts()
    assert expired_count == 1

    expired_record = await temp_db.get_draft_by_id(exp_draft.id)
    assert expired_record.status == "expired"


@pytest.mark.asyncio
async def test_migration_idempotent_and_data_preserved(tmp_path: Path):
    """
    Tests migration safety:
    1. Legacy database without 'mode' column and with existing contact data.
    2. Runs init_db to execute migration.
    3. Confirms data is preserved and mode defaults to MIGRATE_EXISTING_TO ('draft').
    4. Runs init_db a second time (idempotency test).
    5. Confirms schema and data integrity remains unaffected.
    """
    from unittest.mock import patch
    legacy_db_path = tmp_path / "legacy_migration.db"

    # Step 1: Create legacy DB schema without mode column
    async with aiosqlite.connect(legacy_db_path) as db:
        await db.execute("""
        CREATE TABLE contacts (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT NOT NULL,
            relationship_type TEXT DEFAULT 'general'
        );
        """)
        await db.execute(
            "INSERT INTO contacts (user_id, username, full_name, relationship_type) VALUES (?, ?, ?, ?)",
            (888, "legacy_boss", "Legacy User", "vip_client"),
        )
        await db.commit()

    # Step 2: Run migration via init_db with MIGRATE_EXISTING_TO='draft'
    with patch.object(settings, "MIGRATE_EXISTING_TO", "draft"):
        await init_db(legacy_db_path)

    mgr = DatabaseManager(db_path=legacy_db_path)

    # Step 3: Verify data preserved and mode migrated
    contact = await mgr.get_contact_by_id(888)
    assert contact is not None
    assert contact.username == "legacy_boss"
    assert contact.full_name == "Legacy User"
    assert contact.relationship_type == "vip_client"
    assert contact.mode == "draft"

    # Schema version 3 recorded
    v = await mgr.get_schema_version()
    assert v >= 3

    # Step 4: Run init_db a SECOND time (idempotency check)
    with patch.object(settings, "MIGRATE_EXISTING_TO", "draft"):
        await init_db(legacy_db_path)

    # Step 5: Data still preserved, no errors
    contact_after = await mgr.get_contact_by_id(888)
    assert contact_after is not None
    assert contact_after.username == "legacy_boss"
    assert contact_after.mode == "draft"


@pytest.mark.asyncio
async def test_contact_in_memory_cache_and_invalidation(temp_db: DatabaseManager):
    """Verify that get_contact uses in-memory cache and write operations invalidate it."""
    # 1. Upsert contact
    await temp_db.upsert_contact(user_id=777, full_name="Cache Test", username="cache_user", mode="auto")

    # 2. First fetch populates cache
    c1 = await temp_db.get_contact_by_id(777)
    assert c1 is not None
    assert 777 in temp_db._contact_id_cache
    assert "cache_user" in temp_db._contact_uname_cache

    # 3. Second fetch returns cached instance
    c2 = await temp_db.get_contact_by_id(777)
    assert c2 is c1

    # 4. By username returns cached instance
    c3 = await temp_db.get_contact_by_username("cache_user")
    assert c3 is c1

    # 5. Update mode invalidates cache
    await temp_db.update_contact_mode(user_id=777, mode="draft")
    assert 777 not in temp_db._contact_id_cache

    # 6. Re-fetch brings fresh data
    c4 = await temp_db.get_contact_by_id(777)
    assert c4.mode == "draft"
    assert 777 in temp_db._contact_id_cache


