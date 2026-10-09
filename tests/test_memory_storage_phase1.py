"""
Comprehensive unit and integration test suite for Contact Memory Phase 1.
Covers:
- Schema migration v7 table creation and idempotency
- Serialized SQLite writer write concurrency proof (asyncio.Lock)
- Message logging with 5 sender attributions: contact, owner_manual, owner_edited, bot_approved, bot_auto
- Automatic pure redaction before storage (H5)
- Strict per-contact isolation (H4)
- Identity binding and username reassignment pause (H2)
- Mute mode check (H3)
- Media marker storage (is_media=1)
- Message editing and re-redaction
- Retention purge older than MEM_MESSAGE_RETENTION_DAYS
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import pytest
import pytest_asyncio
import aiosqlite

from database.connection import init_db, get_db
from database.crud import DatabaseManager
from database.models import MemMessage, MemContact
from services.memory_service import MemoryService
from config import settings


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    """Provides a fresh isolated temporary SQLite database for memory testing."""
    temp_path = tmp_path / "test_memory.db"
    await init_db(temp_path)
    db_mgr = DatabaseManager(temp_path)
    # Create CRM contacts for foreign keys
    await db_mgr.upsert_contact(user_id=1001, full_name="Ali Valiyev", username="ali_valiyev", mode="draft")
    await db_mgr.upsert_contact(user_id=1002, full_name="Vali Aliyev", username="vali_aliyev", mode="auto")
    await db_mgr.upsert_contact(user_id=1003, full_name="Sardor Muted", username="sardor_muted", mode="mute")
    return db_mgr, temp_path


class TestSchemaMigrationV7:
    @pytest.mark.asyncio
    async def test_v7_tables_and_indexes_exist(self, temp_db):
        db_mgr, temp_path = temp_db
        expected_tables = {
            "mem_contacts",
            "mem_messages",
            "mem_facts",
            "mem_open_items",
            "mem_summaries",
            "mem_embeddings",
            "mem_state",
        }

        async with get_db(temp_path) as db:
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
                rows = await cur.fetchall()
                found_tables = {r["name"] for r in rows}

            for t in expected_tables:
                assert t in found_tables, f"Expected table '{t}' not found in database!"

            # Verify schema_versions includes v7
            async with db.execute("SELECT MAX(version) as max_v FROM schema_versions") as cur:
                row = await cur.fetchone()
                assert row["max_v"] >= 7

    @pytest.mark.asyncio
    async def test_migration_is_idempotent(self, temp_db):
        db_mgr, temp_path = temp_db
        # Running init_db again should not error or duplicate schema
        await init_db(temp_path)
        await init_db(temp_path)
        async with get_db(temp_path) as db:
            async with db.execute("SELECT COUNT(*) as cnt FROM schema_versions WHERE version = 7") as cur:
                row = await cur.fetchone()
                assert row["cnt"] == 1


class TestSerializedWriterConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_writes_do_not_lock(self, temp_db):
        """Proves that multiple concurrent asyncio tasks write safely with serialized_transaction."""
        db_mgr, temp_path = temp_db
        mem_service = MemoryService(db_mgr)

        async def worker(task_idx: int):
            # Each worker attempts to log a message for contact 1001
            msg_id = 1000 + task_idx
            return await mem_service.log_message(
                contact_user_id=1001,
                tg_message_id=msg_id,
                sender="contact",
                text=f"Concurrent message {task_idx}: Mening kartam 8600 1234 5678 9012",
            )

        # Run 25 concurrent write operations simultaneously
        results = await asyncio.gather(*(worker(i) for i in range(25)))

        # All 25 must have succeeded without sqlite3.OperationalError: database is locked
        assert len(results) == 25
        assert all(res is not None for res in results)

        # Verify DB has exactly 25 messages
        msgs = await db_mgr.get_mem_messages(contact_user_id=1001, limit=50)
        assert len(msgs) == 25


class TestSenderAttributionsAndRedaction:
    @pytest.mark.asyncio
    async def test_all_five_sender_labels_and_automatic_redaction(self, temp_db):
        db_mgr, temp_path = temp_db
        mem_service = MemoryService(db_mgr)

        # 1. Contact incoming
        await mem_service.log_message(
            contact_user_id=1001,
            tg_message_id=1,
            sender="contact",
            text="Salom, mening kartam 8600 1234 5678 9012 va parol: secret123",
        )

        # 2. Bot auto reply
        await mem_service.log_message(
            contact_user_id=1001,
            tg_message_id=2,
            sender="bot_auto",
            text="Assalomu alaykum! Xabaringiz qabul qilindi.",
        )

        # 3. Owner manual reply
        await mem_service.log_message(
            contact_user_id=1001,
            tg_message_id=3,
            sender="owner_manual",
            text="Bugun soat 15:00 da uchrashamiz.",
        )

        # 4. Bot approved draft
        await mem_service.log_message(
            contact_user_id=1001,
            tg_message_id=4,
            sender="bot_approved",
            text="Rahmat, hisob raqamimiz 20208000100123456789.",
        )

        # 5. Owner edited draft
        await mem_service.log_message(
            contact_user_id=1001,
            tg_message_id=5,
            sender="owner_edited",
            text="Tahrirlangan javob: sizga tez orada aloqaga chiqaman.",
        )

        msgs = await db_mgr.get_mem_messages(contact_user_id=1001, limit=10)
        assert len(msgs) == 5

        # Check sender attributions
        senders = [m.sender for m in msgs]
        assert senders == ["contact", "bot_auto", "owner_manual", "bot_approved", "owner_edited"]

        # Check redactions before storage
        contact_msg = msgs[0]
        assert "8600 1234 5678 9012" not in contact_msg.text_redacted
        assert "secret123" not in contact_msg.text_redacted
        assert "[REDACTED]" in contact_msg.text_redacted

        bank_msg = msgs[3]
        assert "20208000100123456789" not in bank_msg.text_redacted
        assert "[REDACTED]" in bank_msg.text_redacted

        # Negative checks: normal time and numbers must remain intact
        owner_msg = msgs[2]
        assert "15:00" in owner_msg.text_redacted


class TestIsolationAndIdentityRules:
    @pytest.mark.asyncio
    async def test_strict_per_contact_isolation(self, temp_db):
        """H4: Context for contact A must strictly contain only data for contact A."""
        db_mgr, temp_path = temp_db
        mem_service = MemoryService(db_mgr)

        await mem_service.log_message(contact_user_id=1001, tg_message_id=10, sender="contact", text="Xabar A")
        await mem_service.log_message(contact_user_id=1002, tg_message_id=20, sender="contact", text="Xabar B")

        msgs_a = await db_mgr.get_mem_messages(contact_user_id=1001)
        msgs_b = await db_mgr.get_mem_messages(contact_user_id=1002)

        assert all(m.contact_user_id == 1001 for m in msgs_a)
        assert all(m.contact_user_id == 1002 for m in msgs_b)

    @pytest.mark.asyncio
    async def test_username_reassignment_pauses_memory(self, temp_db):
        """H2: If a username resolves to a different user_id, pause memory and do not store."""
        db_mgr, temp_path = temp_db
        mem_service = MemoryService(db_mgr)

        # Bind user 1001 to username 'ali'
        await mem_service.verify_and_bind_identity(contact_user_id=1001, username="ali")

        # Now someone else (user 1002) claims username 'ali'
        allowed, reason = await mem_service.verify_and_bind_identity(contact_user_id=1002, username="ali")

        assert allowed is False
        assert "username_reassigned" in reason

        # Check that user 1001's memory was paused
        mem_c = await db_mgr.get_mem_contact(1001)
        assert mem_c.memory_enabled == 0
        assert "username_reassigned" in mem_c.paused_reason

    @pytest.mark.asyncio
    async def test_muted_contact_stores_nothing(self, temp_db):
        """H3: Muted contacts store nothing by default."""
        db_mgr, temp_path = temp_db
        mem_service = MemoryService(db_mgr)

        # Contact 1003 is 'mute'
        logged_id = await mem_service.log_message(
            contact_user_id=1003,
            tg_message_id=50,
            sender="contact",
            text="Bu xabar saqlanmasligi kerak",
            contact_mode="mute",
        )
        assert logged_id is None

        msgs = await db_mgr.get_mem_messages(contact_user_id=1003)
        assert len(msgs) == 0


class TestMediaAndEditHandling:
    @pytest.mark.asyncio
    async def test_media_marker_storage(self, temp_db):
        db_mgr, temp_path = temp_db
        mem_service = MemoryService(db_mgr)

        # Media message without caption
        await mem_service.log_message(
            contact_user_id=1001,
            tg_message_id=71,
            sender="contact",
            text="",
            is_media=True,
            media_type="ovozli xabar",
        )

        # Media message with caption
        await mem_service.log_message(
            contact_user_id=1001,
            tg_message_id=72,
            sender="contact",
            text="Mana bu shartnoma nusxasi",
            is_media=True,
            media_type="hujjat",
        )

        msgs = await db_mgr.get_mem_messages(contact_user_id=1001)
        assert len(msgs) == 2
        assert msgs[0].is_media == 1
        assert msgs[0].text_redacted == "[ovozli xabar]"

        assert msgs[1].is_media == 1
        assert msgs[1].text_redacted == "[hujjat] Mana bu shartnoma nusxasi"

    @pytest.mark.asyncio
    async def test_message_edit_updates_and_reredacts(self, temp_db):
        db_mgr, temp_path = temp_db
        mem_service = MemoryService(db_mgr)

        # Original message
        await mem_service.log_message(
            contact_user_id=1001,
            tg_message_id=88,
            sender="contact",
            text="Eski xabar matni",
        )

        # Edit event with sensitive data
        success = await mem_service.handle_message_edit(
            contact_user_id=1001,
            tg_message_id=88,
            new_text="Yangi tahrir: mening PINFL raqamim 31204951234567",
        )
        assert success is True

        msg = await db_mgr.get_mem_message(contact_user_id=1001, tg_message_id=88)
        assert "31204951234567" not in msg.text_redacted
        assert "[REDACTED]" in msg.text_redacted
        assert msg.edited_at is not None


class TestRetentionCleanup:
    @pytest.mark.asyncio
    async def test_purge_retention(self, temp_db):
        db_mgr, temp_path = temp_db
        mem_service = MemoryService(db_mgr)

        # Insert fresh message
        await mem_service.log_message(
            contact_user_id=1001,
            tg_message_id=901,
            sender="contact",
            text="Yangi xabar",
        )

        # Insert expired message (200 days old)
        old_date = (datetime.utcnow() - timedelta(days=200)).isoformat()
        await db_mgr.log_mem_message(
            contact_user_id=1001,
            tg_message_id=902,
            sender="contact",
            text="Eski xabar",
            sent_at=old_date,
        )

        # Pre-check: 2 messages exist
        msgs = await db_mgr.get_mem_messages(contact_user_id=1001)
        assert len(msgs) == 2

        # Purge older than 180 days
        deleted = await mem_service.purge_retention()
        assert deleted == 1

        # Post-check: only fresh message remains
        msgs_after = await db_mgr.get_mem_messages(contact_user_id=1001)
        assert len(msgs_after) == 1
        assert msgs_after[0].tg_message_id == 901
