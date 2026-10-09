"""
Unit and integration tests for Smart Digest V2 - Phase 6:
- Resilience, Limits, and Telemetry Metrics
- Isolated source failures (FloodWait & error isolation)
- AI failure retry and graceful batch skipping
- Strict per-topic post limits (5 for default, 8 for AI topic E)
- Source prioritization and MAX_POSTS_PER_RUN cutoff
- Retention purging of old posts (> RETENTION_DAYS)
- Telemetry metrics storage in digest_runs
- Telemetry stats summary (/digest stats & average duration over last 7 runs)
"""

import pytest
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytz

from database.connection import init_db
from database.models import DigestPost, DigestSource, DigestRun
from database.crud import DatabaseManager
from services.digest_cleaner import DigestCleaner
from services.digest_summarizer import DigestSummarizer, DigestItem
from services.digest_service import DigestService
from config import settings


@pytest.mark.asyncio
async def test_isolated_source_failures_floodwait(tmp_path):
    """
    Verifies that if one source throws FloodWait or error, it is isolated:
    the error is recorded, and all other sources finish collecting successfully.
    """
    db_file = tmp_path / "test_p6_iso.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    # Add two sources
    await db_manager.add_digest_source(peer_id=101, title="Channel A", username="channel_a")
    await db_manager.add_digest_source(peer_id=102, title="Channel B", username="channel_b")

    ai_mock = MagicMock()
    service = DigestService(db_manager, ai_mock)

    class MockFloodWait(Exception):
        seconds = 30

    async def mock_iter_messages(peer_id, **kwargs):
        if peer_id == 101:
            raise MockFloodWait("Rate limited")
        else:
            msg = MagicMock()
            msg.id = 50
            msg.text = "Channel B Muhim yangiligi"
            msg.date = datetime.now(timezone.utc)
            msg.forward = None
            yield msg

    mock_client = MagicMock()
    mock_client.iter_messages = mock_iter_messages
    service.get_client = lambda: mock_client

    with patch("services.digest_service.FloodWaitError", MockFloodWait):
        stats = await service.collect_new_posts(trigger="manual")

    # Channel A failed, but Channel B succeeded
    assert len(stats["errors"]) == 1
    assert stats["errors"][0]["peer_id"] == 101
    assert "FloodWait" in stats["errors"][0]["error"]
    assert stats["per_source"]["102"]["collected"] == 1
    assert stats["total_collected"] == 1


@pytest.mark.asyncio
async def test_strict_topic_limits_and_importance_sorting():
    """
    Verifies that items are strictly capped at DIGEST_TOPIC_LIMIT_DEFAULT (5)
    for standard topics and DIGEST_TOPIC_LIMIT_AI (8) for Topic E, sorted by importance.
    """
    ai_mock = MagicMock()
    ai_mock.generate_json_response = AsyncMock(
        return_value='{"title": "yangilik", "summary": "yangilik tafsilotlari", "post_ids_used": [], "has_conflicts": false, "unverified": false}'
    )
    summarizer = DigestSummarizer(ai_mock)

    # 1. Topic A (Standard topic, limit = 5): create 9 posts with distinct event contents and varying importance
    distinct_texts_a = [
        "parlament yangi qonun loyihasini muhokama qildi va maqulladi",
        "davlat xizmatlarini raqamlashtirish boyicha qaror imzolandi",
        "bosh vazir iqtisodiy islohotlar ijrosini tanqidiy tahlil etdi",
        "saylov kodeksiga kiritilgan tuzatishlar kuchga kirdi",
        "hududiy hokimliklar faoliyatini baholash tizimi joriy etildi",
        "ijtimoiy himoya reyestri boyicha tolovlar yangilandi",
        "xalqaro reytinglardagi orinlarni yaxshilash dasturi qabul qilindi",
        "jamoat xavfsizligi konsepsiyasi tasdiqlandi",
        "sud qarorlari ijrosini nazorat qilish mexanizmi kuchaytirildi",
    ]
    posts_a = []
    for i, t in enumerate(distinct_texts_a):
        p = DigestPost(
            id=i + 1,
            chat_id=100,
            message_id=i + 1,
            chat_title="Siyosat",
            text=f"{t} yangilik tafsilotlari",
            topic="A",
            importance=(i % 5) + 1,  # 1 to 5
            status="classified",
        )
        posts_a.append(p)

    items_a, discarded_a, skipped_a = await summarizer.summarize_topic_posts("A", posts_a)
    assert len(items_a) <= settings.DIGEST_TOPIC_LIMIT_DEFAULT
    assert len(items_a) == 5
    assert skipped_a == 4  # 9 - 5 = 4 skipped

    # 2. Topic E (AI topic, limit = 8): create 12 posts with distinct event contents
    vocab_e = [
        "neyrotarmoq arxitekturasi optimallashtirildi va tezlashdi",
        "til modellari kontekst hajmi kengaytirildi",
        "avtonom dronlar boshqaruvi algoritmi ishlab chiqildi",
        "nutqni tanish va sintez qilish texnologiyasi yangilandi",
        "tasvirlarni generatsiya qilish sifati oshirildi",
        "robototexnika sohasida harakatlar aniqligi yuksaldi",
        "tibbiyotda rentgen tahlili sinovdan otdi",
        "dasturiy kod yozish yordamchisi taqdim etildi",
        "matnlarni xulosa qilish modeli sinovdan otdi",
        "avtomobil boshqaruvi avtopiloti yangilandi",
        "ovozli tarjimon ilovasi ishga tushirildi",
        "suniy intellekt xavfsizligi standartlari qabul qilindi",
    ]
    posts_e = []
    for i, t in enumerate(vocab_e):
        p = DigestPost(
            id=i + 100,
            chat_id=100,
            message_id=i + 100,
            chat_title="AI News",
            text=f"{t} yangilik tafsilotlari",
            topic="E",
            importance=(i % 5) + 1,
            status="classified",
        )
        posts_e.append(p)

    items_e, discarded_e, skipped_e = await summarizer.summarize_topic_posts("E", posts_e)
    assert len(items_e) <= settings.DIGEST_TOPIC_LIMIT_AI
    assert len(items_e) == 8
    assert skipped_e == 4  # 12 - 8 = 4 skipped


@pytest.mark.asyncio
async def test_max_posts_per_run_priority_cutoff(tmp_path):
    """
    Verifies that when total collected reaches MAX_POSTS_PER_RUN,
    collection stops early, respecting source priority ordering.
    """
    db_file = tmp_path / "test_p6_limit.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    # Add 3 sources with priorities: 10 (high), 5 (medium), 1 (low)
    await db_manager.add_digest_source(peer_id=1, title="High Priority", priority=10)
    await db_manager.add_digest_source(peer_id=2, title="Medium Priority", priority=5)
    await db_manager.add_digest_source(peer_id=3, title="Low Priority", priority=1)

    ai_mock = MagicMock()
    service = DigestService(db_manager, ai_mock)

    # Mock client yielding 5 posts per source
    async def mock_iter_messages(peer_id, **kwargs):
        for i in range(5):
            msg = MagicMock()
            msg.id = i + 1
            msg.text = f"Post #{i + 1} from {peer_id}"
            msg.date = datetime.now(timezone.utc)
            msg.forward = None
            yield msg

    mock_client = MagicMock()
    mock_client.iter_messages = mock_iter_messages
    service.get_client = lambda: mock_client

    # Temporarily set MAX_POSTS_PER_RUN = 5
    with patch.object(settings, "MAX_POSTS_PER_RUN", 5):
        stats = await service.collect_new_posts(trigger="manual")

    assert stats["total_collected"] == 5
    assert stats.get("limit_reached") is True
    # Only High Priority source (peer_id 1) should have been processed
    assert "1" in stats["per_source"]
    assert "2" not in stats["per_source"]
    assert "3" not in stats["per_source"]


@pytest.mark.asyncio
async def test_retention_purging_at_run_completion(tmp_path):
    """Verifies that old posts (> RETENTION_DAYS) are purged at the end of each run."""
    db_file = tmp_path / "test_p6_ret.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    # Insert old post (16 days ago) and recent post (2 days ago)
    old_time = (datetime.now(timezone.utc) - timedelta(days=16)).isoformat()
    recent_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    p_old = DigestPost(
        chat_id=1,
        message_id=1,
        chat_title="Kanal",
        text="Eski post",
        posted_at=old_time,
        status="included",
    )
    p_recent = DigestPost(
        chat_id=1,
        message_id=2,
        chat_title="Kanal",
        text="Yangi post",
        posted_at=recent_time,
        status="included",
    )
    await db_manager.bulk_save_digest_posts([p_old, p_recent])

    purged = await db_manager.purge_old_digest_posts(retention_days=14)
    assert purged == 1

    # Only recent post remains
    posts = await db_manager.get_digest_posts(status="included")
    assert len(posts) == 1
    assert posts[0].message_id == 2


@pytest.mark.asyncio
async def test_telemetry_metrics_recorded_in_digest_runs(tmp_path):
    """
    Verifies that finish_digest_run records:
    posts_collected, posts_filtered, posts_classified, items_in_digest, ai_calls_count, duration_seconds.
    """
    db_file = tmp_path / "test_p6_metrics.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    run_id = await db_manager.create_digest_run(trigger="manual")

    stats = {
        "total_collected": 35,
        "spam_filtered": 5,
        "duplicates_filtered": 3,
        "classified_count": 20,
        "total_items_selected": 14,
        "classification": {"ai_calls_made": 2},
        "ai_calls_made": 1,
    }

    await db_manager.finish_digest_run(
        run_id=run_id,
        status="completed",
        stats=stats,
        duration_seconds=4.25,
    )

    latest = await db_manager.get_latest_digest_run()
    assert latest is not None
    assert latest.posts_collected == 35
    assert latest.posts_filtered == 8  # 5 + 3
    assert latest.posts_classified == 20
    assert latest.items_in_digest == 14
    assert latest.ai_calls_count == 3  # 2 + 1
    assert round(latest.duration_seconds, 2) == 4.25


@pytest.mark.asyncio
async def test_digest_stats_summary_metrics(tmp_path):
    """
    Verifies get_digest_stats_summary calculates:
    - Last run metrics
    - Average duration over last 7 runs
    - Active sources count
    """
    db_file = tmp_path / "test_p6_summary.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    # Add 2 active sources
    await db_manager.add_digest_source(peer_id=1, title="S1")
    await db_manager.add_digest_source(peer_id=2, title="S2")

    # Create 8 completed runs with varying durations
    for dur in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]:
        rid = await db_manager.create_digest_run(trigger="scheduled")
        await db_manager.finish_digest_run(
            run_id=rid,
            status="completed",
            stats={"total_collected": 10, "total_items_selected": 4},
            duration_seconds=dur,
        )

    summary = await db_manager.get_digest_stats_summary()
    assert summary["active_sources_count"] == 2
    assert summary["total_runs_count"] == 8

    # Last run should have duration 8.0
    assert summary["last_run"]["duration_seconds"] == 8.0

    # Average over last 7 runs: (2 + 3 + 4 + 5 + 6 + 7 + 8) / 7 = 35 / 7 = 5.0
    assert round(summary["avg_duration_last_7"], 2) == 5.0
