import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from services.ai_engine import (
    AIEngine,
    clean_colloquial_outreach_instruction,
)
from database.crud import DatabaseManager
from database.models import Contact
from database.connection import init_db
from bot.admin_bot import AdminBotService


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    db_path = tmp_path / "test_outreach_composer.db"
    await init_db(db_path)
    db_mgr = DatabaseManager(db_path)
    return db_mgr


def test_clean_colloquial_outreach_instruction_deterministic():
    """Verify deterministic cleaner handles all colloquial Uzbek outreach patterns."""
    # Test case 1: 'Xozir yoniga boryapman aytib quy'
    is_i1, res1 = clean_colloquial_outreach_instruction("Xozir yoniga boryapman aytib quy", "Mansur", "siz")
    assert is_i1 is True
    assert "aytib quy" not in res1.lower()
    assert "yoningizga" in res1
    assert "Hozir" in res1

    # Test case 2: 'Xozir yoniga borayotganimni aytiv quy mansurga'
    is_i2, res2 = clean_colloquial_outreach_instruction("Xozir yoniga borayotganimni aytiv quy mansurga", "Mansur", "siz")
    assert is_i2 is True
    assert "aytiv quy" not in res2.lower()
    assert "mansurga" not in res2.lower()
    assert "yoningizga" in res2

    # Test case 3: Sensirash mode ('sen')
    is_i3, res3 = clean_colloquial_outreach_instruction("Xozir yoniga boryapman aytib quy", "Mansur", "sen")
    assert is_i3 is True
    assert "yoningga" in res3

    # Test case 4: Direct verbatim text without instruction
    is_i4, res4 = clean_colloquial_outreach_instruction("Assalomu alaykum, bugun soat 5 da uchrashamiz", "Mansur", "siz")
    assert is_i4 is False
    assert res4 == "Assalomu alaykum, bugun soat 5 da uchrashamiz"

    # Test case 5: Apology instruction
    is_i5, res5 = clean_colloquial_outreach_instruction("bugun kela olmayman uzr so'rab qo'y", "Mansur", "siz")
    assert is_i5 is True
    assert "uzr" in res5.lower()


@pytest.mark.asyncio
async def test_compose_outreach_message_ai_integration():
    """Verify compose_outreach_message uses AI with fallback."""
    engine = AIEngine(api_key=None)

    # When client is None, it safely uses the deterministic fallback
    res = await engine.compose_outreach_message(
        user_input="Xozir yoniga boryapman aytib quy",
        target_name="Mansur",
        addressing_style="siz",
    )
    assert res["is_instruction"] is True
    assert "aytib quy" not in res["message_text"].lower()
    assert "yoningizga" in res["message_text"]

    # Direct message
    res_direct = await engine.compose_outreach_message(
        user_input="Ertaga ko'rishamiz",
        target_name="Mansur",
        addressing_style="siz",
    )
    assert res_direct["is_instruction"] is False
    assert res_direct["message_text"] == "Ertaga ko'rishamiz"


@pytest.mark.asyncio
async def test_admin_bot_outreach_preview_never_leaks_raw_instruction(temp_db):
    """
    Simulates the exact user scenario from screenshots:
    1. Admin initiates outreach to contact 'Mansur' (@mansurjon_07).
    2. Admin types: 'Xozir yoniga borayotganimni aytiv quy mansurga'.
    3. The preview MUST format a natural message and NEVER display the raw command.
    """
    db_mgr = temp_db
    owner_id = 999111

    contact = await db_mgr.upsert_contact(
        user_id=8036556250,
        username="mansurjon_07",
        full_name="Mansur",
        mode="auto",
        addressing_style="siz",
    )

    ai_engine = MagicMock()
    # Mock compose_outreach_message to return a clean transformed message
    ai_engine.compose_outreach_message = AsyncMock(return_value={
        "is_instruction": True,
        "message_text": "Assalomu alaykum, hozir yoningizga borayotgan edim.",
    })

    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=ai_engine,
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    # Find the outreach handler
    outreach_handler = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "process_contact_outreach_message":
            outreach_handler = h.callback
            break
    assert outreach_handler is not None

    # Simulate FSM State data
    state_mock = MagicMock()
    state_mock.get_data = AsyncMock(return_value={
        "target_user_id": contact.user_id,
        "target_peer": "@mansurjon_07",
        "target_name": "Mansur",
        "target_username": "mansurjon_07",
        "addressing_style": "siz",
    })
    state_mock.clear = AsyncMock()

    msg = MagicMock()
    msg.from_user.id = owner_id
    msg.text = "Xozir yoniga borayotganimni aytiv quy mansurga"
    msg.reply = AsyncMock()

    await outreach_handler(msg, state_mock)

    # Verify compose_outreach_message was invoked
    ai_engine.compose_outreach_message.assert_called_once()
    call_args = ai_engine.compose_outreach_message.call_args[1]
    assert call_args["user_input"] == "Xozir yoniga borayotganimni aytiv quy mansurga"
    assert call_args["target_name"] == "Mansur"
    assert call_args["addressing_style"] == "siz"

    # Verify reply message
    assert msg.reply.called
    reply_text = msg.reply.call_args[0][0]
    assert "XABAR YUBORISHGA TAYYOR (PREVIEW)" in reply_text
    assert "Assalomu alaykum, hozir yoningizga borayotgan edim." in reply_text
    # Most critical: Raw command MUST NOT be the preview text
    assert '"Xozir yoniga borayotganimni aytiv quy mansurga"' not in reply_text
