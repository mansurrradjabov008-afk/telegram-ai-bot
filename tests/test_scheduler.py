"""
Unit and integration tests for Scheduler, Schedule Parser, and Schedule Wizard.
"""

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
import pytest_asyncio
import pytz

from database.connection import init_db
from database.crud import DatabaseManager
from database.models import ScheduledTask
from services.scheduler_service import SchedulerService
from services.schedule_parser import (
    parse_deterministic_schedule,
    parse_schedule_intent_hybrid,
    clean_target_peer,
    clean_message_text,
    ScheduleParseResult,
)
from bot.admin_bot import AdminBotService, ScheduleWizardState
from services.ai_engine import AIEngine


# ============================================================================
# 1. CORE APSCHEDULER EXECUTION FLOW TEST
# ============================================================================

@pytest_asyncio.fixture
async def scheduler_fixture(tmp_path: Path):
    db_file = tmp_path / "test_scheduler.db"
    await init_db(db_file)
    manager = DatabaseManager(db_path=db_file)
    service = SchedulerService(manager)
    service.start()
    yield service, manager
    service.stop()


@pytest.mark.asyncio
async def test_scheduler_execution_flow(scheduler_fixture):
    service, manager = scheduler_fixture

    executed_messages = []
    notified_boss = []

    async def fake_send(target, text):
        executed_messages.append((target, text))

    async def fake_notify(text):
        notified_boss.append(text)

    service.register_callbacks(send_callback=fake_send, notify_boss_callback=fake_notify)

    tz = pytz.timezone("Asia/Tashkent")
    # Schedule 1 second in future
    run_at = datetime.now(tz) + timedelta(seconds=1)

    task = await service.schedule_message(
        target_peer="@friend",
        message_text="Test xabar",
        run_at=run_at,
    )

    assert task.status == "pending"

    # Wait for job to trigger
    await asyncio.sleep(2.0)

    assert len(executed_messages) == 1
    assert executed_messages[0] == ("@friend", "Test xabar")
    assert len(notified_boss) == 1
    assert "muvaffaqiyatli yuborildi" in notified_boss[0]

    # Verify status in database
    pending = await manager.get_pending_tasks()
    assert len(pending) == 0


# ============================================================================
# 2. DETERMINISTIC PARSER TESTS (Exact user formats, relative, dates)
# ============================================================================

def test_deterministic_schedule_parser_exact_user_prompt():
    tz = "Asia/Tashkent"

    # User screenshot exact phrase: /schedule @sah_60 22:07 menga yoz biron nima deb
    r1 = parse_deterministic_schedule("/schedule @sah_60 22:07 menga yoz biron nima deb", tz_name=tz)
    assert r1 is not None
    assert r1.target_peer == "@sah_60"
    assert r1.run_at.hour == 22
    assert r1.run_at.minute == 7
    assert r1.message_text == "menga yoz biron nima"
    assert "22:07" in r1.human_time

    # Without leading '@' in username
    r2 = parse_deterministic_schedule("sah_60 22:07 menga yoz biron nima deb", tz_name=tz)
    assert r2 is not None
    assert r2.target_peer == "@sah_60"
    assert r2.run_at.hour == 22
    assert r2.run_at.minute == 7
    assert r2.message_text == "menga yoz biron nima"

    # Time with suffix 'da': @sah_60 22:07da
    r3 = parse_deterministic_schedule("@sah_60 22:07da menga yoz", tz_name=tz)
    assert r3 is not None
    assert r3.target_peer == "@sah_60"
    assert r3.run_at.hour == 22
    assert r3.run_at.minute == 7
    assert r3.message_text == "menga yoz"

    # Time with particle 'da ': @sah_60 22:07 da menga yoz
    r4 = parse_deterministic_schedule("@sah_60 22:07 da menga yoz", tz_name=tz)
    assert r4 is not None
    assert r4.target_peer == "@sah_60"
    assert r4.run_at.hour == 22
    assert r4.run_at.minute == 7
    assert r4.message_text == "menga yoz"

    # Time first: 22:07 @sah_60 menga yoz
    r5 = parse_deterministic_schedule("22:07 @sah_60 menga yoz", tz_name=tz)
    assert r5 is not None
    assert r5.target_peer == "@sah_60"
    assert r5.run_at.hour == 22
    assert r5.run_at.minute == 7
    assert r5.message_text == "menga yoz"

    # Relative minutes: @sah_60 15 daqiqadan keyin loyiha tayyor
    r6 = parse_deterministic_schedule("@sah_60 15 daqiqadan keyin loyiha tayyor", tz_name=tz)
    assert r6 is not None
    assert r6.target_peer == "@sah_60"
    assert r6.message_text == "loyiha tayyor"
    assert "15 daqiqadan" in r6.human_time

    # Relative minutes inverse: 15 daqiqadan keyin @sah_60 ga loyiha tayyor
    r7 = parse_deterministic_schedule("15 daqiqadan keyin @sah_60 ga loyiha tayyor", tz_name=tz)
    assert r7 is not None
    assert r7.target_peer == "@sah_60"
    assert r7.message_text == "loyiha tayyor"
    assert "15 daqiqadan" in r7.human_time

    # Explicit date: @sah_60 2026-10-09 14:30 uchrashuv
    r8 = parse_deterministic_schedule("@sah_60 2026-10-09 14:30 uchrashuv", tz_name=tz)
    assert r8 is not None
    assert r8.target_peer == "@sah_60"
    assert r8.run_at.year == 2026
    assert r8.run_at.month == 10
    assert r8.run_at.day == 9
    assert r8.run_at.hour == 14
    assert r8.run_at.minute == 30
    assert r8.message_text == "uchrashuv"

    # Uzbek natural keywords: ertaga soat 10:00 da @sah_60 ga salom
    r9 = parse_deterministic_schedule("ertaga soat 10:00 da @sah_60 ga salom", tz_name=tz)
    assert r9 is not None
    assert r9.target_peer == "@sah_60"
    assert r9.run_at.hour == 10
    assert r9.run_at.minute == 0
    assert "Ertaga" in r9.human_time


def test_clean_message_text_helpers():
    # Quotes stripping
    assert clean_message_text('"salom do\'stim"') == "salom do'stim"
    assert clean_message_text("'hujjat tayyor'") == "hujjat tayyor"

    # Uzbek and Russian dispatch suffixes
    assert clean_message_text("menga tezda yoz deb yoz") == "menga tezda yoz"
    assert clean_message_text("loyiha topshirildi deb jo'nat") == "loyiha topshirildi"
    assert clean_message_text("yig'ilish soat 5 da deb") == "yig'ilish soat 5 da"
    assert clean_message_text("встреча завтра напиши") == "встреча завтра"

    # Leading particles stripping
    assert clean_message_text("da loyihani yubor") == "loyihani yubor"
    assert clean_message_text("ga salom ayt") == "salom ayt"


# ============================================================================
# 3. HYBRID PARSER TESTS (Fast path vs. AI semantic fallback)
# ============================================================================

@pytest.mark.asyncio
async def test_hybrid_parser_fastpath_and_fallback():
    mock_ai = MagicMock(spec=AIEngine)
    mock_ai.parse_schedule_intent = AsyncMock()

    # Fastpath should NOT call AI engine
    res = await parse_schedule_intent_hybrid("/schedule @sah_60 22:07 salom", mock_ai)
    assert res is not None
    assert res.target_peer == "@sah_60"
    mock_ai.parse_schedule_intent.assert_not_called()

    # Complex natural language calls AI engine fallback
    mock_ai.parse_schedule_intent.return_value = {
        "is_schedule": True,
        "target_peer": "@hamkor",
        "scheduled_iso": "2026-10-15T09:00:00+05:00",
        "message_text": "Loyiha bo'yicha hisobot",
        "human_time": "15-Oktabr soat 09:00 da",
    }
    res_ai = await parse_schedule_intent_hybrid("Keyingi payshanba ertalab @hamkor ga hisobot jo'natish kerak", mock_ai)
    assert res_ai is not None
    assert res_ai.target_peer == "@hamkor"
    assert res_ai.message_text == "Loyiha bo'yicha hisobot"
    mock_ai.parse_schedule_intent.assert_called_once()


# ============================================================================
# 4. BOT /schedule & WIZARD INTEGRATION TESTS
# ============================================================================

def make_mock_fsm():
    """Helper to mock aiogram FSMContext."""
    storage = {}
    fsm = MagicMock()

    async def fake_set_state(s):
        storage["state"] = s

    async def fake_get_state():
        return storage.get("state")

    async def fake_update_data(**kwargs):
        storage.setdefault("data", {}).update(kwargs)

    async def fake_get_data():
        return storage.get("data", {})

    async def fake_clear():
        storage.clear()

    fsm.set_state = AsyncMock(side_effect=fake_set_state)
    fsm.get_state = AsyncMock(side_effect=fake_get_state)
    fsm.update_data = AsyncMock(side_effect=fake_update_data)
    fsm.get_data = AsyncMock(side_effect=fake_get_data)
    fsm.clear = AsyncMock(side_effect=fake_clear)
    return fsm, storage


@pytest_asyncio.fixture
async def bot_service(tmp_path: Path):
    db_file = tmp_path / "test_bot.db"
    await init_db(db_file)
    manager = DatabaseManager(db_path=db_file)

    mock_ai = MagicMock(spec=AIEngine)
    mock_ai.parse_crm_intent = AsyncMock(return_value=None)
    mock_ai.parse_schedule_intent = AsyncMock(return_value=None)

    mock_scheduler = MagicMock(spec=SchedulerService)
    # Return fake ScheduledTask
    fake_task = ScheduledTask(
        id=77,
        target_peer="@sah_60",
        message_text="Test xabar",
        scheduled_time="2026-10-09T22:07:00+05:00",
        status="pending",
    )
    mock_scheduler.schedule_message = AsyncMock(return_value=fake_task)

    service = AdminBotService(
        db_manager=manager,
        ai_engine=mock_ai,
        scheduler_service=mock_scheduler,
        token="1234567890:mock_token",
        admin_id=7992504344,
    )
    return service, mock_scheduler, manager


@pytest.mark.asyncio
async def test_cmd_schedule_empty_shows_hub(bot_service):
    service, mock_sched, manager = bot_service

    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 7992504344
    msg.text = "/schedule"
    msg.reply = AsyncMock()

    # Find cmd_schedule by name
    handler = next((h.callback for h in service.dp.message.handlers if h.callback.__name__ == "cmd_schedule"), None)
    assert handler is not None
    await handler(msg)

    # Check response text
    msg.reply.assert_called_once()
    reply_text = msg.reply.call_args[0][0]
    assert "VAZIFALARNI REJALASHTIRISH MARKAZI" in reply_text
    # Verify buttons
    markup = msg.reply.call_args[1].get("reply_markup")
    assert markup is not None
    buttons = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "✍️ Yangi vazifa rejalashtirish" in buttons
    assert "⏳ Kutilayotgan vazifalar" in buttons


@pytest.mark.asyncio
async def test_cmd_schedule_with_arguments_schedules_immediately(bot_service):
    service, mock_sched, manager = bot_service

    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 7992504344
    msg.text = "/schedule @sah_60 22:07 menga yoz biron nima deb"
    msg.reply = AsyncMock()

    # Find cmd_schedule by name
    handler = next((h.callback for h in service.dp.message.handlers if h.callback.__name__ == "cmd_schedule"), None)
    assert handler is not None
    await handler(msg)

    # Scheduler service must have been called
    mock_sched.schedule_message.assert_called_once()
    call_kwargs = mock_sched.schedule_message.call_args[1]
    assert call_kwargs["target_peer"] == "@sah_60"
    assert call_kwargs["message_text"] == "menga yoz biron nima"

    # Reply contains confirmation with task ID
    msg.reply.assert_called_once()
    reply_text = msg.reply.call_args[0][0]
    assert "VAZIFA MUVAFFAQIYATLI REJALASHTIRILDI" in reply_text
    assert "#77" in reply_text


@pytest.mark.asyncio
async def test_schedule_wizard_full_flow(bot_service):
    service, mock_sched, manager = bot_service
    fsm, storage = make_mock_fsm()

    # Find wizard handlers by name
    cb_start_handler = next(
        (h.callback for h in service.dp.callback_query.handlers if h.callback.__name__ == "handle_schedule_wizard_start"),
        None,
    )
    assert cb_start_handler is not None

    # Step 0: Start Wizard Callback
    cb = MagicMock()
    cb.from_user = MagicMock()
    cb.from_user.id = 7992504344
    cb.data = "schedule:wizard"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    await cb_start_handler(cb, fsm)
    assert storage.get("state") == ScheduleWizardState.waiting_for_peer
    cb.message.edit_text.assert_called_once()
    assert "1/3" in cb.message.edit_text.call_args[0][0]

    # Find State handlers by name
    peer_handler = next((h.callback for h in service.dp.message.handlers if h.callback.__name__ == "process_wizard_peer"), None)
    time_handler = next((h.callback for h in service.dp.message.handlers if h.callback.__name__ == "process_wizard_time"), None)
    text_handler = next((h.callback for h in service.dp.message.handlers if h.callback.__name__ == "process_wizard_text"), None)

    assert peer_handler is not None
    assert time_handler is not None
    assert text_handler is not None

    # Step 1: Send Peer
    msg1 = MagicMock()
    msg1.from_user = MagicMock()
    msg1.from_user.id = 7992504344
    msg1.text = "@sah_60"
    msg1.reply = AsyncMock()

    await peer_handler(msg1, fsm)
    assert storage["data"]["target_peer"] == "@sah_60"
    assert storage.get("state") == ScheduleWizardState.waiting_for_time
    assert "2/3" in msg1.reply.call_args[0][0]

    # Step 2: Send Time
    msg2 = MagicMock()
    msg2.from_user = MagicMock()
    msg2.from_user.id = 7992504344
    msg2.text = "22:07"
    msg2.reply = AsyncMock()

    await time_handler(msg2, fsm)
    assert "run_at_iso" in storage["data"]
    assert storage.get("state") == ScheduleWizardState.waiting_for_text
    assert "3/3" in msg2.reply.call_args[0][0]

    # Step 3: Send Text
    msg3 = MagicMock()
    msg3.from_user = MagicMock()
    msg3.from_user.id = 7992504344
    msg3.text = "Salom do'stim, uchrashuv belgilandi!"
    msg3.reply = AsyncMock()

    await text_handler(msg3, fsm)
    # State should be cleared
    assert storage.get("state") is None

    # Scheduler service called
    mock_sched.schedule_message.assert_called_once()
    assert mock_sched.schedule_message.call_args[1]["target_peer"] == "@sah_60"
    assert mock_sched.schedule_message.call_args[1]["message_text"] == "Salom do'stim, uchrashuv belgilandi!"
    assert "VAZIFA MUVAFFAQIYATLI REJALASHTIRILDI" in msg3.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_cancel_aborts_wizard(bot_service):
    service, mock_sched, manager = bot_service
    fsm, storage = make_mock_fsm()

    # Put into wizard state
    await fsm.set_state(ScheduleWizardState.waiting_for_time)

    # Find cancel handler by name
    cancel_handler = next((h.callback for h in service.dp.message.handlers if h.callback.__name__ == "cmd_cancel"), None)
    assert cancel_handler is not None

    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 7992504344
    msg.text = "/cancel"
    msg.reply = AsyncMock()

    await cancel_handler(msg, fsm)
    assert storage.get("state") is None
    msg.reply.assert_called_once()
    assert "bekor qilindi" in msg.reply.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_natural_schedule_routed_and_scheduled(bot_service):
    service, mock_sched, manager = bot_service

    # Find handle_natural_crm_or_schedule
    nat_handler = next(
        (h.callback for h in service.dp.message.handlers if h.callback.__name__ == "handle_natural_crm_or_schedule"),
        None,
    )
    assert nat_handler is not None

    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 7992504344
    msg.text = "@sah_60 22:07 menga yoz biron nima deb"
    msg.reply = AsyncMock()

    await nat_handler(msg)

    # Should have scheduled task
    mock_sched.schedule_message.assert_called_once()
    assert mock_sched.schedule_message.call_args[1]["target_peer"] == "@sah_60"
    assert mock_sched.schedule_message.call_args[1]["message_text"] == "menga yoz biron nima"
    msg.reply.assert_called_once()
    assert "VAZIFA MUVAFFAQIYATLI REJALASHTIRILDI" in msg.reply.call_args[0][0]

