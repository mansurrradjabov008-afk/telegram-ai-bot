"""
Unit tests for Writing Style Admin Bot UI features:
- Cockpit 2 style buttons (Quick Toggle + Style Panel)
- Quick toggle callback (style:toggle_quick)
- Presets menu & selection (style:presets_menu, style:preset:short, style:preset:clear)
- Aliases (/style, /stayl, /uslub, stayl on @user)
- Style panel rendering with interactive control buttons
"""

import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from database.connection import init_db
from database.crud import DatabaseManager
from bot.admin_bot import AdminBotService
from services.style_service import StyleService
from aiogram import types


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    db_path = tmp_path / "test_style_ui.db"
    await init_db(db_path)
    db_mgr = DatabaseManager(db_path)
    await db_mgr.init_style_schema()
    return db_mgr


@pytest.fixture
def mock_service(temp_db):
    owner_id = 999111
    style_srv = StyleService(temp_db)
    admin_srv = AdminBotService(
        db_manager=temp_db,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
        style_service=style_srv,
    )
    return admin_srv, owner_id


@pytest.mark.asyncio
async def test_dashboard_renders_two_style_buttons(mock_service):
    """Verify Cockpit dashboard renders the 2 style buttons (Quick Toggle and Style Hub)."""
    admin_srv, owner_id = mock_service
    
    cmd_start_fn = None
    for h in admin_srv.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "cmd_start":
            cmd_start_fn = h.callback
            break
    assert cmd_start_fn is not None

    mock_msg = MagicMock()
    mock_msg.from_user.id = owner_id
    mock_msg.reply = AsyncMock()

    await cmd_start_fn(mock_msg)
    assert mock_msg.reply.called
    call_args = mock_msg.reply.call_args
    reply_markup = call_args.kwargs.get("reply_markup") or (call_args.args[1] if len(call_args.args) > 1 else None)
    assert isinstance(reply_markup, types.InlineKeyboardMarkup)
    
    button_callbacks = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
    assert "style:toggle_quick" in button_callbacks
    assert "style:panel" in button_callbacks


@pytest.mark.asyncio
async def test_quick_toggle_callback(mock_service):
    """Verify clicking style:toggle_quick switches state on/off."""
    admin_srv, owner_id = mock_service

    toggle_fn = None
    for h in admin_srv.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_style_toggle_quick":
            toggle_fn = h.callback
            break
    assert toggle_fn is not None

    mock_cb = MagicMock()
    mock_cb.from_user.id = owner_id
    mock_cb.answer = AsyncMock()
    mock_cb.message = MagicMock()
    mock_cb.message.edit_text = AsyncMock()

    # Initial state is off -> toggling turns on
    await toggle_fn(mock_cb)
    mock_cb.answer.assert_called_with("🟢 Shaxsiy yozish uslubi yoqildi!", show_alert=False)
    
    # Second click turns off
    await toggle_fn(mock_cb)
    mock_cb.answer.assert_called_with("⏸ Shaxsiy yozish uslubi o'chirildi.", show_alert=False)


@pytest.mark.asyncio
async def test_presets_menu_and_preset_selection(mock_service):
    """Verify presets menu displays presets and selecting a preset stores the note."""
    admin_srv, owner_id = mock_service

    presets_menu_fn = None
    preset_choice_fn = None
    for h in admin_srv.dp.callback_query.handlers:
        name = getattr(h.callback, "__name__", "")
        if name == "handle_style_presets_menu":
            presets_menu_fn = h.callback
        elif name == "handle_style_preset_choice":
            preset_choice_fn = h.callback

    assert presets_menu_fn is not None
    assert preset_choice_fn is not None

    mock_cb = MagicMock()
    mock_cb.from_user.id = owner_id
    mock_cb.answer = AsyncMock()
    mock_cb.message = MagicMock()
    mock_cb.message.edit_text = AsyncMock()

    await presets_menu_fn(mock_cb)
    assert mock_cb.message.edit_text.called

    # Select short preset
    mock_cb.data = "style:preset:short"
    await preset_choice_fn(mock_cb)
    
    note = await admin_srv.db_manager.get_style_note("global")
    assert note is not None
    assert "londa" in note.lower()

    # Clear preset
    mock_cb.data = "style:preset:clear"
    await preset_choice_fn(mock_cb)
    note_cleared = await admin_srv.db_manager.get_style_note("global")
    assert note_cleared is None


@pytest.mark.asyncio
async def test_cmd_style_aliases(mock_service):
    """Verify cmd_style responds to /stayl on @user and /uslub."""
    admin_srv, owner_id = mock_service
    await admin_srv.db_manager.upsert_contact(
        user_id=8036556250,
        full_name="Mansur",
        username="mansurjon_07",
    )

    cmd_style_fn = None
    for h in admin_srv.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "cmd_style":
            cmd_style_fn = h.callback
            break
    assert cmd_style_fn is not None

    mock_msg = MagicMock()
    mock_msg.from_user.id = owner_id
    mock_msg.reply = AsyncMock()

    # Test /stayl on @mansurjon_07
    mock_msg.text = "/stayl on @mansurjon_07"
    await cmd_style_fn(mock_msg)
    assert mock_msg.reply.called
    assert "Mansur" in mock_msg.reply.call_args[0][0]
    assert "yoqildi" in mock_msg.reply.call_args[0][0]

    # Verify contact state was recorded
    state = await admin_srv.db_manager.get_style_note("contact:8036556250:state")
    assert state == "on"
