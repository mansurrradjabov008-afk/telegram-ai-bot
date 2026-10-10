"""
Tests for UI Interactive Hubs and Persistent Reply Keyboard (Admin Cockpit).
Verifies:
- get_main_reply_keyboard() returns 8 executive buttons with resize_keyboard=True.
- cmd_start sends both persistent reply keyboard and rich inline dashboard.
- _render_memory_panel renders one-tap contact buttons for all saved contacts.
- _render_contacts_list contains memory shortcut button for every contact.
- Reply Keyboard button text handlers are registered and invoke corresponding panels.
"""

import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from database.connection import init_db
from database.crud import DatabaseManager
from bot.admin_bot import AdminBotService, get_main_reply_keyboard
from aiogram import types


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    db_path = tmp_path / "test_ui_hubs.db"
    await init_db(db_path)
    db_mgr = DatabaseManager(db_path)
    return db_mgr


def test_main_reply_keyboard_structure():
    """Verify persistent reply keyboard has all 8 hubs and resize options."""
    kb = get_main_reply_keyboard()
    assert isinstance(kb, types.ReplyKeyboardMarkup)
    assert kb.resize_keyboard is True
    assert kb.persistent is True

    # Check button texts
    all_texts = [btn.text for row in kb.keyboard for btn in row]
    expected_buttons = [
        "🧠 Xotira",
        "👥 Kontaktlar",
        "📝 Qoralamalar",
        "⏰ Vazifalar",
        "📰 Executive Digest",
        "⚙️ Avtonomiya (Mode)",
        "⚡ Tizim holati",
        "❓ Yordam",
    ]
    for exp in expected_buttons:
        assert exp in all_texts, f"Missing expected button: {exp}"


@pytest.mark.asyncio
async def test_cmd_start_sends_keyboards(temp_db):
    """Verify cmd_start activates reply keyboard and sends inline dashboard."""
    db_mgr = temp_db
    owner_id = 999111
    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    cmd_start_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "cmd_start":
            cmd_start_fn = h.callback
            break

    assert cmd_start_fn is not None

    mock_msg = MagicMock()
    mock_msg.from_user.id = owner_id
    mock_msg.answer = AsyncMock()
    mock_msg.reply = AsyncMock()

    await cmd_start_fn(mock_msg)

    # Verify answer was called (reply keyboard activation)
    assert mock_msg.answer.called
    call_kwargs = mock_msg.answer.call_args.kwargs
    assert isinstance(call_kwargs.get("reply_markup"), types.ReplyKeyboardMarkup)


@pytest.mark.asyncio
async def test_render_memory_panel_has_contact_buttons(temp_db):
    """Verify memory panel generates clickable buttons for each saved contact."""
    db_mgr = temp_db
    await db_mgr.upsert_contact(user_id=1001, username="alisher_navoiy", full_name="Alisher Navoiy")
    await db_mgr.upsert_contact(user_id=1002, username="bobur_mirzo", full_name="Bobur Mirzo")

    owner_id = 999111
    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    # Find handle_btn_memory
    btn_memory_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "handle_btn_memory":
            btn_memory_fn = h.callback
            break

    assert btn_memory_fn is not None

    mock_msg = MagicMock()
    mock_msg.from_user.id = owner_id
    mock_msg.reply = AsyncMock()

    await btn_memory_fn(mock_msg)
    assert mock_msg.reply.called
    call_args = mock_msg.reply.call_args
    markup = call_args.kwargs.get("reply_markup")
    assert markup is not None

    # Check callbacks in markup
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "mem_back:1001" in callbacks
    assert "mem_back:1002" in callbacks
    assert "menu:contacts" in callbacks
    assert "menu:main" in callbacks


@pytest.mark.asyncio
async def test_dashboard_renders_live_metrics_and_hubs(temp_db):
    """Verify _render_dashboard outputs executive layout and all 8 hubs."""
    db_mgr = temp_db
    await db_mgr.upsert_contact(user_id=2001, username="test_ceo", full_name="Test CEO", mode="auto")
    owner_id = 999111
    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    mock_msg = MagicMock()
    mock_msg.from_user.id = owner_id
    mock_msg.reply = AsyncMock()

    # Call cmd_start which calls _render_dashboard
    cmd_start_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "cmd_start":
            cmd_start_fn = h.callback
            break
    assert cmd_start_fn is not None

    mock_msg.answer = AsyncMock()
    await cmd_start_fn(mock_msg)

    # Check dashboard reply text
    assert mock_msg.reply.called
    text = mock_msg.reply.call_args[0][0]
    assert "ANTIGRAVITY EXECUTIVE COCKPIT" in text
    assert "JONLI KO'RSATKICHLAR" in text

    markup = mock_msg.reply.call_args.kwargs.get("reply_markup")
    assert markup is not None
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "menu:memory" in callbacks
    assert "menu:contacts" in callbacks
    assert "menu:drafts" in callbacks
    assert "menu:tasks" in callbacks
    assert "digest:open" in callbacks
    assert "menu:mode" in callbacks
    assert "menu:status" in callbacks
    assert "menu:main" in callbacks


@pytest.mark.asyncio
async def test_callback_menu_main_renders_dashboard(temp_db):
    """Verify menu:main callback edits message to executive dashboard."""
    db_mgr = temp_db
    owner_id = 999111
    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    menu_handler = None
    for h in service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_menu_callbacks":
            menu_handler = h.callback
            break
    assert menu_handler is not None

    cb = MagicMock()
    cb.from_user.id = owner_id
    cb.data = "menu:main"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    await menu_handler(cb)
    assert cb.message.edit_text.called
    edited_text = cb.message.edit_text.call_args[0][0]
    assert "ANTIGRAVITY EXECUTIVE COCKPIT" in edited_text
    assert "JONLI KO'RSATKICHLAR" in edited_text


@pytest.mark.asyncio
async def test_callback_crm_add_prompt(temp_db):
    """Verify crm:add_prompt callback edits message with clear instructions."""
    db_mgr = temp_db
    owner_id = 999111
    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    add_prompt_handler = None
    for h in service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_crm_add_prompt":
            add_prompt_handler = h.callback
            break
    assert add_prompt_handler is not None

    cb = MagicMock()
    cb.from_user.id = owner_id
    cb.data = "crm:add_prompt"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    await add_prompt_handler(cb)
    assert cb.message.edit_text.called
    edited_text = cb.message.edit_text.call_args[0][0]
    assert "YANGI KONTAKT QO'SHISH" in edited_text


@pytest.mark.asyncio
async def test_unified_contact_card_renders_all_facets(temp_db):
    """Verify unified contact card renders CRM, mode, memory stats, and 1-tap toggles."""
    db_mgr = temp_db
    await db_mgr.upsert_contact(
        user_id=5001,
        username="alisher_dev",
        full_name="Alisher Dev",
        addressing_style="siz",
        mode="draft",
        custom_instructions="Hurmat bilan gaplash",
    )
    await db_mgr.create_mem_fact(
        contact_user_id=5001,
        category="preference",
        text="Ertalab qora kofe ichadi",
        source_type="owner_confirmed",
        source_message_ids=[-1],
        confidence=1.0,
    )

    owner_id = 999111
    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    mem_back_fn = None
    for h in service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_mem_back":
            mem_back_fn = h.callback
            break
    assert mem_back_fn is not None

    cb = MagicMock()
    cb.from_user.id = owner_id
    cb.data = "mem_back:5001"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    await mem_back_fn(cb)
    assert cb.message.edit_text.called
    text = cb.message.edit_text.call_args[0][0]
    markup = cb.message.edit_text.call_args.kwargs.get("reply_markup")

    # Verify all facets in unified card text
    assert "KONTAKT" in text
    assert "Murojaat uslubi:" in text
    assert "Avtonomiya rejimi:" in text
    assert "Saqlangan xabarlar:" in text
    assert "Faol faktlar:" in text

    # Verify 1-tap buttons in markup
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "crm_style:alisher_dev:sen" in callbacks
    assert "mode_set:5001:auto" in callbacks
    assert "mode_set:5001:draft" in callbacks
    assert "mode_set:5001:mute" in callbacks
    assert "mem_facts_view:5001" in callbacks
    assert "mem_forget_prompt:5001" in callbacks
    assert "menu:contacts" in callbacks
    assert "menu:main" in callbacks


@pytest.mark.asyncio
async def test_natural_language_contact_actions(temp_db):
    """Verify natural language without @ updates contact style and mode."""
    db_mgr = temp_db
    await db_mgr.upsert_contact(
        user_id=6001,
        username="dilshod_pro",
        full_name="Dilshod Aka",
        addressing_style="siz",
        mode="draft",
    )

    owner_id = 999111
    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    fallback_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "handle_fallback_natural_text":
            fallback_fn = h.callback
            break
    assert fallback_fn is not None

    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)

    # 1. Test "Dilshodni senla"
    msg1 = MagicMock()
    msg1.from_user.id = owner_id
    msg1.text = "Dilshodni senla"
    msg1.reply_to_message = None
    msg1.reply = AsyncMock()

    await fallback_fn(msg1, state)
    contact_after_1 = await db_mgr.get_contact_by_id(6001)
    assert contact_after_1.addressing_style == "sen"

    # 2. Test "Dilshodni auto qil"
    msg2 = MagicMock()
    msg2.from_user.id = owner_id
    msg2.text = "Dilshodni auto qil"
    msg2.reply_to_message = None
    msg2.reply = AsyncMock()

    await fallback_fn(msg2, state)
    contact_after_2 = await db_mgr.get_contact_by_id(6001)
    assert contact_after_2.mode == "auto"


@pytest.mark.asyncio
async def test_drafts_bulk_actions_and_smart_buttons(temp_db):
    """Verify drafts list has bulk send/reject buttons and callbacks work."""
    db_mgr = temp_db
    d1 = await db_mgr.create_draft(
        source_message_id=101,
        contact_user_id=7001,
        contact_username="user1",
        incoming_text="Salom",
        proposed_reply="Salom, sizga qanday yordam bera olaman?",
    )
    d2 = await db_mgr.create_draft(
        source_message_id=102,
        contact_user_id=7002,
        contact_username="user2",
        incoming_text="Qayerdasiz?",
        proposed_reply="Ofisdaman.",
    )

    owner_id = 999111
    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    cmd_drafts_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "cmd_drafts":
            cmd_drafts_fn = h.callback
            break
    assert cmd_drafts_fn is not None

    mock_msg = MagicMock()
    mock_msg.from_user.id = owner_id
    mock_msg.reply = AsyncMock()
    await cmd_drafts_fn(mock_msg)

    assert mock_msg.reply.called
    markup = mock_msg.reply.call_args.kwargs.get("reply_markup")
    assert markup is not None
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "draft_bulk:send_all" in callbacks
    assert "draft_bulk:reject_all" in callbacks

    bulk_send_fn = None
    for h in service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_draft_bulk_send_all":
            bulk_send_fn = h.callback
            break
    assert bulk_send_fn is not None

    cb = MagicMock()
    cb.from_user.id = owner_id
    cb.data = "draft_bulk:send_all"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    await bulk_send_fn(cb)
    pending = await db_mgr.list_pending_drafts()
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_tasks_bulk_actions_and_smart_buttons(temp_db):
    """Verify tasks list has bulk cancel button and callback works."""
    db_mgr = temp_db
    t1 = await db_mgr.create_scheduled_task(
        target_peer="@user1",
        message_text="Salom 1",
        scheduled_time="2030-01-01T10:00:00",
    )
    t2 = await db_mgr.create_scheduled_task(
        target_peer="@user2",
        message_text="Salom 2",
        scheduled_time="2030-01-01T11:00:00",
    )

    owner_id = 999111
    scheduler_mock = MagicMock()
    scheduler_mock.cancel_task = AsyncMock()
    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=scheduler_mock,
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    btn_tasks_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "handle_btn_tasks":
            btn_tasks_fn = h.callback
            break
    assert btn_tasks_fn is not None

    mock_msg = MagicMock()
    mock_msg.from_user.id = owner_id
    mock_msg.reply = AsyncMock()
    await btn_tasks_fn(mock_msg)

    assert mock_msg.reply.called
    markup = mock_msg.reply.call_args.kwargs.get("reply_markup")
    assert markup is not None
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "task_bulk:cancel_all" in callbacks

    bulk_cancel_fn = None
    for h in service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_task_bulk_cancel_all":
            bulk_cancel_fn = h.callback
            break
    assert bulk_cancel_fn is not None

    cb = MagicMock()
    cb.from_user.id = owner_id
    cb.data = "task_bulk:cancel_all"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    await bulk_cancel_fn(cb)
    assert scheduler_mock.cancel_task.call_count == 2


@pytest.mark.asyncio
async def test_natural_language_smart_commands(temp_db):
    """Verify natural language commands for bulk drafts, tasks, and memory facts."""
    db_mgr = temp_db
    await db_mgr.upsert_contact(
        user_id=8001,
        username="sherzod_dev",
        full_name="Sherzod Dev",
    )
    d1 = await db_mgr.create_draft(
        source_message_id=201,
        contact_user_id=8001,
        contact_username="sherzod_dev",
        incoming_text="Qachon topshirasiz?",
        proposed_reply="Ertaga kechga tayyor bo'ladi.",
    )
    t1 = await db_mgr.create_scheduled_task(
        target_peer="@sherzod_dev",
        message_text="Ertalabki reja",
        scheduled_time="2030-01-01T09:00:00",
    )

    owner_id = 999111
    scheduler_mock = MagicMock()
    scheduler_mock.cancel_task = AsyncMock()
    scheduler_mock.execute_now = AsyncMock(return_value=True)

    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=MagicMock(),
        scheduler_service=scheduler_mock,
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    fallback_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "handle_fallback_natural_text":
            fallback_fn = h.callback
            break
    assert fallback_fn is not None

    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)

    # 1. Natural Fact Addition: "Sherzod haqida fakt: Ertalab qahva ichmaydi"
    msg_fact = MagicMock()
    msg_fact.from_user.id = owner_id
    msg_fact.text = "Sherzod haqida fakt: Ertalab qahva ichmaydi"
    msg_fact.reply_to_message = None
    msg_fact.reply = AsyncMock()

    await fallback_fn(msg_fact, state)
    facts = await db_mgr.get_mem_facts(8001)
    assert len(facts) >= 1
    assert any("Ertalab qahva ichmaydi" in f.text for f in facts)

    # 2. Natural Draft Action: "Hammasini tasdiqla"
    msg_bulk_draft = MagicMock()
    msg_bulk_draft.from_user.id = owner_id
    msg_bulk_draft.text = "Hammasini tasdiqla"
    msg_bulk_draft.reply_to_message = None
    msg_bulk_draft.reply = AsyncMock()

    await fallback_fn(msg_bulk_draft, state)
    pending_drafts = await db_mgr.list_pending_drafts()
    assert len(pending_drafts) == 0

    # 3. Natural Task Action: "Vazifa #1 ni bekor qil"
    msg_task = MagicMock()
    msg_task.from_user.id = owner_id
    msg_task.text = f"Vazifa #{t1.id} ni bekor qil"
    msg_task.reply_to_message = None
    msg_task.reply = AsyncMock()

    await fallback_fn(msg_task, state)
    assert scheduler_mock.cancel_task.called


