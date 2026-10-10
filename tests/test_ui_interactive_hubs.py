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
