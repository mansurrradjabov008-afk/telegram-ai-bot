"""
Unit and integration tests for Phase 3: Message Pipeline, Debounce, and Draft Flow.
Verifies the 8-step pipeline execution, ignore filters, non-text handling, debouncing,
auto reply with superseded check, draft creation, sensitive-topic routing, and FloodWait safety.
"""

import asyncio
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from telethon.tl.types import User, MessageMediaPhoto, MessageMediaWebPage
from telethon.errors import FloodWaitError

from config import settings
from database.connection import init_db
from database.crud import DatabaseManager
from bot.userbot import UserbotService
from services.ai_engine import AIEngine
from services.rate_limiter import HumanRateLimiter
from services.digest_service import DigestService
from services.sensitive_guard import check_sensitive, SensitiveCheckResult


@pytest_asyncio.fixture
async def test_db(tmp_path: Path):
    db_file = tmp_path / "test_pipeline.db"
    await init_db(db_file)
    manager = DatabaseManager(db_path=db_file)
    return manager


@pytest.fixture
def mock_ai_engine():
    engine = MagicMock(spec=AIEngine)
    engine.generate_reply = AsyncMock(return_value="Salom! Hammasi yaxshi, rahmat.")
    engine.classify_sensitive_topic = AsyncMock(return_value={"sensitive": False, "reasons": []})
    return engine


@pytest.fixture
def mock_rate_limiter():
    limiter = MagicMock(spec=HumanRateLimiter)
    limiter.calculate_typing_duration = MagicMock(return_value=0.01)
    return limiter


@pytest.fixture
def mock_digest_service():
    service = MagicMock(spec=DigestService)
    service.process_incoming_post = AsyncMock()
    return service


@pytest_asyncio.fixture
async def userbot_service(test_db, mock_ai_engine, mock_rate_limiter, mock_digest_service):
    service = UserbotService(
        db_manager=test_db,
        ai_engine=mock_ai_engine,
        rate_limiter=mock_rate_limiter,
        digest_service=mock_digest_service,
    )
    # Mock telethon client
    client = MagicMock()
    client.send_message = AsyncMock()
    client.get_messages = AsyncMock(return_value=[])
    client.action = MagicMock()
    client.action.return_value.__aenter__ = AsyncMock()
    client.action.return_value.__aexit__ = AsyncMock()
    service.client = client
    return service


def make_mock_event(
    sender_id: int = 11111,
    username: str = "valid_user",
    first_name: str = "Ali",
    is_self: bool = False,
    is_bot: bool = False,
    raw_text: str = "Salom, qandaysiz?",
    action: any = None,
    media: any = None,
    msg_id: int = 100,
):
    event = MagicMock()
    event.id = msg_id
    event.chat_id = sender_id
    event.raw_text = raw_text

    sender = MagicMock(spec=User)
    sender.id = sender_id
    sender.username = username
    sender.first_name = first_name
    sender.is_self = is_self
    sender.bot = is_bot

    event.get_sender = AsyncMock(return_value=sender)
    event.message = MagicMock()
    event.message.id = msg_id
    event.message.action = action
    event.message.media = media
    event.message.voice = None
    event.message.photo = None
    event.message.video_note = None
    event.message.document = None

    return event


# ============================================================================
# 1. PIPELINE STEP 1: IGNORE FILTERS
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_ignores_own_messages(userbot_service: UserbotService, test_db: DatabaseManager):
    event = make_mock_event(is_self=True)
    await userbot_service._handle_private_message(event)

    # Verify no debounce tasks and no messages logged
    assert len(userbot_service._debounce_buffers) == 0
    history = await test_db.get_recent_history(chat_id=11111)
    assert len(history) == 0


@pytest.mark.asyncio
async def test_pipeline_ignores_bot_messages(userbot_service: UserbotService, test_db: DatabaseManager):
    event = make_mock_event(is_bot=True)
    await userbot_service._handle_private_message(event)

    assert len(userbot_service._debounce_buffers) == 0
    history = await test_db.get_recent_history(chat_id=11111)
    assert len(history) == 0


@pytest.mark.asyncio
async def test_pipeline_ignores_service_messages(userbot_service: UserbotService, test_db: DatabaseManager):
    event = make_mock_event(action=MagicMock())
    await userbot_service._handle_private_message(event)

    assert len(userbot_service._debounce_buffers) == 0
    history = await test_db.get_recent_history(chat_id=11111)
    assert len(history) == 0


@pytest.mark.asyncio
async def test_pipeline_ignores_unlisted_contact(userbot_service: UserbotService, test_db: DatabaseManager):
    # Sender is NOT in database /contacts
    event = make_mock_event(sender_id=99999, username="stranger", raw_text="Salom")
    await userbot_service._handle_private_message(event)

    # Strictly ignored: no buffering, no auto-reply, no draft
    assert 99999 not in userbot_service._debounce_buffers
    drafts = await test_db.list_pending_drafts()
    assert len(drafts) == 0


# ============================================================================
# 2. PIPELINE STEP 2: MUTE MODE
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_stops_on_mute_contact(userbot_service: UserbotService, test_db: DatabaseManager):
    # Save contact with mode='mute'
    await test_db.upsert_contact(
        user_id=11111,
        username="muted_user",
        full_name="Muted Contact",
        mode="mute",
    )

    event = make_mock_event(sender_id=11111, username="muted_user", raw_text="Salom!")
    await userbot_service._handle_private_message(event)

    # Pipeline stopped at step 2: no debounce enqueue, no drafts
    assert 11111 not in userbot_service._debounce_buffers
    drafts = await test_db.list_pending_drafts()
    assert len(drafts) == 0


# ============================================================================
# 3. PIPELINE STEP 3: NON-TEXT MESSAGES
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_notifies_owner_on_non_text_message(userbot_service: UserbotService, test_db: DatabaseManager):
    await test_db.upsert_contact(
        user_id=22222,
        username="media_sender",
        full_name="Media Ali",
        mode="auto",
    )

    # Non-text media message (e.g. photo)
    event = make_mock_event(
        sender_id=22222,
        username="media_sender",
        first_name="Media Ali",
        raw_text="",
        media=MagicMock(spec=MessageMediaPhoto),
    )
    event.message.photo = True

    notification_mock = AsyncMock()
    userbot_service.set_notification_callback(notification_mock)

    await userbot_service._handle_private_message(event)

    # Owner should receive short notification
    assert notification_mock.await_count == 1
    call_text = notification_mock.await_args[0][0]
    assert "Media Ali" in call_text
    assert "@media_sender" in call_text
    assert "rasm" in call_text
    assert "Avto-javob yuborilmadi" in call_text

    # No auto-reply sent, no debounce buffer
    assert 22222 not in userbot_service._debounce_buffers


# ============================================================================
# 4. PIPELINE STEP 4: DEBOUNCE BUFFER AND CONSECUTIVE COMBINING
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_debounces_consecutive_messages(userbot_service: UserbotService, test_db: DatabaseManager):
    await test_db.upsert_contact(
        user_id=33333,
        username="chatty_user",
        full_name="Chatty Contact",
        mode="draft",
    )

    draft_callback_mock = AsyncMock()
    userbot_service.set_draft_approval_callback(draft_callback_mock)

    # Override debounce window to 0.1s for fast test
    with patch.object(settings, "DEBOUNCE_WINDOW_SECONDS", 0.1):
        event1 = make_mock_event(sender_id=33333, username="chatty_user", raw_text="Salom!", msg_id=101)
        event2 = make_mock_event(sender_id=33333, username="chatty_user", raw_text="Qayerdasiz?", msg_id=102)

        await userbot_service._handle_private_message(event1)
        await asyncio.sleep(0.01)
        await userbot_service._handle_private_message(event2)

        task = userbot_service._debounce_tasks.get(33333)
        if task:
            await task

    # Debounce combined the two messages into ONE
    assert draft_callback_mock.await_count == 1
    draft = draft_callback_mock.await_args[0][0]
    assert "Salom!" in draft.incoming_text
    assert "Qayerdasiz?" in draft.incoming_text
    assert draft.source_message_id == 102


# ============================================================================
# 5. PIPELINE STEP 7: AUTO MODE WITH SUPERSEDED CHECK
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_auto_mode_sends_when_no_owner_reply(userbot_service: UserbotService, test_db: DatabaseManager):
    await test_db.upsert_contact(
        user_id=44444,
        username="auto_user",
        full_name="Auto Contact",
        mode="auto",
    )

    with patch.object(settings, "DEBOUNCE_WINDOW_SECONDS", 0.05), \
         patch.object(settings, "HUMAN_DELAY_MIN", 0.01), \
         patch.object(settings, "HUMAN_DELAY_MAX", 0.02):

        event = make_mock_event(sender_id=44444, username="auto_user", raw_text="Qalaysiz?", msg_id=200)
        # Mock owner did not reply
        userbot_service.client.get_messages = AsyncMock(return_value=[])

        await userbot_service._handle_private_message(event)
        task = userbot_service._debounce_tasks.get(44444)
        if task:
            await task

    # Auto reply sent safely
    assert userbot_service.client.send_message.await_count == 1
    sent_args = userbot_service.client.send_message.await_args[0]
    assert sent_args[0] == 44444
    assert sent_args[1] == "Salom! Hammasi yaxshi, rahmat."

    # History logged to DB
    history = await test_db.get_recent_history(chat_id=44444)
    assert any(h.role == "assistant" for h in history)


@pytest.mark.asyncio
async def test_pipeline_auto_mode_cancels_if_owner_replied_manually(userbot_service: UserbotService, test_db: DatabaseManager):
    await test_db.upsert_contact(
        user_id=55555,
        username="manual_replied_user",
        full_name="Replied Contact",
        mode="auto",
    )

    with patch.object(settings, "DEBOUNCE_WINDOW_SECONDS", 0.05):
        event = make_mock_event(sender_id=55555, username="manual_replied_user", raw_text="Salom", msg_id=300)

        # Mock owner already sent a message with msg_id=305 > 300
        manual_msg = MagicMock()
        manual_msg.out = True
        manual_msg.id = 305
        userbot_service.client.get_messages = AsyncMock(return_value=[manual_msg])

        await userbot_service._handle_private_message(event)
        task = userbot_service._debounce_tasks.get(55555)
        if task:
            await task

    # Auto reply CANCELLED (superseded)
    assert userbot_service.client.send_message.await_count == 0


# ============================================================================
# 6. PIPELINE STEP 8: SENSITIVE TOPIC GUARD FORCES DRAFT FOR AUTO CONTACT
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_sensitive_topic_forces_draft_for_auto_contact(userbot_service: UserbotService, test_db: DatabaseManager):
    await test_db.upsert_contact(
        user_id=66666,
        username="sensitive_contact",
        full_name="Sensitive Contact",
        mode="auto",  # Mode is auto!
    )

    draft_callback_mock = AsyncMock()
    userbot_service.set_draft_approval_callback(draft_callback_mock)

    with patch.object(settings, "DEBOUNCE_WINDOW_SECONDS", 0.05):
        # Incoming text includes sensitive keyword ("pul") and card number
        sensitive_text = "Menga 8600 1234 5678 9012 kartaga 50000 som pul tashlab bering"
        event = make_mock_event(sender_id=66666, username="sensitive_contact", raw_text=sensitive_text, msg_id=400)

        await userbot_service._handle_private_message(event)
        task = userbot_service._debounce_tasks.get(66666)
        if task:
            await task

    # Even though mode is AUTO, sensitive guard forced DRAFT flow!
    assert userbot_service.client.send_message.await_count == 0
    assert draft_callback_mock.await_count == 1
    draft = draft_callback_mock.await_args[0][0]
    assert draft.is_sensitive is True
    assert draft.sensitive_reason is not None
    assert "Pul" in draft.sensitive_reason or "karta" in draft.sensitive_reason.lower()

    # Draft saved to database
    db_draft = await test_db.get_draft_by_id(draft.id)
    assert db_draft is not None
    assert db_draft.status == "pending"


# ============================================================================
# 7. TELEGRAM FLOODWAIT ERROR RETRY SAFETY
# ============================================================================

@pytest.mark.asyncio
async def test_send_with_floodwait_retries_safely(userbot_service: UserbotService):
    # Mock client raising FloodWaitError once, then succeeding
    attempt = 0

    async def mock_send(peer, text):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            fw = FloodWaitError(request=None)
            fw.seconds = 0.05
            raise fw
        return "success"

    userbot_service.client.send_message = AsyncMock(side_effect=mock_send)

    result = await userbot_service._send_with_floodwait(peer=12345, text="Salom", max_retries=3)
    assert result == "success"
    assert attempt == 2


# ============================================================================
# 8. SMART AUTONOMY & OWNER MANUAL HISTORY LOGGING
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_auto_mode_safely_handles_routine_meeting_chitchat(userbot_service: UserbotService, test_db: DatabaseManager):
    """Verifies that casual meeting inquiries in auto mode auto-reply when intent is SAFE_CHITCHAT."""
    from services.ai_engine import ClassificationResult

    await test_db.upsert_contact(
        user_id=77777,
        username="chitchat_friend",
        full_name="Friend Contact",
        mode="auto",
    )

    # Mock classify_incoming_message to return SAFE_CHITCHAT
    userbot_service.ai_engine.classify_incoming_message = AsyncMock(return_value=ClassificationResult(
        category="SAFE_CHITCHAT",
        reason="Do'stona kundalik hol-ahvol so'rovi",
        urgency="normal",
        confidence=0.95,
    ))
    userbot_service.ai_engine.generate_reply = AsyncMock(
        return_value="Salom jigar! Hozir biroz bandroq edim, bo'shashim bilan o'zim yozaman."
    )

    draft_callback_mock = AsyncMock()
    userbot_service.set_draft_approval_callback(draft_callback_mock)

    with patch.object(settings, "DEBOUNCE_WINDOW_SECONDS", 0.05), \
         patch.object(settings, "HUMAN_DELAY_MIN", 0.01), \
         patch.object(settings, "HUMAN_DELAY_MAX", 0.02):

        event = make_mock_event(
            sender_id=77777,
            username="chitchat_friend",
            raw_text="Bugun uchrashuv qachon bo'ladi?",
            msg_id=500,
        )
        userbot_service.client.get_messages = AsyncMock(return_value=[])

        await userbot_service._handle_private_message(event)
        task = userbot_service._debounce_tasks.get(77777)
        if task:
            await task

    # Auto reply SENT without drafting
    assert userbot_service.client.send_message.await_count == 1
    assert draft_callback_mock.await_count == 0


@pytest.mark.asyncio
async def test_owner_manual_message_logged_to_conversation_history(userbot_service: UserbotService, test_db: DatabaseManager):
    """Verifies that when owner sends manual message from phone, it is logged to conversation_messages."""
    await test_db.upsert_contact(
        user_id=88888,
        username="manual_sync_user",
        full_name="Sync User",
        mode="auto",
    )

    # Simulate outgoing owner manual message
    event = make_mock_event(
        sender_id=88888,
        username="manual_sync_user",
        raw_text="Men hozir ofisdaman, kelaver",
        msg_id=601,
        is_self=True,
    )
    event.out = True

    await userbot_service._handle_outgoing_message(event)

    # Verify message is in conversation_messages DB
    history = await test_db.get_recent_history(chat_id=88888, limit=10)
    assert len(history) >= 1
    assert any(m.role == "owner" and "ofisdaman" in m.content for m in history)

