"""
Comprehensive Test Suite for Group Radar (Shaxsiy Guruh Kuzatuvchisi).
Tests detection logic, Uzbek name regex, custom keywords, jump links,
mute/unmute controls, debounce sliding window, and Admin Cockpit integration.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from typing import Optional

from services.group_radar import (
    GroupRadarService,
    generate_jump_link,
    DEFAULT_NAME_REGEX,
)
from database.models import RadarMention
from database.crud import DatabaseManager


# ------------------ TEST 1: Jump Link Generation ------------------
def test_generate_jump_link():
    # Public group with username
    assert generate_jump_link("my_super_group", -1001234567890, 42) == "https://t.me/my_super_group/42"
    assert generate_jump_link("@my_super_group", -1001234567890, 42) == "https://t.me/my_super_group/42"

    # Private supergroup (-100 prefix)
    assert generate_jump_link(None, -1001987654321, 105) == "https://t.me/c/1987654321/105"

    # Regular group (negative without 100)
    assert generate_jump_link(None, -5544332211, 200) == "https://t.me/c/5544332211/200"


# ------------------ TEST 2: Uzbek Name and Keyword Regex Matching ------------------
def test_name_and_keyword_regex_matching():
    radar = GroupRadarService(db_manager=MagicMock())

    # Direct name variations
    m1 = radar.check_text_trigger("Salom Mansur, bugun uchrashuv bormi?", [])
    assert m1 is not None
    assert m1["trigger_type"] == "name_keyword"
    assert m1["keyword"].lower() == "mansur"

    m2 = radar.check_text_trigger("Mansurjon, faylni ko'rib chiqing", [])
    assert m2 is not None
    assert m2["trigger_type"] == "name_keyword"

    # Uzbek case declensions (-ga, -ning, -ni, -dan, -da)
    m3 = radar.check_text_trigger("Mansurga aytib qo'ying", [])
    assert m3 is not None
    assert m3["trigger_type"] == "name_keyword"

    m4 = radar.check_text_trigger("Mansurning noutbuki qayerda?", [])
    assert m4 is not None

    m5 = radar.check_text_trigger("Mansurni qidirishyapti", [])
    assert m5 is not None

    m6 = radar.check_text_trigger("Mansurdan so'rab ber", [])
    assert m6 is not None

    # Surnames
    m7 = radar.check_text_trigger("Radjabov bugun keladimi?", [])
    assert m7 is not None
    assert m7["trigger_type"] == "name_keyword"

    m8 = radar.check_text_trigger("Rajabovga jo'natildi", [])
    assert m8 is not None

    # Telegram handle
    m9 = radar.check_text_trigger("Iltimos @mansurjon_07 ga xabar bering", [])
    assert m9 is not None

    # Custom keywords
    m10 = radar.check_text_trigger("Yangi startup loyihasi bo'yicha yig'ilish", ["startup"])
    assert m10 is not None
    assert m10["trigger_type"] == "custom_keyword"
    assert m10["keyword"] == "startup"

    # Unrelated neutral text
    m_none = radar.check_text_trigger("Bugun ob-havo ancha iliq bo'ladi", ["server", "depozit"])
    assert m_none is None


# ------------------ TEST 3: Telethon Event Inspection ------------------
@pytest.mark.asyncio
async def test_event_inspection_filters_and_triggers():
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.list_radar_muted_groups = AsyncMock(return_value=[])
    mock_db.is_radar_group_muted = AsyncMock(return_value=False)
    mock_db.list_radar_keywords = AsyncMock(return_value=["muhim_loyixa"])

    radar = GroupRadarService(db_manager=mock_db)
    mock_client = AsyncMock()

    # Case A: Private message (not a group) -> should be ignored
    event_private = MagicMock()
    event_private.is_group = False
    assert await radar.inspect_event(event_private, mock_client) is None

    # Case B: Mansur's own outgoing message -> should be ignored
    event_out = MagicMock()
    event_out.is_group = True
    event_out.out = True
    assert await radar.inspect_event(event_out, mock_client) is None

    # Case C: Service message (action is not None) -> should be ignored
    event_action = MagicMock()
    event_action.is_group = True
    event_action.out = False
    event_action.message.action = MagicMock()
    assert await radar.inspect_event(event_action, mock_client) is None

    # Case D: Sender is a bot -> should be ignored
    event_bot = MagicMock()
    event_bot.is_group = True
    event_bot.out = False
    event_bot.message.action = None
    bot_sender = MagicMock()
    bot_sender.bot = True
    bot_sender.is_self = False
    event_bot.get_sender = AsyncMock(return_value=bot_sender)
    assert await radar.inspect_event(event_bot, mock_client) is None

    # Case E: Valid incoming message with Direct @Mention
    event_mention = MagicMock()
    event_mention.is_group = True
    event_mention.out = False
    event_mention.message.action = None
    event_mention.mentioned = True
    event_mention.is_reply = False
    event_mention.raw_text = "Salom hamma!"
    event_mention.id = 991
    event_mention.chat_id = -10011223344

    chat_mock = MagicMock()
    chat_mock.id = -10011223344
    chat_mock.title = "Dasturchilar Guruhi"
    chat_mock.username = "devs_uz"
    event_mention.get_chat = AsyncMock(return_value=chat_mock)

    user_sender = MagicMock()
    user_sender.id = 777
    user_sender.bot = False
    user_sender.is_self = False
    user_sender.first_name = "Akmal"
    user_sender.last_name = "Karimov"
    user_sender.username = "akmalk"
    event_mention.get_sender = AsyncMock(return_value=user_sender)

    detected = await radar.inspect_event(event_mention, mock_client)
    assert detected is not None
    assert detected["group_id"] == -10011223344
    assert detected["group_title"] == "Dasturchilar Guruhi"
    assert detected["trigger_type"] == "direct_mention"
    assert detected["jump_link"] == "https://t.me/devs_uz/991"

    # Case F: Reply to owner's message
    event_reply = MagicMock()
    event_reply.is_group = True
    event_reply.out = False
    event_reply.message.action = None
    event_reply.mentioned = False
    event_reply.is_reply = True
    event_reply.raw_text = "Rahmat katta!"
    event_reply.id = 992
    event_reply.chat_id = -10011223344
    event_reply.get_chat = AsyncMock(return_value=chat_mock)
    event_reply.get_sender = AsyncMock(return_value=user_sender)

    reply_msg = MagicMock()
    reply_msg.out = True  # sent by Mansur
    event_reply.get_reply_message = AsyncMock(return_value=reply_msg)
    me_mock = MagicMock(id=123456)
    mock_client.get_me = AsyncMock(return_value=me_mock)

    detected_reply = await radar.inspect_event(event_reply, mock_client)
    assert detected_reply is not None
    assert detected_reply["trigger_type"] == "reply_to_owner"

    # Case G: Custom keyword match
    event_kw = MagicMock()
    event_kw.is_group = True
    event_kw.out = False
    event_kw.message.action = None
    event_kw.mentioned = False
    event_kw.is_reply = False
    event_kw.raw_text = "Bizga muhim_loyixa bo'yicha hisobot kerak"
    event_kw.id = 993
    event_kw.chat_id = -10011223344
    event_kw.get_chat = AsyncMock(return_value=chat_mock)
    event_kw.get_sender = AsyncMock(return_value=user_sender)

    detected_kw = await radar.inspect_event(event_kw, mock_client)
    assert detected_kw is not None
    assert detected_kw["trigger_type"] == "custom_keyword"


# ------------------ TEST 4: Mute and Unmute Controls ------------------
@pytest.mark.asyncio
async def test_radar_mute_and_unmute():
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.list_radar_muted_groups = AsyncMock(return_value=[])
    mock_db.is_radar_group_muted = AsyncMock(return_value=False)
    mock_db.mute_radar_group = AsyncMock(return_value=True)
    mock_db.unmute_radar_group = AsyncMock(return_value=True)

    radar = GroupRadarService(db_manager=mock_db)

    group_id = -10099887766
    assert await radar.is_group_muted(group_id) is False

    # Mute group
    await radar.mute_group(group_id, title="Shovqinli Guruh")
    mock_db.mute_radar_group.assert_called_with(group_id, "Shovqinli Guruh")
    assert await radar.is_group_muted(group_id) is True

    # Unmute group
    await radar.unmute_group(group_id)
    mock_db.unmute_radar_group.assert_called_with(group_id)
    assert await radar.is_group_muted(group_id) is False


# ------------------ TEST 5: Debounce Sliding Window ------------------
@pytest.mark.asyncio
async def test_radar_debounce_sliding_window():
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.list_radar_muted_groups = AsyncMock(return_value=[])
    mock_db.is_radar_group_muted = AsyncMock(return_value=False)
    mock_db.list_radar_keywords = AsyncMock(return_value=[])
    mock_db.log_radar_mention = AsyncMock(return_value=1)

    radar = GroupRadarService(db_manager=mock_db)
    radar.debounce_seconds = 60

    # Callback tracking
    alerts_dispatched = []

    async def mock_alert_cb(detected, existing_alert_msg_id=None):
        alerts_dispatched.append({
            "detected": detected,
            "existing_id": existing_alert_msg_id,
        })
        return 77701  # Mock sent message ID in Admin Bot

    radar.set_alert_callback(mock_alert_cb)

    # Event helper
    def make_event(msg_id: int, text: str):
        ev = MagicMock()
        ev.is_group = True
        ev.out = False
        ev.message.action = None
        ev.mentioned = False
        ev.is_reply = False
        ev.raw_text = text
        ev.id = msg_id
        ev.chat_id = -100555
        chat = MagicMock(id=-100555, title="Test Guruh", username=None)
        ev.get_chat = AsyncMock(return_value=chat)
        user = MagicMock(id=888, bot=False, is_self=False, first_name="Ali", last_name="", username="ali88")
        ev.get_sender = AsyncMock(return_value=user)
        return ev

    mock_client = AsyncMock()

    # First mention: should trigger fresh alert (existing_alert_msg_id=None)
    ev1 = make_event(101, "Mansur aka salom")
    await radar.handle_group_message(ev1, mock_client)

    assert len(alerts_dispatched) == 1
    assert alerts_dispatched[0]["existing_id"] is None
    assert alerts_dispatched[0]["detected"]["mentions_count"] == 1

    # Second mention immediately after in same group: should reuse existing alert msg id
    ev2 = make_event(102, "Mansur aka qayerdasiz?")
    await radar.handle_group_message(ev2, mock_client)

    assert len(alerts_dispatched) == 2
    assert alerts_dispatched[1]["existing_id"] == 77701
    assert alerts_dispatched[1]["detected"]["mentions_count"] == 2


# ------------------ TEST 6: SQLite CRUD Integration ------------------
@pytest.mark.asyncio
async def test_database_radar_crud(tmp_path):
    from database.connection import init_db

    test_db = tmp_path / "test_radar.sqlite"
    await init_db(test_db)
    db = DatabaseManager(test_db)

    # 1. Keywords
    await db.add_radar_keyword("server")
    await db.add_radar_keyword("loyixa")
    kws = await db.list_radar_keywords()
    assert "server" in kws
    assert "loyixa" in kws

    await db.remove_radar_keyword("server")
    kws2 = await db.list_radar_keywords()
    assert "server" not in kws2
    assert "loyixa" in kws2

    # 2. Muted groups
    assert await db.is_radar_group_muted(-10099) is False
    await db.mute_radar_group(-10099, "IT Guruh")
    assert await db.is_radar_group_muted(-10099) is True

    muted = await db.list_radar_muted_groups()
    assert len(muted) == 1
    assert muted[0]["group_id"] == -10099
    assert muted[0]["title"] == "IT Guruh"

    await db.unmute_radar_group(-10099)
    assert await db.is_radar_group_muted(-10099) is False

    # 3. Log mention
    m_id = await db.log_radar_mention(
        group_id=-10055,
        group_title="Startuplar",
        message_id=456,
        sender_id=123,
        sender_name="Jasur",
        sender_username="jasur_dev",
        message_text="Mansurjon keldi",
        trigger_type="name_keyword",
    )
    assert m_id > 0

    recent = await db.get_recent_radar_mentions(limit=5)
    assert len(recent) == 1
    assert recent[0].group_id == -10055
    assert recent[0].sender_name == "Jasur"
    assert recent[0].trigger_type == "name_keyword"


# ------------------ TEST 7: Userbot reply_to_group_message ------------------
@pytest.mark.asyncio
async def test_userbot_reply_to_group_message():
    from bot.userbot import UserbotService

    mock_db = MagicMock(spec=DatabaseManager)
    mock_ai = MagicMock()
    mock_limiter = MagicMock()
    mock_limiter.calculate_typing_duration.return_value = 0.01
    mock_digest = MagicMock()

    userbot = UserbotService(
        db_manager=mock_db,
        ai_engine=mock_ai,
        rate_limiter=mock_limiter,
        digest_service=mock_digest,
    )

    mock_client = AsyncMock()
    mock_entity = MagicMock(id=-100555)
    mock_client.get_input_entity = AsyncMock(return_value=mock_entity)
    mock_client.get_entity = AsyncMock(return_value=mock_entity)
    mock_client.action = MagicMock()
    mock_client.send_message = AsyncMock(return_value=MagicMock(id=999))
    userbot.client = mock_client

    sent = await userbot.reply_to_group_message(
        group_id=-100555,
        message_id=42,
        reply_text="Assalomu alaykum, eshitaman!",
    )

    assert sent is not None
    mock_client.send_message.assert_called_with(mock_entity, "Assalomu alaykum, eshitaman!", reply_to=42)


# ------------------ TEST 8: AI Analyze Radar Mention ------------------
@pytest.mark.asyncio
async def test_ai_analyze_radar_mention():
    from services.ai_engine import AIEngine
    ai = AIEngine()

    # Case 1: False positive (Abu Mansur Moturidiy) -> should return is_relevant=False
    res_false = await ai.analyze_radar_mention(
        group_title="Diniy Ma'rifat",
        sender_name="Said",
        message_text="Abu Mansur Moturidiy asarlarini o'rganish kerak",
        trigger_type="name_keyword",
    )
    assert res_false["is_relevant"] is False

    # Case 2: Technical urgent issue -> should return urgency='urgent'
    res_urgent = await ai.analyze_radar_mention(
        group_title="DevOps Uz",
        sender_name="Bobur",
        message_text="Mansur aka server tushdi 500 error beryapti tezda qarang",
        trigger_type="name_keyword",
    )
    assert res_urgent["is_relevant"] is True
    assert res_urgent["urgency"] == "urgent"
    assert "🚨" in res_urgent["urgency_label"]
    assert len(res_urgent["suggested_reply"]) > 5

    # Case 3: Question / Inquiry -> should return urgency='question'
    res_q = await ai.analyze_radar_mention(
        group_title="Startuplar",
        sender_name="Nodir",
        message_text="Mansur aka loyihani yangi versiyasi qachon chiqadi?",
        trigger_type="name_keyword",
    )
    assert res_q["is_relevant"] is True
    assert res_q["urgency"] == "question"
    assert "❓" in res_q["urgency_label"]


# ------------------ TEST 9: Radar 1-Tap Quick Reply Callback ------------------
@pytest.mark.asyncio
async def test_radar_quick_reply_callback():
    from datetime import datetime
    from aiogram import Bot
    from aiogram.types import CallbackQuery, User, Message, Chat, Update
    from bot.admin_bot import AdminBotService

    mock_db = MagicMock(spec=DatabaseManager)
    mock_ai = MagicMock()
    mock_sched = MagicMock()

    admin_bot = AdminBotService(
        db_manager=mock_db,
        ai_engine=mock_ai,
        scheduler_service=mock_sched,
        admin_id=12345,
    )

    mock_userbot = MagicMock()
    mock_userbot.reply_to_group_message = AsyncMock(return_value=True)
    admin_bot.set_userbot(mock_userbot)

    admin_bot._pending_radar_quick_replies["-100555:42"] = "Hozir qarayman, kutib turing!"

    radar_send_handler = None
    for h in admin_bot.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "on_radar_quick_send":
            radar_send_handler = h.callback
            break
    assert radar_send_handler is not None

    fake_cb = MagicMock()
    fake_cb.from_user.id = 12345
    fake_cb.data = "radar_quick:-100555:42"
    fake_cb.answer = AsyncMock()
    fake_cb.message = MagicMock()
    fake_cb.message.html_text = "🎯 Guruhda siz haqingizda gaplashildi!"
    fake_cb.message.text = "🎯 Guruhda siz haqingizda gaplashildi!"
    fake_cb.message.edit_text = AsyncMock()

    await radar_send_handler(fake_cb)

    mock_userbot.reply_to_group_message.assert_called_with(-100555, 42, "Hozir qarayman, kutib turing!")
    fake_cb.message.edit_text.assert_called()


# ------------------ TEST 10: Radar Natural Language Commands ------------------
@pytest.mark.asyncio
async def test_radar_natural_language_commands():
    from datetime import datetime
    from aiogram import Bot
    from aiogram.types import Message, User, Chat, Update
    from bot.admin_bot import AdminBotService
    from config import settings

    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.list_contacts = AsyncMock(return_value=[])
    mock_db.add_radar_keyword = AsyncMock(return_value=True)
    mock_db.remove_radar_keyword = AsyncMock(return_value=True)
    mock_db.list_radar_keywords = AsyncMock(return_value=["server"])
    mock_db.list_radar_muted_groups = AsyncMock(return_value=[])
    mock_db.get_recent_radar_mentions = AsyncMock(return_value=[])

    admin_bot = AdminBotService(
        db_manager=mock_db,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        admin_id=12345,
    )

    fake_user = User(id=12345, is_bot=False, first_name="Mansur")
    fake_chat = Chat(id=12345, type="private")

    def make_update(text: str, update_id: int):
        fake_msg = Message(
            message_id=990 + update_id,
            date=datetime.now(),
            chat=fake_chat,
            from_user=fake_user,
            text=text,
        )
        return Update(update_id=update_id, message=fake_msg)

    mock_bot = AsyncMock(spec=Bot)
    mock_bot.send_message = AsyncMock()

    # Command: "radarga depozit so'zini qo'sh"
    up1 = make_update("radarga depozit so'zini qo'sh", 1)
    await admin_bot.dp.feed_update(mock_bot, up1)
    mock_db.add_radar_keyword.assert_called_with("depozit")

    # Command: "radardan server so'zini o'chir"
    up2 = make_update("radardan server so'zini o'chir", 2)
    await admin_bot.dp.feed_update(mock_bot, up2)
    mock_db.remove_radar_keyword.assert_called_with("server")

    # Command: "radarni o'chir"
    up3 = make_update("radarni o'chir", 3)
    await admin_bot.dp.feed_update(mock_bot, up3)
    assert getattr(settings, "RADAR_ENABLED") is False

    # Command: "radarni yoq"
    up4 = make_update("radarni yoq", 4)
    await admin_bot.dp.feed_update(mock_bot, up4)
    assert getattr(settings, "RADAR_ENABLED") is True


# ------------------ TEST 11: Idempotency & Cache Eviction ------------------
@pytest.mark.asyncio
async def test_radar_quick_reply_idempotency_and_cache_eviction():
    from bot.admin_bot import AdminBotService

    mock_db = MagicMock(spec=DatabaseManager)
    admin_bot = AdminBotService(
        db_manager=mock_db,
        ai_engine=MagicMock(),
        scheduler_service=MagicMock(),
        admin_id=12345,
    )

    mock_userbot = MagicMock()
    mock_userbot.reply_to_group_message = AsyncMock(return_value=True)
    admin_bot.set_userbot(mock_userbot)

    admin_bot._pending_radar_quick_replies["-100999:11"] = "Salom, hozir ko'rib chiqyapman!"

    radar_send_handler = None
    for h in admin_bot.dp.callback_query.handlers:
        if getattr(h.callback, "__name__", "") == "on_radar_quick_send":
            radar_send_handler = h.callback
            break
    assert radar_send_handler is not None

    fake_cb = MagicMock()
    fake_cb.from_user.id = 12345
    fake_cb.data = "radar_quick:-100999:11"
    fake_cb.answer = AsyncMock()
    fake_cb.message = MagicMock()
    fake_cb.message.html_text = "Test radar alert"
    fake_cb.message.edit_text = AsyncMock()

    # 1. First click: dispatches successfully and clears cache
    await radar_send_handler(fake_cb)
    mock_userbot.reply_to_group_message.assert_called_with(-100999, 11, "Salom, hozir ko'rib chiqyapman!")
    assert "-100999:11" not in admin_bot._pending_radar_quick_replies

    # 2. Second rapid click (double-click simulation): blocked safely without duplicate dispatch
    mock_userbot.reply_to_group_message.reset_mock()
    await radar_send_handler(fake_cb)
    mock_userbot.reply_to_group_message.assert_not_called()
    assert "allaqachon yuborilgan" in fake_cb.answer.call_args[0][0]

    # 3. Test LRU/FIFO eviction cap at 200 items in send_radar_alert
    admin_bot.bot = AsyncMock()
    for i in range(205):
        await admin_bot.send_radar_alert({
            "group_id": -1000 - i,
            "message_id": 100 + i,
            "group_title": f"Group {i}",
            "sender_name": "User",
            "message_text": "Hello",
            "suggested_reply": f"Reply {i}",
        })
    assert len(admin_bot._pending_radar_quick_replies) == 200


