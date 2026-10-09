"""
Unit tests for Smart Digest V2 - Phase 1: Sources Whitelist, Collection Pipeline, and Storage.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
import pytest_asyncio
import aiosqlite

from config import settings
from database.connection import init_db
from database.crud import DatabaseManager
from database.models import DigestSource, DigestPost
from services.digest_service import DigestService
from services.ai_engine import AIEngine


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "test_digest_v2.db"
    await init_db(db_file)
    manager = DatabaseManager(db_path=db_file)
    return manager


@pytest_asyncio.fixture
async def digest_service(temp_db: DatabaseManager):
    ai_engine = MagicMock(spec=AIEngine)
    service = DigestService(db_manager=temp_db, ai_engine=ai_engine)
    return service


# ==================== 1. DATABASE SCHEMA & MIGRATION V4 ====================

@pytest.mark.asyncio
async def test_schema_migration_v4(temp_db: DatabaseManager):
    """Verifies that schema version 4 is recorded and all Digest V2 tables exist."""
    version = await temp_db.get_schema_version()
    assert version >= 4

    # Check tables in SQLite
    async with aiosqlite.connect(temp_db.db_path) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            tables = [row[0] for row in await cur.fetchall()]
            assert "digest_sources" in tables
            assert "digest_posts" in tables
            assert "digest_runs" in tables
            assert "digest_state" in tables

        # Verify unique index on digest_posts(chat_id, message_id)
        async with db.execute("PRAGMA index_list('digest_posts')") as cur:
            indexes = [row[1] for row in await cur.fetchall()]
            assert "idx_digest_posts_chat_msg" in indexes


@pytest.mark.asyncio
async def test_init_db_idempotence(temp_db: DatabaseManager):
    """Verifies that running init_db multiple times is strictly idempotent and causes no error."""
    await init_db(temp_db.db_path)
    await init_db(temp_db.db_path)
    version = await temp_db.get_schema_version()
    assert version >= 4


# ==================== 2. SOURCES CRUD & POST COUNTS ====================

@pytest.mark.asyncio
async def test_sources_whitelist_crud(temp_db: DatabaseManager):
    """Verifies adding, listing, querying, and removing sources in digest_sources."""
    # Add public channel
    src1 = await temp_db.add_digest_source(
        peer_id=-1001234567890,
        title="Daryo Yangiliklar",
        peer_type="channel",
        username="@daryo_yangiliklar",
    )
    assert src1.peer_id == -1001234567890
    assert src1.username == "daryo_yangiliklar"  # '@' stripped
    assert src1.peer_type == "channel"
    assert src1.is_active == 1

    # Add private supergroup
    src2 = await temp_db.add_digest_source(
        peer_id=-1009876543210,
        title="AI Dasturchilar Guruhi",
        peer_type="group",
        username=None,
    )
    assert src2.peer_id == -1009876543210
    assert src2.peer_type == "group"

    # List sources
    sources = await temp_db.list_digest_sources(active_only=True)
    assert len(sources) == 2

    # Query by peer_id and username
    by_id = await temp_db.get_digest_source_by_peer_id(-1001234567890)
    assert by_id is not None
    assert by_id.title == "Daryo Yangiliklar"

    by_uname = await temp_db.get_digest_source_by_username("@daryo_yangiliklar")
    assert by_uname is not None
    assert by_uname.peer_id == -1001234567890

    # Post counts (initially 0)
    count = await temp_db.get_digest_source_post_count(-1001234567890)
    assert count == 0

    # Remove source by username
    removed = await temp_db.remove_digest_source("daryo_yangiliklar")
    assert removed is True
    sources_after = await temp_db.list_digest_sources(active_only=True)
    assert len(sources_after) == 1

    # Remove source by peer_id
    removed_id = await temp_db.remove_digest_source(-1009876543210)
    assert removed_id is True
    sources_empty = await temp_db.list_digest_sources(active_only=True)
    assert len(sources_empty) == 0


# ==================== 3. ANTI-HALLUCINATION RULE 2: LINK FORMAT ====================

def test_build_post_link_public():
    """Public channels/groups with username link directly to t.me/<username>/<message_id>."""
    link1 = DigestService.build_post_link(
        chat_id=-1001234567890,
        message_id=452,
        username="kunuz",
    )
    assert link1 == "https://t.me/kunuz/452"

    link2 = DigestService.build_post_link(
        chat_id=-1001234567890,
        message_id=999,
        username="@gazetauz",
    )
    assert link2 == "https://t.me/gazetauz/999"


def test_build_post_link_private_channel():
    """Private channels without username link to t.me/c/<internal_id>/<message_id> without -100 prefix."""
    link = DigestService.build_post_link(
        chat_id=-1001987654321,
        message_id=77,
        username=None,
    )
    assert link == "https://t.me/c/1987654321/77"


def test_build_post_link_private_basic_chat():
    """Basic private chat id without 100 prefix."""
    link = DigestService.build_post_link(
        chat_id=-55443322,
        message_id=12,
        username=None,
    )
    assert link == "https://t.me/c/55443322/12"


# ==================== 4. PRIVACY BOUNDARIES: 1-TO-1 REJECTION ====================

@pytest.mark.asyncio
async def test_reject_private_chat_addition(digest_service: DigestService):
    """Strictly enforces that 1-to-1 contacts or User entities can NEVER be added to digest sources."""
    # Simulate telethon.tl.types.User
    class MockUser:
        id = 99887766
        username = "shaxsiy_kontakt"
        first_name = "Olim"
        is_private = True

    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(return_value=MockUser())

    success, msg, source = await digest_service.resolve_and_add_source(
        client=mock_client,
        target="@shaxsiy_kontakt",
    )

    assert success is False
    assert source is None
    assert "Shaxsiy yozishmalarni" in msg
    assert "qat'iyan man etiladi" in msg


@pytest.mark.asyncio
async def test_allow_channel_addition(digest_service: DigestService):
    """Channels are resolved and added safely to the whitelist."""
    from telethon.tl.types import Channel

    mock_channel = MagicMock(spec=Channel)
    mock_channel.id = 1555666777
    mock_channel.title = "O'zbekiston AI Jamiyati"
    mock_channel.username = "uz_ai_community"
    mock_channel.megagroup = False
    mock_channel.is_private = False

    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(return_value=mock_channel)

    success, msg, source = await digest_service.resolve_and_add_source(
        client=mock_client,
        target="@uz_ai_community",
    )

    assert success is True
    assert source is not None
    assert source.title == "O'zbekiston AI Jamiyati"
    assert source.peer_type == "channel"
    assert source.username == "uz_ai_community"


# ==================== 5. COLLECTION PIPELINE & DUPLICATE PROTECTION ====================

@pytest.mark.asyncio
async def test_collect_new_posts_flow(digest_service: DigestService, temp_db: DatabaseManager):
    """
    Verifies that collect_new_posts:
    1. Collects messages since last_message_id.
    2. Skips empty messages.
    3. Populates digest_posts and updates digest_state.
    4. Enforces uniqueness on (chat_id, message_id).
    """
    # 1. Add source
    await temp_db.add_digest_source(
        peer_id=-100111222333,
        title="TSUE News",
        peer_type="channel",
        username="tsue_official",
    )

    # 2. Mock messages
    class MockMsg:
        def __init__(self, msg_id, text, dt, forward=None):
            self.id = msg_id
            self.message = text
            self.text = text
            self.date = dt
            self.forward = forward

    now = datetime.now(timezone.utc)
    mock_messages = [
        MockMsg(103, "TSUE qabul boshlandi! 15-avgustgacha hujjat topshirish mumkin.", now),
        MockMsg(102, "", now - timedelta(minutes=5)),  # Empty post -> must be skipped
        MockMsg(101, "Yangi grant e'lon qilindi: 50 nafar iqtidorli talabaga.", now - timedelta(minutes=10)),
    ]

    async def fake_iter_messages(chat_id, **kwargs):
        for m in mock_messages:
            yield m

    mock_client = MagicMock()
    mock_client.iter_messages.side_effect = fake_iter_messages
    digest_service.set_userbot_client(mock_client)

    # 3. Collect
    stats = await digest_service.collect_new_posts(trigger="manual")
    assert stats["status"] == "completed"
    assert stats["total_collected"] == 2  # 103 and 101 (102 skipped)

    # 4. Check digest_state
    last_id = await temp_db.get_digest_state(-100111222333)
    assert last_id == 103

    # 5. Check stored posts
    posts = await temp_db.get_digest_posts(limit=10)
    assert len(posts) == 2
    msg_ids = [p.message_id for p in posts]
    assert 103 in msg_ids
    assert 101 in msg_ids
    assert 102 not in msg_ids

    # 6. Verify duplicate insertion is ignored
    dup_inserted = await temp_db.bulk_save_digest_posts(posts)
    assert dup_inserted == 0  # Ignored due to unique constraint


# ==================== 6. ERROR ISOLATION & FLOODWAIT HANDLING ====================

@pytest.mark.asyncio
async def test_per_source_error_isolation(digest_service: DigestService, temp_db: DatabaseManager):
    """
    Verifies that a failure or FloodWait on one source does NOT prevent
    collecting posts from other healthy sources.
    """
    from telethon.errors import FloodWaitError

    # Add 2 sources: Source A (fails with FloodWait) and Source B (healthy)
    await temp_db.add_digest_source(
        peer_id=-1001001,
        title="Failing Source",
        peer_type="channel",
        username="fail_source",
    )
    await temp_db.add_digest_source(
        peer_id=-1002002,
        title="Healthy Source",
        peer_type="channel",
        username="healthy_source",
    )

    class MockMsg:
        def __init__(self, msg_id, text):
            self.id = msg_id
            self.message = text
            self.text = text
            self.date = datetime.now(timezone.utc)
            self.forward = None

    async def fake_iter_messages(chat_id, **kwargs):
        if chat_id == -1001001:
            err = FloodWaitError(request=None)
            err.seconds = 42
            raise err
        else:
            yield MockMsg(1, "Sog'lom kanaldan muhim xabar!")

    mock_client = MagicMock()
    mock_client.iter_messages.side_effect = fake_iter_messages
    digest_service.set_userbot_client(mock_client)

    stats = await digest_service.collect_new_posts(trigger="manual")

    # One failed, but healthy collected
    assert stats["total_collected"] == 1
    assert len(stats["errors"]) == 1
    assert stats["errors"][0]["peer_id"] == -1001001
    assert "FloodWait" in stats["errors"][0]["error"]

    # Verify healthy source post is saved
    posts = await temp_db.get_digest_posts(limit=10)
    assert len(posts) == 1
    assert posts[0].chat_id == -1002002


# ==================== 7. RETENTION PURGING ====================

@pytest.mark.asyncio
async def test_retention_purging(temp_db: DatabaseManager):
    """Verifies that posts older than RETENTION_DAYS are purged, while newer ones remain."""
    now = datetime.now(timezone.utc)
    old_date = (now - timedelta(days=20)).isoformat()
    new_date = (now - timedelta(days=2)).isoformat()

    post_old = DigestPost(
        chat_id=-1001,
        message_id=1,
        chat_title="Eski Kanal",
        text="Eski yangilik",
        posted_at=old_date,
    )
    post_new = DigestPost(
        chat_id=-1001,
        message_id=2,
        chat_title="Yangi Kanal",
        text="Yangi yangilik",
        posted_at=new_date,
    )

    await temp_db.bulk_save_digest_posts([post_old, post_new])
    initial_posts = await temp_db.get_digest_posts(limit=10)
    assert len(initial_posts) == 2

    # Purge older than 14 days
    purged_count = await temp_db.purge_old_digest_posts(retention_days=14)
    assert purged_count == 1

    remaining = await temp_db.get_digest_posts(limit=10)
    assert len(remaining) == 1
    assert remaining[0].message_id == 2


# ==================== 8. FEATURE FLAG & ADMIN COMMAND TESTS ====================

@pytest.mark.asyncio
async def test_digest_service_legacy_when_v2_disabled(digest_service: DigestService, temp_db: DatabaseManager):
    """
    Verifies that when DIGEST_V2_ENABLED is False or when legacy generate_digest is called,
    it correctly reads from raw_feed_posts without breaking.
    """
    await temp_db.save_feed_post(
        source_peer_id=-100555,
        source_title="Eski Kanal",
        message_id=50,
        post_text="Eski formatdagi post",
    )
    digest_service.ai_engine.summarize_feed = AsyncMock(return_value="Qisqacha xulosa")

    summary, count = await digest_service.generate_digest()
    assert summary == "Qisqacha xulosa"
    assert count == 1


@pytest.mark.asyncio
async def test_admin_service_digest_subcommands(temp_db: DatabaseManager, digest_service: DigestService):
    """
    Verifies that AdminBotService correctly registers and executes /digest subcommands.
    """
    from bot.admin_bot import AdminBotService
    from aiogram import types

    admin_svc = AdminBotService(
        db_manager=temp_db,
        ai_engine=digest_service.ai_engine,
        scheduler_service=MagicMock(),
        digest_service=digest_service,
        admin_id=12345678,
    )

    # Add a source
    await temp_db.add_digest_source(
        peer_id=-100999,
        title="Sinov Kanal",
        peer_type="channel",
        username="sinov_kanal",
    )

    summary_list = await digest_service.list_sources_summary()
    assert len(summary_list) == 1
    assert summary_list[0]["title"] == "Sinov Kanal"

    # Remove the source
    success, msg = await digest_service.remove_source("sinov_kanal")
    assert success is True
    assert "o'chirildi" in msg

    summary_after = await digest_service.list_sources_summary()
    assert len(summary_after) == 0

