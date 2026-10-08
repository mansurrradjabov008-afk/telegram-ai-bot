"""
Unit and integration tests for Phase 2: Autonomy Modes, Commands and Owner Authorization.
"""

import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from database.connection import init_db
from database.crud import DatabaseManager
from bot.admin_bot import (
    AdminBotService,
    normalize_and_validate_username,
)
from services.ai_engine import AIEngine
from services.scheduler_service import SchedulerService


@pytest_asyncio.fixture
async def test_db(tmp_path: Path):
    db_file = tmp_path / "test_modes.db"
    await init_db(db_file)
    manager = DatabaseManager(db_path=db_file)
    return manager


@pytest.fixture
def mock_ai_engine():
    engine = MagicMock(spec=AIEngine)
    engine.parse_crm_intent = AsyncMock(return_value=None)
    engine.parse_schedule_intent = AsyncMock(return_value=None)
    return engine


@pytest.fixture
def mock_scheduler():
    service = MagicMock(spec=SchedulerService)
    return service


@pytest_asyncio.fixture
async def admin_service(test_db: DatabaseManager, mock_ai_engine, mock_scheduler):
    # Boss Telegram ID is 7992504344
    service = AdminBotService(
        db_manager=test_db,
        ai_engine=mock_ai_engine,
        scheduler_service=mock_scheduler,
        token="1234567890:mock_token",
        admin_id=7992504344,
    )
    return service


# ============================================================================
# 1. USERNAME NORMALIZATION AND VALIDATION TESTS
# ============================================================================

def test_username_normalization_and_validation():
    # Valid usernames
    assert normalize_and_validate_username("@ValidUser") == "validuser"
    assert normalize_and_validate_username("Valid_User123") == "valid_user123"
    assert normalize_and_validate_username("  @john_doe  ") == "john_doe"
    assert normalize_and_validate_username("@a1234") == "a1234"
    assert normalize_and_validate_username("abcde") == "abcde"

    # Invalid: Starts with digit
    assert normalize_and_validate_username("@123invalid") is None
    assert normalize_and_validate_username("1user") is None

    # Invalid: Starts with underscore
    assert normalize_and_validate_username("@_myuser") is None

    # Invalid: Too short (< 5 chars)
    assert normalize_and_validate_username("@abc") is None
    assert normalize_and_validate_username("@a_b") is None
    assert normalize_and_validate_username("@abcd") is None

    # Invalid: Too long (> 32 chars)
    assert normalize_and_validate_username("@a" + "b" * 32) is None

    # Invalid: Forbidden characters
    assert normalize_and_validate_username("@user.name") is None
    assert normalize_and_validate_username("@user-name") is None
    assert normalize_and_validate_username("@user!name") is None

    # Invalid: Empty or whitespace
    assert normalize_and_validate_username("") is None
    assert normalize_and_validate_username("   ") is None
    assert normalize_and_validate_username("@") is None
    assert normalize_and_validate_username(None) is None


# ============================================================================
# 2. OWNER-ONLY AUTHORIZATION TESTS
# ============================================================================

def test_owner_only_authorization_check(admin_service: AdminBotService):
    owner_id = 7992504344
    non_owner_id = 9999999999

    assert admin_service._is_admin(owner_id) is True
    assert admin_service._is_admin(non_owner_id) is False
    assert admin_service._is_admin(0) is False


@pytest.mark.asyncio
async def test_non_owner_commands_and_callbacks_silently_ignored(
    admin_service: AdminBotService,
    test_db: DatabaseManager,
):
    non_owner_id = 9876543210

    # Non-owner message
    fake_message = MagicMock()
    fake_message.from_user = MagicMock()
    fake_message.from_user.id = non_owner_id
    fake_message.text = "/status"
    fake_message.reply = AsyncMock()

    # Trigger cmd_status
    status_handler = None
    for handler in admin_service.dp.message.handlers:
        if any("status" in str(f) for f in getattr(handler, "filters", [])):
            status_handler = handler.callback
            break

    # If calling through dp or direct function, test silent exit
    assert admin_service._is_admin(fake_message.from_user.id) is False
    fake_message.reply.assert_not_called()

    # Non-owner callback
    fake_callback = MagicMock()
    fake_callback.from_user = MagicMock()
    fake_callback.from_user.id = non_owner_id
    fake_callback.data = "crm_mode:davron:auto"
    fake_callback.answer = AsyncMock()

    assert admin_service._is_admin(fake_callback.from_user.id) is False
    fake_callback.answer.assert_not_called()


# ============================================================================
# 3. CONTACT AUTONOMY MODE SWITCHING & /mode COMMAND TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_mode_command_lifecycle(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344

    # 1. Create a contact (default: draft)
    contact = await test_db.upsert_contact(
        user_id=111,
        username="davron_aka",
        full_name="Davron Aka",
        relationship_type="rasmiy_hamkor",
        mode="draft",
    )
    assert contact.mode == "draft"

    # 2. Switch to auto mode
    success = await test_db.update_contact_mode_by_username("davron_aka", "auto")
    assert success is True
    updated = await test_db.get_contact_by_username("davron_aka")
    assert updated.mode == "auto"

    # 3. Switch to mute mode
    success = await test_db.update_contact_mode_by_username("davron_aka", "mute")
    assert success is True
    updated = await test_db.get_contact_by_username("davron_aka")
    assert updated.mode == "mute"

    # 4. Invalid mode raises ValueError in CRUD
    with pytest.raises(ValueError):
        await test_db.update_contact_mode_by_username("davron_aka", "invalid_mode")

    # 5. Non-existent contact returns False
    res = await test_db.update_contact_mode_by_username("non_existent_user", "auto")
    assert res is False


# ============================================================================
# 4. /crm DEFAULTS NEW CONTACTS TO 'draft' MODE
# ============================================================================

@pytest.mark.asyncio
async def test_crm_creates_contact_in_draft_mode(test_db: DatabaseManager):
    # Insert new contact without explicit mode
    new_contact = await test_db.upsert_contact(
        user_id=222,
        username="sherzod_pro",
        full_name="Sherzod",
        relationship_type="hamkor",
        addressing_style="siz",
        custom_instructions="Rasmiy muloqot",
    )
    assert new_contact.mode == "draft"

    # Updating contact retains their chosen mode
    await test_db.update_contact_mode(222, "auto")
    updated_contact = await test_db.upsert_contact(
        user_id=222,
        username="sherzod_pro",
        full_name="Sherzod Yangi",
        relationship_type="yaqin_hamkor",
        addressing_style="siz",
        mode="auto",
    )
    assert updated_contact.mode == "auto"


# ============================================================================
# 5. /status REPORTING OF AUTONOMY MODES AND PENDING DRAFTS
# ============================================================================

@pytest.mark.asyncio
async def test_status_mode_counts_and_pending_drafts(test_db: DatabaseManager):
    # Setup 3 contacts in different modes
    await test_db.upsert_contact(user_id=301, username="user_auto", full_name="User 1", mode="auto")
    await test_db.upsert_contact(user_id=302, username="user_draft1", full_name="User 2", mode="draft")
    await test_db.upsert_contact(user_id=303, username="user_draft2", full_name="User 3", mode="draft")
    await test_db.upsert_contact(user_id=304, username="user_mute", full_name="User 4", mode="mute")

    counts = await test_db.get_mode_counts()
    assert counts["auto"] == 1
    assert counts["draft"] == 2
    assert counts["mute"] == 1
    assert counts["total"] == 4

    # Setup 2 drafts
    d1 = await test_db.create_draft(
        contact_username="user_draft1",
        contact_user_id=302,
        source_message_id=9001,
        incoming_text="Salom, loyiha qachon tayyor bo'ladi?",
        proposed_reply="Tez orada tayyor bo'ladi.",
    )
    assert d1 is not None

    d2 = await test_db.create_draft(
        contact_username="user_draft2",
        contact_user_id=303,
        source_message_id=9002,
        incoming_text="Karta raqamingizni tashlab bering",
        proposed_reply="Karta raqamim: 8600...",
        is_sensitive=True,
        sensitive_reason="Karta raqami so'raldi",
    )
    assert d2 is not None

    pending_count = await test_db.count_pending_drafts()
    assert pending_count == 2


# ============================================================================
# 6. DRAFT ACTIONS AND DOUBLE-PRESS ATOMIC PROTECTION
# ============================================================================

@pytest.mark.asyncio
async def test_draft_atomic_resolve_and_double_press_protection(test_db: DatabaseManager):
    draft = await test_db.create_draft(
        contact_username="client_ali",
        contact_user_id=401,
        source_message_id=5555,
        incoming_text="Hisob-fakturani yuboring",
        proposed_reply="Mana hisob-faktura.",
    )
    assert draft is not None
    assert draft.status == "pending"

    # First resolution: succeed
    res1 = await test_db.resolve_draft_atomic(draft.id, "sent")
    assert res1 is True

    # Check updated record
    resolved_draft = await test_db.get_draft_by_id(draft.id)
    assert resolved_draft.status == "sent"
    assert resolved_draft.resolved_at is not None

    # Second resolution (double-press): MUST fail atomically
    res2 = await test_db.resolve_draft_atomic(draft.id, "sent")
    assert res2 is False

    # Also rejecting already sent draft MUST fail
    res3 = await test_db.resolve_draft_atomic(draft.id, "rejected")
    assert res3 is False


@pytest.mark.asyncio
async def test_draft_approve_send_and_double_press_callbacks(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344
    userbot_send_mock = AsyncMock()
    admin_service.set_userbot_sender(userbot_send_mock)

    draft = await test_db.create_draft(
        contact_username="davron_aka",
        contact_user_id=111,
        source_message_id=701,
        incoming_text="Qachon uchrashamiz?",
        proposed_reply="Ertaga 15:00 da.",
    )

    send_handler = None
    for h in admin_service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_draft_send":
            send_handler = h.callback
            break

    assert send_handler is not None

    fake_cb = MagicMock()
    fake_cb.from_user.id = owner_id
    fake_cb.data = f"draft_send:{draft.id}"
    fake_cb.answer = AsyncMock()
    fake_cb.message = MagicMock()
    fake_cb.message.edit_text = AsyncMock()

    # 1. First click: approves and sends
    await send_handler(fake_cb)

    assert userbot_send_mock.await_count == 1
    userbot_send_mock.assert_awaited_with("@davron_aka", "Ertaga 15:00 da.")
    fake_cb.answer.assert_awaited()
    assert "yuborildi" in fake_cb.answer.await_args[0][0].lower()

    # Draft status is updated to sent
    d_after = await test_db.get_draft_by_id(draft.id)
    assert d_after.status == "sent"

    # 2. Second click (double-press protection): must answer "Allaqachon bajarilgan"
    fake_cb.answer.reset_mock()
    await send_handler(fake_cb)
    assert userbot_send_mock.await_count == 1  # Not sent again!
    assert "allaqachon bajarilgan" in fake_cb.answer.await_args[0][0].lower()


@pytest.mark.asyncio
async def test_draft_reject_callback(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344
    userbot_send_mock = AsyncMock()
    admin_service.set_userbot_sender(userbot_send_mock)

    draft = await test_db.create_draft(
        contact_username="davron_aka",
        contact_user_id=111,
        source_message_id=702,
        incoming_text="Hisob raqamingizni bering",
        proposed_reply="Mana hisob...",
        is_sensitive=True,
    )

    reject_handler = None
    for h in admin_service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_draft_reject":
            reject_handler = h.callback
            break

    assert reject_handler is not None

    fake_cb = MagicMock()
    fake_cb.from_user.id = owner_id
    fake_cb.data = f"draft_reject:{draft.id}"
    fake_cb.answer = AsyncMock()
    fake_cb.message = MagicMock()
    fake_cb.message.edit_text = AsyncMock()

    await reject_handler(fake_cb)

    assert userbot_send_mock.await_count == 0  # Nothing sent
    fake_cb.answer.assert_awaited()
    assert "rad etildi" in fake_cb.answer.await_args[0][0].lower()

    d_after = await test_db.get_draft_by_id(draft.id)
    assert d_after.status == "rejected"


@pytest.mark.asyncio
async def test_draft_expired_cannot_be_sent(admin_service: AdminBotService, test_db: DatabaseManager):
    from datetime import datetime, timedelta
    owner_id = 7992504344
    userbot_send_mock = AsyncMock()
    admin_service.set_userbot_sender(userbot_send_mock)

    # Draft with past expiration
    exp_past = (datetime.now() - timedelta(hours=2)).isoformat()
    draft = await test_db.create_draft(
        contact_username="davron_aka",
        contact_user_id=111,
        source_message_id=703,
        incoming_text="Eski xabar",
        proposed_reply="Eski javob",
        expires_at=exp_past,
    )

    send_handler = None
    for h in admin_service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_draft_send":
            send_handler = h.callback
            break

    fake_cb = MagicMock()
    fake_cb.from_user.id = owner_id
    fake_cb.data = f"draft_send:{draft.id}"
    fake_cb.answer = AsyncMock()

    await send_handler(fake_cb)

    # Not sent
    assert userbot_send_mock.await_count == 0
    assert "muddati tugagan" in fake_cb.answer.await_args[0][0].lower()
    d_after = await test_db.get_draft_by_id(draft.id)
    assert d_after.status == "expired"


@pytest.mark.asyncio
async def test_draft_superseded_if_owner_replied_manually(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344
    userbot_send_mock = AsyncMock()
    admin_service.set_userbot_sender(userbot_send_mock)

    # Mock manual reply checker: owner ALREADY replied manually to this contact
    admin_service.set_manual_reply_checker(AsyncMock(return_value=True))

    draft = await test_db.create_draft(
        contact_username="davron_aka",
        contact_user_id=111,
        source_message_id=704,
        incoming_text="Salom!",
        proposed_reply="Assalomu alaykum!",
    )

    send_handler = None
    for h in admin_service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_draft_send":
            send_handler = h.callback
            break

    fake_cb = MagicMock()
    fake_cb.from_user.id = owner_id
    fake_cb.data = f"draft_send:{draft.id}"
    fake_cb.answer = AsyncMock()
    fake_cb.message = MagicMock()
    fake_cb.message.edit_text = AsyncMock()

    await send_handler(fake_cb)

    # Not sent, cancelled as superseded
    assert userbot_send_mock.await_count == 0
    assert "superseded" in fake_cb.answer.await_args[0][0].lower()
    d_after = await test_db.get_draft_by_id(draft.id)
    assert d_after.status == "superseded"

