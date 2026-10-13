import pytest
from unittest.mock import AsyncMock, MagicMock
from aiogram import types
from bot.admin_bot import AdminBotService, ReplyState
from database.models import Contact

@pytest.mark.asyncio
async def test_outreach_state_and_registry():
    db_mock = AsyncMock()
    ai_mock = AsyncMock()
    sched_mock = AsyncMock()

    service = AdminBotService(
        db_manager=db_mock,
        ai_engine=ai_mock,
        scheduler_service=sched_mock,
        token="1234567890:dummy",
        admin_id=12345,
    )

    # 1. Verify waiting_for_contact_message state exists
    assert hasattr(ReplyState, "waiting_for_contact_message")

    # 2. Verify _pending_outreach_drafts registry exists
    assert hasattr(service, "_pending_outreach_drafts")
    assert isinstance(service._pending_outreach_drafts, dict)

    # 3. Verify handlers are registered on Dispatcher
    msg_handler_names = [getattr(h.callback, "__name__", "") for h in service.dp.message.handlers]
    assert "process_contact_outreach_message" in msg_handler_names

    cb_handler_names = [getattr(h.callback, "__name__", "") for h in service.dp.callback_query.handlers]
    assert "handle_crm_outreach_menu" in cb_handler_names
    assert "handle_crm_outreach_direct" in cb_handler_names
    assert "handle_crm_outreach_cancel" in cb_handler_names
    assert "handle_outreach_confirm" in cb_handler_names
    assert "handle_outreach_cancel" in cb_handler_names


@pytest.mark.asyncio
async def test_contacts_list_keyboard_has_outreach_button():
    db_mock = AsyncMock()
    ai_mock = AsyncMock()
    sched_mock = AsyncMock()
    owner_id = 12345

    service = AdminBotService(
        db_manager=db_mock,
        ai_engine=ai_mock,
        scheduler_service=sched_mock,
        token="1234567890:dummy",
        admin_id=owner_id,
    )

    mock_contact = Contact(
        user_id=111,
        username="test_contact",
        full_name="Test User",
        addressing_style="sen",
        mode="auto",
    )
    db_mock.list_contacts.return_value = [mock_contact]

    btn_contacts_fn = None
    for h in service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "handle_btn_contacts":
            btn_contacts_fn = h.callback
            break
    assert btn_contacts_fn is not None

    mock_msg = MagicMock()
    mock_msg.from_user.id = owner_id
    mock_msg.reply = AsyncMock()

    await btn_contacts_fn(mock_msg)
    assert mock_msg.reply.called

    markup = mock_msg.reply.call_args.kwargs.get("reply_markup")
    assert markup is not None
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    texts = [btn.text for row in markup.inline_keyboard for btn in row]

    # Verify "✍️ Kontaktga xabar yozish" button is present and links to crm:outreach_menu
    assert "crm:outreach_menu" in callbacks
    assert any("xabar yozish" in t.lower() for t in texts)
    assert any("yangi kontakt" in t.lower() for t in texts)


@pytest.mark.asyncio
async def test_unified_contact_card_has_outreach_button():
    db_mock = AsyncMock()
    ai_mock = AsyncMock()
    sched_mock = AsyncMock()
    owner_id = 12345

    service = AdminBotService(
        db_manager=db_mock,
        ai_engine=ai_mock,
        scheduler_service=sched_mock,
        token="1234567890:dummy",
        admin_id=owner_id,
    )

    mock_contact = Contact(
        user_id=999,
        username="SuxrobOmonov12",
        full_name="Suxrob Omonov",
        addressing_style="siz",
        mode="auto",
    )
    db_mock.get_contact_by_id.return_value = mock_contact
    db_mock.get_mem_contact.return_value = None
    db_mock.get_mem_stats.return_value = {"messages": 0, "facts": 0, "embeddings": 0}
    db_mock.get_latest_mem_summary.return_value = None
    db_mock.get_mem_open_items.return_value = []

    crm_view_fn = None
    for h in service.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "handle_crm_view":
            crm_view_fn = h.callback
            break
    assert crm_view_fn is not None

    mock_cb = MagicMock()
    mock_cb.from_user.id = owner_id
    mock_cb.data = "crm_view:999"
    mock_cb.answer = AsyncMock()
    mock_cb.message = MagicMock()
    mock_cb.message.edit_text = AsyncMock()

    await crm_view_fn(mock_cb)
    assert mock_cb.message.edit_text.called

    markup = mock_cb.message.edit_text.call_args.kwargs.get("reply_markup")
    assert markup is not None
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    texts = [btn.text for row in markup.inline_keyboard for btn in row]

    # Verify direct outreach button exists for contact 999
    assert "crm_outreach_direct:999" in callbacks
    assert any("xabar yozish" in t.lower() for t in texts)
