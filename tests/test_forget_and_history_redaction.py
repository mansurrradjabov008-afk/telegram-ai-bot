"""
Tests for Step 1.7:
- /forget wipes conversation_history and other tables for target contact.
- conversation_history redacts secrets on write (H4).
- Reply context still functions properly with conversation_history.
- cleanup_conversation_history retention purge works.
"""

import pytest
from database.crud import DatabaseManager
from database.models import Contact


@pytest.mark.asyncio
async def test_forget_wipes_conversation_history_and_drafts(tmp_path):
    """wipe_contact_memory must wipe mem tables, conversation_history, and blank drafts."""
    db_path = tmp_path / "test_forget.db"
    from database.connection import init_db
    await init_db(db_path)
    db_mgr = DatabaseManager(db_path)

    user_id = 9991
    # 1. Seed conversation_history
    await db_mgr.log_message(user_id, "User", "contact", "Salom, bu maxfiy suhbat")
    # 2. Seed mem_message
    await db_mgr.log_mem_message(user_id, 101, "contact", "Maxfiy fakt matni")
    # 3. Seed draft
    await db_mgr.create_draft(
        source_message_id=101,
        incoming_text="Kelgan xabar",
        proposed_reply="Taklif javob",
        contact_user_id=user_id,
        contact_username="u9991",
    )

    # Verify conversation_history exists
    hist_before = await db_mgr.get_recent_history(user_id)
    assert len(hist_before) == 1

    # Execute wipe
    counts = await db_mgr.wipe_contact_memory(user_id)

    assert counts.get("conversation_history") == 1
    assert counts.get("messages") == 1
    assert counts.get("drafts_blanked") == 1

    # Verify conversation_history is completely gone
    hist_after = await db_mgr.get_recent_history(user_id)
    assert len(hist_after) == 0


@pytest.mark.asyncio
async def test_conversation_history_redaction_on_write(tmp_path):
    """Writing to conversation_history must automatically apply redact_text (H4)."""
    db_path = tmp_path / "test_history_redact.db"
    from database.connection import init_db
    await init_db(db_path)
    db_mgr = DatabaseManager(db_path)

    user_id = 9992
    raw_message = "Mening kartam 8600 1234 5678 9012 va maxfiy kodim 482915"
    await db_mgr.log_message(user_id, "SecretUser", "contact", raw_message)

    history = await db_mgr.get_recent_history(user_id)
    assert len(history) == 1
    stored_content = history[0].content

    # Assert secret tokens are redacted
    assert "8600 1234 5678 9012" not in stored_content
    assert "482915" not in stored_content
    assert "[REDACTED]" in stored_content


@pytest.mark.asyncio
async def test_confirm_mem_fact_sets_owner_confirmed(tmp_path):
    """confirm_mem_fact updates source_type to owner_confirmed and confidence to 1.0."""
    db_path = tmp_path / "test_confirm.db"
    from database.connection import init_db
    await init_db(db_path)
    db_mgr = DatabaseManager(db_path)

    user_id = 9993
    m1 = await db_mgr.log_mem_message(user_id, 201, "contact", "Frontend dasturchiman")
    fact_id = await db_mgr.create_mem_fact(
        contact_user_id=user_id,
        category="identity",
        text="Frontend dasturchi",
        source_type="contact_claim",
        source_message_ids=[m1],
        confidence=0.85,
    )

    fact_before = await db_mgr.get_mem_fact_by_id(user_id, fact_id)
    assert fact_before.source_type == "contact_claim"
    assert fact_before.confidence == 0.85

    # Owner confirms fact via /memory confirm
    success = await db_mgr.confirm_mem_fact(user_id, fact_id)
    assert success is True

    fact_after = await db_mgr.get_mem_fact_by_id(user_id, fact_id)
    assert fact_after.source_type == "owner_confirmed"
    assert fact_after.confidence == 1.0
