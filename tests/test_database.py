"""
Unit tests for SQLite Database and CRM operations.
"""

import pytest
import pytest_asyncio
from pathlib import Path
import aiosqlite

from database.connection import init_db, get_db
from database.crud import DatabaseManager


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
