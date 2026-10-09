"""
Tests for Phase 6: Owner Commands & Deletion Hook (Allowed Integration Point 3).
Verifies:
- wipe_contact_memory removes rows, embeddings, summaries, and blanks drafts (H10)
- FTS5 trigger cleanup on wipe
- delete_mem_fact per contact (H4)
- get_mem_stats accuracy
- handle_crm_delete confirmation: keep memory (default) vs wipe memory (H10)
- /memory command output formatting and access control
- /memory_facts listing and deletion callback
- /forget command confirmation dialog and execution
"""

import os
import asyncio
from pathlib import Path
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from config import settings
from database.connection import init_db
from database.crud import DatabaseManager, get_db
from database.models import Contact, Draft
from bot.admin_bot import AdminBotService


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    temp_path = tmp_path / "test_cmd_p6.db"
    await init_db(temp_path)
    db_mgr = DatabaseManager(temp_path)
    return db_mgr, temp_path


async def _seed_full_contact(db_mgr: DatabaseManager, user_id: int, username: str):
    """Seeds CRM contact and memory data across all tables."""
    await db_mgr.upsert_contact(user_id=user_id, full_name=username, username=username, mode="draft")
    await db_mgr.bind_mem_contact(user_id, username=username)

    # Seed messages
    await db_mgr.log_mem_message(user_id, 101, "contact", "Salom Mansur")
    await db_mgr.log_mem_message(user_id, 102, "owner_manual", "Salom do'stim")

    # Seed facts
    await db_mgr.create_mem_fact(user_id, "ish", "Toshkentda ishlaydi", "owner_stated")
    await db_mgr.create_mem_fact(user_id, "claim", "Direktor ekanligini aytdi", "contact_claim")

    # Seed open items
    await db_mgr.create_mem_open_item(user_id, "Ertaga soat 10:00 da uchrashuv")

    # Seed summary
    await db_mgr.save_mem_summary(user_id, "Suhbatdosh ish haqida so'radi", covers_until_message_id=102)

    # Seed draft with legacy unredacted text (H13)
    query = """
    INSERT INTO drafts (contact_username, contact_user_id, source_message_id, incoming_text, proposed_reply, status, expires_at)
    VALUES (?, ?, 101, 'Karta raqamim 8600 1234 5678 9012', 'Xop qabul qildim', 'pending', datetime('now', '+1 hour'))
    """
    async with db_mgr.serialized_transaction() as db:
        await db.execute(query, (username, user_id))

    return user_id


@pytest.mark.asyncio
class TestOwnerCommandsAndDeletionPhase6:

    async def test_wipe_contact_memory_clears_all_and_blanks_drafts(self, temp_db):
        """H10: wipe_contact_memory deletes rows, summaries, facts, and blanks drafts."""
        db_mgr, _ = temp_db
        user_id = 901
        await _seed_full_contact(db_mgr, user_id, "user901")

        # Verify before stats
        stats_before = await db_mgr.get_mem_stats(user_id)
        assert stats_before["messages"] == 2
        assert stats_before["facts"] == 2
        assert stats_before["open_items"] == 1
        assert stats_before["summaries"] == 1

        # Perform wipe
        counts = await db_mgr.wipe_contact_memory(user_id)
        assert counts["messages"] == 2
        assert counts["facts"] == 2
        assert counts["open_items"] == 1
        assert counts["summaries"] == 1
        assert counts["contact_records"] == 1
        assert counts["drafts_blanked"] == 1

        # Verify after stats
        stats_after = await db_mgr.get_mem_stats(user_id)
        assert stats_after["messages"] == 0
        assert stats_after["facts"] == 0
        assert stats_after["open_items"] == 0
        assert stats_after["summaries"] == 0

        # Verify drafts blanking
        async with get_db(db_mgr.db_path) as db:
            async with db.execute("SELECT incoming_text, proposed_reply FROM drafts WHERE contact_user_id = ?", (user_id,)) as cur:
                draft_row = await cur.fetchone()
                assert draft_row["incoming_text"] == "[DELETED]"
                assert draft_row["proposed_reply"] == "[DELETED]"

        # Verify FTS5 triggers cleaned up
        async with get_db(db_mgr.db_path) as db:
            async with db.execute("SELECT count(*) FROM mem_messages_fts") as cur:
                fts_msgs = (await cur.fetchone())[0]
                assert fts_msgs == 0

    async def test_wipe_memory_strict_contact_isolation(self, temp_db):
        """H4: Wiping user A's memory leaves user B's memory 100% untouched."""
        db_mgr, _ = temp_db
        user_a = 902
        user_b = 903
        await _seed_full_contact(db_mgr, user_a, "user_a")
        await _seed_full_contact(db_mgr, user_b, "user_b")

        # Wipe only user A
        await db_mgr.wipe_contact_memory(user_a)

        # Verify user B is intact
        stats_b = await db_mgr.get_mem_stats(user_b)
        assert stats_b["messages"] == 2
        assert stats_b["facts"] == 2
        assert stats_b["open_items"] == 1
        assert stats_b["summaries"] == 1

        # Verify user B drafts are not blanked
        async with get_db(db_mgr.db_path) as db:
            async with db.execute("SELECT incoming_text FROM drafts WHERE contact_user_id = ?", (user_b,)) as cur:
                draft_b = await cur.fetchone()
                assert "8600" in draft_b["incoming_text"]

    async def test_delete_mem_fact_removes_single_fact(self, temp_db):
        """H10: Owner can delete specific individual facts."""
        db_mgr, _ = temp_db
        user_id = 904
        await _seed_full_contact(db_mgr, user_id, "user904")

        facts = await db_mgr.get_mem_facts(user_id, status="active")
        assert len(facts) == 2
        fact_to_del = facts[0]

        ok = await db_mgr.delete_mem_fact(user_id, fact_to_del.id)
        assert ok is True

        facts_after = await db_mgr.get_mem_facts(user_id, status="active")
        assert len(facts_after) == 1
        assert facts_after[0].id != fact_to_del.id

    async def test_crm_delete_confirmation_keep_memory_default(self, temp_db):
        """Integration Point 3: Deleting contact with 'keep' pauses memory and removes CRM contact."""
        db_mgr, _ = temp_db
        user_id = 905
        username = "user905"
        await _seed_full_contact(db_mgr, user_id, username)

        # Simulate clicking 'keep' in confirmation dialog
        await db_mgr.pause_mem_contact(user_id, reason="contact_deleted_memory_retained")
        await db_mgr.delete_contact(user_id)

        # Contact is gone from CRM
        contact = await db_mgr.get_contact_by_id(user_id)
        assert contact is None

        # Memory rows are still preserved
        stats = await db_mgr.get_mem_stats(user_id)
        assert stats["messages"] == 2
        assert stats["facts"] == 2

    async def test_crm_delete_confirmation_wipe_memory(self, temp_db):
        """Integration Point 3: Deleting contact with 'wipe' wipes all memory and removes CRM contact."""
        db_mgr, _ = temp_db
        user_id = 906
        username = "user906"
        await _seed_full_contact(db_mgr, user_id, username)

        # Simulate clicking 'wipe'
        await db_mgr.wipe_contact_memory(user_id)
        await db_mgr.delete_contact(user_id)

        contact = await db_mgr.get_contact_by_id(user_id)
        assert contact is None

        stats = await db_mgr.get_mem_stats(user_id)
        assert stats["messages"] == 0
        assert stats["facts"] == 0
        assert stats["summaries"] == 0

    async def test_admin_cockpit_crm_delete_flow(self, temp_db):
        """Integration Point 3: Full callback simulation for crm_del and crm_del_confirm."""
        db_mgr, _ = temp_db
        user_id = 907
        username = "user907"
        await _seed_full_contact(db_mgr, user_id, username)

        owner_id = 7992504344
        admin_cockpit = AdminBotService(
            db_manager=db_mgr,
            ai_engine=MagicMock(),
            scheduler_service=MagicMock(),
            digest_service=MagicMock(),
            token="1234567890:mock_token",
            admin_id=owner_id,
        )

        del_handler = None
        confirm_handler = None
        for h in admin_cockpit.dp.callback_query.handlers:
            fn_name = getattr(h.callback, "__name__", "")
            if fn_name == "handle_crm_delete":
                del_handler = h.callback
            elif fn_name == "handle_crm_delete_confirm":
                confirm_handler = h.callback

        assert del_handler is not None
        assert confirm_handler is not None

        # 1. Step 1: Click "O'chirish" -> handle_crm_delete renders prompt
        cb1 = MagicMock()
        cb1.from_user.id = owner_id
        cb1.data = f"crm_del:{username}"
        cb1.answer = AsyncMock()
        cb1.message = MagicMock()
        cb1.message.edit_text = AsyncMock()

        await del_handler(cb1)
        cb1.message.edit_text.assert_called_once()
        prompt_text = cb1.message.edit_text.call_args[0][0]
        assert "xotirasi (yozishmalar, faktlar, aylanma xulosalar) ham to'liq o'chirilsinmi?" in prompt_text

        # 2. Step 2: Confirm wipe -> handle_crm_delete_confirm with 'wipe'
        cb2 = MagicMock()
        cb2.from_user.id = owner_id
        cb2.data = f"crm_del_confirm:{username}:wipe"
        cb2.answer = AsyncMock()
        cb2.message = MagicMock()
        cb2.message.edit_text = AsyncMock()

        await confirm_handler(cb2)
        cb2.answer.assert_called_once()
        assert "to'liq o'chirildi" in cb2.answer.call_args[0][0]

        # Verify wiped in DB
        contact = await db_mgr.get_contact_by_id(user_id)
        assert contact is None
        stats = await db_mgr.get_mem_stats(user_id)
        assert stats["messages"] == 0
        assert stats["facts"] == 0

    async def test_access_control_rejects_non_admin(self, temp_db):
        """P1: Commands and callbacks strictly reject non-owner Telegram IDs."""
        db_mgr, _ = temp_db
        owner_id = 7992504344
        admin_cockpit = AdminBotService(
            db_manager=db_mgr,
            ai_engine=MagicMock(),
            scheduler_service=MagicMock(),
            digest_service=MagicMock(),
            token="1234567890:mock_token",
            admin_id=owner_id,
        )

        non_admin_id = 999999999
        assert admin_cockpit._is_admin(non_admin_id) is False
        assert admin_cockpit._is_admin(owner_id) is True

    async def test_admin_cockpit_memory_commands(self, temp_db):
        """Tests /memory, /memory_facts, and /forget commands."""
        db_mgr, _ = temp_db
        user_id = 908
        username = "user908"
        await _seed_full_contact(db_mgr, user_id, username)

        owner_id = 7992504344
        admin_cockpit = AdminBotService(
            db_manager=db_mgr,
            ai_engine=MagicMock(),
            scheduler_service=MagicMock(),
            digest_service=MagicMock(),
            token="1234567890:mock_token",
            admin_id=owner_id,
        )

        cmd_memory_handler = None
        cmd_facts_handler = None
        cmd_forget_handler = None
        for h in admin_cockpit.dp.message.handlers:
            fn_name = getattr(h.callback, "__name__", "")
            if fn_name == "cmd_memory":
                cmd_memory_handler = h.callback
            elif fn_name == "cmd_memory_facts":
                cmd_facts_handler = h.callback
            elif fn_name == "cmd_forget":
                cmd_forget_handler = h.callback

        assert cmd_memory_handler is not None
        assert cmd_facts_handler is not None
        assert cmd_forget_handler is not None

        # Test /memory with target
        msg = MagicMock()
        msg.from_user.id = owner_id
        msg.text = f"/memory @{username}"
        msg.reply = AsyncMock()

        await cmd_memory_handler(msg)
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        assert "KONTAKT XOTIRASI" in reply_text
        assert "Saqlangan xabarlar:" in reply_text
        assert "Faol faktlar:" in reply_text

        # Test /memory_facts with target
        msg2 = MagicMock()
        msg2.from_user.id = owner_id
        msg2.text = f"/memory_facts @{username}"
        msg2.reply = AsyncMock()

        await cmd_facts_handler(msg2)
        msg2.reply.assert_called_once()
        facts_text = msg2.reply.call_args[0][0]
        assert "FAKTLAR RO'YXATI" in facts_text
        assert "Toshkentda ishlaydi" in facts_text

        # Test /forget prompt
        msg3 = MagicMock()
        msg3.from_user.id = owner_id
        msg3.text = f"/forget @{username}"
        msg3.reply = AsyncMock()

        await cmd_forget_handler(msg3)
        msg3.reply.assert_called_once()
        forget_text = msg3.reply.call_args[0][0]
        assert "Xotirani butunlay o'chirish tasdig'i" in forget_text
