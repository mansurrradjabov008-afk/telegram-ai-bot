"""
Unit and integration tests for Historical Style Importer (D9, Phase 2).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
import pytest_asyncio

from database.connection import init_db
from database.crud import DatabaseManager
from services.style_importer import StyleImporter, determine_bot_cutoff


class MockMessage:
    def __init__(
        self,
        id: int,
        text: str,
        out: bool = True,
        date: datetime = None,
        fwd_from=None,
        via_bot_id=None,
        action=None,
        media=None,
    ):
        self.id = id
        self.raw_text = text
        self.text = text
        self.out = out
        self.date = date or (datetime.now() - timedelta(days=30))
        self.fwd_from = fwd_from
        self.via_bot_id = via_bot_id
        self.action = action
        self.media = media


@pytest_asyncio.fixture
async def temp_import_db(tmp_path: Path):
    temp_path = tmp_path / "test_style_import.db"
    await init_db(temp_path)
    db_mgr = DatabaseManager(temp_path)
    await db_mgr.init_style_schema()
    return db_mgr, temp_path


@pytest.mark.asyncio
async def test_import_refuses_non_owner(temp_import_db):
    db_mgr, _ = temp_import_db
    client = MagicMock()
    importer = StyleImporter(db_manager=db_mgr, telethon_client=client, owner_id=12345)

    with pytest.raises(PermissionError, match="owner"):
        await importer.import_history(caller_user_id=99999)


@pytest.mark.asyncio
async def test_import_stops_if_cutoff_undetermined(temp_import_db):
    db_mgr, _ = temp_import_db
    client = MagicMock()
    importer = StyleImporter(db_manager=db_mgr, telethon_client=client, owner_id=12345)

    # Empty DB has no bot messages, so cutoff cannot be determined
    with pytest.raises(ValueError, match="BOT_CUTOFF"):
        await importer.import_history(caller_user_id=12345)


@pytest.mark.asyncio
async def test_determine_bot_cutoff_from_db(temp_import_db):
    db_mgr, _ = temp_import_db

    # Insert an assistant message into conversation_history
    await db_mgr.log_message(
        chat_id=1001,
        sender_name="Bot",
        role="assistant",
        content="Salom",
    )
    # Log message set timestamp to CURRENT_TIMESTAMP
    cutoff = await determine_bot_cutoff(db_mgr)
    assert cutoff is not None
    # Cutoff should be earlier than now minus 23 hours
    assert cutoff < datetime.now() - timedelta(hours=23)


@pytest.mark.asyncio
async def test_import_pipeline_filters_and_never_marks_read(temp_import_db):
    db_mgr, _ = temp_import_db
    owner_id = 12345

    # Create contacts: 1001 (auto), 1002 (draft), 1003 (mute)
    await db_mgr.upsert_contact(user_id=1001, full_name="Ali", mode="auto")
    await db_mgr.upsert_contact(user_id=1002, full_name="Vali", mode="draft")
    await db_mgr.upsert_contact(user_id=1003, full_name="Gani", mode="mute")

    # Set custom cutoff: 10 days ago
    cutoff_date = datetime.now() - timedelta(days=10)

    # Mock Telethon client
    mock_client = MagicMock()
    mock_client.send_read_acknowledge = AsyncMock()

    # Create test messages for contact 1001:
    # 1. Clean pre-cutoff outgoing message
    msg1 = MockMessage(1, "Ertaga soat 10 da ko'rishamiz.", out=True, date=cutoff_date - timedelta(days=2))
    # 2. Post-cutoff message (should be dropped)
    msg2 = MockMessage(2, "Post cutoff xabar", out=True, date=cutoff_date + timedelta(days=1))
    # 3. Incoming message (out=False, should be dropped)
    msg3 = MockMessage(3, "Kelgan xabar", out=False, date=cutoff_date - timedelta(days=3))
    # 4. Forwarded message (should be dropped)
    msg4 = MockMessage(4, "Fwd xabar", out=True, date=cutoff_date - timedelta(days=4), fwd_from=MagicMock())
    # 5. Via bot message (should be dropped)
    msg5 = MockMessage(5, "Via bot xabar", out=True, date=cutoff_date - timedelta(days=5), via_bot_id=999)
    # 6. Message with credit card (should be redacted, then dropped by [REDACTED] filter)
    msg6 = MockMessage(6, "Karta raqam 8600 1234 5678 9012", out=True, date=cutoff_date - timedelta(days=6))

    async def mock_iter_messages(entity, limit=200):
        if entity == 1001:
            for m in [msg1, msg2, msg3, msg4, msg5, msg6]:
                yield m
        elif entity == 1002:
            yield MockMessage(10, "Vali, ertaga gaplashamiz.", out=True, date=cutoff_date - timedelta(days=1))
        elif entity == 1003:
            pytest.fail("Muted contact 1003 should NEVER be queried!")

    mock_client.iter_messages = mock_iter_messages

    importer = StyleImporter(db_manager=db_mgr, telethon_client=mock_client, owner_id=owner_id)
    summary = await importer.import_history(caller_user_id=owner_id, custom_cutoff=cutoff_date)

    # Assert that read-acknowledge was NEVER called
    assert mock_client.send_read_acknowledge.call_count == 0

    # Assert imported counts: only msg1 (Ali) and msg10 (Vali) should be imported
    assert summary["samples_imported"] == 2

    samples = await db_mgr.get_style_samples()
    assert len(samples) == 2
    texts = [s["text_redacted"] for s in samples]
    assert "Ertaga soat 10 da ko'rishamiz." in texts
    assert "Vali, ertaga gaplashamiz." in texts
