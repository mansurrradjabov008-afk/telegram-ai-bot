"""
Comprehensive test suite verifying all 6 audit fixes:
1. MemoryContextAssembler fallback to instant local facts on retriever timeout.
2. Telethon send_message fallback to parse_mode=None on markdown formatting errors.
3. Empty AI reply recovery with smart fallback and owner notification.
4. Unlisted sender filtering with throttled owner cockpit alert.
5. Honesty question in auto mode allows Rule 11 natural human comeback instead of blocking into draft.
6. Addressing style cognitive harmony between CRM profile and background memory facts.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from database.models import Contact, MemFact, MemSummary
from services.ai_engine import AIEngine
from services.memory_context import MemoryContextAssembler
from bot.userbot import UserbotService


# ============================================================================
# 1. RETRIEVER TIMEOUT INSTANT LOCAL FACTS FALLBACK
# ============================================================================

@pytest.mark.asyncio
async def test_memory_context_timeout_instant_facts_fallback():
    mock_db = MagicMock()
    mock_retriever = MagicMock()

    mock_db.get_mem_contact = AsyncMock(return_value=MagicMock(memory_enabled=1))
    mock_db.get_latest_mem_summary = AsyncMock(
        return_value=MemSummary(id=1, contact_user_id=123, summary_text="Kutubxona haqida gaplashilgan.")
    )
    mock_db.get_mem_facts = AsyncMock(
        return_value=[
            MemFact(id=1, contact_user_id=123, category="relationship_note", text="Kontakt egasiga 'uka' deb murojaat qiladi", source_type="contact_claim", status="active"),
        ]
    )

    # Retriever hangs indefinitely to trigger timeout
    async def slow_retrieve(*args, **kwargs):
        await asyncio.sleep(5)
        return [], []

    mock_retriever.retrieve = AsyncMock(side_effect=slow_retrieve)

    assembler = MemoryContextAssembler(db_manager=mock_db, retriever=mock_retriever, timeout_seconds=0.2)
    ctx = await assembler.assemble_context(contact_user_id=123, incoming_text="Salom")

    assert "SUHBATDOSH HAQIDAGI FAKTLAR" in ctx
    assert "Kontakt egasiga 'uka' deb murojaat qiladi" in ctx
    assert "Kutubxona haqida gaplashilgan" in ctx


# ============================================================================
# 2. TELETHON SEND_MESSAGE MARKDOWN FALLBACK TO PLAIN TEXT
# ============================================================================

@pytest.mark.asyncio
async def test_send_with_floodwait_markdown_fallback():
    mock_client = MagicMock()
    mock_sent_msg = MagicMock()

    call_count = 0
    def mock_send(peer, text, parse_mode=...):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("Can't parse markdown: unclosed entity")
        return mock_sent_msg

    mock_client.send_message = AsyncMock(side_effect=mock_send)

    service = UserbotService(
        db_manager=MagicMock(),
        ai_engine=MagicMock(),
        rate_limiter=MagicMock(),
        digest_service=MagicMock(),
    )
    service.client = mock_client

    result = await service._send_with_floodwait(peer=12345, text="Hello _world", max_retries=2)
    assert result == mock_sent_msg
    assert call_count == 2
    # Second call should specify parse_mode=None
    _, kwargs = mock_client.send_message.call_args
    assert kwargs.get("parse_mode") is None


# ============================================================================
# 3. UNLISTED SENDER THROTTLED NOTIFICATION
# ============================================================================

@pytest.mark.asyncio
async def test_unlisted_sender_throttled_notification():
    from telethon.tl.types import User

    mock_db = MagicMock()
    mock_db.get_contact_by_id = AsyncMock(return_value=None)
    mock_db.get_contact_by_username = AsyncMock(return_value=None)

    service = UserbotService(
        db_manager=mock_db,
        ai_engine=MagicMock(),
        rate_limiter=MagicMock(),
        digest_service=MagicMock(),
    )
    mock_notif = AsyncMock()
    service.set_notification_callback(mock_notif)

    # Mock Telegram event
    mock_sender = MagicMock(spec=User)
    mock_sender.id = 999888777
    mock_sender.username = "unknown_user"
    mock_sender.first_name = "Noma'lum"
    mock_sender.is_self = False
    mock_sender.bot = False

    mock_event = MagicMock()
    mock_event.get_sender = AsyncMock(return_value=mock_sender)
    mock_event.message.action = None
    mock_event.raw_text = "Salom, kim bu?"
    mock_event.message.media = None

    # First incoming message from unknown user -> triggers alert
    await service._handle_private_message(mock_event)
    await asyncio.sleep(0.05)  # yield control for asyncio.create_task
    assert mock_notif.call_count == 1
    alert_text = mock_notif.call_args[0][0]
    assert "Yangi inson yozdi" in alert_text
    assert "999888777" in alert_text

    # Second incoming message immediately after -> throttled (no second alert)
    await service._handle_private_message(mock_event)
    await asyncio.sleep(0.05)
    assert mock_notif.call_count == 1


# ============================================================================
# 4. EMPTY AI REPLY SMART FALLBACK AND OWNER NOTIFICATION
# ============================================================================

@pytest.mark.asyncio
async def test_empty_ai_reply_falls_back_and_notifies_owner():
    mock_db = MagicMock()
    contact = Contact(user_id=111, username="friend", full_name="Friend", mode="auto", addressing_style="sen")
    mock_db.get_contact_by_id = AsyncMock(return_value=contact)
    mock_db.get_recent_history = AsyncMock(return_value=[])

    mock_ai = MagicMock()
    mock_ai.generate_reply = AsyncMock(return_value="")
    mock_ai._dynamic_smart_fallback = MagicMock(return_value="")  # Fallback also empty

    service = UserbotService(
        db_manager=mock_db,
        ai_engine=mock_ai,
        rate_limiter=MagicMock(),
        digest_service=MagicMock(),
    )
    mock_notif = AsyncMock()
    service.set_notification_callback(mock_notif)

    service._debounce_buffers[111] = [
        {
            "text": "Nega yozmayapsan?",
            "message_id": 1,
            "chat_id": 111,
            "first_name": "Friend",
            "username": "friend",
            "peer_repr": "@friend",
            "contact": contact,
            "reply_to": None,
            "date": None,
        }
    ]

    await service._process_debounced_batch(111)
    await asyncio.sleep(0.05)
    assert mock_notif.call_count == 1
    notif_text = mock_notif.call_args[0][0]
    assert "AI javob yarata olmadi" in notif_text


# ============================================================================
# 5. HONESTY GUARD IN AUTO MODE ALLOWS RULE 11 WIT
# ============================================================================

@pytest.mark.asyncio
async def test_honesty_guard_auto_vs_draft_mode():
    mock_style = MagicMock()
    mock_style.is_style_enabled_for_contact = AsyncMock(return_value=True)
    mock_style.check_honesty_question = MagicMock(return_value=True)

    # 1. In AUTO mode: honesty check must NOT lock into draft
    contact_auto = Contact(user_id=222, username="auto_user", full_name="Bro", mode="auto", addressing_style="sen")
    mock_db = MagicMock()
    mock_db.get_contact_by_id = AsyncMock(return_value=contact_auto)
    mock_db.get_recent_history = AsyncMock(return_value=[])
    mock_db.log_message = AsyncMock()

    mock_ai = MagicMock()
    mock_ai.generate_reply = AsyncMock(return_value="E qanaqa bot jigar, o'zim yozayapman 😂")
    mock_ai.classify_sensitive_topic = AsyncMock(
        return_value={"sensitive": False, "hard_sensitive": False, "category": "safe", "reasons": []}
    )
    mock_ai.classify_incoming_message = AsyncMock(
        return_value=MagicMock(category="SAFE_CHITCHAT", reason=None)
    )

    service = UserbotService(
        db_manager=mock_db,
        ai_engine=mock_ai,
        rate_limiter=MagicMock(),
        digest_service=MagicMock(),
        style_service=mock_style,
    )
    service.client = MagicMock()
    service._send_with_floodwait = AsyncMock()
    service.check_owner_manually_replied = AsyncMock(return_value=False)

    service._debounce_buffers[222] = [
        {
            "text": "Sen botmisan?",
            "message_id": 10,
            "chat_id": 222,
            "first_name": "Bro",
            "username": "auto_user",
            "peer_repr": "@auto_user",
            "contact": contact_auto,
            "reply_to": None,
            "date": None,
        }
    ]

    with patch("asyncio.sleep", AsyncMock()):
        await service._process_debounced_batch(222)

    # In auto mode, it should send the witty reply without blocking
    assert service._send_with_floodwait.call_count == 1


# ============================================================================
# 6. ADDRESSING STYLE COGNITIVE HARMONY (CRM vs MEMORY FACTS)
# ============================================================================

@pytest.mark.asyncio
async def test_addressing_style_harmonized_with_memory_facts():
    ai = AIEngine()
    ai._get_candidate_models = MagicMock(return_value=["test-model"])
    ai._client = MagicMock()

    captured_prompts = []

    async def mock_raw(model, prompt, **kwargs):
        captured_prompts.append(prompt)
        return "Tushundim bro, hozir chiqaman."

    ai._call_gemini_raw = AsyncMock(side_effect=mock_raw)

    # Contact profile has addressing_style='siz' from older CRM default,
    # but relationship is close friend and memory context specifies peer calls owner 'uka' and 'bro'
    contact = Contact(
        user_id=8036556250,
        username="mansurjon_07",
        full_name="Mansur",
        relationship_type="qadrdon_dost",
        addressing_style="siz",  # Stale CRM field
        mode="auto",
    )

    memory_context = (
        "<memory>\n"
        "SUHBATDOSH HAQIDAGI FAKTLAR:\n"
        "• Kontakt egasiga 'uka' va 'bro' deb murojaat qiladi (manba: kontaktning da'vosi)\n"
        "</memory>"
    )

    reply = await ai.generate_reply(
        incoming_text="Salom, ishlar qalay?",
        sender_name="Mansur",
        contact_profile=contact,
        memory_context=memory_context,
    )

    assert len(captured_prompts) > 0
    prompt_used = captured_prompts[0]
    # Addressing directive MUST be SEN because memory facts clearly indicate kinship/brotherhood!
    assert 'MUROJAAT SHAKLI: "SEN"' in prompt_used
    assert 'MUROJAAT SHAKLI: "SIZ"' not in prompt_used


@pytest.mark.asyncio
async def test_addressing_style_respects_explicit_owner_instruction_for_siz():
    ai = AIEngine()
    ai._get_candidate_models = MagicMock(return_value=["test-model"])
    ai._client = MagicMock()

    captured_prompts = []

    async def mock_raw(model, prompt, **kwargs):
        captured_prompts.append(prompt)
        return "Assalomu alaykum, yaxshimisiz."

    ai._call_gemini_raw = AsyncMock(side_effect=mock_raw)

    # Even if memory has casual facts, if owner explicitly says 'qat'iy siz', keep 'siz':
    contact = Contact(
        user_id=123456,
        username="vip_client",
        full_name="Mijoz Aka",
        relationship_type="mijoz",
        addressing_style="siz",
        custom_instructions="Faqat sizlash shart, rasmiy bo'lsin",
        mode="auto",
    )

    memory_context = (
        "<memory>\n"
        "• Kontakt egasiga 'bro' degan edi\n"
        "</memory>"
    )

    await ai.generate_reply(
        incoming_text="Salom",
        sender_name="Mijoz Aka",
        contact_profile=contact,
        memory_context=memory_context,
    )

    assert len(captured_prompts) > 0
    prompt_used = captured_prompts[0]
    assert 'MUROJAAT SHAKLI: "SIZ"' in prompt_used


# ============================================================================
# 7. 1-TAP CRM INLINE BUTTON QUICK ADD CALLBACK
# ============================================================================

@pytest.mark.asyncio
async def test_quick_add_contact_callback(tmp_path):
    from bot.admin_bot import AdminBotService
    from database.connection import init_db
    from database.crud import DatabaseManager

    db_file = tmp_path / "test_quick_add.db"
    await init_db(db_file)
    db_manager = DatabaseManager(db_path=db_file)

    service = AdminBotService(
        db_manager=db_manager,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        token="123456:mock_token",
        admin_id=7992504344,
    )

    quick_add_handler = None
    for h in service.dp.callback_query.handlers:
        name = getattr(h.callback, "__name__", "")
        if name == "handle_quick_add":
            quick_add_handler = h.callback
            break

    assert quick_add_handler is not None, "handle_quick_add callback handler not found"

    fake_cb = MagicMock()
    fake_cb.from_user.id = 7992504344
    fake_cb.data = "quick_add:99887766:auto"
    fake_cb.message = MagicMock()
    fake_cb.message.text = "Yangi inson yozdi"
    fake_cb.message.edit_text = AsyncMock()
    fake_cb.answer = AsyncMock()

    # Trigger quick add
    await quick_add_handler(fake_cb)

    # Verify contact was saved into DB with auto mode
    saved_contact = await db_manager.get_contact_by_id(99887766)
    assert saved_contact is not None
    assert saved_contact.mode == "auto"
    fake_cb.answer.assert_awaited()
    fake_cb.message.edit_text.assert_awaited()
