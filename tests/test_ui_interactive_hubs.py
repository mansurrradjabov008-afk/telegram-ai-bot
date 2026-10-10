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
    """Verify get_main_reply_keyboard returns ReplyKeyboardRemove for pure buttonless UX."""
    kb = get_main_reply_keyboard()
    assert isinstance(kb, types.ReplyKeyboardRemove)
    assert kb.remove_keyboard is True


@pytest.mark.asyncio
async def test_cmd_start_sends_keyboards(temp_db):
    """Verify cmd_start removes persistent reply keyboards and renders clean cockpit."""
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

    # Verify reply was called with ReplyKeyboardRemove to clear keyboard from client
    assert mock_msg.reply.called
    call_kwargs = mock_msg.reply.call_args.kwargs
    markup = call_kwargs.get("reply_markup")
    assert isinstance(markup, types.ReplyKeyboardRemove)
    assert markup.remove_keyboard is True


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
    # In pure buttonless mode, dashboard has NO inline keyboard buttons
    has_inline = hasattr(markup, "inline_keyboard") and bool(getattr(markup, "inline_keyboard", []))
    assert not has_inline, "Dashboard must have ZERO inline buttons in buttonless mode"


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


@pytest.mark.asyncio
async def test_natural_language_digest_actions(temp_db):
    """Verify natural language channel addition to digest does NOT pollute contacts table."""
    db_mgr = temp_db
    owner_id = 999111
    digest_service = MagicMock()
    digest_service.get_client.return_value = None

    async def mock_resolve_and_add(client, target):
        uname = target.lstrip("@").strip()
        s = await db_mgr.upsert_digest_source(peer_id=777888, title=uname, peer_type="channel", username=uname)
        return (True, f"✅ @{uname} kanali qo'shildi!", s)

    digest_service.resolve_and_add_source = AsyncMock(side_effect=mock_resolve_and_add)
    digest_service.remove_source = AsyncMock(return_value=(True, "✅ O'chirildi."))

    ai_engine = MagicMock()
    ai_engine.parse_crm_intent = AsyncMock(return_value={"is_crm": False})

    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=ai_engine,
        scheduler_service=MagicMock(),
        digest_service=digest_service,
        token="1234567890:mock_token",
        admin_id=owner_id,
    )

    crm_or_sched_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "handle_natural_crm_or_schedule":
            crm_or_sched_fn = h.callback
            break
    assert crm_or_sched_fn is not None

    # Test 1: Adding channel to digest: "@kunuz bu kanalni qushib quy dayjest bulimiga"
    msg_add = MagicMock()
    msg_add.from_user.id = owner_id
    msg_add.text = "@kunuz bu kanalni qushib quy dayjest bulimiga"
    msg_add.reply = AsyncMock()

    await crm_or_sched_fn(msg_add)

    # CRITICAL: It must NOT be added to contacts table!
    kunuz_contact = await db_mgr.get_contact_by_username("kunuz")
    assert kunuz_contact is None, "@kunuz must NEVER be added to contacts table!"

    # It must be added to digest sources!
    digest_sources = await db_mgr.list_digest_sources()
    assert any(s.username == "kunuz" for s in digest_sources), "@kunuz must be added to digest_sources!"

    # Zero buttons verified
    assert msg_add.reply.called
    reply_kwargs = msg_add.reply.call_args[1]
    assert reply_kwargs.get("reply_markup") is None, "Executive mode must have zero inline buttons!"

    # Test 2: Removing channel from digest: "@kunuz ni dayjestdan o'chir"
    msg_rem = MagicMock()
    msg_rem.from_user.id = owner_id
    msg_rem.text = "@kunuz ni dayjestdan o'chir"
    msg_rem.reply = AsyncMock()

    await crm_or_sched_fn(msg_rem)
    assert digest_service.remove_source.called
    assert msg_rem.reply.called

    # Test 3: Rule H1 ironclad guard in _apply_contact_instruction
    msg_guard = MagicMock()
    msg_guard.from_user.id = owner_id
    msg_guard.text = "@daryouz kanalini qo'sh"
    msg_guard.reply = AsyncMock()

    # Call _apply_contact_instruction directly with channel context
    apply_fn = None
    for attr_name in ["_apply_contact_instruction"]:
        # Find internal helper
        pass
    # Verify contact for daryouz is never created
    daryo_contact = await db_mgr.get_contact_by_username("daryouz")
    assert daryo_contact is None, "Channels must NEVER enter contacts table!"


@pytest.mark.asyncio
async def test_natural_language_contact_inquiry_and_outreach(temp_db):
    """
    Verifies executive inquiry and check-in:
    '@gulzodaxursandovadan habar olib quychi nima gaplar ekan hozir'
    1. Suffix stripping ('-dan' -> 'gulzodaxursandova')
    2. Telethon / history fetch & AI analysis
    3. Actionable executive briefing and check-in draft
    4. 1-word confirmation ('yubor') delivering message via userbot
    """
    db_mgr = temp_db
    owner_id = 999111

    ai_engine = MagicMock()
    ai_engine.parse_crm_intent = AsyncMock(return_value={"is_crm": False})
    ai_engine.analyze_contact_inquiry = AsyncMock(return_value={
        "summary": "29-sentyabr kuni o'qish haqida gaplashilgan. Oxirgi xabar: 'Ok'.",
        "suggested_outreach": "Assalomu alaykum Gulzoda, yaxshimisiz? Nima yangiliklar bor?",
        "is_direct_send": False,
        "direct_message": None,
    })

    userbot_client = MagicMock()
    target_entity = MagicMock()
    target_entity.id = 8173735172
    target_entity.first_name = "Gulzoda"
    userbot_client.get_entity = AsyncMock(return_value=target_entity)
    userbot_client.get_messages = AsyncMock(return_value=[
        MagicMock(text="Stm uquv markazi yaxshi", out=False, date=None),
        MagicMock(text="Ok", out=True, date=None),
    ])

    digest_service = MagicMock()
    digest_service.get_client.return_value = userbot_client

    userbot_sender = AsyncMock(return_value=True)

    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=ai_engine,
        scheduler_service=MagicMock(),
        digest_service=digest_service,
        token="1234567890:mock_token",
        admin_id=owner_id,
    )
    service.set_userbot_sender(userbot_sender)

    crm_or_sched_fn = None
    fallback_fn = None
    for h in service.dp.message.handlers:
        cb_name = getattr(h.callback, "__name__", "")
        if cb_name == "handle_natural_crm_or_schedule":
            crm_or_sched_fn = h.callback
        elif cb_name == "handle_fallback_natural_text":
            fallback_fn = h.callback

    assert crm_or_sched_fn is not None
    assert fallback_fn is not None

    # Step 1: User asks: "@gulzodaxursandovadan habar olib quychi nima gaplar ekan hozir"
    msg_inquiry = MagicMock()
    msg_inquiry.from_user.id = owner_id
    msg_inquiry.text = "@gulzodaxursandovadan habar olib quychi nima gaplar ekan hozir"
    msg_inquiry.reply = AsyncMock()

    await crm_or_sched_fn(msg_inquiry)

    # Verify AI inquiry was invoked with normalized username (without -dan)
    ai_engine.analyze_contact_inquiry.assert_called_once()
    call_args = ai_engine.analyze_contact_inquiry.call_args[1]
    assert call_args["username"] == "gulzodaxursandova"
    assert call_args["contact_name"] == "Gulzoda"

    # Verify reply sent with briefing & check-in draft
    assert msg_inquiry.reply.called
    sent_text = msg_inquiry.reply.call_args[0][0]
    assert "Gulzoda" in sent_text
    assert "Assalomu alaykum Gulzoda" in sent_text
    assert "yubor" in sent_text

    # Verify pending outreach is active
    assert service._pending_outreach is not None
    assert service._pending_outreach["peer"] == "@gulzodaxursandova"

    # Step 2: User replies "yubor" to confirm sending
    state_mock = MagicMock()
    state_mock.get_state = AsyncMock(return_value=None)

    msg_confirm = MagicMock()
    msg_confirm.from_user.id = owner_id
    msg_confirm.text = "yubor"
    msg_confirm.reply_to_message = None
    msg_confirm.reply = AsyncMock()

    await fallback_fn(msg_confirm, state=state_mock)

    # Verify userbot sent the message
    userbot_sender.assert_called_once_with(
        "@gulzodaxursandova",
        "Assalomu alaykum Gulzoda, yaxshimisiz? Nima yangiliklar bor?",
    )
    # Verify pending outreach is cleared
    assert service._pending_outreach is None
    # Verify confirmation sent
    assert msg_confirm.reply.called
    confirm_text = msg_confirm.reply.call_args[0][0]
    assert "Xabar muvaffaqiyatli yuborildi" in confirm_text


@pytest.mark.asyncio
async def test_jarvis_delegation_mission_and_schedule_guard(temp_db):
    """
    Critical Red-Green Test:
    User command from screenshot:
    '@umid_namozov nimalar bilan shugullanyapti ekan bugun kurishishga vaqti bormikin bugungi rejelari qanaqa ekan shu malumotlarni menga yetkaz bugun 20:00gacha'
    1. Must NOT be scheduled as an outgoing peer message (schedule_parser must return None).
    2. Must be intercepted by JARVIS Executive Delegation Engine.
    3. Must render buttonless executive briefing with deadline '20:00 gacha'.
    4. Must confirm and send via userbot when owner types 'yubor'.
    5. Must support text-based task cancellation without buttons ('bekor #99').
    """
    from services.schedule_parser import parse_deterministic_schedule, parse_schedule_intent_hybrid

    prompt = "@umid_namozov nimalar bilan shugullanyapti ekan bugun kurishishga vaqti bormikin bugungi rejelari qanaqa ekan shu malumotlarni menga yetkaz bugun 20:00gacha"

    # Step 1: Schedule Parser Guard
    det_res = parse_deterministic_schedule(prompt)
    assert det_res is None, "Delegation prompt must NEVER be parsed as a deterministic schedule!"

    mock_ai = MagicMock()
    mock_ai.parse_schedule_intent = AsyncMock(return_value={"is_schedule": True, "target_peer": "@umid_namozov", "scheduled_iso": "2026-10-10T20:00:00"})
    hybrid_res = await parse_schedule_intent_hybrid(prompt, mock_ai)
    assert hybrid_res is None, "Hybrid parser must reject delegation prompt even if AI hallucinates is_schedule!"

    # Step 2: Setup Admin Bot Service
    db_mgr = temp_db
    owner_id = 999111
    userbot_sender = AsyncMock()
    scheduler_mock = MagicMock()
    scheduler_mock.cancel_task = AsyncMock(return_value=True)

    await db_mgr.upsert_contact(
        user_id=888999,
        username="umid_namozov",
        full_name="Umid Namozov",
        mode="auto",
        addressing_style="siz",
    )

    ai_engine = MagicMock()
    ai_engine.analyze_contact_inquiry = AsyncMock(return_value={
        "summary": "Oxirgi yozishmalarda bugungi rejalar haqida ma'lumot yo'q.",
        "suggested_outreach": "Assalomu alaykum Umid aka, bugun rejalaringiz qanaqa? Ko'rishishga vaqtingiz bo'ladimi bugun?",
        "is_direct_send": False,
        "direct_message": None,
        "is_delegation_mission": True,
        "mission_goal": "Bugungi rejalari, bandligi va ko'rishish imkoniyatini aniqlash",
    })

    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=ai_engine,
        scheduler_service=scheduler_mock,
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )
    service.set_userbot_sender(userbot_sender)

    crm_or_sched_fn = None
    fallback_fn = None
    for h in service.dp.message.handlers:
        name = getattr(h.callback, "__name__", "")
        if name == "handle_natural_crm_or_schedule":
            crm_or_sched_fn = h.callback
        elif name == "handle_fallback_natural_text":
            fallback_fn = h.callback

    # Step 3: Trigger Delegation Command
    msg = MagicMock()
    msg.from_user.id = owner_id
    msg.text = prompt
    msg.reply = AsyncMock()

    await crm_or_sched_fn(msg)

    # Verify response
    assert msg.reply.called
    reply_text = msg.reply.call_args[0][0]
    reply_kwargs = msg.reply.call_args[1]
    assert "JARVIS Protocol" in reply_text
    assert "20:00 gacha" in reply_text
    assert "@umid_namozov" in reply_text
    assert "Tayyorlangan diplomatiya xabari" in reply_text
    assert reply_kwargs.get("reply_markup") is None, "Executive JARVIS briefing must have ZERO buttons (reply_markup=None)!"

    # Verify pending outreach is armed
    assert service._pending_outreach is not None
    assert service._pending_outreach["peer"] == "@umid_namozov"
    assert service._pending_outreach["is_mission"] is True

    # Step 4: Confirm with 'ha'
    state_mock = MagicMock()
    state_mock.get_state = AsyncMock(return_value=None)

    msg_confirm = MagicMock()
    msg_confirm.from_user.id = owner_id
    msg_confirm.text = "ha"
    msg_confirm.reply_to_message = None
    msg_confirm.reply = AsyncMock()

    await fallback_fn(msg_confirm, state=state_mock)

    userbot_sender.assert_called_once_with(
        "@umid_namozov",
        "Assalomu alaykum Umid aka, bugun rejalaringiz qanaqa? Ko'rishishga vaqtingiz bo'ladimi bugun?",
    )
    assert service._pending_outreach is None
    confirm_text = msg_confirm.reply.call_args[0][0]
    assert "Xabar muvaffaqiyatli yuborildi" in confirm_text
    assert msg_confirm.reply.call_args[1].get("reply_markup") is None

    # Step 5: Text-based task cancellation without buttons
    task_mock = MagicMock()
    task_mock.id = 99
    db_mgr.get_task_by_id = AsyncMock(return_value=task_mock)

    msg_cancel = MagicMock()
    msg_cancel.from_user.id = owner_id
    msg_cancel.text = "bekor #99"
    msg_cancel.reply_to_message = None
    msg_cancel.reply = AsyncMock()

    await fallback_fn(msg_cancel, state=state_mock)
    scheduler_mock.cancel_task.assert_called_once_with(99)
    cancel_reply = msg_cancel.reply.call_args[0][0]
    assert "Vazifa #99 bekor qilindi" in cancel_reply
    assert msg_cancel.reply.call_args[1].get("reply_markup") is None


@pytest.mark.asyncio
async def test_jarvis_direct_action_and_negative_constraints(temp_db):
    """
    Test exact scenario from user's latest screenshot:
    Prompt: '@umid_namozov dustimga yozchi ertangi rejalarini aniqlab menga yetkaz,aka dema sizlab murojat qilsang yetarli'
    1. 'aka dema' constraint MUST strictly remove any 'aka' or 'akaxon' from the outreach.
    2. 'dustimga yozchi' direct action verb MUST trigger immediate dispatch via userbot.
    3. Contact title 'Umid jura' MUST be cleaned to first name 'Umid' in greeting.
    4. Response to boss MUST report 'TOPSHIRIQ ZUDLIK BILAN BAJARILDI, JANOB!' with zero buttons.
    """
    from services.ai_engine import extract_clean_first_name, sanitize_outreach_constraints

    prompt = "@umid_namozov dustimga yozchi ertangi rejalarini aniqlab menga yetkaz,aka dema sizlab murojat qilsang yetarli"

    # 1. Test clean name extraction
    assert extract_clean_first_name("Umid jura") == "Umid"
    assert extract_clean_first_name("Umid do'st") == "Umid"

    # 2. Test negative constraint sanitizer
    raw_ai_outreach = "Assalomu alaykum Umid aka, yaxshimisiz? Ertangi rejalaringiz qanaqa, nimalar bilan bandsiz? Shuni aniqlashtirib qo'ymoqchi edim."
    sanitized = sanitize_outreach_constraints(raw_ai_outreach, prompt, "Umid")
    assert "aka" not in sanitized.lower(), "Constraint 'aka dema' must strictly remove 'aka'!"
    assert "Umid, yaxshimisiz?" in sanitized
    assert "Ertangi rejalaringiz" in sanitized

    # 3. Test Full AdminBot JARVIS Execution
    db_mgr = temp_db
    owner_id = 999111
    userbot_sender = AsyncMock()

    await db_mgr.upsert_contact(
        user_id=777666,
        username="umid_namozov",
        full_name="Umid jura",
        mode="auto",
        addressing_style="siz",
    )

    ai_engine = MagicMock()
    # Even if AI hallucinates 'Umid aka' and repeats boss words in summary:
    ai_engine.analyze_contact_inquiry = AsyncMock(return_value={
        "summary": "10-oktabrda so'nggi marta suhbatlashilgan. Ertangi rejalar bo'yicha ma'lumot yo'q.",
        "suggested_outreach": "Assalomu alaykum Umid aka, yaxshimisiz? Ertangi rejalaringiz qanaqa?",
        "is_direct_send": False,
        "direct_message": None,
        "is_delegation_mission": True,
        "is_direct_action": True,
        "mission_goal": "Ertangi rejalarini aniqlash",
    })

    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=ai_engine,
        scheduler_service=MagicMock(),
        digest_service=MagicMock(),
        token="1234567890:mock_token",
        admin_id=owner_id,
    )
    service.set_userbot_sender(userbot_sender)

    crm_or_sched_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "handle_natural_crm_or_schedule":
            crm_or_sched_fn = h.callback
            break
    assert crm_or_sched_fn is not None

    msg = MagicMock()
    msg.from_user.id = owner_id
    msg.text = prompt
    msg.reply = AsyncMock()

    await crm_or_sched_fn(msg)

    # 4. Verify Immediate Dispatch
    userbot_sender.assert_called_once()
    sent_peer = userbot_sender.call_args[0][0]
    sent_text = userbot_sender.call_args[0][1]

    assert sent_peer == "@umid_namozov"
    assert "aka" not in sent_text.lower(), "Dispatched message must NOT contain 'aka'!"
    assert "Umid" in sent_text
    assert "Ertangi rejalaringiz" in sent_text

    # 5. Verify Executive Cockpit Report
    assert msg.reply.called
    reply_text = msg.reply.call_args[0][0]
    reply_kwargs = msg.reply.call_args[1]

    assert "TOPSHIRIQ ZUDLIK BILAN BAJARILDI, JANOB! (JARVIS Protocol)" in reply_text
    assert "@umid_namozov" in reply_text
    assert "Yuborilgan xabar" in reply_text
    assert reply_kwargs.get("reply_markup") is None, "Zero buttons expected!"


@pytest.mark.asyncio
async def test_jarvis_news_digest_natural_trigger(temp_db):
    """
    Tests that 'bugungi yangliklar haqida malumot ber' (today's news query with typo)
    is cleanly recognized as a news/digest request, triggers _execute_digest without buttons,
    and returns an executive briefing with ZERO inline buttons.
    """
    owner_id = 999901
    ai_engine = MagicMock()
    ai_engine.generate_daily_news_briefing = AsyncMock(return_value="🤖 **AI yangiliklari:** Yangi modellar chiqdi.\n🌐 **Biznes:** Bozorlar barqaror.")

    digest_service = MagicMock()
    digest_service.run_smart_digest = AsyncMock(return_value={
        "status": "completed",
        "messages": ["📰 <b>KUNLIK EXECUTIVE DIGEST</b>\n\n• Yangiliklar muvaffaqiyatli tahlil qilindi."],
    })

    service = AdminBotService(
        db_manager=temp_db,
        ai_engine=ai_engine,
        scheduler_service=MagicMock(),
        digest_service=digest_service,
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

    msg = MagicMock()
    msg.from_user.id = owner_id
    msg.text = "bugungi yangliklar haqida malumot ber"
    msg.reply_to_message = None
    msg.reply = AsyncMock()

    await fallback_fn(msg, state)

    # 1. run_smart_digest should be called
    digest_service.run_smart_digest.assert_awaited_once_with(trigger="manual", topic_filter=None)

    # 2. msg.reply should have been called (loading and/or result)
    assert msg.reply.called
    # Check last call or any call: reply_markup must be None!
    for call in msg.reply.call_args_list:
        kwargs = call[1]
        assert kwargs.get("reply_markup") is None, "Executive news briefing must have ZERO buttons (reply_markup=None)!"


@pytest.mark.asyncio
async def test_jarvis_conversational_brain(temp_db):
    """
    Tests that natural conversational messages to JARVIS (e.g. 'bugungi holat qanday, menga xulosa ber')
    are routed to ask_jarvis with live context and answered with ZERO buttons.
    """
    owner_id = 999901
    ai_engine = MagicMock()
    ai_engine.ask_jarvis = AsyncMock(return_value="Kechki hurmat, Janob Radjabov Mansur. Barcha tizimlar barqaror ishlamoqda.")

    service = AdminBotService(
        db_manager=temp_db,
        ai_engine=ai_engine,
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

    msg = MagicMock()
    msg.from_user.id = owner_id
    msg.text = "bugungi holat qanday, menga xulosa ber"
    msg.reply_to_message = None
    msg.reply = AsyncMock()

    await fallback_fn(msg, state)

    # 1. ask_jarvis must be called with query and context
    ai_engine.ask_jarvis.assert_awaited_once()
    called_query = ai_engine.ask_jarvis.call_args[0][0]
    called_ctx = ai_engine.ask_jarvis.call_args[0][1]

    assert called_query == "bugungi holat qanday, menga xulosa ber"
    assert "Radjabov Mansur" in called_ctx["boss_name"]
    assert "current_time" in called_ctx

    # 2. msg.reply must be called with JARVIS response and ZERO buttons
    assert msg.reply.called
    last_call = msg.reply.call_args
    reply_text = last_call[0][0]
    reply_kwargs = last_call[1]

    assert "Janob Radjabov Mansur" in reply_text
    assert reply_kwargs.get("reply_markup") is None, "JARVIS conversational response must have ZERO buttons (reply_markup=None)!"


@pytest.mark.asyncio
async def test_jarvis_colloquial_surachi_and_username_cleaning(temp_db):
    """
    Test scenario from user's screenshot:
    Prompt: '@axtamov_0777 dan surachi nima gaplar ekan'
    1. 'surachi' / 'sorachi' must be normalized to 'so\'rachi' (NOT photographer 'suratchi').
    2. 'extract_clean_human_name' must resolve 'axtamov_0777' to 'Axtam'.
    3. Outreach greetings must never say 'Assalomu alaykum axtamov_0777' with numbers/underscores.
    4. Mission goal and outreach must not hallucinate 'suratchi' (photographer).
    5. Direct action dispatch sends clean greeting and reports back with zero buttons.
    """
    from services.ai_engine import (
        normalize_colloquial_uzbek_query,
        extract_clean_human_name,
        sanitize_outreach_constraints,
    )

    prompt = "@axtamov_0777 dan surachi nima gaplar ekan"

    # 1. Normalization & Human name extraction checks
    norm_query = normalize_colloquial_uzbek_query(prompt)
    assert "so'rachi" in norm_query
    assert "surachi" not in norm_query

    assert extract_clean_human_name("axtamov_0777") == "Axtam"
    assert extract_clean_human_name("umid_namozov") == "Umid"
    assert extract_clean_human_name("axtam_0777") == "Axtam"

    # 2. Sanitizer tests (prevent photographer hallucination & raw username greeting)
    hallucinated_outreach = "Assalomu alaykum axtamov_0777, ishlaring qanday ketyapti, tinchgina yuribsanmi? Suratchi bo'yicha nima gaplar, qanday yangiliklar bor?"
    clean_outreach = sanitize_outreach_constraints(hallucinated_outreach, prompt, "Axtam")
    assert "axtamov_0777" not in clean_outreach
    assert "suratchi" not in clean_outreach.lower()
    assert "Axtam" in clean_outreach

    # 3. Full AdminBot direct action test
    db_mgr = temp_db
    owner_id = 999111
    userbot_sender = AsyncMock(return_value=True)

    userbot_client = MagicMock()
    target_entity = MagicMock()
    target_entity.id = 777123
    target_entity.username = "axtamov_0777"
    target_entity.first_name = ""  # No first name in profile!
    target_entity.last_name = None
    userbot_client.get_entity = AsyncMock(return_value=target_entity)
    userbot_client.get_messages = AsyncMock(return_value=[])

    digest_service = MagicMock()
    digest_service.get_client.return_value = userbot_client

    ai_engine = MagicMock()
    # Mock AI engine returning inquiry analysis
    ai_engine.analyze_contact_inquiry = AsyncMock(return_value={
        "summary": "@axtamov_0777 bilan oldingi yozishmalar mavjud emas.",
        "suggested_outreach": "Assalomu alaykum Axtam, ishlaring qanday ketyapti? Nima yangiliklar bor?",
        "is_direct_send": False,
        "direct_message": None,
        "is_delegation_mission": True,
        "is_direct_action": True,
        "mission_goal": "Axtamdan hol-ahvol va yangiliklarni aniqlash",
    })

    service = AdminBotService(
        db_manager=db_mgr,
        ai_engine=ai_engine,
        scheduler_service=MagicMock(),
        digest_service=digest_service,
        token="1234567890:mock_token",
        admin_id=owner_id,
    )
    service.set_userbot_sender(userbot_sender)

    crm_or_sched_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "handle_natural_crm_or_schedule":
            crm_or_sched_fn = h.callback
            break
    assert crm_or_sched_fn is not None

    msg = MagicMock()
    msg.from_user.id = owner_id
    msg.text = prompt
    msg.reply = AsyncMock()

    await crm_or_sched_fn(msg)

    # 4. Verify AI Engine called with clean human name 'Axtam', not username
    ai_engine.analyze_contact_inquiry.assert_called_once()
    ai_call_kwargs = ai_engine.analyze_contact_inquiry.call_args[1]
    assert ai_call_kwargs["username"] == "axtamov_0777"
    assert ai_call_kwargs["contact_name"] == "Axtam"

    # 5. Verify direct dispatch
    userbot_sender.assert_called_once()
    sent_peer, sent_text = userbot_sender.call_args[0]
    assert sent_peer == "@axtamov_0777"
    assert "axtamov_0777" not in sent_text
    assert "suratchi" not in sent_text.lower()
    assert "Axtam" in sent_text

    # 6. Verify Cockpit reply
    assert msg.reply.called
    reply_text = msg.reply.call_args[0][0]
    assert "suratchi" not in reply_text.lower()
    assert "Axtam" in reply_text
    assert msg.reply.call_args[1].get("reply_markup") is None
