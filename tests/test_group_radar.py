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
