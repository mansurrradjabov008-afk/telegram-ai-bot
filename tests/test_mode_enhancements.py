"""
Unit and integration tests for Autonomy Mode Enhancements:
Flexible argument parsing, Uzbek aliases, interactive inline panels, bulk actions, and multi-strategy contact resolution.
"""

import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from database.connection import init_db
from database.crud import DatabaseManager
from bot.admin_bot import (
    AdminBotService,
    parse_autonomy_mode,
)
from services.ai_engine import AIEngine
from services.scheduler_service import SchedulerService


@pytest_asyncio.fixture
async def test_db(tmp_path: Path):
    db_file = tmp_path / "test_mode_enhancements.db"
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
    service = AdminBotService(
        db_manager=test_db,
        ai_engine=mock_ai_engine,
        scheduler_service=mock_scheduler,
        token="1234567890:mock_token",
        admin_id=7992504344,
    )
    return service


# ============================================================================
# 1. PARSE AUTONOMY MODE TESTS (UZBEK + ENGLISH ALIASES)
# ============================================================================

def test_parse_autonomy_mode():
    # Canonical
    assert parse_autonomy_mode("auto") == "auto"
    assert parse_autonomy_mode("draft") == "draft"
    assert parse_autonomy_mode("mute") == "mute"

    # Case & whitespace
    assert parse_autonomy_mode("  AUTO  ") == "auto"
    assert parse_autonomy_mode("Draft ") == "draft"
    assert parse_autonomy_mode("MUTE") == "mute"

    # Uzbek & colloquial aliases
    assert parse_autonomy_mode("avto") == "auto"
    assert parse_autonomy_mode("avtomatik") == "auto"
    assert parse_autonomy_mode("automatic") == "auto"
    assert parse_autonomy_mode("qoralama") == "draft"
    assert parse_autonomy_mode("tasdiq") == "draft"
    assert parse_autonomy_mode("tekshir") == "draft"
    assert parse_autonomy_mode("ovozsiz") == "mute"
    assert parse_autonomy_mode("ochir") == "mute"
    assert parse_autonomy_mode("o'chir") == "mute"
    assert parse_autonomy_mode("jim") == "mute"
    assert parse_autonomy_mode("ignore") == "mute"

    # Invalid
    assert parse_autonomy_mode("random") is None
    assert parse_autonomy_mode("") is None
    assert parse_autonomy_mode(None) is None


# ============================================================================
# 2. DATABASE MULTI-STRATEGY FIND_CONTACT & UPDATE_ALL TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_find_contact_strategies(test_db: DatabaseManager):
    c1 = await test_db.upsert_contact(user_id=101, username="ali_dev", full_name="Ali Valiyev", mode="draft")
    c2 = await test_db.upsert_contact(user_id=102, username=None, full_name="Vali Toshmatov", mode="auto")

    # By ID
    found = await test_db.find_contact("101")
    assert found is not None
    assert found.user_id == 101

    # By username with @
    found = await test_db.find_contact("@ali_dev")
    assert found is not None
    assert found.user_id == 101

    # By username without @
    found = await test_db.find_contact("ali_dev")
    assert found is not None
    assert found.user_id == 101

    # Case-insensitive username
    found = await test_db.find_contact("ALI_DEV")
    assert found is not None
    assert found.user_id == 101

    # By full_name exact
    found = await test_db.find_contact("Vali Toshmatov")
    assert found is not None
    assert found.user_id == 102

    # By full_name partial
    found = await test_db.find_contact("Vali")
    assert found is not None
    assert found.user_id in (101, 102)

    # Not found
    assert await test_db.find_contact("non_existent_person") is None
    assert await test_db.find_contact("") is None


@pytest.mark.asyncio
async def test_update_all_contacts_mode(test_db: DatabaseManager):
    await test_db.upsert_contact(user_id=201, username="u1", full_name="User 1", mode="draft")
    await test_db.upsert_contact(user_id=202, username="u2", full_name="User 2", mode="auto")
    await test_db.upsert_contact(user_id=203, username="u3", full_name="User 3", mode="draft")

    count = await test_db.update_all_contacts_mode("mute")
    assert count == 3

    counts = await test_db.get_mode_counts()
    assert counts["mute"] == 3
    assert counts["auto"] == 0
    assert counts["draft"] == 0

    with pytest.raises(ValueError):
        await test_db.update_all_contacts_mode("invalid")


# ============================================================================
# 3. /mode COMMAND FLEXIBLE INPUT TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_cmd_mode_no_args_renders_interactive_panel(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344
    await test_db.upsert_contact(user_id=301, username="mansur_07", full_name="Mansur", mode="auto")
    await test_db.upsert_contact(user_id=302, username="umid_dev", full_name="Umid", mode="draft")

    # Find cmd_mode handler
    handler = None
    for h in admin_service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "cmd_mode":
            handler = h.callback
            break
    assert handler is not None

    fake_msg = MagicMock()
    fake_msg.from_user.id = owner_id
    fake_msg.text = "/mode"
    fake_msg.reply_to_message = None
    fake_msg.reply = AsyncMock()

    await handler(fake_msg)

    fake_msg.reply.assert_awaited()
    reply_text = fake_msg.reply.await_args[0][0]
    reply_markup = fake_msg.reply.await_args[1].get("reply_markup")

    assert "AVTONOMIYA REJIMLARI BOSHQARUVI" in reply_text
    assert "@mansur_07" in reply_text
    assert "@umid_dev" in reply_text
    assert reply_markup is not None
    # Check that interactive buttons exist
    button_callbacks = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
    assert any("mode_set:301:auto" in cb for cb in button_callbacks)
    assert any("mode_set:301:draft" in cb for cb in button_callbacks)
    assert any("mode_set:301:mute" in cb for cb in button_callbacks)
    assert any("mode_bulk:auto" in cb for cb in button_callbacks)


@pytest.mark.asyncio
async def test_cmd_mode_flexible_order_and_aliases(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344
    await test_db.upsert_contact(user_id=401, username="alisher", full_name="Alisher", mode="draft")

    handler = None
    for h in admin_service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "cmd_mode":
            handler = h.callback
            break

    fake_msg = MagicMock()
    fake_msg.from_user.id = owner_id
    fake_msg.reply_to_message = None
    fake_msg.reply = AsyncMock()

    # 1. Normal order: /mode @alisher auto
    fake_msg.text = "/mode @alisher auto"
    await handler(fake_msg)
    c = await test_db.get_contact_by_id(401)
    assert c.mode == "auto"

    # 2. Inverted order: /mode mute @alisher
    fake_msg.text = "/mode mute @alisher"
    await handler(fake_msg)
    c = await test_db.get_contact_by_id(401)
    assert c.mode == "mute"

    # 3. Uzbek alias: /mode @alisher qoralama
    fake_msg.text = "/mode @alisher qoralama"
    await handler(fake_msg)
    c = await test_db.get_contact_by_id(401)
    assert c.mode == "draft"

    # 4. Inverted with Uzbek alias: /mode avto @alisher
    fake_msg.text = "/mode avto @alisher"
    await handler(fake_msg)
    c = await test_db.get_contact_by_id(401)
    assert c.mode == "auto"


@pytest.mark.asyncio
async def test_cmd_mode_bulk_command(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344
    await test_db.upsert_contact(user_id=501, username="u1", full_name="U1", mode="auto")
    await test_db.upsert_contact(user_id=502, username="u2", full_name="U2", mode="auto")

    handler = None
    for h in admin_service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "cmd_mode":
            handler = h.callback
            break

    fake_msg = MagicMock()
    fake_msg.from_user.id = owner_id
    fake_msg.reply_to_message = None
    fake_msg.reply = AsyncMock()

    fake_msg.text = "/mode all draft"
    await handler(fake_msg)

    counts = await test_db.get_mode_counts()
    assert counts["draft"] == 2
    assert counts["auto"] == 0


@pytest.mark.asyncio
async def test_cmd_mode_single_contact_opens_card(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344
    await test_db.upsert_contact(user_id=601, username="sherzod", full_name="Sherzod Aka", mode="draft")

    handler = None
    for h in admin_service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "cmd_mode":
            handler = h.callback
            break

    fake_msg = MagicMock()
    fake_msg.from_user.id = owner_id
    fake_msg.reply_to_message = None
    fake_msg.reply = AsyncMock()

    # /mode @sherzod (without mode)
    fake_msg.text = "/mode @sherzod"
    await handler(fake_msg)

    reply_text = fake_msg.reply.await_args[0][0]
    reply_markup = fake_msg.reply.await_args[1].get("reply_markup")

    assert "KONTAKT AVTONOMIYA REJIMI" in reply_text
    assert "@sherzod" in reply_text
    button_callbacks = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
    assert any("mode_set:601:auto" in cb for cb in button_callbacks)
    assert any("mode_set:601:draft" in cb for cb in button_callbacks)
    assert any("mode_set:601:mute" in cb for cb in button_callbacks)


# ============================================================================
# 4. CALLBACK TOGGLE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_mode_set_and_bulk_callbacks(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344
    await test_db.upsert_contact(user_id=701, username="bobur", full_name="Bobur", mode="draft")

    # Find handle_mode_set callback
    set_handler = None
    bulk_handler = None
    for h in admin_service.dp.callback_query.handlers:
        name = getattr(h.callback, "__name__", "")
        if name == "handle_mode_set":
            set_handler = h.callback
        elif name == "handle_mode_bulk":
            bulk_handler = h.callback

    assert set_handler is not None
    assert bulk_handler is not None

    fake_cb = MagicMock()
    fake_cb.from_user.id = owner_id
    fake_cb.message = MagicMock()
    fake_cb.message.text = "AVTONOMIYA REJIMLARI BOSHQARUVI"
    fake_cb.message.edit_text = AsyncMock()
    fake_cb.answer = AsyncMock()

    # 1. Mode set to auto
    fake_cb.data = "mode_set:701:auto"
    await set_handler(fake_cb)

    c = await test_db.get_contact_by_id(701)
    assert c.mode == "auto"
    fake_cb.answer.assert_awaited()

    # 2. Bulk mode to mute
    fake_cb.data = "mode_bulk:mute"
    await bulk_handler(fake_cb)

    c = await test_db.get_contact_by_id(701)
    assert c.mode == "mute"


# ============================================================================
# 5. NATURAL LANGUAGE MODE INTENT TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_natural_language_mode_change(admin_service: AdminBotService, test_db: DatabaseManager):
    owner_id = 7992504344
    await test_db.upsert_contact(user_id=801, username="umid_bro", full_name="Umid Jura", mode="draft")

    handler = None
    for h in admin_service.dp.message.handlers:
        if getattr(h.callback, "__name__", "") == "handle_natural_crm_or_schedule":
            handler = h.callback
            break

    assert handler is not None

    fake_msg = MagicMock()
    fake_msg.from_user.id = owner_id
    fake_msg.text = "@umid_bro ni auto qil"
    fake_msg.reply = AsyncMock()

    await handler(fake_msg)

    c = await test_db.get_contact_by_id(801)
    assert c.mode == "auto"
    fake_msg.reply.assert_awaited()
    assert "avtonomiya rejimi o'zgartirildi" in fake_msg.reply.await_args[0][0].lower()
